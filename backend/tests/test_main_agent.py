"""Main Agent: 도구 넷의 유효성, 하위 Run 시작·종료, 사실 지문 (AG-24, ST-20)."""

import uuid

from conftest import add_run, pin_tasks
from langchain_core.messages import AIMessage
from scripted import (
    Router,
    blocked,
    main_call,
    main_close,
    main_escalate,
    main_wait,
    solve,
)

from app.agents import casefacts, runtime
from app.agents.observers import main as main_observer
from app.agents.specs import main as main_spec
from app.agents.specs import replanning as replanning_spec
from app.api.state import run_summary
from app.commands.approval import RejectRequest, reject_candidate
from app.commands.events import EventReport, receive_event
from app.commands.messages import ReplyRequest, reply_message
from app.commands.pins import (
    PreferredWindow,
    TaskRef,
    pin_task,
    set_preferred_window,
    unpin_task,
)
from app.commands.runs import CancelRun, cancel_run
from app.commands.task_request import TaskRequestForm, submit_task_request
from app.coordinator.dispatcher import run_until_idle
from app.store import db
from app.store.repos.case_events import list_case_events, record_case_event
from app.store.repos.consultations import list_review_queue
from app.store.repos.records import list_validations
from app.store.repos.runs import get_run, list_steps


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
    pack = with_a
    call = main_call("REPLANNING")
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
    # 재계획 Run에는 주체 Unit도 대신 움직이는 Actor도 없다 (AG-24)
    assert (child.status, child.end_reason, child.acting_unit_id, child.acting_actor_id) == (
        "BLOCKED",
        "RETURN_BLOCKED",
        None,
        None,
    )
    assert child.input_ref["call_key"] == "REPLANNING:MIN_CHANGE"
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
    replanning = last["observation"]["replanning"]
    assert replanning["last_result"]["facts_changed"] is False
    # 엮인 충돌은 설명이다. 충돌에 걸린 작업 가운데 고정되지 않은 것(A)만 움직일 수 있다(B는 고정)
    assert last["observation"]["groups"][0]["task_ids"] == ["A", "B"]
    assert (replanning["movable_task_ids"], replanning["request_task_ids"]) == (["A"], ["A"])
    # 같은 접근의 재계획은 받지 않고 다른 접근은 부를 수 있다 (AG-24)
    assert replanning["last_result"]["paths"] == []
    assert {n["kind"] for n in replanning["last_result"]["openers"]} <= {"FACT_CHANGE"}
    assert last["observation"]["calls"] == [{"agent": "REPLANNING", "approach": "PREFER_WINDOW"}]
    with db.read() as conn:
        [notice] = conn.execute(
            "SELECT to_actor_id, type, agent_text FROM message WHERE run_id = 'main'"
        ).fetchall()
    assert tuple(notice) == ("supervisor", "NOTICE", "재계획이 막혔다")


def test_call_agent_takes_only_an_approach_and_refuses_same_facts(with_a):
    pack = with_a
    _main(pack, [main_call("REPLANNING", approach=None), main_call("REPLANNING")])
    assert _guards("main") == [("CALL_AGENT", "APPROACH_REQUIRED"), ("CALL_AGENT", None)]
    # 하위 Run이 막힌 채 끝난 뒤 아무것도 바뀌지 않았다: 같은 호출은 거절된다
    again = main_call("REPLANNING")
    router = Router(replanning=[blocked()], main=[again, main_escalate()])
    run_until_idle(pack, model_factory=router.factory())
    assert _guards("main")[2:] == [("CALL_AGENT", "SAME_FACTS"), ("ESCALATE", None)]
    assert len(_runs("REPLANNING")) == 1


def test_replanning_call_needs_a_movable_task(with_a):
    """충돌에 걸린 작업이 모두 고정되어 있으면 재계획으로 바뀌는 것이 없다: 호출 목록에 없고 거절된다."""
    pack = with_a
    pin_tasks(pack, ["A"])
    _main(pack, [main_call("REPLANNING"), main_escalate()])
    assert _guards("main") == [("CALL_AGENT", "NO_MOVABLE_TASK"), ("ESCALATE", None)]
    obs = _steps("main")[0]["observation"]
    assert obs["replanning"]["movable_task_ids"] == [] and obs["calls"] == []


