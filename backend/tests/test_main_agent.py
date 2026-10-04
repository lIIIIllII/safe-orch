"""Main Agent: 도구 넷의 유효성, 하위 Run 시작·종료, 사실 지문 (AG-24, ST-20)."""

import uuid

from conftest import add_run
from langchain_core.messages import AIMessage
from scripted import (
    Router,
    blocked,
    call,
    main_call,
    main_close,
    main_escalate,
    main_wait,
    solve,
)

from app.agents import casefacts, runtime
from app.agents.specs import main as main_spec
from app.commands.approval import RejectRequest, reject_candidate
from app.commands.events import EventReport, receive_event
from app.commands.messages import ReplyRequest, reply_message
from app.commands.runs import CancelRun, cancel_run
from app.commands.task_request import TaskRequestForm, submit_task_request
from app.coordinator.dispatcher import run_until_idle
from app.store import db
from app.store.repos.case_events import list_case_events, record_case_event
from app.store.repos.consultations import list_review_queue
from app.store.repos.records import list_validations
from app.store.repos.runs import get_run, list_steps


def _group(pack):
    with db.read() as conn:
        _, _, groups = casefacts.current_groups(conn, pack)
    return groups[0]


def _main(pack, replies, run_id="main"):
    """손으로 만든 메인 Run을 스크립트로 한 번 부른다(대기·종료까지)."""
    with db.read() as conn:
        exists = get_run(conn, run_id) is not None
    if not exists:
        add_run(pack, run_id, agent_type="MAIN", acting_unit_id="SITE", acting_actor_id=None)
    return runtime.invoke(pack, {"run_id": run_id}, Router(main=replies).factory()())


def _steps(run_id):
    with db.read() as conn:
        return list_steps(conn, run_id)


def _runs(agent_type=None):
    with db.read() as conn:
        ids = [r[0] for r in conn.execute("SELECT run_id FROM agent_run ORDER BY rowid")]
        runs = [get_run(conn, rid) for rid in ids]
    return [r for r in runs if agent_type is None or r.agent_type == agent_type]


def _guards(run_id):
    return [
        (s["action"]["name"], s["guard"]["reason_code"])
        for s in _steps(run_id)
        if s["status"] == "COMPLETED"
    ]


def test_call_agent_starts_child_and_result_wakes_main(with_a):
    """CALL_AGENT → 하위 Run이 메인의 Case에서 돈다 → 막힌 결과 → 메인이 깨어나 이관한다."""
    pack, group = with_a, _group(with_a)
    call = main_call("REPLANNING", group_id=group.group_id, acting_unit_id="UA")
    main = _main(pack, [call])
    assert (main.status, main.wait_kind, main.agent_calls_used) == ("WAITING_HUMAN", "CHILD_RUN", 1)
    router = Router(replanning=[blocked("해 없음")], main=[main_escalate("재계획이 막혔다")])
    run_until_idle(pack, model_factory=router.factory())

    [child] = _runs("REPLANNING")
    assert (child.run_id, child.parent_run_id, child.case_id) == (
        main.wait_ref,
        "main",
        main.case_id,
    )
    assert (child.status, child.end_reason, child.acting_unit_id) == (
        "BLOCKED",
        "RETURN_BLOCKED",
        "UA",
    )
    assert child.input_ref["group_id"] == group.group_id
    [main] = _runs("MAIN")
    assert (main.status, main.end_reason) == ("ESCALATED", "ESCALATE")
    last = _steps("main")[-1]
    # 깨어난 관찰에 하위 Run 결과가 보이고, 같은 사실에서 같은 호출은 더 받지 않는다
    [result] = last["observation"]["child_results"]
    assert (result["run_status"], result["result"]["status"], result["result"]["by"]) == (
        "BLOCKED",
        "BLOCKED",
        "AGENT",
    )
    units = {u["unit_id"]: u for u in last["observation"]["groups"][0]["units"]}
    assert units["UA"]["last_result"]["facts_changed"] is False
    # UB는 그룹에 작업이 있지만 움직일 수 있는 작업이 없어 호출 목록에 없다
    assert (units["UB"]["task_ids"], units["UB"]["movable_task_ids"]) == (["B"], [])
    # 막힌 결과에 서버가 붙인 담당자 확인은 사전 확인으로 부를 수 있다. 재계획 호출은 없다
    openers = units["UA"]["last_result"]["openers"]
    asks = [n["need_id"] for n in openers if n["kind"] == "OWNER_CONSENT"]
    assert (units["UA"]["last_result"]["paths"], [n["task_id"] for n in openers[:2]]) == (
        [],
        ["A", "C"],
    )
    assert last["observation"]["calls"] == [
        {"agent": "COORDINATION", "phase": "ASK", "need_ids": asks}
    ]
    with db.read() as conn:
        [notice] = conn.execute(
            "SELECT to_actor_id, type, agent_text FROM message WHERE run_id = 'main'"
        ).fetchall()
    assert tuple(notice) == ("supervisor", "NOTICE", "재계획이 막혔다")


