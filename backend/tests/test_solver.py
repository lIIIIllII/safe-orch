"""SearchSpec·CP-SAT·Candidate (설계서 §7, 부록 A.11). A.11 회귀 기대값과 T10·T16·T31·T32."""

import json

import pytest
from conftest import add_task, make_task, take_snapshot, with_facts

from app.domain.hashes import candidate_hash
from app.domain.models import Conflict, FeedbackConstraint, Movable, SolverResult
from app.rules.engine import detect_conflicts
from app.solver import cpsat
from app.solver.candidate import build_candidate
from app.solver.search_spec import SearchSpecError, build_search_spec
from app.store import db
from app.store.repos.records import StaleError, insert_search_spec, register_solver_outcome
from app.store.repos.site import bump_context_version


def _conflict(pack, snapshot):
    found = detect_conflicts(snapshot, snapshot.facts().check_assignments(), pack)
    assert [(c.rule_id, c.task_ids) for c in found] == [("SEP-LIFT-BELOW", ("A", "B"))]
    return found[0]


def _run(pack, snapshot, level, try_resources=None, acting="UA"):
    spec = build_search_spec(snapshot, _conflict(pack, snapshot), acting, level, try_resources)
    result = cpsat.solve(snapshot, spec, pack)
    return spec, result


def _placed(result, *task_ids):
    by_id = {a["task_id"]: a for a in result.solution}
    return [(t, by_id[t]["start"], by_id[t]["resource_id"]) for t in task_ids]


@pytest.fixture
def resource_movable_a(with_a):
    """A를 movable.resource = true인 새 revision으로 바꾼다 (이동 축 확인 가정)."""
    add_task(with_a, make_task(with_a, revision=2, movable={"time": True, "resource": True}))
    return with_a


def _fix(snapshot, *task_ids, axes=("TIME", "RESOURCE")):
    constraints = tuple(
        FeedbackConstraint(
            constraint_id=f"fc_{t}",
            task_id=t,
            frozen_axes=axes,
            source_type="DECISION",
            source_id="dec_test",
        )
        for t in task_ids
    )
    return with_facts(snapshot, constraints=constraints)


def _forbid_solver(monkeypatch):
    def fail(*_a, **_k):
        raise AssertionError("solver must not be called")

    monkeypatch.setattr(cpsat, "_solve_stage", fail)


# ── A.11 회귀 기대값 ───────────────────────────────────────────


def test_l0_infeasible_no_candidate(with_a):
    snap = take_snapshot(with_a)
    spec, result = _run(with_a, snap, "L0")
    assert list(spec.axes) == ["A"]
    assert result.stage1 == {"status": "INFEASIBLE", "changed": None, "solution": None}
    assert result.stage2 is None and result.chosen_stage is None
    assert build_candidate(snap, spec, result) is None


def test_l1_alpha(with_a):
    snap = take_snapshot(with_a)
    spec, result = _run(with_a, snap, "L1")
    assert list(spec.axes) == ["A", "C"]  # C: 같은 기준 자원 A-CR-01
    assert (result.stage1["status"], result.stage1["changed"]) == ("OPTIMAL", 2)
    assert (result.stage2["status"], result.stage2["delay"]) == ("OPTIMAL", 90)
    assert result.chosen_stage == 2
    assert _placed(result, "A", "C") == [("A", 60, "A-CR-01"), ("C", 90, "A-CR-01")]
    assert _placed(result, "B", "D", "E") == [("B", 0, None), ("D", 0, None), ("E", 45, None)]
    assert result.minimal_change and not result.delay_optimality_unconfirmed


def test_beta_site_crane(resource_movable_a):
    pack = resource_movable_a
    snap = take_snapshot(pack)
    spec, result = _run(pack, snap, "L0", {"A": ["SITE-CR-01"]})
    assert spec.resource_alternatives == {"A": ("SITE-CR-01",)}
    assert (result.stage1["changed"], result.stage2["delay"]) == (1, 60)
    assert _placed(result, "A", "C") == [("A", 60, "SITE-CR-01"), ("C", 60, "A-CR-01")]


def test_b_crane_not_authorized_solver_not_called(resource_movable_a, monkeypatch):
    """T09(SearchSpec)·T10: 권한 필터 후 대안이 비면 Solver 미호출."""
    _forbid_solver(monkeypatch)
    snap = take_snapshot(resource_movable_a)
    with pytest.raises(SearchSpecError) as exc:
        _run(resource_movable_a, snap, "L0", {"A": ["B-CR-01"]})
    assert exc.value.reason_code == "RESOURCE_NOT_AUTHORIZED"