def _reply(pack, message_id, actor, decision="ACCEPT"):
    body = ReplyRequest(message_id=message_id, decision=decision)
    return reply_message(pack, actor, uuid.uuid4().hex, body)


def test_only_one_child_and_wait_close_need_facts(with_a):
    pack = with_a
    call = main_call("REPLANNING")
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
    pack = with_a
    _main(pack, [main_call("REPLANNING")])
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
    _report(pack)
    replies = [main_call("REPLANNING"), main_wait()]
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
    call = main_call("REPLANNING")
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
    assert woke["replanning"]["last_result"]["facts_changed"] is True
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


def _human_work(pack, case_id):
    with db.read() as conn:
        return casefacts.human_work(conn, pack.site_id, case_id)


def test_main_budget_limits_grow_per_human_work_up_to_the_cap():
    """한도 = 기본값 + 사람이 만든 일 × 한 바퀴분, 상한까지 (AG-30)."""
    base = dict(main_spec.SPEC.budget)
    assert main_spec.budget_limits(0) == base
    one = main_spec.budget_limits(1)
    assert {k: one[k] - base[k] for k in base} == {
        "steps": main_spec.ROUND_STEPS,
        "llm_attempts": main_spec.ROUND_STEPS * 2,
        "agent_calls": main_spec.ROUND_AGENT_CALLS,
    }
    top = main_spec.budget_limits(main_spec.MAX_EXTRA_ROUNDS)
    assert top["steps"] == base["steps"] + main_spec.MAX_EXTRA_ROUNDS * main_spec.ROUND_STEPS
    assert main_spec.budget_limits(main_spec.MAX_EXTRA_ROUNDS + 5) == top
    # 그래프 반복 한도가 상한까지 간 step 수를 덮는다
    assert main_spec.RECURSION_LIMIT >= top["steps"] * 4


def test_human_work_extends_main_budget_and_agent_actions_do_not(seeded, main_on, monkeypatch):
    """Supervisor 거절·작업 고정·고정 해제마다 메인 한도가 한 바퀴분 늘고, 재계획 결과·가드 거절·
    재호출로는 늘지 않는다. 남은 Budget은 늘어난 한도로 보인다 (AG-30)."""
    pack = seeded
    base = dict(main_spec.SPEC.budget)
    assert _submit(pack, "N1").status == "APPLIED"
    call = main_call("REPLANNING")
    router = Router(replanning=[solve("L0")], main=[call, call, main_wait()])
    run_until_idle(pack, model_factory=router.factory())
    [main] = _runs("MAIN")
    # Agent의 행동(호출, 재계획이 낸 후보, 같은 호출의 가드 거절)은 사람이 만든 일이 아니다
    assert _guards(main.run_id)[1] == ("CALL_AGENT", "SAME_FACTS")
    assert _human_work(pack, main.case_id) == 0
    waiting = _steps(main.run_id)[-1]
    assert waiting["observation"]["budget_remaining"]["steps"] == base["steps"] - 2
    assert waiting["budget_remaining"]["agent_calls"] == base["agent_calls"] - 1
    # 기본 한도를 이미 쓴 만큼으로 줄이면 이 메인은 소진이다
    monkeypatch.setitem(main_spec.SPEC.budget, "steps", 3)
    with db.read() as conn:
        assert main_observer.build_observation(conn, pack, main.run_id).budget_exhausted

    assert _reject(pack, _review_candidate(pack)).status == "APPLIED"
    assert _human_work(pack, main.case_id) == 1
    assert pin_task(pack, "planner_a", _key(), TaskRef(task_id="N1")).status == "APPLIED"
    assert unpin_task(pack, "planner_a", _key(), TaskRef(task_id="N1")).status == "APPLIED"
    assert _human_work(pack, main.case_id) == 3

    # 사람이 만든 일 셋만큼 늘어난 한도로 이어 간다
    run_until_idle(pack, model_factory=Router(main=[main_escalate()]).factory())
    [main] = _runs("MAIN")
    assert (main.status, main.steps_used) == ("ESCALATED", 4)
    last = _steps(main.run_id)[-1]
    assert last["observation"]["budget_remaining"] == {
        "steps": 3 + 3 * main_spec.ROUND_STEPS - 3,
        "llm_attempts": base["llm_attempts"] + 3 * main_spec.ROUND_STEPS * 2 - 3,
        "agent_calls": base["agent_calls"] + 3 * main_spec.ROUND_AGENT_CALLS - 1,
    }
    assert last["budget_remaining"]["steps"] == 3 + 3 * main_spec.ROUND_STEPS - 4
    with db.read() as conn:
        assert run_summary(conn, main.run_id)["budget_max"]["steps"] == (
            3 + 3 * main_spec.ROUND_STEPS
        )