def test_call_agent_checks_group_unit_and_same_facts(with_a):
    pack, group = with_a, _group(with_a)
    gid = group.group_id
    assert "SITE" not in group.units  # 메인 자신의 Unit은 이 그룹에 작업이 없다
    replies = [
        main_call("REPLANNING", group_id="grp_none", acting_unit_id="UA"),
        main_call("REPLANNING", group_id=gid, acting_unit_id="SITE"),
        main_call("REPLANNING", group_id=gid, acting_unit_id="UB"),
        main_call("REPLANNING", group_id=gid, acting_unit_id="UA"),
    ]
    _main(pack, replies)
    assert _guards("main") == [
        ("CALL_AGENT", "GROUP_NOT_FOUND"),
        ("CALL_AGENT", "UNIT_NOT_IN_GROUP"),
        ("CALL_AGENT", "UNIT_HAS_NO_MOVABLE_TASK"),
        ("CALL_AGENT", None),
    ]
    # 하위 Run이 막힌 채 끝난 뒤 아무것도 바뀌지 않았다: 같은 호출은 거절된다
    again = main_call("REPLANNING", group_id=gid, acting_unit_id="UA")
    router = Router(replanning=[blocked()], main=[again, main_escalate()])
    run_until_idle(pack, model_factory=router.factory())
    assert _guards("main")[4:] == [("CALL_AGENT", "SAME_FACTS"), ("ESCALATE", None)]
    assert len(_runs("REPLANNING")) == 1


def _reply(pack, message_id, actor, decision="ACCEPT"):
    body = ReplyRequest(message_id=message_id, decision=decision)
    return reply_message(pack, actor, uuid.uuid4().hex, body)


