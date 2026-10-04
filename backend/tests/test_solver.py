"""SearchSpec·CP-SAT·Candidate. 회귀 기대값과 T10·T16·T31·T32."""

import json

import pytest
from conftest import add_task, make_task, take_snapshot, with_facts

from app.domain.hashes import candidate_hash
from app.domain.models import (
    Condition,
    Conflict,
    Movable,
    Pin,
    Requirement,
    SolverResult,
)
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


def _run(pack, snapshot, level, acting="UA"):
    spec = build_search_spec(snapshot, _conflict(pack, snapshot), acting, level)
    result = cpsat.solve(snapshot, spec, pack)
    return spec, result


def _placed(result, *task_ids):
    by_id = {a["task_id"]: a for a in result.solution}
    return [(t, by_id[t]["start"], by_id[t]["resource_id"]) for t in task_ids]


def _fix(snapshot, *task_ids):
    """그 작업들을 고정한 Snapshot (메모리)."""
    pins = tuple(
        Pin(pin_id=f"pin_{t}", task_id=t, pinned_by="supervisor", by_role="SUPERVISOR")
        for t in task_ids
    )
    return with_facts(snapshot, pins=(*snapshot.facts().pins, *pins))


def _forbid_solver(monkeypatch):
    def fail(*_a, **_k):
        raise AssertionError("solver must not be called")

    monkeypatch.setattr(cpsat, "_solve_stage", fail)


# ── 회귀 기대값 ───────────────────────────────────────────


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


def _site_crane_free(snapshot):
    """기준 장면은 공용 크레인을 첫날에 못 쓴다. 이 테스트들은 첫날에도 쓸 수 있게 되돌린다."""
    return _resources(snapshot, "SITE-CR-01", available_intervals=((0, 3360),))


def test_unpinned_task_moves_to_eligible_resource(with_a):
    """고정되지 않은 작업은 자원도 움직인다: 서버가 채운 적격 자원 가운데서 Solver가 고른다 (AG-34)."""
    pack = with_a
    snap = _site_crane_free(take_snapshot(pack))
    spec, result = _run(pack, snap, "L0")
    assert spec.axes["A"] == Movable(time=True, resource=True)
    assert spec.resource_alternatives == {"A": ("SITE-CR-01",)}  # B-CR-01은 UA가 쓸 수 없다
    assert (result.stage1["changed"], result.stage2["delay"]) == (1, 60)
    assert _placed(result, "A", "C") == [("A", 60, "SITE-CR-01"), ("C", 60, "A-CR-01")]


def test_no_eligible_alternative_leaves_only_the_base_resource(with_a):
    """SITE-CR-01 권한을 빼면 A에는 대체 자원이 없다. 자원 대안은 비고 기준 자원만 남는다."""
    snap = take_snapshot(with_a)
    snap = with_facts(
        snap,
        resources=tuple(
            r.model_copy(update={"allowed_unit_ids": ("UB",)})
            if r.resource_id == "SITE-CR-01"
            else r
            for r in snap.facts().resources
        ),
    )
    spec, result = _run(with_a, snap, "L0")
    assert spec.resource_alternatives == {}
    assert result.stage1["status"] == "INFEASIBLE"


def _resources(snapshot, resource_id, **changes):
    return with_facts(
        snapshot,
        resources=tuple(
            r.model_copy(update=changes) if r.resource_id == resource_id else r
            for r in snapshot.facts().resources
        ),
    )


def _tasks(snapshot, task_id, **changes):
    return with_facts(
        snapshot,
        tasks=tuple(
            t.model_copy(update=changes) if t.task_id == task_id else t
            for t in snapshot.facts().tasks
        ),
    )


