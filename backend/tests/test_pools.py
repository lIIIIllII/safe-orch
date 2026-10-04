"""수량 풀·작업 수요·필수 직종 (CV-11·21·23). 누적 제약을 Rule Engine·Validator·Solver가 같은 기준으로 본다."""

import json
import uuid

import pytest
from conftest import free_alternative, take_snapshot, with_facts
from scripted import Router, solve

from app.commands.task_request import TaskRequestForm, submit_task_request
from app.coordinator.dispatcher import run_until_idle
from app.domain.models import Assignment, Candidate, Conflict, Demand
from app.rules.engine import detect_conflicts
from app.solver import cpsat
from app.solver.search_spec import build_search_spec
from app.store import db
from app.store.repos.records import get_candidate
from app.store.repos.runs import get_run, list_steps
from app.store.repos.tasks import list_current_tasks
from app.validator.validator import validate

A_B = Conflict(rule_id="SEP-LIFT-BELOW", task_ids=("A", "B"), zone_ids=("B",), interval=(0, 60))


def _asg(task_id, start, end, resource_id=None):
    return Assignment(task_id=task_id, start=start, end=end, resource_id=resource_id)


def _pools(snapshot, drop=(), **quantities):
    """풀 수량을 바꾸거나 풀을 뺀 Snapshot."""
    pools = tuple(
        p.model_copy(update={"quantity": quantities.get(p.pool_id.replace("-", "_"), p.quantity)})
        for p in snapshot.facts().pools
        if p.pool_id not in drop
    )
    return with_facts(snapshot, pools=pools)


def _retask(snapshot, task_id, **changes):
    tasks = tuple(
        t.model_copy(update=changes) if t.task_id == task_id else t for t in snapshot.facts().tasks
    )
    return with_facts(snapshot, tasks=tasks)


def _pool_conflicts(snapshot, pack, *moved):
    by_id = {a.task_id: a for a in snapshot.facts().check_assignments()}
    by_id.update({a.task_id: a for a in moved})
    found = detect_conflicts(snapshot, by_id.values(), pack)
    return [c for c in found if c.rule_id.startswith("POOL_")]


def _reconfirm(snap):
    """현재 계획을 그대로 검증하는 후보(hash는 맞추지 않는다. 풀 검사만 본다)."""
    facts = snap.facts()
    return Candidate(
        candidate_id="cand_test",
        snapshot_id=snap.snapshot_id,
        search_spec_id=None,
        search_spec_hash=None,
        solver_result_id=None,
        base_plan_revision=facts.plan_revision,
        context_version=facts.context_version,
        pack_hash=facts.pack_hash,
        assignments=facts.check_assignments(),
        candidate_hash="x",
        kind="RECONFIRM",
    )


# ── 수요 ───────────────────────────────────────────────────────


def test_demands_are_derived_and_task_values_cannot_lower_them(with_a):
    tasks = take_snapshot(with_a).facts().task_map()
    assert tasks["A"].demands == {"SIGNALER": 1, "WORKER": 4}  # 인양: 작업 인원 4, 신호수 1(필수)
    assert tasks["A"].required_kinds == ("SIGNALER",)
    assert (tasks["B"].demands, tasks["B"].required_kinds) == ({"WORKER": 3}, ())
    assert tasks["D"].demands == tasks["E"].demands == {"WORKER": 2}
    more = tasks["A"].model_copy(update={"pool_demands": (Demand(kind="WORKER", quantity=7),)})
    assert more.demands == {"SIGNALER": 1, "WORKER": 7}
    less = tasks["A"].model_copy(update={"pool_demands": (Demand(kind="WORKER", quantity=1),)})
    assert less.demands == {"SIGNALER": 1, "WORKER": 4}  # 작업 값으로 낮출 수 없다


# ── Rule Engine ────────────────────────────────────────────────


def test_no_pool_conflict_with_pack_quantities(with_a):
    snap = take_snapshot(with_a)
    assert _pool_conflicts(snap, with_a) == []
    # A와 C가 겹쳐도 UA 작업 인원 4 + 4 = 8 ≤ 10, 신호수 1 + 1 = 2 ≤ 2
    assert _pool_conflicts(snap, with_a, _asg("A", 60, 90, "SITE-CR-01")) == []