def test_ask_call_takes_valid_owner_consent_need_ids(with_a):
    """사전 확인은 need ID로 부른다. 받는 것은 지금 물을 수 있는 자원 축 담당자 확인뿐이다. 재계획 Agent가
    엮은 길에 그런 need가 없어도 서버가 붙인 열 수 있는 것의 ID로 부를 수 있고, 기록에 출처가 남는다.
    수락 → 작업 새 revision + Consent → 다시 부른 재계획이 대체 자원으로 탐색한다 (AG-09·AG-23)."""
    pack, gid = with_a, _group(with_a).group_id
    replan = main_call("REPLANNING", group_id=gid, acting_unit_id="UA")
    child = _main(pack, [replan]).wait_ref  # 하위 Run ID = need ID의 앞부분
    time_need = {"kind": "OWNER_CONSENT", "task_id": "A", "axis": "TIME"}
    crane = {"kind": "OWNER_CONSENT", "task_id": "A", "axis": "RESOURCE", "values": ["SITE-CR-01"]}

    def ask(*need_ids):
        return main_call("COORDINATION", phase="ASK", need_ids=list(need_ids))

    a, c, fact = (f"{child}:s:{i}" for i in range(3))
    woven = f"{child}:p1:0"  # 재계획 Agent가 엮은 길의 A 자원 확인(서버 need a와 같은 확인)
    paths = [{"needs": [time_need]}, {"needs": [crane]}]
    router = Router(
        replanning=[blocked("A의 시간을 넓히거나 대체 자원을 쓴다", paths)],
        main=[
            ask(f"{child}:p0:0"),  # 시간 축: 유효한 need지만 사전 확인 대상이 아니다
            ask("run_none:s:0"),
            ask(fact),
            ask(),
            main_call("COORDINATION", phase="ASK", candidate_id="cand_x", need_ids=[a]),
            ask(woven, a, c),  # 여러 need를 한 번에
        ],
    )
    run_until_idle(pack, model_factory=router.factory())
    assert _guards("main")[1:] == [
        ("CALL_AGENT", "TIME_AXIS_NOT_ASKABLE"),
        ("CALL_AGENT", "NEED_NOT_FOUND"),
        ("CALL_AGENT", "NOT_OWNER_CONSENT"),
        ("CALL_AGENT", "ACTION_NOT_AVAILABLE"),
        ("CALL_AGENT", "ACTION_NOT_AVAILABLE"),
        ("CALL_AGENT", None),
    ]
    woke = _steps("main")[1]
    last = next(u for u in woke["observation"]["groups"][0]["units"] if u["unit_id"] == "UA")[
        "last_result"
    ]
    # 출처가 갈린다: 모델이 엮은 길(p)과 서버가 붙인 열 수 있는 것(s)
    assert [n["need_id"] for p in last["paths"] for n in p["needs"]] == [f"{child}:p0:0", woven]
    assert [(n["need_id"], n["kind"]) for n in last["openers"]] == [
        (a, "OWNER_CONSENT"),
        (c, "OWNER_CONSENT"),
        (fact, "FACT_CHANGE"),
    ]
    assert {"agent": "COORDINATION", "phase": "ASK", "need_ids": [woven, a, c]} in woke[
        "observation"
    ]["calls"]
    schema = next(t["function"] for t in woke["available_actions"] if t["function"]["name"])
    assert schema["parameters"]["properties"]["need_ids"]["items"]["enum"] == [woven, a, c]

    # 사전 확인 Run: need를 담당자별로 정렬해 받고, need마다 질문 하나를 보낸 뒤 기다린다
    [asking] = _runs("COORDINATION")
    assert (asking.parent_run_id, asking.status, asking.input_ref["need_ids"]) == (
        "main",
        "WAITING_HUMAN",
        [woven, a, c],
    )
    # 같은 확인(A의 SITE-CR-01)은 한 번만 묻는다: Agent가 엮은 길의 need가 남는다
    asks = _steps(asking.run_id)[0]["observation"]["asks"]
    assert [(x["owner_actor_id"], x["task_id"], x["need_id"]) for x in asks] == [
        ("foreman_a2", "C", c),
        ("planner_a", "A", woven),
    ]
    with db.read() as conn:
        sent = conn.execute(
            "SELECT message_id, to_actor_id FROM message WHERE run_id = ? ORDER BY rowid",
            (asking.run_id,),
        ).fetchall()
    assert [m[1] for m in sent] == ["foreman_a2", "planner_a"]
    for message_id, actor in sent:
        assert _reply(pack, message_id, actor).status == "APPLIED"
    with db.read() as conn:
        revisions = conn.execute(
            "SELECT task_id, MAX(revision) FROM task WHERE task_id IN ('A', 'C') GROUP BY task_id"
        ).fetchall()
        consents = conn.execute(
            "SELECT task_id, scope FROM consent WHERE source_ref LIKE 'message:%' ORDER BY task_id"
        ).fetchall()
    assert [tuple(r) for r in revisions] == [("A", 2), ("C", 2)]
    assert [(r[0], "SITE-CR-01" in r[1]) for r in consents] == [("A", True), ("C", True)]

    # 답을 받은 사전 확인이 끝나면 메인이 재계획을 다시 부를 수 있고(사실이 바뀌었다), 대체 자원으로 탐색한다
    try_a = call("TRY_ALTERNATIVE_RESOURCE", "대체 자원", task_id="A", resource_id="SITE-CR-01")
    router = Router(replanning=[try_a], main=[ask(a), replan, main_wait()])
    assert [n["need_id"] for n in asking.input_ref["needs"]] == [woven, c]
    run_until_idle(pack, model_factory=router.factory())
    done = _steps(asking.run_id)[-1]["tool_result"]
    assert (done["status"], [(x["task_id"], x["result"]) for x in done["asks"]]) == (
        "DONE",
        [("C", "ACCEPTED"), ("A", "ACCEPTED")],
    )
    assert _guards("main")[7:] == [
        ("CALL_AGENT", "ALREADY_CONSENTED"),  # 이미 연 것은 다시 묻지 않는다
        ("CALL_AGENT", None),
        ("WAIT", None),
    ]
    second = _runs("REPLANNING")[-1]
    s_try = _steps(second.run_id)[0]
    assert (second.status, s_try["tool_result"]["try_resources"]) == (
        "SUCCEEDED",
        {"A": ["SITE-CR-01"]},
    )
    assert s_try["tool_result"]["candidate_id"] is not None


