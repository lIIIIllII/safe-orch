"""여러 안 제안: 접근을 달리한 재계획, 목적 순서, 같은 안 표시, Supervisor가 고른 안만 협의 (AG-28·CV-27).

Pack 파일 그대로(seeded_real)에서 요청 A를 넣은 장면을 쓴다.
"""

import uuid

from conftest import add_run, add_task, choose, make_task, take_snapshot
from scripted import (
    Router,
    ScriptedChatModel,
    call,
    cond,
    done,
    main_call,
    main_wait,
    solve,
    solve_with,
)

from app.agents import casefacts, runtime
from app.api.state import build_state
from app.commands.approval import ChooseRequest, choose_candidate
from app.commands.task_request import TaskRequestForm, submit_task_request
from app.coordinator.dispatcher import run_until_idle
from app.domain.models import Condition
from app.rules.engine import detect_conflicts
from app.solver import cpsat
from app.solver.candidate import build_candidate
from app.solver.search_spec import build_search_spec
from app.store import db
from app.store.repos.calls import call_key
from app.store.repos.records import list_validations
from app.store.repos.runs import get_run, list_steps
from app.validator.validator import validate

CONFLICT = {"conflict": {"rule_id": "SEP-LIFT-BELOW", "task_ids": ["A", "B"]}}


def _key():
    return uuid.uuid4().hex


def _submit_a(pack):
    a = pack.new_task.model_dump(exclude={"requested", "unit_id", "owner_actor_id", "movable"})
    assert submit_task_request(pack, "planner_a", _key(), TaskRequestForm(**a)).status == "APPLIED"


def _runs(agent_type):
    with db.read() as conn:
        ids = [
            r[0]
            for r in conn.execute(
                "SELECT run_id FROM agent_run WHERE agent_type = ? ORDER BY rowid", (agent_type,)
            )
        ]
        return [get_run(conn, rid) for rid in ids]


def _steps(run_id):
    with db.read() as conn:
        return list_steps(conn, run_id)


def _guards(run_id):
    return [(s["action"]["name"], s["guard"]["reason_code"]) for s in _steps(run_id)]


def _candidate_ids():
    with db.read() as conn:
        return [r[0] for r in conn.execute("SELECT candidate_id FROM candidate ORDER BY rowid")]


def _group_id(pack):
    with db.read() as conn:
        _, _, [group] = casefacts.current_groups(conn, pack)
    return group.group_id


def _choose(pack, actor, candidate_id):
    with db.read() as conn:
        v = list_validations(conn, pack.site_id, candidate_id)[-1]
    body = ChooseRequest(candidate_id=candidate_id, validation_id=v.validation_id)
    return choose_candidate(pack, actor, _key(), body)


def _events(kind):
    with db.read() as conn:
        return conn.execute(
            "SELECT ref, case_id FROM case_event WHERE kind = ? ORDER BY seq", (kind,)
        ).fetchall()


# ── 목적 순서 ──────────────────────────────────────────────────


def test_delay_first_swaps_stages_and_is_a_different_search(seeded_real):
    pack = seeded_real
    add_task(pack, make_task(pack))
    snap = take_snapshot(pack)
    conflict = detect_conflicts(snap, snap.facts().check_assignments(), pack)[0]
    conds = {"C": Condition(start_min=60)}
    plain = build_search_spec(snap, conflict, "UA", "L1", None, conds)
    spec = build_search_spec(snap, conflict, "UA", "L1", None, conds, "DELAY_FIRST")
    assert (spec.objective, plain.objective) == ("DELAY_FIRST", "CHANGE_FIRST")
    assert spec.search_key != plain.search_key and spec.hash != plain.hash

    result = cpsat.solve(snap, spec, pack)
    # 1단계가 총 지연, 2단계가 그 지연 안에서 변경 작업 수다 (A 09:00→10:00, C 10:00→10:30)
    assert (result.stage1["status"], result.stage1["delay"]) == ("OPTIMAL", 90)
    assert (result.stage2["delay"], result.stage2["changed"]) == (90, 2)
    change_first = cpsat.solve(snap, plain, pack)
    assert (change_first.stage1["changed"], change_first.stage2["delay"]) == (2, 90)
    assert "delay" not in change_first.stage1  # 변경 먼저의 결과 모양은 그대로다
    candidate = build_candidate(snap, spec, result)
    assert validate(snap, candidate, spec, pack).status == "PASS"