def test_alternative_filtered_by_zone_and_requirement(with_a):
    """대체 자원 필터는 적격성 함수다: 구역·요구 조건이 안 맞으면 Solver 입력에 들어가지 않는다 (CV-20)."""
    pack = with_a
    snap = take_snapshot(pack)
    assert _run(pack, snap, "L0")[0].resource_alternatives == {"A": ("SITE-CR-01",)}
    for changed in (
        _resources(snap, "SITE-CR-01", allowed_zone_ids=("C", "D")),  # A는 B구역
        _resources(
            snap, "SITE-CR-01", attributes={"max_load": 10, "usage": ("일반",)}
        ),  # 기본값 ≥ 20
        _tasks(
            snap,
            "A",
            resource_requirements=(Requirement(attribute="max_load", op="LTE", value=30),),
        ),  # 작업 값 ≤ 30: A-CR-01(25 t)은 맞고 SITE-CR-01(50 t)은 안 맞는다
    ):
        assert _run(pack, changed, "L0")[0].resource_alternatives == {}


# ── SearchSpec 오류·hash ───────────────────────────────────────


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
    assert list(ub.axes) == ["B", "D", "E", "K", "P", "W"]  # 확장 작업은 고정
    assert ub.axes["B"] == Movable(time=False, resource=False)
    assert list(build_search_spec(snap, conflict, "UB", "L0").axes) == ["B"]
    ua = build_search_spec(snap, conflict, "UA", "L2")
    assert list(ua.axes) == ["A", "C", "M", "Q"]
    # 두 축이 모두 고정인 확장 작업은 hash에서 빠진다: L2 = L1
    assert ua.hash == build_search_spec(snap, conflict, "UA", "L1").hash


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


# ── 마지막 단계: 자원을 바꾸는 작업 수 (CV-12) ─────────────────


def test_equal_metrics_prefer_the_solution_that_changes_fewer_resources(seeded):
    """변경 수와 지연이 같으면 자원을 덜 바꾸는 해가 나온다. 변경 수는 작업당 하나라, 시각을 바꿔야 하는
    작업의 자원을 더 바꿔도 앞의 두 값은 같다."""
    pack = seeded
    snap = _site_crane_free(take_snapshot(pack))
    conflict = Conflict(rule_id="TEST", task_ids=("B", "C"), zone_ids=(), interval=(0, 1))
    later = {"C": Condition(start_min=90)}  # C는 시각을 바꿔야 한다
    spec = build_search_spec(snap, conflict, "UA", "L0", later)
    assert spec.resource_alternatives == {"C": ("SITE-CR-01",)}  # 자원도 바꿀 수 있다
    result = cpsat.solve(snap, spec, pack)
    assert _placed(result, "C") == [("C", 90, "A-CR-01")]  # 시각만 바꾼다
    assert (result.stage1["changed"], result.stage2["delay"]) == (1, 30)
    assert (result.stage2["resource_status"], result.stage2["resource_changed"]) == ("OPTIMAL", 0)

    # 자원까지 바꾼 해도 변경 수와 지연은 같다: 마지막 단계가 없으면 둘은 구분되지 않는다
    swapped = {"C": Condition(start_min=90, resource_id="SITE-CR-01")}
    forced = cpsat.solve(snap, build_search_spec(snap, conflict, "UA", "L0", swapped), pack)
    assert _placed(forced, "C") == [("C", 90, "SITE-CR-01")]
    assert (forced.stage1["changed"], forced.stage2["delay"]) == (1, 30)
    assert forced.stage2["resource_changed"] == 1

    # 지연 먼저로 풀어도 마지막 단계는 같다
    first = build_search_spec(snap, conflict, "UA", "L0", later, "DELAY_FIRST")
    delay_first = cpsat.solve(snap, first, pack)
    assert _placed(delay_first, "C") == [("C", 90, "A-CR-01")]
    assert (delay_first.stage2["changed"], delay_first.stage2["resource_changed"]) == (1, 0)