def test_exhausted_replanning_result_has_its_live_candidates(seeded, main_on, monkeypatch):
    """결과를 돌려주지 못하고 Budget 소진으로 끝난 재계획: 서버가 만든 결과에 그 Run이 만든 살아 있는
    후보가 들어간다 (ST-20)."""
    pack = seeded
    monkeypatch.setitem(replanning_spec.SPEC.budget, "steps", 2)
    assert _submit(pack).status == "APPLIED"
    router = Router(replanning=[solve("L0"), solve("L1")], auto_done=False)
    run_until_idle(pack, model_factory=router.factory())
    [child] = _runs("REPLANNING")
    assert (child.status, child.end_reason) == ("BUDGET_EXHAUSTED", "BUDGET_EXHAUSTED")
    candidate_id = _review_candidate(pack)
    [main] = _runs("MAIN")
    [result] = _steps(main.run_id)[-1]["observation"]["child_results"]
    assert result["result"] == {
        "by": "SERVER",
        "status": "BLOCKED",
        "paths": [],
        "candidate_ids": [candidate_id],
    }
    # 후보가 무효가 되면 빠진다
    assert _reject(pack, candidate_id).status == "APPLIED"
    with db.read() as conn:
        assert casefacts.run_result(conn, child)["candidate_ids"] == []


def test_main_sees_tasks_with_a_preferred_window(with_a):
    """메인 관찰: 충돌에 걸린 작업 가운데 담당자가 희망 영역을 그려 둔 작업이 보인다(서버 규칙은 없다)."""
    pack = with_a
    _main(pack, [main_escalate()])
    assert _steps("main")[0]["observation"]["replanning"]["preferred_task_ids"] == []

    window = PreferredWindow(task_id="A", start=60, end=120)
    assert set_preferred_window(pack, "planner_a", _key(), window).status == "APPLIED"
    _main(pack, [main_escalate()], run_id="main2")
    step = _steps("main2")[0]
    assert step["observation"]["replanning"]["preferred_task_ids"] == ["A"]
    # 희망 영역이 없어도 희망 영역 우선 접근은 그대로 고를 수 있다
    calls = [c for c in step["observation"]["calls"] if c["agent"] == "REPLANNING"]
    assert "PREFER_WINDOW" in {c["approach"] for c in calls}


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
    assert result["result"] == {
        "by": "SERVER",
        "status": "BLOCKED",
        "paths": [],
        "candidate_ids": [],
    }

    assert _submit(pack, "N1").result_refs["queued"] is False  # 앞 메인은 이관으로 끝났다
    run_until_idle(pack, model_factory=Router(main=[bad, bad]).factory())
    main = _runs("MAIN")[-1]
    assert (main.status, main.end_reason) == ("ESCALATED", "MALFORMED_TWICE")
    with db.read() as conn:
        [notice] = conn.execute(
            "SELECT to_actor_id FROM message WHERE run_id = ?", (main.run_id,)
        ).fetchall()
    assert notice[0] == "supervisor"
