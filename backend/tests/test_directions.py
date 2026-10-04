"""일정의 합치기 방향 (AG-28·CV-27).

일정 넣기 사건이 있는 Case의 접근은 세 방향이다: 기존 위주, 추가 위주, 적절하게. 목적 순서는 접근이 정하고
방향 호출의 범위는 서버가 작업 전체로 채운다. 기존 작업은 지금 계획에 있는 작업, 추가 작업은 없는 작업이다.
Pack 그대로의 R0에 화기 작업 둘(S1·S2)을 일정으로 넣은 장면에서 본다: 도장 P·W와 하나씩 부딪힌다.
"""

from conftest import add_run, choose, reply_request, take_snapshot
from scripted import (
    Router,
    ScriptedChatModel,
    blocked,
    done,
    main_call,
    main_escalate,
    main_wait,
    solve,
    solve_with,
)
from test_schedule_review import _groups, _import_two_conflicts, _runs, _steps, _submit

from app.agents import casefacts, runtime
from app.agents.types import APPROACHES, DIRECTIONS, approaches_for, objective_of, scope_of
from app.api.state import build_state, candidate_view
from app.commands.approval import RejectRequest, reject_candidate
from app.coordinator.dispatcher import run_until_idle
from app.domain.ids import new_id
from app.rules.engine import detect_conflicts
from app.solver import cpsat
from app.solver.candidate import build_candidate
from app.solver.search_spec import build_search_spec
from app.store import db
from app.store.repos.consultations import list_review_queue
from app.store.repos.records import list_validations


def _untried(observation):
    """메인 관찰의 열린 일 가운데 아직 부르지 않은 방향."""
    return [w["approach"] for w in observation["open_work"] if w["kind"] == "DIRECTION_UNTRIED"]


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
    # 1단계 결과에는 기존·추가 변경 수가 따로 남고, 그 값은 그 해를 서버가 센 수와 같다(2단계는 두 수를
    # 고정한 채 옮긴 거리를 줄인다)
    for result, counts in ((existing, n_first), (added, m_first)):
        s1 = result.stage1
        assert (s1["changed_existing"], s1["changed_added"]) == (
            counts["existing"],
            counts["added"],
        )
        assert s1["changed"] == counts["existing"] + counts["added"]
    assert "changed_existing" not in balanced.stage1  # 변경 먼저의 결과 모양은 그대로다
    assert balanced.stage1["changed"] == total[2]
    # 넓은 범위는 좁은 범위보다 나쁠 수 없다: 방향마다 먼저 줄이는 수로 본다
    for objective, side in (("EXISTING_FIRST", "existing"), ("ADDED_FIRST", "added")):
        _, wide = _solved(pack, objective, "L2")
        _, narrow = _solved(pack, objective, "L0")
        assert wide[side] <= narrow[side]


def test_schedule_case_runs_three_directions_and_names_the_plans(seeded_real, main_on):
    """일정 Case에서는 세 방향이 열리고 변경 최소·덜 옮기기는 열리지 않는다. 방향 호출에서 재계획은 목적
    순서도 범위도 고르지 못한다. 안의 이름은 그 안을 낸 호출의 방향이고, 같은 배치가 여러 방향에서 나오면
    번호와 방향 이름이 함께 보인다. 숫자(기존 n · 추가 m · 옮긴 거리)는 서버가 계산한다. 어느 방향이 어떤
    배치를 냈는지는 장면에 달린 값이라 고정하지 않는다."""
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
    # 아직 부르지 않은 방향은 열린 일이다: 부를 때마다 내려가고, 남아 있는 동안에는 끝낼 수 없다
    untried = [_untried(s["observation"]) for s in _steps(main.run_id)]
    assert untried[1:] == [
        list(DIRECTIONS),
        list(DIRECTIONS),
        list(DIRECTIONS[1:]),
        list(DIRECTIONS[2:]),
        [],
    ]
    for s in _steps(main.run_id)[:5]:
        names = [t["function"]["name"] for t in s["available_actions"]]
        assert "CLOSE" not in names and "ESCALATE" in names
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

    with db.read() as conn:
        state = build_state(conn, pack, "supervisor")
    views = {c["candidate_id"]: c for c in state["candidates"]}
    # 결과마다 그 배치의 안(새 후보이거나, 같은 배치였던 기존 후보)
    reached = [r["candidate_id"] or r["same_as_candidate_id"] for r in results]
    assert all(cid in views for cid in reached)
    for cid, view in views.items():
        # 이름은 그 안을 낸 호출의 방향이다. 같은 배치가 여러 방향에서 나오면 방향 이름도 번호도 함께 보인다
        directions = [a for a, c in zip(DIRECTIONS, reached, strict=True) if c == cid]
        assert [a["approach"] for a in view["approaches"]] == directions
        assert [a["same"] for a in view["approaches"]] == [False] + [True] * (len(directions) - 1)
        assert len(view["plan_label"].split(" + ")) == len(directions)
        # 숫자는 서버가 계산한다: 기존(계획에 있던 작업)·추가(없던 작업) 변경 수는 변경 목록과 맞는다
        changed = [x for x in view["plan_changes"] if x["changed"]]
        assert view["change_counts"]["existing"] == sum(x["kind"] == "CHANGED" for x in changed)
        assert view["change_counts"]["added"] == sum(x["kind"] == "NEW" for x in changed)
        assert view["change_counts"]["delay"] == sum(x["delay"] for x in view["plan_changes"])
        assert not view["solver"]["first_unconfirmed"]
    # 새 후보를 낸 결과에 남긴 숫자는 그 안의 숫자와 같다
    for r in results:
        if r["candidate_id"] is not None:
            assert r["change_counts"] == views[r["candidate_id"]]["change_counts"]