def test_last_stage_replaces_a_solution_that_changes_more_resources(seeded, monkeypatch):
    """2단계 해가 자원을 불필요하게 바꿨으면 마지막 단계의 해가 그 자리를 대신한다."""
    pack = seeded
    snap = _site_crane_free(take_snapshot(pack))
    conflict = Conflict(rule_id="TEST", task_ids=("B", "C"), zone_ids=(), interval=(0, 1))
    spec = build_search_spec(snap, conflict, "UA", "L0", {"C": Condition(start_min=90)})
    real, calls = cpsat._solution, []

    def swapped_at_stage2(b, solver):
        """두 번째로 읽는 해(2단계)만 C의 자원을 바꾼 것으로 돌려준다."""
        calls.append(1)
        out = real(b, solver)
        if len(calls) == 2:
            out = [{**a, "resource_id": "SITE-CR-01"} if a["task_id"] == "C" else a for a in out]
        return out

    monkeypatch.setattr(cpsat, "_solution", swapped_at_stage2)
    result = cpsat.solve(snap, spec, pack)
    assert len(calls) == 3  # 1단계, 2단계, 마지막 단계
    assert _placed(result, "C") == [("C", 90, "A-CR-01")]
    assert result.stage2["resource_changed"] == 0


# ── T31 축별 독립 ──────────────────────────────────────────────


def test_t31_time_fixed_resource_moves(seeded):
    """X: 시간창이 한 점·자원 축 열림. C와 A-CR-01을 두고 겹친다 → 자원만 바꾼다."""
    pack = seeded
    x = make_task(
        pack,
        task_id="X",
        zone_id="C",
        earliest_start=60,
        latest_start=60,
        latest_end=90,
    )
    add_task(pack, x)
    snap = _site_crane_free(take_snapshot(pack))
    cap = next(
        c
        for c in detect_conflicts(snap, snap.facts().check_assignments(), pack)
        if c.rule_id == "CAP-RESOURCE" and "X" in c.task_ids
    )
    spec = build_search_spec(snap, cap, "UA", "L0")
    assert spec.axes["X"] == Movable(time=True, resource=True)  # 시각은 시간창(60–60)이 묶는다
    result = cpsat.solve(snap, spec, pack)
    assert list(spec.axes) == ["C", "X"]
    assert (result.stage1["changed"], result.stage2["delay"]) == (1, 0)
    # C를 90으로 미는 해(지연 30)보다 X의 자원 변경(지연 0)이 선택된다
    assert _placed(result, "C", "X") == [("C", 60, "A-CR-01"), ("X", 60, "SITE-CR-01")]


def test_pinned_task_closes_both_axes(with_a):
    """고정된 A는 시각·자원 모두 닫힌다. 쓸 수 있는 대체 자원이 있어도 대안에 들어가지 않는다 (AG-27)."""
    pack = with_a
    snap = _fix(_site_crane_free(take_snapshot(pack)), "A")
    spec = build_search_spec(snap, _conflict(pack, snap), "UA", "L2")
    assert spec.axes["A"] == Movable(time=False, resource=False)
    result = cpsat.solve(snap, spec, pack)
    assert result.stage1["status"] == "INFEASIBLE"  # A가 0–30에 묶이면 B 아래를 벗어날 수 없다
    assert "A" not in spec.resource_alternatives


def test_solver_keeps_pinned_task_and_moves_unpinned(with_a):
    """고정된 작업은 상수로 남고, 고정되지 않은 작업만 움직인다 (AG-27)."""
    snap = take_snapshot(with_a)
    spec = build_search_spec(snap, _conflict(with_a, snap), "UA", "L1")
    assert spec.axes == {
        "A": Movable(time=True, resource=True),
        "C": Movable(time=True, resource=True),
    }
    pinned = _fix(snap, "C")
    spec_c = build_search_spec(pinned, _conflict(with_a, pinned), "UA", "L1")
    assert spec_c.axes["C"] == Movable(time=False, resource=False)
    result = cpsat.solve(pinned, spec_c, with_a)
    if result.solution is not None:
        assert _placed(result, "C") == [("C", 60, "A-CR-01")]


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
    assert [a.task_id for a in cand.assignments] == [
        "A", "B", "C", "D", "E", "K", "M", "P", "Q", "W",
    ]  # fmt: skip
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


