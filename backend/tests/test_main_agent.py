"""Main Agent: 도구 넷의 유효성, 하위 Run 시작·종료, 사실 지문 (AG-24·AG-27, ST-20)."""

from conftest import add_run
from scripted import (
    Router,
    blocked,
    main_call,
    main_close,
    main_escalate,
    main_wait,
)

from app.agents import casefacts, runtime
from app.coordinator.dispatcher import run_until_idle
from app.store import db
from app.store.repos.case_events import list_case_events
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
    assert [c["acting_unit_id"] for c in last["observation"]["calls"]] == ["UB"]
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
        main_call("REPLANNING", group_id=gid, acting_unit_id="UA"),
    ]
    _main(pack, replies)
    assert _guards("main") == [
        ("CALL_AGENT", "GROUP_NOT_FOUND"),
        ("CALL_AGENT", "UNIT_NOT_IN_GROUP"),
        ("CALL_AGENT", None),
    ]
    # 하위 Run이 막힌 채 끝난 뒤 아무것도 바뀌지 않았다: 같은 호출은 거절된다
    again = main_call("REPLANNING", group_id=gid, acting_unit_id="UA")
    router = Router(replanning=[blocked()], main=[again, main_escalate()])
    run_until_idle(pack, model_factory=router.factory())
    assert _guards("main")[3:] == [("CALL_AGENT", "SAME_FACTS"), ("ESCALATE", None)]
    assert len(_runs("REPLANNING")) == 1


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