def test_untried_directions_are_dropped_when_one_direction_is_blocked(seeded_real, main_on):
    """방향은 해의 유무를 바꾸지 않는다. 한 방향의 재계획이 막힌 결과를 돌려주면 나머지 방향은 열린 일에
    오르지 않고, 메인은 이관할 수 있다."""
    pack = seeded_real
    _import_two_conflicts(pack)
    router = Router(
        main=[main_call("REPLANNING", approach="KEEP_EXISTING"), main_escalate()],
        replanning=[blocked("해가 없다")],
        auto_done=False,
    )
    run_until_idle(pack, model_factory=router.factory())
    [main] = _runs("MAIN")
    first, last = _steps(main.run_id)
    assert _untried(first["observation"]) == list(DIRECTIONS)
    assert _untried(last["observation"]) == []
    # 부를 수 있는 호출에는 남아 있다: 서버가 순서를 막지 않는다. 부르지 않는 것은 지침이다
    assert {c["approach"] for c in last["observation"]["calls"] if c["agent"] == "REPLANNING"} == {
        "KEEP_ADDED",
        "BALANCED",
    }
    assert (main.status, last["guard"]["verdict"]) == ("ESCALATED", "ACCEPTED")


def _three_directions_called(pack):
    """일정을 넣고 메인이 세 방향을 모두 부른 뒤 기다리는 데까지. 메인 Run."""
    _import_two_conflicts(pack)
    router = Router(
        main=[main_call("REPLANNING", approach=a) for a in DIRECTIONS] + [main_wait()],
        replanning=[solve("L2"), done()] * 3,
        auto_done=False,
    )
    run_until_idle(pack, model_factory=router.factory())
    [main] = _runs("MAIN")
    return main


def _open_untried(pack, main):
    """지금 메인이 보게 될 열린 일의 미시도 방향 (서버 사실)."""
    with db.read() as conn:
        return _untried(casefacts.build(conn, pack, main))


def _open_requests():
    with db.read() as conn:
        return conn.execute(
            "SELECT DISTINCT request_group_id, to_actor_id FROM message"
            " WHERE type = 'CHANGE_REQUEST' AND status = 'OPEN' ORDER BY rowid"
        ).fetchall()


def test_choosing_and_accepting_do_not_reopen_directions(seeded_real, main_on):
    """고르기와 담당자 수락은 재계획의 입력이 아니다: 세 방향을 부른 뒤에는 다시 열린 일에 오르지 않는다."""
    pack = seeded_real
    main = _three_directions_called(pack)
    assert _open_untried(pack, main) == []
    choose(pack)
    assert _open_untried(pack, main) == []
    run_until_idle(pack, model_factory=Router().factory())  # 메인이 고른 안의 협의를 부른다
    requests = _open_requests()
    assert requests
    for group, owner in requests:
        assert reply_request(pack, owner, group).status == "APPLIED"
    assert _open_untried(pack, main) == []


def test_objection_reopens_every_direction(seeded_real, main_on):
    """담당자 이견은 재계획의 입력이다(CV-26): 사유를 반영한 새 안을 내도록 세 방향이 다시 오른다."""
    pack = seeded_real
    main = _three_directions_called(pack)
    choose(pack)
    run_until_idle(pack, model_factory=Router().factory())
    group, owner = _open_requests()[0]
    assert reply_request(pack, owner, group, "DECLINE", "그 시각은 안 됩니다").status == "APPLIED"
    assert _open_untried(pack, main) == list(DIRECTIONS)


def test_rejection_reopens_every_direction(seeded_real, main_on):
    """Supervisor 거절은 재계획의 입력이다(CV-26): 세 방향이 다시 오른다."""
    pack = seeded_real
    main = _three_directions_called(pack)
    with db.read() as conn:
        cid = list_review_queue(conn, pack.site_id)[0]
        v = list_validations(conn, pack.site_id, cid)[-1]
    body = RejectRequest(
        candidate_id=cid, validation_id=v.validation_id, reason_code="OTHER", comment="다시"
    )
    assert reject_candidate(pack, "supervisor", new_id("key"), body).status == "APPLIED"
    assert _open_untried(pack, main) == list(DIRECTIONS)


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