def test_pool_capacity_conflict_names_pool_kind_time_and_tasks(with_a):
    snap = _pools(take_snapshot(with_a), UA_WRK=7)
    assert _pool_conflicts(snap, with_a) == []  # 기준 위치: A 09:00, C 10:00은 겹치지 않는다
    [c] = _pool_conflicts(snap, with_a, _asg("A", 60, 90, "SITE-CR-01"))
    assert c.model_dump() == {
        "rule_id": "POOL_CAPACITY",
        "task_ids": ("A", "C"),
        "resource_id": None,
        "zone_ids": ("B", "C"),
        "interval": (60, 90),
        "pool": {"pool_id": "UA-WRK", "kind": "WORKER", "at": 60, "demand": 8, "quantity": 7},
    }
    # 끝과 시작이 맞닿으면 겹치지 않는다
    assert _pool_conflicts(snap, with_a, _asg("A", 30, 60, "SITE-CR-01")) == []
    # 필수 직종도 수량을 넘으면 초과다
    one = _pools(take_snapshot(with_a), UA_SIG=1)
    [s] = _pool_conflicts(one, with_a, _asg("A", 60, 90, "SITE-CR-01"))
    assert (s.task_ids, s.pool.pool_id, s.pool.demand, s.pool.quantity) == (
        ("A", "C"),
        "UA-SIG",
        2,
        1,
    )


def test_fixed_tasks_count_and_each_task_set_is_reported_once(seeded):
    """고정 작업도 수요에 들어간다. B(3)·D(2)는 09:00, B(3)·E(2)는 09:45, K(4)·P(2)는 다음 날 10:00에 겹친다."""
    snap = _pools(take_snapshot(seeded), UB_WRK=4)
    found = _pool_conflicts(snap, seeded)
    assert [(c.task_ids, c.pool.at, c.pool.demand) for c in found] == [
        (("B", "D"), 0, 5),
        (("B", "E"), 45, 5),
        (("K", "P"), 1500, 6),
    ]
    assert _pool_conflicts(_pools(take_snapshot(seeded), UB_WRK=6), seeded) == []


def test_other_units_do_not_use_the_pool(with_a):
    """풀은 허용 Unit의 작업만 쓴다: UB 작업은 UA 풀 수요에 들어가지 않는다."""
    snap = _pools(take_snapshot(with_a), UA_WRK=7)
    # A(UA) 4와 B 3·D 2(UB)가 09:00에 겹쳐도 UA 풀 초과가 아니다. UA는 Q 4 + M 3 = 7이 최대다
    assert _pool_conflicts(snap, with_a) == []


def test_task_value_raises_demand(with_a):
    snap = _retask(take_snapshot(with_a), "A", pool_demands=(Demand(kind="WORKER", quantity=7),))
    [c] = _pool_conflicts(snap, with_a, _asg("A", 60, 90, "SITE-CR-01"))
    assert (c.pool.demand, c.pool.quantity) == (11, 10)


def test_pool_missing_for_required_kind(with_a):
    snap = _pools(take_snapshot(with_a), drop=("UA-SIG",))
    found = _pool_conflicts(snap, with_a)
    assert [(c.rule_id, c.task_ids) for c in found] == [
        ("POOL_MISSING", (t,))
        for t in ("A", "C", "Q")  # UA의 인양 작업
    ]
    # 필수가 아닌 종류는 풀이 없어도 충돌이 아니다
    assert _pool_conflicts(_pools(take_snapshot(with_a), drop=("UA-WRK",)), with_a) == []


def test_conflict_dump_has_pool_only_for_pool_conflicts(with_a):
    snap = take_snapshot(with_a)
    [c] = detect_conflicts(snap, snap.facts().check_assignments(), with_a)
    assert c.rule_id == "SEP-LIFT-BELOW" and "pool" not in c.model_dump(mode="json")


# ── Validator ──────────────────────────────────────────────────


def test_validator_catches_pool_conflicts_through_rule_engine(seeded):
    def bad(snap):
        v = validate(snap, _reconfirm(snap), None, seeded)
        return [(c.check_id, c.reason_code, c.task_ids) for c in v.checks if c.status != "PASS"]

    over = bad(_pools(take_snapshot(seeded), UB_WRK=4))
    assert ("C09", "POOL_CAPACITY", ("B", "D")) in over
    missing = bad(_pools(take_snapshot(seeded), drop=("UB-SIG",)))
    assert ("C07", "POOL_MISSING", ("K",)) in missing  # UB의 인양 작업
    drift = bad(_retask(take_snapshot(seeded), "C", default_demands=()))
    assert ("C11", "DEFAULT_DEMANDS_MISMATCH", ("C",)) in drift


