"""일정의 합치기 방향 (AG-28·CV-27).

일정 넣기 사건이 있는 Case의 접근은 세 방향이다: 기존 위주, 추가 위주, 적절하게. 목적 순서는 접근이 정하고
방향 호출의 범위는 서버가 작업 전체로 채운다. 기존 작업은 지금 계획에 있는 작업, 추가 작업은 없는 작업이다.
Pack 그대로의 R0에 화기 작업 둘(S1·S2)을 일정으로 넣은 장면에서 본다: 도장 P·W와 하나씩 부딪힌다.
"""

from conftest import add_run, take_snapshot
from scripted import Router, ScriptedChatModel, done, main_call, main_wait, solve, solve_with
from test_schedule_review import _groups, _import_two_conflicts, _runs, _steps, _submit

from app.agents import runtime
from app.agents.types import APPROACHES, DIRECTIONS, approaches_for, objective_of, scope_of
from app.api.state import build_state, candidate_view
from app.coordinator.dispatcher import run_until_idle
from app.rules.engine import detect_conflicts
from app.solver import cpsat
from app.solver.candidate import build_candidate
from app.solver.search_spec import build_search_spec
from app.store import db


def _solved(pack, objective, level="L2"):
    snap = take_snapshot(pack)
    facts = snap.facts()
    conflicts = detect_conflicts(snap, facts.check_assignments(), pack)
    spec = build_search_spec(snap, conflicts, level, objective=objective)
    result = cpsat.solve(snap, spec, pack)
    return result, facts.change_counts(build_candidate(snap, spec, result).assignments)


def test_approach_decides_the_objective_and_the_scope():
    assert (approaches_for(True), approaches_for(False)) == (DIRECTIONS, APPROACHES)
    assert [objective_of(a) for a in DIRECTIONS] == [
        "EXISTING_FIRST",
        "ADDED_FIRST",
        "CHANGE_FIRST",
    ]
    assert [objective_of(a) for a in APPROACHES] == ["CHANGE_FIRST", "DELAY_FIRST"]
    # 방향 호출의 범위는 서버가 작업 전체로 채우고, 그 밖의 접근은 재계획이 고른다
    assert [scope_of(a) for a in DIRECTIONS] == ["L2", "L2", "L2"]
    assert [scope_of(a) for a in (*APPROACHES, None)] == [None, None, None]


def test_existing_first_and_added_first_minimize_their_own_side(seeded_real):
    """같은 범위에서 최적이 확인되면 기존 위주 안의 기존 변경 수가 가장 작고, 추가 위주 안의 추가 변경
    수가 가장 작고, 적절하게 안의 전체 변경 수가 가장 작다."""
    pack = seeded_real
    _import_two_conflicts(pack)
    existing, n_first = _solved(pack, "EXISTING_FIRST")
    added, m_first = _solved(pack, "ADDED_FIRST")
    balanced, total_first = _solved(pack, "CHANGE_FIRST")
    for result in (existing, added, balanced):
        assert (result.stage1["status"], result.stage2["status"]) == ("OPTIMAL", "OPTIMAL")
    others = (n_first, m_first, total_first)
    assert n_first["existing"] == min(c["existing"] for c in others)
    assert m_first["added"] == min(c["added"] for c in others)
    total = [c["existing"] + c["added"] for c in others]
    assert total[2] == min(total)
    # 이 장면에서는 방향이 실제로 갈린다: 들어온 화기 작업을 옮기거나, 기존 도장 작업을 옮기거나
    assert n_first == {"existing": 0, "added": 2, "delay": 210}
    assert m_first == {"existing": 2, "added": 0, "delay": 1275}
    assert total_first == n_first
    # 1단계 결과에는 기존·추가 변경 수가 따로 남고, 2단계는 두 수를 고정한 채 옮긴 거리를 줄인다
    assert (existing.stage1["changed_existing"], existing.stage1["changed_added"]) == (0, 2)
    assert (added.stage1["changed_existing"], added.stage1["changed_added"]) == (2, 0)
    assert (existing.stage1["changed"], added.stage1["changed"]) == (2, 2)
    assert "changed_existing" not in balanced.stage1  # 변경 먼저의 결과 모양은 그대로다
    # 범위가 같으면 좁은 범위보다 나쁠 수 없다
    _, narrow = _solved(pack, "EXISTING_FIRST", "L0")
    assert n_first["existing"] <= narrow["existing"]