def test_t10_no_authorized_alternative(resource_movable_a, monkeypatch):
    """fixture 변형: SITE-CR-01 권한 제거 → 대체 자원 없음."""
    _forbid_solver(monkeypatch)
    snap = take_snapshot(resource_movable_a)
    snap = with_facts(
        snap,
        resources=tuple(
            r.model_copy(update={"allowed_unit_ids": ("UB",)})
            if r.resource_id == "SITE-CR-01"
            else r
            for r in snap.facts().resources
        ),
    )
    with pytest.raises(SearchSpecError) as exc:
        _run(resource_movable_a, snap, "L0", {"A": ["SITE-CR-01", "B-CR-01"]})
    assert exc.value.reason_code == "RESOURCE_NOT_AUTHORIZED"


def test_unauthorized_filtered_when_authorized_remains(resource_movable_a):
    snap = take_snapshot(resource_movable_a)
    spec = build_search_spec(
        snap, _conflict(resource_movable_a, snap), "UA", "L0", {"A": ["B-CR-01", "SITE-CR-01"]}
    )
    assert spec.resource_alternatives == {"A": ("SITE-CR-01",)}


# ── SearchSpec 오류·hash ───────────────────────────────────────


def test_resource_axis_not_allowed(with_a, monkeypatch):
    _forbid_solver(monkeypatch)
    snap = take_snapshot(with_a)
    with pytest.raises(SearchSpecError) as exc:
        _run(with_a, snap, "L0", {"A": ["SITE-CR-01"]})
    assert exc.value.reason_code == "RESOURCE_AXIS_NOT_ALLOWED"
    with pytest.raises(SearchSpecError) as exc:  # 범위 밖 작업
        _run(with_a, snap, "L0", {"C": ["SITE-CR-01"]})
    assert exc.value.reason_code == "RESOURCE_AXIS_NOT_ALLOWED"


def test_no_acting_tasks(with_a):
    snap = take_snapshot(with_a)
    other = Conflict(
        rule_id="SEP-HOT-FLAM", task_ids=("D", "E"), zone_ids=("D", "D2"), interval=(0, 75)
    )
    with pytest.raises(SearchSpecError) as exc:
        build_search_spec(snap, other, "UA", "L0")
    assert exc.value.reason_code == "NO_ACTING_TASKS"


def test_scope_levels(with_a):
    snap = take_snapshot(with_a)
    conflict = _conflict(with_a, snap)
    ub = build_search_spec(snap, conflict, "UB", "L2")
    assert list(ub.axes) == ["B", "D", "E"]
    assert ub.axes["B"] == Movable(time=False, resource=False)
    assert list(build_search_spec(snap, conflict, "UB", "L0").axes) == ["B"]
    assert list(build_search_spec(snap, conflict, "UA", "L2").axes) == ["A", "C"]


def test_search_spec_hash_ignores_ids_and_scope_name(with_a):
    snap = take_snapshot(with_a)
    conflict = _conflict(with_a, snap)
    l0a = build_search_spec(snap, conflict, "UA", "L0")
    l0b = build_search_spec(snap, conflict, "UA", "L0")
    l1 = build_search_spec(snap, conflict, "UA", "L1")
    assert l0a.search_spec_id != l0b.search_spec_id and l0a.search_spec_id.startswith("ss_")
    assert l0a.hash == l0b.hash
    assert l1.hash != l0a.hash  # C가 실제로 움직일 수 있다

    # C 고정 후: L1·L2는 L0와 같은 실효 탐색 → 같은 hash
    fixed = _fix(snap, "C")
    h0, h1, h2 = (build_search_spec(fixed, conflict, "UA", lvl) for lvl in ("L0", "L1", "L2"))
    assert h1.axes["C"] == Movable(time=False, resource=False)
    assert h0.hash == h1.hash == h2.hash
    assert h0.scope_level != h1.scope_level


def test_search_spec_hash_depends_on_snapshot_hash_not_id(with_a):
    s1, s2 = take_snapshot(with_a), take_snapshot(with_a)
    c = _conflict(with_a, s1)
    assert build_search_spec(s1, c, "UA", "L0").hash == build_search_spec(s2, c, "UA", "L0").hash
    add_task(with_a, make_task(with_a, revision=2))  # 같은 값이라도 Context가 바뀜
    s3 = take_snapshot(with_a)
    assert build_search_spec(s3, c, "UA", "L0").hash != build_search_spec(s1, c, "UA", "L0").hash