# ── 실효 탐색 키: 미시도 판정용 ─────


def _key(pack, snapshot, level="L0"):
    spec = build_search_spec(snapshot, _conflict(pack, snapshot), "UA", level)
    return spec.search_key, spec.hash


def test_search_key_ignores_versions_consents_and_revisions(with_a):
    """Consent·context_version·plan_revision·revision 번호만 다르면 Solver 입력이 같다 → 같은 키."""
    snapshot = take_snapshot(with_a)
    facts = snapshot.facts()
    bumped = with_facts(
        snapshot,
        context_version=facts.context_version + 5,
        plan_revision=facts.plan_revision + 1,
        consents=(),
        tasks=tuple(t.model_copy(update={"revision": t.revision + 3}) for t in facts.tasks),
    )
    key, digest = _key(with_a, snapshot)
    other_key, other_digest = _key(with_a, bumped)
    assert key == other_key
    assert digest != other_digest  # 무결성 hash는 snapshot_hash를 따라 달라진다


def test_search_key_follows_eligible_alternatives(with_a):
    """탐색 키에는 서버가 채운 대체 자원이 들어간다: 쓸 수 있는 자원이 달라지면 다른 탐색이다 (CV-04)."""
    snapshot = take_snapshot(with_a)
    key, _ = _key(with_a, snapshot)
    assert _key(with_a, take_snapshot(with_a))[0] == key  # 같은 사실이면 같은 키
    none = _resources(snapshot, "SITE-CR-01", allowed_unit_ids=("UB",))
    assert _key(with_a, none)[0] != key


def test_search_key_changes_with_ready_set_and_in_scope_constraints(with_a):
    snapshot = take_snapshot(with_a)
    key, _ = _key(with_a, snapshot)
    facts = snapshot.facts()
    # 철회로 READY 작업 집합이 바뀌면(충돌과 무관한 작업이라도) 다른 키
    withdrawn = with_facts(snapshot, tasks=tuple(t for t in facts.tasks if t.task_id != "E"))
    assert _key(with_a, withdrawn)[0] != key
    # 제약은 axes로만 Solver 입력에 들어간다: 범위 안 작업(A)의 축을 막으면 그 범위의 키가 바뀌고,
    # 범위 밖 작업(C)의 제약은 L0 키를 바꾸지 않는다. C가 들어 있는 L1 키는 바뀐다
    assert _key(with_a, _fix(snapshot, "A"))[0] != key
    assert _key(with_a, _fix(snapshot, "C"))[0] == key
    assert _key(with_a, _fix(snapshot, "C"), "L1")[0] != _key(with_a, snapshot, "L1")[0]


def test_search_key_includes_zone_attributes_and_requirements(with_a):
    """구역·속성·요구 조건은 Solver 입력이다. 표시 이름·메모·비용은 아니다 (CV-04)."""
    snapshot = take_snapshot(with_a)
    key, _ = _key(with_a, snapshot)
    for changed in (
        _resources(snapshot, "SITE-CR-01", allowed_zone_ids=("*",)),
        _resources(snapshot, "SITE-CR-01", attributes={"max_load": 60, "usage": ("일반",)}),
        _tasks(
            snapshot,
            "A",
            resource_requirements=(Requirement(attribute="max_load", op="GTE", value=22),),
        ),
        _tasks(snapshot, "C", default_requirements=()),
    ):
        assert _key(with_a, changed)[0] != key
    shown = _resources(
        snapshot, "SITE-CR-01", display_name="다른 이름", note="메모", cost_per_hour=1
    )
    other_key, other_digest = _key(with_a, shown)
    assert other_key == key
    assert other_digest != _key(with_a, snapshot)[1]  # 무결성 hash는 snapshot 전체를 따른다