def test_objective_argument_through_the_tool(seeded_real):
    pack = seeded_real
    add_task(pack, make_task(pack))
    add_run(pack, "run_1", input_ref=CONFLICT)
    replies = [
        solve_with("L1"),  # 조건도 없고 목적 순서도 기본이면 범위 계산과 같다
        solve_with("L0", objective="DELAY_FIRST"),  # L0은 해가 없다
        solve_with("L0", objective="DELAY_FIRST"),
        solve_with("L1", cond("C", start_from=60), objective="DELAY_FIRST"),
    ]
    run = runtime.invoke(pack, {"run_id": "run_1"}, ScriptedChatModel(replies))
    steps = _steps("run_1")
    assert [(s["result_kind"], s["guard"]["reason_code"]) for s in steps] == [
        ("REJECTED", "CONDITION_INVALID"),
        ("CONTINUE", None),
        ("REJECTED", "ALREADY_TRIED"),
        ("WAIT", None),
    ]
    result = steps[3]["tool_result"]
    assert (result["objective"], result["stage1"]["delay"], result["stage2"]["changed"]) == (
        "DELAY_FIRST",
        90,
        2,
    )
    assert run.solver_calls_used == 2
    # 이전 계산에 목적 순서와, 건 조건이 도구 인자와 같은 모양(현장 날짜·시각 문자열)으로 보인다
    add_run(pack, "run_2", case_id="case_run_1", input_ref=CONFLICT)
    with db.read() as conn:
        from app.agents.observers import replanning as observer

        attempts = observer.build_observation(conn, pack, "run_2").data["attempts"]
    assert [(a["objective"], a["conditions_as_args"]) for a in attempts] == [
        ("DELAY_FIRST", []),
        ("DELAY_FIRST", [{"task_id": "C", "start_from": "2026-10-12(월) 10:00"}]),
    ]
    assert attempts[1]["stage1"] == {"status": "OPTIMAL", "changed": None, "delay": 90}


# ── 접근을 달리한 재계획과 같은 안 ─────────────────────────────


def test_approach_is_in_call_key_and_same_placement_joins_candidate(seeded_real, main_on):
    """같은 접근·같은 사실의 재호출은 거절되고 다른 접근은 받아들여진다. 다른 접근이 같은 배치를 내면
    새 후보 없이 그 후보에 접근이 더해진다. 고르기 전에는 협의가 나가지 않는다."""
    pack = seeded_real
    _submit_a(pack)
    group = _group_id(pack)
    assert call_key(
        "REPLANNING", {"group_id": group, "acting_unit_id": "UA", "approach": "MIN_DELAY"}
    ) == (f"REPLANNING:{group}:UA:MIN_DELAY")

    def replan(approach=None, note=None):
        refs = {"group_id": group, "acting_unit_id": "UA"}
        if approach is None:  # 접근 없이
            return call("CALL_AGENT", "부른다", agent="REPLANNING", **refs)
        return main_call("REPLANNING", approach=approach, approach_note=note, **refs)

    def consult_unchosen():
        return main_call("COORDINATION", phase="CONSULT", candidate_id=_candidate_ids()[0])

    router = Router(
        main=[
            replan("MIN_CHANGE"),
            replan("MIN_CHANGE"),
            replan("MIN_DELAY", "늦어지는 작업이 없게"),
            replan(),
            consult_unchosen,
            main_wait(),
        ],
        replanning=[
            solve("L1"),
            done(),
            solve_with(
                "L1", objective="DELAY_FIRST", summary="이유: 지연을 먼저 줄인다/다음: 검증"
            ),
            done(),
        ],
        auto_done=False,
    )
    run_until_idle(pack, model_factory=router.factory())
    [main] = _runs("MAIN")
    assert _guards(main.run_id) == [
        ("CALL_AGENT", None),
        ("CALL_AGENT", "SAME_FACTS"),  # 같은 접근, 사실이 그대로
        ("CALL_AGENT", None),  # 접근이 다르면 받아들여진다
        ("CALL_AGENT", "APPROACH_REQUIRED"),
        ("CALL_AGENT", "CANDIDATE_NOT_CHOSEN"),
        ("WAIT", None),
    ]
    assert (main.status, main.wait_kind, _runs("COORDINATION")) == (
        "WAITING_HUMAN",
        "HUMAN_DECISION",
        [],
    )
    first, second = _runs("REPLANNING")
    assert [r.input_ref["call_key"].rsplit(":", 1)[1] for r in (first, second)] == [
        "MIN_CHANGE",
        "MIN_DELAY",
    ]
    [cid] = _candidate_ids()  # 두 접근이 같은 배치를 냈다
    s1 = _steps(second.run_id)[0]
    obs = s1["observation"]
    assert obs["approach"] == {"approach": "MIN_DELAY", "quoted_note": "늦어지는 작업이 없게"}
    assert obs["approach_candidates"] == [
        {"no": 1, "approach": "MIN_CHANGE", "candidate_id": cid, "same": False}
    ]
    assert s1["tool_result"]["same_as_candidate_id"] == cid

    # 메인 관찰과 화면 상태: 그 후보에 접근 둘, 고르지 않음, 협의 호출 없음
    waiting = _steps(main.run_id)[-1]["observation"]
    [seen] = waiting["candidates"]
    assert (seen["approaches"], seen["chosen"]) == (["MIN_CHANGE", "MIN_DELAY"], False)
    assert not [c for c in waiting["calls"] if c["agent"] == "COORDINATION"]
    with db.read() as conn:
        state = build_state(conn, pack, "supervisor")
    view = next(c for c in state["candidates"] if c["candidate_id"] == cid)
    assert [(a["no"], a["approach"], a["same"]) for a in view["approaches"]] == [
        (1, "MIN_CHANGE", False),
        (2, "MIN_DELAY", True),
    ]
    assert view["approaches"][1]["quoted_reason"] == "이유: 지연을 먼저 줄인다/다음: 검증"
    assert (view["chosen"], view["case_id"], view["solver"]["objective"]) == (
        False,
        main.case_id,
        "CHANGE_FIRST",
    )

    # Supervisor만 고른다. 고르면 사건이 되어 메인이 깨어나고, 고른 안만 협의가 나간다
    assert _choose(pack, "planner_a", cid).reason_codes == ("NOT_AUTHORIZED",)
    out = _choose(pack, "supervisor", cid)
    assert out.status == "APPLIED" and out.result_refs["replaced"] == []
    assert _choose(pack, "supervisor", cid).reason_codes == ("ALREADY_CHOSEN",)
    [(ref, case_id)] = _events("CANDIDATE_CHOSEN")
    assert case_id == main.case_id and cid in ref
    assert _runs("MAIN")[0].wake_seq == main.wake_seq + 1
    run_until_idle(pack, model_factory=Router().factory())
    [consult] = _runs("COORDINATION")
    assert (consult.input_ref["phase"], consult.input_ref["candidate_id"], consult.status) == (
        "CONSULT",
        cid,
        "WAITING_HUMAN",
    )
    with db.read() as conn:
        state = build_state(conn, pack, "supervisor")
    assert next(c for c in state["candidates"] if c["candidate_id"] == cid)["chosen"] is True