# ── T31 축별 독립 ──────────────────────────────────────────────


def test_t31_time_fixed_resource_moves(seeded):
    """X: 시간 고정·자원 이동 가능. C와 A-CR-01을 두고 겹친다 → 자원만 바꾼다."""
    pack = seeded
    x = make_task(
        pack,
        task_id="X",
        zone_id="C",
        earliest_start=60,
        latest_start=60,
        latest_end=90,
        movable={"time": False, "resource": True},
    )
    add_task(pack, x)
    snap = take_snapshot(pack)
    cap = next(
        c
        for c in detect_conflicts(snap, snap.facts().check_assignments(), pack)
        if c.rule_id == "CAP-RESOURCE" and "X" in c.task_ids
    )
    spec = build_search_spec(snap, cap, "UA", "L0", {"X": ["SITE-CR-01"]})
    assert spec.axes["X"] == Movable(time=False, resource=True)
    result = cpsat.solve(snap, spec, pack)
    assert list(spec.axes) == ["C", "X"]
    assert (result.stage1["changed"], result.stage2["delay"]) == (1, 0)
    # C를 90으로 미는 해(지연 30)보다 X의 자원 변경(지연 0)이 선택된다
    assert _placed(result, "C", "X") == [("C", 60, "A-CR-01"), ("X", 60, "SITE-CR-01")]


def test_t31_time_constraint_does_not_block_resource(resource_movable_a):
    """A에 TIME 제약만 있으면 시간은 기준값(0), 자원 축은 여전히 열려 있다."""
    snap = _fix(take_snapshot(resource_movable_a), "A", axes=("TIME",))
    spec = build_search_spec(
        snap, _conflict(resource_movable_a, snap), "UA", "L0", {"A": ["SITE-CR-01"]}
    )
    assert spec.axes["A"] == Movable(time=False, resource=True)
    assert spec.resource_alternatives == {"A": ("SITE-CR-01",)}
    result = cpsat.solve(snap, spec, resource_movable_a)
    assert result.stage1["status"] == "INFEASIBLE"  # 0–30은 B 아래라 자원만으로는 풀 수 없다

    snap_r = _fix(take_snapshot(resource_movable_a), "A", axes=("RESOURCE",))
    with pytest.raises(SearchSpecError, match="RESOURCE_AXIS_NOT_ALLOWED"):
        build_search_spec(
            snap_r, _conflict(resource_movable_a, snap_r), "UA", "L0", {"A": ["SITE-CR-01"]}
        )


# ── T16·T32 상태 구분 ──────────────────────────────────────────


def _patch_stages(monkeypatch, statuses):
    """n번째 단계 실행을 지정 상태로 바꾼다. None이면 실제 실행."""
    real = cpsat._solve_stage
    calls = []

    def fake(model, time_limit_s):
        forced = statuses[len(calls)] if len(calls) < len(statuses) else None
        calls.append(forced)
        return (forced, None) if forced else real(model, time_limit_s)

    monkeypatch.setattr(cpsat, "_solve_stage", fake)
    return calls


def test_t16_statuses_distinguished(with_a, monkeypatch):
    snap = take_snapshot(with_a)
    assert _run(with_a, snap, "L0")[1].stage1["status"] == "INFEASIBLE"
    assert _run(with_a, snap, "L1")[1].stage1["status"] == "OPTIMAL"
    _patch_stages(monkeypatch, ["UNKNOWN"])
    spec, result = _run(with_a, snap, "L1")
    assert result.stage1 == {"status": "UNKNOWN", "changed": None, "solution": None}
    assert result.stage1["status"] != "INFEASIBLE"  # UNKNOWN을 불가능으로 표시하지 않는다
    assert result.stage2 is None and result.chosen_stage is None
    assert not result.minimal_change and not result.delay_optimality_unconfirmed
    assert build_candidate(snap, spec, result) is None