# ── Solver ─────────────────────────────────────────────────────


def _solve(pack, snap, level):
    spec = build_search_spec(snap, A_B, "UA", level)
    return cpsat.solve(snap, spec, pack)


def test_cumulative_constraint_counts_fixed_tasks(with_a):
    """Beta(A 10:00 SITE-CR-01)는 고정된 C(10:00)와 겹친다: 작업 인원 4 + 4 = 8."""
    pack = with_a
    snap = free_alternative(take_snapshot(pack))
    beta = _solve(pack, snap, "L0")
    placed = {a["task_id"]: (a["start"], a["resource_id"]) for a in beta.solution}
    assert placed["A"] == (60, "SITE-CR-01") and placed["C"] == (60, "A-CR-01")
    assert _solve(pack, _pools(snap, UA_WRK=8), "L0").stage1["status"] == "OPTIMAL"
    assert _solve(pack, _pools(snap, UA_WRK=7), "L0").stage1["status"] == "INFEASIBLE"
    assert _solve(pack, _pools(snap, UA_SIG=1), "L0").stage1["status"] == "INFEASIBLE"
    # 범위를 넓혀 C도 움직이면 겹치지 않게 푼다(Alpha와 같은 해)
    alpha = _solve(pack, _pools(snap, UA_WRK=7), "L1")
    placed = {a["task_id"]: a["start"] for a in alpha.solution}
    assert (placed["A"], placed["C"]) == (60, 90)


def test_solver_result_passes_rule_engine_pool_check(with_a):
    pack = with_a
    snap = _pools(free_alternative(take_snapshot(pack)), UA_WRK=7)
    result = _solve(pack, snap, "L1")
    assignments = [Assignment(**a) for a in result.solution]
    assert [c for c in detect_conflicts(snap, assignments, pack) if c.pool] == []


def test_solver_infeasible_when_required_pool_is_missing(with_a):
    snap = _pools(take_snapshot(with_a), drop=("UA-SIG",))
    assert _solve(with_a, snap, "L1").stage1["status"] == "INFEASIBLE"


def test_search_key_includes_pools_and_demands(with_a):
    """풀 수량·허용 Unit과 작업 수요는 Solver 입력이다. 표시 이름·단가는 아니다 (CV-04)."""
    snap = take_snapshot(with_a)

    def key(snapshot):
        return build_search_spec(snapshot, A_B, "UA", "L0").search_key

    base = key(snap)
    assert key(_pools(snap, UA_WRK=9)) != base
    assert key(_pools(snap, drop=("UB-SIG",))) != base
    assert key(_retask(snap, "A", pool_demands=(Demand(kind="WORKER", quantity=5),))) != base
    assert key(_retask(snap, "C", default_demands=())) != base
    shared = tuple(
        p.model_copy(update={"allowed_unit_ids": ("UA", "SITE")}) if p.pool_id == "UA-WRK" else p
        for p in snap.facts().pools
    )
    assert key(with_facts(snap, pools=shared)) != base
    shown = tuple(
        p.model_copy(update={"display_name": "다른 이름", "cost_per_hour": 1})
        for p in snap.facts().pools
    )
    assert key(with_facts(snap, pools=shown)) == base
    # 낮은 작업 값은 수요를 바꾸지 않으므로 키도 같다
    assert key(_retask(snap, "A", pool_demands=(Demand(kind="WORKER", quantity=2),))) == base


# ── 폼 → 충돌 → 재계획 ─────────────────────────────────────────


def _key():
    return uuid.uuid4().hex


def _form(pack, **changes):
    data = pack.new_task.model_dump(exclude={"requested", "unit_id", "owner_actor_id", "movable"})
    data.update(changes)
    return TaskRequestForm(**data)


def _task(pack, task_id):
    with db.read() as conn:
        return next(t for t in list_current_tasks(conn, pack.site_id, pack) if t.task_id == task_id)