def test_only_one_child_and_wait_close_need_facts(with_a):
    pack, group = with_a, _group(with_a)
    call = main_call("REPLANNING", group_id=group.group_id, acting_unit_id="UA")
    # 기다릴 것(검토 대기 후보·Hold)이 없으면 WAIT는 거절된다
    _main(pack, [main_wait(), call])
    assert _guards("main") == [("WAIT", "NOTHING_TO_WAIT_FOR"), ("CALL_AGENT", None)]
    # 하위 Run이 아직 열려 있다(시작 대기 포함): 더 부르지도 끝내지도 못한다
    with db.write() as tx:
        tx.execute(
            "UPDATE agent_run SET status = 'RUNNING', wait_kind = NULL, wait_ref = NULL"
            " WHERE run_id = 'main'"
        )
    # 스크립트가 끝나면 Run은 오류로 멈춘다(여기서는 거절 사유만 본다)
    _main(pack, [call, main_escalate(), main_close()])
    assert _guards("main")[2:] == [
        ("CALL_AGENT", "CHILD_RUN_OPEN"),
        ("ESCALATE", "CHILD_RUN_OPEN"),
        ("CLOSE", "CHILD_RUN_OPEN"),
    ]


def test_child_start_refusal_is_reported_to_main(with_a):
    """부른 뒤 사실이 바뀌어 시작 조건이 맞지 않으면 Run을 만들지 않고 사건으로 알린다."""
    pack, group = with_a, _group(with_a)
    _main(pack, [main_call("REPLANNING", group_id=group.group_id, acting_unit_id="UA")])
    with db.write() as tx:
        tx.execute("UPDATE site SET context_version = context_version + 1")
    run_until_idle(pack, model_factory=Router(main=[main_escalate()]).factory())
    assert _runs("REPLANNING") == []
    with db.read() as conn:
        [event] = list_case_events(conn, pack.site_id)
    assert (event["kind"], event["ref"]["status"], event["ref"]["reason"]) == (
        "CHILD_RUN_ENDED",
        "NOT_STARTED",
        "START_NOT_ALLOWED",
    )
    [result] = _steps("main")[-1]["observation"]["child_results"]
    assert (result["run_status"], result["result"]) == ("NOT_STARTED", None)
    assert _runs("MAIN")[0].status == "ESCALATED"


# ── 사건 → 메인 (자동 시작) ────────────────────────────────────


def _key():
    return uuid.uuid4().hex


def _submit(pack, task_id="A"):
    if task_id == "A":
        data = pack.new_task.model_dump(
            exclude={"requested", "unit_id", "owner_actor_id", "movable"}
        )
        actor = "planner_a"
    else:
        d = next(x for x in pack.demo_requests if x.task_id == task_id)
        data, actor = d.model_dump(exclude={"label", "requester"}), d.requester
    return submit_task_request(pack, actor, _key(), TaskRequestForm(**data))


def _events(pack, case_id=None):
    with db.read() as conn:
        return [e for e in list_case_events(conn, pack.site_id) if case_id in (None, e["case_id"])]


def _review_candidate(pack):
    with db.read() as conn:
        return list_review_queue(conn, pack.site_id)[-1]


def _reject(pack, candidate_id, reason="PREFERENCE"):
    with db.read() as conn:
        [v] = list_validations(conn, pack.site_id, candidate_id)
    body = RejectRequest(
        candidate_id=candidate_id, validation_id=v.validation_id, reason_code=reason
    )
    return reject_candidate(pack, "supervisor", _key(), body)


def _report(pack, event_type="OTHER"):
    body = EventReport(source_event_id=_key(), event_type=event_type, text="확인 필요")
    out = receive_event(pack, "reporter", _key(), body)
    assert out.status == "APPLIED"
    return out.result_refs


def test_events_share_the_one_open_main(seeded, main_on):
    """열린 메인이 없을 때 사건이 생기면 메인이 뜨고, 뒤이은 사건은 그 메인의 Case에 들어간다."""
    pack = seeded
    assert _submit(pack).status == "APPLIED"
    _report(pack)
    [main] = _runs("MAIN")
    assert (main.status, main.acting_actor_id, main.input_ref["trigger"]) == (
        "RUNNING",
        None,
        "TASK_READY:A:1",
    )
    assert [(e["kind"], e["case_id"]) for e in _events(pack)] == [
        ("TASK_READY", main.case_id),
        ("EVENT_REPORTED", main.case_id),
    ]
    # 메인이 열려 있는 동안 새 작업은 대기열에 선다(메인을 더 띄우지 않는다)
    assert _submit(pack, "N1").result_refs["queued"] is True
    assert len(_runs("MAIN")) == 1