def test_schedule_case_runs_three_directions_and_names_the_plans(seeded_real, main_on):
    """일정 Case에서는 세 방향이 열리고 변경 최소·덜 옮기기는 열리지 않는다. 방향 호출에서 재계획은 목적
    순서도 범위도 고르지 못한다. 안의 이름은 그 안을 낸 호출의 방향이고, 같은 배치가 두 방향에서 나오면
    번호와 방향 이름이 함께 보인다. 숫자(기존 n · 추가 m · 옮긴 거리)는 서버가 계산한다."""
    pack = seeded_real
    _import_two_conflicts(pack)
    _, groups = _groups(pack)
    router = Router(
        main=[
            main_call("SCHEDULE_REVIEW"),
            main_call("REPLANNING", approach="MIN_CHANGE"),  # 일정 Case에서는 열리지 않는다
            main_call("REPLANNING", approach="KEEP_EXISTING"),
            main_call("REPLANNING", approach="KEEP_ADDED"),
            main_call("REPLANNING", approach="BALANCED"),
            main_wait(),
        ],
        schedule_review=[_submit([g.group_id for g in groups])],
        replanning=[
            solve("L0"),  # 방향 호출의 범위는 서버가 정한 하나다
            solve_with("L2"),  # 조건 없이 푸는 것은 범위 계산과 같다
            solve("L2"),
            done(),
            solve("L2"),
            done(),
            solve("L2"),
            done(),
        ],
        auto_done=False,
    )
    run_until_idle(pack, model_factory=router.factory())
    [main] = _runs("MAIN")
    guards = [(s["action"]["name"], s["guard"]["reason_code"]) for s in _steps(main.run_id)]
    assert guards == [
        ("CALL_AGENT", None),
        ("CALL_AGENT", "APPROACH_NOT_OPEN"),
        ("CALL_AGENT", None),
        ("CALL_AGENT", None),
        ("CALL_AGENT", None),
        ("WAIT", None),
    ]
    seen = _steps(main.run_id)[1]["observation"]
    assert seen["replanning"]["approaches"] == list(DIRECTIONS)
    assert [c["approach"] for c in seen["calls"] if c["agent"] == "REPLANNING"] == list(DIRECTIONS)

    first, second, third = _runs("REPLANNING")
    steps = _steps(first.run_id)
    assert [(s["result_kind"], s["guard"]["reason_code"]) for s in steps] == [
        ("REJECTED", "ACTION_NOT_AVAILABLE"),
        ("REJECTED", "CONDITION_INVALID"),
        ("WAIT", None),
        ("DONE", None),
    ]
    # 목적 순서와 범위는 서버가 채운다: 도구에 목적 순서 인자가 없고 범위는 값 하나만 열린다
    obs = steps[0]["observation"]
    assert obs["approach"] == {
        "approach": "KEEP_EXISTING",
        "quoted_note": None,
        "objective": "EXISTING_FIRST",
        "scope_level": "L2",
    }
    assert obs["untried_levels"] == ["L2"]
    tools = {
        t["function"]["name"]: t["function"]["parameters"]["properties"]
        for t in steps[0]["available_actions"]
    }
    for name in ("SOLVE_WITH_SCOPE", "SOLVE_WITH_CONDITIONS"):
        assert tools[name]["level"]["enum"] == ["L2"]
        assert "objective" not in tools[name]
    results = [_steps(r.run_id)[-2]["tool_result"] for r in (first, second, third)]
    assert [r.get("objective") for r in results] == ["EXISTING_FIRST", "ADDED_FIRST", None]
    assert [r["scope_level"] for r in results] == ["L2", "L2", "L2"]
    # 결과에 서버가 남기는 숫자. 적절하게는 기존 위주와 같은 배치라 새 후보가 없다
    assert results[0]["change_counts"] == {"existing": 0, "added": 2, "delay": 210}
    assert results[1]["change_counts"] == {"existing": 2, "added": 0, "delay": 1275}
    assert results[2]["same_as_candidate_id"] == results[0]["candidate_id"]

    with db.read() as conn:
        state = build_state(conn, pack, "supervisor")
    views = {c["candidate_id"]: c for c in state["candidates"]}
    keep_existing, keep_added = views[results[0]["candidate_id"]], views[results[1]["candidate_id"]]
    # 이름은 그 안을 낸 호출의 방향이다. 같은 배치가 두 방향에서 나오면 번호도 방향 이름도 함께 보인다
    assert (keep_existing["plan_label"], keep_added["plan_label"]) == ("1안 + 3안", "2안")
    assert [(a["approach"], a["same"]) for a in keep_existing["approaches"]] == [
        ("KEEP_EXISTING", False),
        ("BALANCED", True),
    ]
    assert [a["approach"] for a in keep_added["approaches"]] == ["KEEP_ADDED"]
    assert keep_existing["change_counts"] == {"existing": 0, "added": 2, "delay": 210}
    assert keep_added["change_counts"] == {"existing": 2, "added": 0, "delay": 1275}
    assert [x["kind"] for x in keep_existing["plan_changes"]] == ["NEW", "NEW"]
    assert [(x["kind"], x["changed"]) for x in keep_added["plan_changes"]] == [
        ("CHANGED", True),
        ("NEW", False),  # 문서 자리 그대로 놓인 추가 작업
        ("NEW", False),
        ("CHANGED", True),
    ]
    assert not keep_existing["solver"]["first_unconfirmed"]


def test_first_stage_not_optimal_is_shown_as_unconfirmed(seeded_real, monkeypatch):
    """시간 한도에 걸려 1단계의 최적이 확인되지 않으면 그 안은 "최적 미확인"이다."""
    pack = seeded_real
    _import_two_conflicts(pack)
    real = cpsat._solve_stage

    def limited(model, time_limit_s):
        status, solver = real(model, time_limit_s)
        return ("FEASIBLE" if status == "OPTIMAL" else status), solver

    monkeypatch.setattr(cpsat, "_solve_stage", limited)
    add_run(pack, "run_1", input_ref={"approach": "KEEP_EXISTING"})
    runtime.invoke(pack, {"run_id": "run_1"}, ScriptedChatModel([solve("L2")]))
    [step] = _steps("run_1")
    result = step["tool_result"]
    assert (result["stage1"]["status"], result["stage2"], result["minimal_change"]) == (
        "FEASIBLE",
        None,
        False,
    )
    with db.read() as conn:
        view = candidate_view(conn, pack.site_id, result["candidate_id"])
    assert view["solver"]["first_unconfirmed"] is True
    assert view["approaches"][0]["approach"] == "KEEP_EXISTING"