def test_form_demand_is_stored_as_task_value(seeded):
    form = _form(seeded, pool_demands=[{"kind": "WORKER", "quantity": 6}])
    assert submit_task_request(seeded, "planner_a", _key(), form).status == "APPLIED"
    a = _task(seeded, "A")
    assert a.demands == {"SIGNALER": 1, "WORKER": 6}
    with db.read() as conn:
        stored = conn.execute("SELECT pool_demands FROM task WHERE task_id = 'A'").fetchone()[0]
    assert json.loads(stored) == [{"kind": "WORKER", "quantity": 6}]  # DB에는 작업 값만
    assert "pool_demands" not in json.dumps(
        {k: v.value for k, v in a.fields.items()}
    )  # 새 필드 없음


@pytest.mark.parametrize(
    ("demands", "reason"),
    [
        ([{"kind": "WELDER", "quantity": 1}], "INVALID_DEMAND"),  # 선언되지 않은 종류
        (
            [{"kind": "WORKER", "quantity": 5}, {"kind": "WORKER", "quantity": 6}],
            "INVALID_DEMAND",  # 같은 종류 두 번
        ),
    ],
)
def test_form_rejects_invalid_demands(seeded, demands, reason):
    out = submit_task_request(seeded, "planner_a", _key(), _form(seeded, pool_demands=demands))
    assert out.status == "REJECTED" and out.reason_codes == (reason,)


def test_form_rejects_when_required_pool_is_missing(seeded):
    with db.write() as tx:
        tx.execute("DELETE FROM pool WHERE pool_id = 'UA-SIG'")
    out = submit_task_request(seeded, "planner_a", _key(), _form(seeded))
    assert out.reason_codes == ("REQUIRED_POOL_MISSING",)
    hot = _form(
        seeded, work_type="HOT_WORK", required_resource_type=None, requested_resource_id=None
    )
    assert (
        submit_task_request(seeded, "planner_a", _key(), hot).status == "APPLIED"
    )  # 필수 직종 없음


def test_pool_excess_becomes_conflict_and_replanning_resolves_it(seeded, main_on):
    """UA 작업 인원: Q 4 + M 3 + X 5 = 12 > 10 → POOL_CAPACITY 충돌 → 재계획이 Q가 끝난 10:30으로 옮긴다."""
    pack = seeded
    form = TaskRequestForm(
        task_id="X",
        work_type="HOT_WORK",
        zone_id="G",
        duration=60,
        earliest_start=2910,  # 10/14 09:30
        latest_start=3060,
        latest_end=3120,
        pool_demands=[{"kind": "WORKER", "quantity": 5}],
    )
    assert submit_task_request(pack, "planner_a", _key(), form).status == "APPLIED"
    run_until_idle(pack, model_factory=Router(replanning=[solve("L0")]).factory())
    with db.read() as conn:
        [run_id] = [
            r[0]
            for r in conn.execute("SELECT run_id FROM agent_run WHERE agent_type = 'REPLANNING'")
        ]
        run = get_run(conn, run_id)
        step = list_steps(conn, run_id)[0]
        cand = get_candidate(conn, pack.site_id, step["tool_result"]["candidate_id"])
    excess = {"pool_id": "UA-WRK", "kind": "WORKER", "at": 2910, "demand": 12, "quantity": 10}
    assert run.input_ref["conflict"] == {"rule_id": "POOL_CAPACITY", "task_ids": ["M", "Q", "X"]}
    obs = step["observation"]
    # 관찰: 충돌에 풀·종류·초과 시각이 보이고, 움직일 수 있는 작업에 수요가 보인다
    assert [(c["rule_id"], c["task_ids"], c["pool"]) for c in obs["conflicts"]] == [
        ("POOL_CAPACITY", ["M", "Q", "X"], excess)
    ]
    assert obs["primary_conflict"]["pool"] == excess
    acting = {t["task_id"]: t["demands"] for t in obs["acting_tasks"]}
    assert acting["X"] == {"WORKER": 5} and acting["Q"] == {"SIGNALER": 1, "WORKER": 4}
    # Q가 끝난 10:30(2970)에는 M 3 + X 5 = 8 ≤ 10
    placed = {a.task_id: a.start for a in cand.assignments}
    assert placed["X"] == 2970 and placed["Q"] == 2910 and placed["M"] == 2910
    assert step["tool_result"]["stage1"]["changed"] == 1