def test_replanning_call_is_refused_while_hold_is_active(seeded, main_on):
    pack = seeded
    assert _submit(pack).status == "APPLIED"
    group = _group(pack)
    _report(pack)
    replies = [main_call("REPLANNING", group_id=group.group_id, acting_unit_id="UA"), main_wait()]
    run_until_idle(pack, model_factory=Router(main=replies).factory())
    [main] = _runs("MAIN")
    assert _guards(main.run_id) == [("CALL_AGENT", "HOLD_ACTIVE"), ("WAIT", None)]
    first = _steps(main.run_id)[0]["observation"]
    assert first["calls"] == [] and len(first["holds"]) == 1
    assert (main.wait_kind, _runs("REPLANNING")) == ("HUMAN_DECISION", [])


def test_recall_is_allowed_after_plain_rejection_and_refused_when_nothing_changed(seeded, main_on):
    """사실 지문: 제약 없는 거절은 현장 버전을 올리지 않지만 같은 호출을 다시 받게 한다 (AG-24)."""
    pack = seeded
    assert _submit(pack, "N1").status == "APPLIED"
    group = _group(pack)
    call = main_call("REPLANNING", group_id=group.group_id, acting_unit_id="UA")
    # 후보가 나온 뒤 아무것도 바뀌지 않았다: 같은 호출은 거절된다
    router = Router(replanning=[solve("L0")], main=[call, call, main_wait()])
    run_until_idle(pack, model_factory=router.factory())
    [main] = _runs("MAIN")
    assert _guards(main.run_id) == [
        ("CALL_AGENT", None),
        ("CALL_AGENT", "SAME_FACTS"),
        ("WAIT", None),
    ]
    with db.read() as conn:
        ctx = conn.execute("SELECT context_version FROM site").fetchone()[0]
    assert _reject(pack, _review_candidate(pack)).status == "APPLIED"
    with db.read() as conn:
        assert conn.execute("SELECT context_version FROM site").fetchone()[0] == ctx
    router = Router(replanning=[blocked()], main=[call, main_escalate()])
    run_until_idle(pack, model_factory=router.factory())
    assert _guards(main.run_id)[3:] == [("CALL_AGENT", None), ("ESCALATE", None)]
    woke = _steps(main.run_id)[3]["observation"]
    assert woke["rejections"]["count"] == 1
    units = {u["unit_id"]: u for u in woke["groups"][0]["units"]}
    assert units["UA"]["last_result"]["facts_changed"] is True
    assert len(_runs("REPLANNING")) == 2


def test_close_and_escalate_need_no_open_work_and_no_unseen_event(seeded, main_on):
    pack = seeded
    assert _submit(pack).status == "APPLIED"

    def with_new_event(reply):
        """모델이 답을 고르는 사이에 이 Case에 새 사건이 생긴다."""

        def make():
            with db.write() as tx:
                [case_id] = tx.execute("SELECT case_id FROM agent_run").fetchone()
                record_case_event(tx, pack.site_id, "HOLD_RELEASED", _key(), {}, case_id)
            return reply

        return make

    replies = [
        main_close(),  # 계획에 들어가지 못한 작업이 이 Case의 열린 일이다
        with_new_event(main_close()),
        with_new_event(main_escalate()),
        main_escalate(),
    ]
    run_until_idle(pack, model_factory=Router(main=replies).factory())
    [main] = _runs("MAIN")
    assert _guards(main.run_id) == [
        ("CLOSE", "OPEN_WORK"),
        ("CLOSE", "NEW_EVENT"),
        ("ESCALATE", "NEW_EVENT"),
        ("ESCALATE", None),
    ]
    first = _steps(main.run_id)[0]
    assert first["observation"]["open_work"] == [
        {"kind": "TASK_UNPLANNED", "task_id": "A", "placed_by": []}
    ]
    assert "CLOSE" not in [t["function"]["name"] for t in first["available_actions"]]
    assert main.last_event_seq == 3