def test_choosing_another_plan_ends_the_open_consultation(seeded_real, main_on):
    """고른 안을 바꾸면 앞 안의 열린 협의는 정리되고(요청 취소) 새로 고른 안만 협의한다."""
    pack = seeded_real
    _submit_a(pack)
    replies = [solve("L1"), solve_with("L2", cond("M", start_at=2940)), done()]
    run_until_idle(pack, model_factory=Router(replanning=replies, auto_done=False).factory())
    first, second = _candidate_ids()
    assert _runs("COORDINATION") == []
    choose(pack, first)
    run_until_idle(pack, model_factory=Router().factory())
    [consult] = _runs("COORDINATION")
    assert (consult.input_ref["candidate_id"], consult.status) == (first, "WAITING_HUMAN")

    out = _choose(pack, "supervisor", second)
    assert out.status == "APPLIED" and out.result_refs["replaced"] == [first]
    [ended] = _runs("COORDINATION")
    assert (ended.status, ended.end_reason) == ("STALE", f"CHOICE_CHANGED:{second}")
    with db.read() as conn:
        requests = conn.execute(
            "SELECT candidate_id, status FROM message WHERE type = 'CHANGE_REQUEST' ORDER BY rowid"
        ).fetchall()
        state = build_state(conn, pack, "supervisor")
    assert [tuple(r) for r in requests] == [(first, "CANCELLED")]
    chosen = {c["candidate_id"]: c["chosen"] for c in state["candidates"]}
    assert (chosen[first], chosen[second]) == (False, True)
    assert set(state["review_queue"]) == {first, second}  # 고르지 않은 안은 그대로 둔다

    run_until_idle(pack, model_factory=Router().factory())
    again = _runs("COORDINATION")[-1]
    assert (again.input_ref["candidate_id"], again.status) == (second, "WAITING_HUMAN")
    assert _choose(pack, "supervisor", first).status == "APPLIED"  # 다시 바꿀 수 있다