def test_t32_stage2_unknown_keeps_stage1(with_a, monkeypatch):
    snap = take_snapshot(with_a)
    calls = _patch_stages(monkeypatch, [None, "UNKNOWN"])
    spec, result = _run(with_a, snap, "L1")
    assert len(calls) == 2
    assert (result.stage1["status"], result.stage1["changed"]) == ("OPTIMAL", 2)
    assert result.stage2 == {"status": "UNKNOWN", "delay": None, "solution": None}
    assert result.chosen_stage == 1
    assert result.solution == result.stage1["solution"]
    assert result.minimal_change and result.delay_optimality_unconfirmed
    cand = build_candidate(snap, spec, result)
    assert [a.model_dump() for a in cand.assignments] == result.stage1["solution"]


@pytest.mark.parametrize(
    ("stage1", "stage2", "chosen", "minimal", "unconfirmed"),
    [
        ({"status": "OPTIMAL"}, {"status": "OPTIMAL"}, 2, True, False),
        ({"status": "OPTIMAL"}, {"status": "FEASIBLE"}, 2, True, True),
        ({"status": "OPTIMAL"}, {"status": "UNKNOWN"}, 1, True, True),
        ({"status": "FEASIBLE"}, None, 1, False, True),
        ({"status": "INFEASIBLE"}, None, None, False, False),
    ],
)
def test_solver_result_display_properties(stage1, stage2, chosen, minimal, unconfirmed):
    sol = [{"task_id": "A", "start": 0, "end": 30, "resource_id": None}]
    s1 = {**stage1, "solution": sol if stage1["status"] in ("OPTIMAL", "FEASIBLE") else None}
    s2 = None if stage2 is None else {**stage2, "solution": sol if chosen == 2 else None}
    r = SolverResult(
        solver_result_id="sr_x", search_spec_id="ss_x", stage1=s1, stage2=s2, chosen_stage=chosen
    )
    assert (r.minimal_change, r.delay_optimality_unconfirmed) == (minimal, unconfirmed)


# ── Candidate·등록 ─────────────────────────────────────────────


def test_candidate_and_register(with_a):
    snap = take_snapshot(with_a)
    spec, result = _run(with_a, snap, "L1")
    cand = build_candidate(snap, spec, result)
    facts = snap.facts()
    assert cand.candidate_id.startswith("cand_") and cand.kind == "REPLAN"
    assert [a.task_id for a in cand.assignments] == ["A", "B", "C", "D", "E"]
    assert (cand.base_plan_revision, cand.context_version) == (0, 1)
    assert (cand.pack_hash, cand.search_spec_hash) == (facts.pack_hash, spec.hash)
    assert cand.candidate_hash == candidate_hash(
        cand.assignments, 0, 1, snap.snapshot_hash, spec.hash, facts.pack_hash
    )

    with db.write() as tx:
        insert_search_spec(tx, with_a.site_id, spec)
    with db.write() as tx:
        register_solver_outcome(tx, snap, result, cand)
    with db.read() as conn:
        sr = conn.execute("SELECT stage1, stage2, chosen_stage FROM solver_result").fetchone()
        stored = conn.execute("SELECT candidate_id, candidate_hash FROM candidate").fetchall()
    assert json.loads(sr[0]) == result.stage1 and json.loads(sr[1]) == result.stage2
    assert sr[2] == 2
    assert stored == [(cand.candidate_id, cand.candidate_hash)]


def test_register_infeasible_stores_result_without_candidate(with_a):
    snap = take_snapshot(with_a)
    spec, result = _run(with_a, snap, "L0")
    with db.write() as tx:
        insert_search_spec(tx, with_a.site_id, spec)
        register_solver_outcome(tx, snap, result, None)
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM solver_result").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM candidate").fetchone()[0] == 0


def test_register_stale_context_discarded(with_a):
    snap = take_snapshot(with_a)
    spec, result = _run(with_a, snap, "L1")
    cand = build_candidate(snap, spec, result)
    with db.write() as tx:
        insert_search_spec(tx, with_a.site_id, spec)
    with db.write() as tx:  # Solver 계산 중 Context 변경
        bump_context_version(tx, with_a.site_id)
    with pytest.raises(StaleError), db.write() as tx:
        register_solver_outcome(tx, snap, result, cand)
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM solver_result").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM candidate").fetchone()[0] == 0


def test_register_stale_plan_discarded(with_a):
    snap = take_snapshot(with_a)
    spec, result = _run(with_a, snap, "L1")
    with db.write() as tx:
        insert_search_spec(tx, with_a.site_id, spec)
        tx.execute("UPDATE site SET plan_revision = plan_revision + 1")
    with pytest.raises(StaleError), db.write() as tx:
        register_solver_outcome(tx, snap, result, build_candidate(snap, spec, result))