def test_main_budget_exhaustion_notifies_supervisor_and_promotes_queue(
    seeded, main_on, monkeypatch
):
    """Budget 소진으로 끝난 메인의 남은 일은 넘기지 않는다. Supervisor에게 알리고 대기열 1건을 올린다."""
    pack = seeded
    monkeypatch.setitem(main_spec.SPEC.budget, "steps", 1)
    assert _submit(pack).status == "APPLIED"
    assert _submit(pack, "N1").result_refs["queued"] is True
    # 첫 메인: 재계획을 부르고(1 step) 막힌 결과로 깨어나면 Budget이 없다
    run_until_idle(pack, model_factory=Router(replanning=[blocked(), blocked()]).factory())
    first, second = _runs("MAIN")
    assert (first.status, first.end_reason) == ("BUDGET_EXHAUSTED", "BUDGET_EXHAUSTED")
    with db.read() as conn:
        notices = conn.execute(
            "SELECT run_id, to_actor_id, body FROM message WHERE type = 'NOTICE' ORDER BY rowid"
        ).fetchall()
        task = conn.execute(
            "SELECT lifecycle FROM task WHERE task_id = 'N1' ORDER BY revision DESC"
        ).fetchone()[0]
    assert (notices[0][0], notices[0][1]) == (first.run_id, "supervisor")
    assert "BUDGET_EXHAUSTED" in notices[0][2] and "넘겨지지 않습니다" in notices[0][2]
    # 대기열의 N1이 올라가 새 메인이 받는다. 첫 메인의 작업(A)은 새 메인의 사건이 아니다
    assert task == "READY" and second.case_id != first.case_id
    ready = [e for e in _events(pack, second.case_id) if e["kind"] == "TASK_READY"]
    assert [e["ref"]["task_id"] for e in ready] == ["N1"]


def test_decision_for_closed_case_goes_to_a_new_main(seeded, main_on):
    """메인이 끝난 뒤 온 후보 결과는 새 메인이 받는다. 사건은 새 메인의 Case에 들어가 안 본 사건이 된다."""
    pack = seeded
    assert _submit(pack, "N1").status == "APPLIED"
    run_until_idle(pack, model_factory=Router(replanning=[solve("L0")]).factory())
    [old] = _runs("MAIN")
    candidate = _review_candidate(pack)
    out = cancel_run(pack, "supervisor", _key(), CancelRun(run_id=old.run_id))
    assert out.status == "APPLIED"

    assert _reject(pack, candidate).status == "APPLIED"
    old, new = _runs("MAIN")
    assert (old.status, new.status) == ("CANCELLED", "RUNNING")
    [decided] = [e for e in _events(pack) if e["kind"] == "CANDIDATE_DECIDED"]
    assert (decided["case_id"], decided["ref"]["origin_case_id"]) == (new.case_id, old.case_id)

    run_until_idle(pack, model_factory=Router(main=[main_escalate()]).factory())
    seen = _steps(new.run_id)[0]["observation"]
    assert [(e["kind"], e["new"]) for e in seen["events"]] == [("CANDIDATE_DECIDED", True)]
    [shown] = [c for c in seen["candidates"] if c["candidate_id"] == candidate]
    assert (shown["live"], shown["decision"]["type"], shown["decision"]["reason_code"]) == (
        False,
        "REJECT",
        "PREFERENCE",
    )
    assert _runs("MAIN")[-1].last_event_seq == decided["seq"]


def test_malformed_twice_ends_main_escalated_and_specialist_blocked(seeded, main_on):
    """형식 오류 2회: 전문 Agent는 막힘(BLOCKED)으로, 메인만 이관 상태로 끝난다 (AG-06)."""
    pack = seeded
    assert _submit(pack).status == "APPLIED"
    bad = AIMessage(content="도구를 부르지 않는다")
    run_until_idle(pack, model_factory=Router(replanning=[bad, bad]).factory())
    [child] = _runs("REPLANNING")
    assert (child.status, child.end_reason) == ("BLOCKED", "MALFORMED_TWICE")
    [result] = _steps(_runs("MAIN")[0].run_id)[-1]["observation"]["child_results"]
    assert result["result"] == {"by": "SERVER", "status": "BLOCKED", "paths": []}

    assert _submit(pack, "N1").result_refs["queued"] is False  # 앞 메인은 이관으로 끝났다
    run_until_idle(pack, model_factory=Router(main=[bad, bad]).factory())
    main = _runs("MAIN")[-1]
    assert (main.status, main.end_reason) == ("ESCALATED", "MALFORMED_TWICE")
    with db.read() as conn:
        [notice] = conn.execute(
            "SELECT to_actor_id FROM message WHERE run_id = ?", (main.run_id,)
        ).fetchall()
    assert notice[0] == "supervisor"
