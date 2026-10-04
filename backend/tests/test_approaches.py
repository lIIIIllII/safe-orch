"""여러 안 제안: 접근을 달리한 재계획, 목적 순서, 같은 안 표시, Supervisor가 고른 안만 협의 (AG-28·AG-29·CV-27).

Pack 파일 그대로(seeded_real)에서 요청 A를 넣은 장면을 쓴다.
"""

import uuid

from conftest import add_run, add_task, choose, make_task, pin_tasks, take_snapshot
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
from app.commands.approval import (
    ChooseRequest,
    RejectRequest,
    choose_candidate,
    reject_candidate,
)
from app.commands.messages import ReplyRequest, reply_message
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


def _first_day_busy():
    """목적 순서의 효과를 보려고 대체 자원(공용 크레인)은 첫날에 쓸 수 없게 둔다: 고정되지 않은 작업은
    자원도 움직이므로 그대로 두면 자원을 바꿔 풀린다 (AG-34)."""
    with db.write() as tx:
        tx.execute(
            "UPDATE resource SET available_intervals = '[[1440, 3360]]'"
            " WHERE resource_id = 'SITE-CR-01'"
        )


def test_delay_first_swaps_stages_and_is_a_different_search(seeded_real):
    pack = seeded_real
    _first_day_busy()
    add_task(pack, make_task(pack))
    pin_tasks(pack, ["B"])  # 목적 순서의 차이를 A·C에서 보려고 충돌 상대 B는 고정해 둔다
    snap = take_snapshot(pack)
    conflict = detect_conflicts(snap, snap.facts().check_assignments(), pack)[0]
    conds = {"C": Condition(start_min=60)}
    plain = build_search_spec(snap, [conflict], "L1", conds)
    spec = build_search_spec(snap, [conflict], "L1", conds, "DELAY_FIRST")
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
    _first_day_busy()
    add_task(pack, make_task(pack))
    pin_tasks(pack, ["B"])  # 목적 순서의 차이를 A·C에서 보려고 충돌 상대 B는 고정해 둔다
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
    # 호출 키는 접근뿐이다: 재계획은 현장의 충돌 전체를 푼다 (AG-24)
    assert call_key("REPLANNING", {"approach": "PREFER_WINDOW"}) == "REPLANNING:PREFER_WINDOW"

    def replan(approach=None, note=None):
        if approach is None:  # 접근 없이
            return call("CALL_AGENT", "부른다", agent="REPLANNING")
        return main_call("REPLANNING", approach=approach, approach_note=note)

    def consult_unchosen():
        return main_call("COORDINATION", phase="CONSULT", candidate_id=_candidate_ids()[0])

    router = Router(
        main=[
            replan("MIN_CHANGE"),
            replan("MIN_CHANGE"),
            replan("PREFER_WINDOW", "늦어지는 작업이 없게"),
            replan("MIN_DELAY"),  # 접근은 둘뿐이다 (AG-28)
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
        ("CALL_AGENT", "MALFORMED"),
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
        "PREFER_WINDOW",
    ]
    [cid] = _candidate_ids()  # 두 접근이 같은 배치를 냈다
    s1 = _steps(second.run_id)[0]
    obs = s1["observation"]
    assert obs["approach"] == {"approach": "PREFER_WINDOW", "quoted_note": "늦어지는 작업이 없게"}
    assert obs["approach_candidates"] == [
        {"no": 1, "approach": "MIN_CHANGE", "candidate_id": cid, "same": False}
    ]
    assert s1["tool_result"]["same_as_candidate_id"] == cid

    # 메인 관찰과 화면 상태: 그 후보에 접근 둘, 고르지 않음, 협의 호출 없음
    waiting = _steps(main.run_id)[-1]["observation"]
    [seen] = waiting["candidates"]
    assert (seen["approaches"], seen["chosen"]) == (["MIN_CHANGE", "PREFER_WINDOW"], False)
    assert not [c for c in waiting["calls"] if c["agent"] == "COORDINATION"]
    with db.read() as conn:
        state = build_state(conn, pack, "supervisor")
    view = next(c for c in state["candidates"] if c["candidate_id"] == cid)
    assert [(a["no"], a["approach"], a["same"]) for a in view["approaches"]] == [
        (1, "MIN_CHANGE", False),
        (2, "PREFER_WINDOW", True),
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
    # 안 번호는 후보마다 만들어진 순서로 매긴다: 한 호출이 낸 두 후보도 번호가 다르다
    views = {c["candidate_id"]: c for c in state["candidates"]}
    assert (views[first]["plan_no"], views[second]["plan_no"]) == (1, 2)
    assert [a["no"] for cid in (first, second) for a in views[cid]["approaches"]] == [1, 1]

    run_until_idle(pack, model_factory=Router().factory())
    again = _runs("COORDINATION")[-1]
    assert (again.input_ref["candidate_id"], again.status) == (second, "WAITING_HUMAN")
    assert _choose(pack, "supervisor", first).status == "APPLIED"  # 다시 바꿀 수 있다

    # 안 번호는 고정이다: 앞 안이 거절되어도 남은 안의 번호가 당겨지지 않는다
    with db.read() as conn:
        v = list_validations(conn, pack.site_id, first)[-1]
    body = RejectRequest(
        candidate_id=first, validation_id=v.validation_id, reason_code="PREFERENCE"
    )
    assert reject_candidate(pack, "supervisor", _key(), body).status == "APPLIED"
    with db.read() as conn:
        state = build_state(conn, pack, "supervisor")
    views = {c["candidate_id"]: c for c in state["candidates"]}
    assert (views[first]["display_status"], views[first]["plan_no"]) == ("REJECTED", 1)
    assert views[second]["plan_no"] == 2


def test_choice_change_and_objection_count_as_human_work(seeded_real, main_on):
    """처음 고르기는 흐름의 일부라 세지 않는다. 고른 안을 바꾸면 1, 그 안에 담당자 이견이 나면 1이고,
    이견이 난 안을 떠나 다시 고르는 것은 그 이견에서 이미 셌다 (AG-30)."""
    pack = seeded_real
    _submit_a(pack)
    replies = [solve("L1"), solve_with("L2", cond("M", start_at=2940)), done()]
    run_until_idle(pack, model_factory=Router(replanning=replies, auto_done=False).factory())
    first, second = _candidate_ids()
    [main] = _runs("MAIN")

    def count():
        with db.read() as conn:
            return casefacts.human_work(conn, pack.site_id, main.case_id)

    choose(pack, first)
    run_until_idle(pack, model_factory=Router().factory())
    assert count() == 0
    choose(pack, second)
    run_until_idle(pack, model_factory=Router().factory())
    assert count() == 1

    with db.read() as conn:
        message_id, owner = conn.execute(
            "SELECT message_id, to_actor_id FROM message WHERE type = 'CHANGE_REQUEST'"
            " AND status = 'OPEN' AND candidate_id = ?",
            (second,),
        ).fetchone()
    body = ReplyRequest(message_id=message_id, decision="DECLINE", comment="그 시각은 안 됩니다")
    assert reply_message(pack, owner, _key(), body).status == "APPLIED"
    assert count() == 2
    choose(pack, first)
    assert count() == 2
