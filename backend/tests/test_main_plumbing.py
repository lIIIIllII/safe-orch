"""2단계 배관: 사건 기록(case_event), 종료 트랜잭션, 기동 복구, 메인 Run 행 (ST-18·ST-19)."""

import sqlite3
import uuid

import pytest
from conftest import add_run
from scripted import Router, ScriptedChatModel, escalate, solve

from app.agents import runtime
from app.agents.observers import main as main_observer
from app.agents.observers import replanning as replanning_observer
from app.commands.approval import (
    ApproveRequest,
    RejectRequest,
    approve_and_commit,
    reject_candidate,
)
from app.commands.events import EventReport, HoldRelease, receive_event, release_hold_command
from app.commands.task_request import (
    TaskRequestForm,
    TaskWithdraw,
    submit_task_request,
    withdraw_task_request,
)
from app.coordinator.dispatcher import recover_on_startup, run_until_idle
from app.store import db
from app.store.repos.case_events import list_case_events, record_case_event
from app.store.repos.cases import end_case_run, supervisor_actor
from app.store.repos.dispatch import list_jobs
from app.store.repos.records import list_validations
from app.store.repos.runs import get_run, list_steps
from app.store.repos.site import get_site


class Crash(BaseException):
    """프로세스가 죽은 것을 흉내 낸다. 어떤 except Exception에도 잡히지 않는다."""


def _key():
    return uuid.uuid4().hex


def _form(pack):
    data = pack.new_task.model_dump(exclude={"requested", "unit_id", "owner_actor_id", "movable"})
    return TaskRequestForm(**data)


def _submit_a(pack, key=None):
    out = submit_task_request(pack, "planner_a", key or _key(), _form(pack))
    assert out.status == "APPLIED"


def _events(pack, kind=None):
    with db.read() as conn:
        return [e for e in list_case_events(conn, pack.site_id) if kind in (None, e["kind"])]


def _runs():
    with db.read() as conn:
        ids = [r[0] for r in conn.execute("SELECT run_id FROM agent_run ORDER BY rowid")]
        return [get_run(conn, rid) for rid in ids]


def _steps(run_id):
    with db.read() as conn:
        return list_steps(conn, run_id)


def _factory(*replies):
    return lambda: ScriptedChatModel(list(replies))


# ── 사건 기록 ──────────────────────────────────────────────────


def test_record_case_event_dedupes_by_key(seeded):
    sid = seeded.site_id
    with db.write() as tx:
        assert record_case_event(tx, sid, "TASK_READY", "k1", {"task_id": "A"}) is True
        assert record_case_event(tx, sid, "TASK_READY", "k1", {"task_id": "A"}) is False
    [event] = _events(seeded)
    assert (event["kind"], event["ref"], event["created_context_version"]) == (
        "TASK_READY",
        {"task_id": "A"},
        0,
    )
    assert event["case_id"].startswith("case_")


def test_case_event_is_immutable(seeded):
    with db.write() as tx:
        record_case_event(tx, seeded.site_id, "TASK_READY", "k1", {})
    for sql in ("UPDATE case_event SET case_id = 'x'", "DELETE FROM case_event"):
        with pytest.raises(sqlite3.IntegrityError, match="immutable: case_event"), db.write() as tx:
            tx.execute(sql)


def test_commands_record_each_event_once(seeded):
    """후보 결과·신고·Hold 해제를 생긴 트랜잭션에서 한 번씩 적는다. 재전송은 다시 적지 않는다.

    메인 자동 시작이 꺼져 있으면 사건은 기록만 된다(후보를 만든 Run의 Case, 신고는 새 Case).
    """
    pack = seeded
    key = _key()
    _submit_a(pack, key)
    # 같은 멱등 키 재전송
    assert submit_task_request(pack, "planner_a", key, _form(pack)).status == "REPLAYED"
    [ready] = _events(pack)
    assert (ready["kind"], ready["ref"]["task_id"]) == ("TASK_READY", "A")
    assert _runs() == []  # 메인이 뜨지 않는다

    add_run(pack, "r", case_id="case_r")
    runtime.invoke(pack, {"run_id": "r"}, ScriptedChatModel([solve("L0"), solve("L1")]))
    run_until_idle(pack)  # 검증·협의 항목(서버 계산)만
    [run] = _runs()
    alpha = run.wait_ref
    with db.read() as conn:
        [validation] = list_validations(conn, pack.site_id, alpha)
    x = pack.demo_rejections[0]
    body = RejectRequest(
        candidate_id=alpha,
        validation_id=validation.validation_id,
        reason_code=x.reason_code,
        target_task_ids=x.target_task_ids,
        comment=x.comment,
    )
    key = _key()
    assert reject_candidate(pack, "supervisor", key, body).status == "APPLIED"
    reject_candidate(pack, "supervisor", key, body)
    [decided] = _events(pack, "CANDIDATE_DECIDED")
    assert (decided["case_id"], decided["ref"]["candidate_id"], decided["ref"]["type"]) == (
        "case_r",
        alpha,
        "REJECT",
    )

    report = EventReport(source_event_id="rep-1", event_type="DELAY", text="도장 준비 지연")
    out = receive_event(pack, "reporter", _key(), report)
    receive_event(pack, "reporter", _key(), report)  # 같은 source_event_id 재전송
    [reported] = _events(pack, "EVENT_REPORTED")
    assert _runs()[0].status == "STALE"  # 열린 전문 Agent Run은 같은 tx에서 무효가 된다

    with db.read() as conn:
        ctx = get_site(conn, pack.site_id).context_version
    release = HoldRelease(
        hold_id=out.result_refs["hold_id"], resolution="NO_CHANGE", expected_context_version=ctx
    )
    key = _key()
    assert release_hold_command(pack, "supervisor", key, release).status == "APPLIED"
    release_hold_command(pack, "supervisor", key, release)
    [released] = _events(pack, "HOLD_RELEASED")
    assert released["case_id"] == reported["case_id"]
    assert [e["kind"] for e in _events(pack)] == [
        "TASK_READY",
        "CANDIDATE_DECIDED",
        "EVENT_REPORTED",
        "HOLD_RELEASED",
    ]


def test_withdraw_and_queue_promotion_record_events(seeded, main_on):
    pack = seeded
    _submit_a(pack)
    run_until_idle(pack, model_factory=Router(replanning=[solve("L0"), solve("L1")]).factory())
    second = _form(pack).model_copy(update={"task_id": "A2"})
    out = submit_task_request(pack, "planner_a", _key(), second)
    assert out.result_refs["queued"] is True  # 열린 메인이 있는 동안은 대기열
    assert len(_events(pack, "TASK_READY")) == 1  # 대기열 작업은 승격 때 적는다

    body = TaskWithdraw(task_id="A")
    assert withdraw_task_request(pack, "planner_a", _key(), body).status == "APPLIED"
    [withdrawn] = _events(pack, "TASK_REQUEST_WITHDRAWN")
    assert withdrawn["ref"]["task_id"] == "A"
    # 철회로 사실이 바뀌어 재확인 후보가 생긴다. 메인은 그 승인을 기다린다
    factory = Router(replanning=[solve("L0"), solve("L1")]).factory()
    run_until_idle(pack, model_factory=factory)
    [main] = [r for r in _runs() if r.agent_type == "MAIN"]
    assert (main.status, main.wait_kind) == ("WAITING_HUMAN", "HUMAN_DECISION")
    with db.read() as conn:
        site = get_site(conn, pack.site_id)
        reconfirm = main.wait_ref
        [validation] = list_validations(conn, pack.site_id, reconfirm)
    approve = ApproveRequest(
        candidate_id=reconfirm,
        validation_id=validation.validation_id,
        expected_context_version=site.context_version,
    )
    assert approve_and_commit(pack, "supervisor", _key(), approve).status == "APPLIED"
    # 메인이 열린 일이 없음을 보고 끝내면 대기열에서 1건이 올라가고 새 메인이 받는다
    run_until_idle(pack, model_factory=factory)
    promoted = _events(pack, "TASK_READY")[-1]
    assert (promoted["ref"]["task_id"], promoted["ref"]["kind"]) == ("A2", "QUEUE")
    mains = [r for r in _runs() if r.agent_type == "MAIN"]
    assert mains[0].status == "SUCCEEDED" and len(mains) == 2
    assert promoted["case_id"] == mains[1].case_id != mains[0].case_id


def test_child_run_end_is_recorded_once(seeded):
    pack = seeded
    main = add_run(pack, "main", agent_type="MAIN", case_id="case_m")
    add_run(pack, "child", case_id="case_m", parent_run_id=main)
    with db.write() as tx:
        assert end_case_run(tx, pack, "child", "STALE", "TEST") is True
        assert end_case_run(tx, pack, "child", "STALE", "TEST") is False
    [ended] = _events(pack, "CHILD_RUN_ENDED")
    assert (ended["case_id"], ended["ref"]) == (
        "case_m",
        {"run_id": "child", "parent_run_id": "main", "status": "STALE"},
    )


# ── 메인 Run 행 (ST-18) ────────────────────────────────────────


def test_only_one_open_main_and_one_open_child(seeded):
    add_run(seeded, "main1", agent_type="MAIN")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        add_run(seeded, "main2", agent_type="MAIN")
    add_run(seeded, "child1", parent_run_id="main1")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        add_run(seeded, "child2", parent_run_id="main1")
    with db.write() as tx:
        end_case_run(tx, seeded, "child1", "BLOCKED", "RETURN_BLOCKED")
        end_case_run(tx, seeded, "main1", "SUCCEEDED", "CLOSE")
    add_run(seeded, "main2", agent_type="MAIN")  # 앞 메인이 끝나면 다시 열 수 있다
    add_run(seeded, "child2", parent_run_id="main2")


def test_main_has_no_parent_and_blocked_is_terminal(seeded):
    add_run(seeded, "r1", status="BLOCKED")
    with pytest.raises(sqlite3.IntegrityError, match="terminal status"), db.write() as tx:
        tx.execute("UPDATE agent_run SET status = 'RUNNING' WHERE run_id = 'r1'")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        add_run(seeded, "m", agent_type="MAIN", parent_run_id="r1")
    add_run(seeded, "c", parent_run_id="r1")
    with pytest.raises(sqlite3.IntegrityError, match="counters"), db.write() as tx:
        tx.execute("UPDATE agent_run SET parent_run_id = NULL WHERE run_id = 'c'")


def test_run_units_are_not_used_for_permission(with_a):
    """Run에 적힌 acting unit은 재계획의 판정에 들어가지 않는다 (AG-24).

    재계획의 관찰(움직일 수 있는 작업, 서버 적격성)은 부모가 있든 없든, Run에 어느 Unit이 적혀 있든 같다.
    메인의 관찰에도 자기 acting unit이 없다.
    """
    pack = with_a
    with db.read() as conn:
        supervisor_unit = supervisor_actor(conn, pack).unit_id
    assert supervisor_unit != "UA"
    main = add_run(pack, "main", agent_type="MAIN", acting_unit_id=supervisor_unit)
    add_run(pack, "child", parent_run_id=main, case_id="case_main")
    add_run(pack, "alone", case_id="case_alone", acting_unit_id=None, acting_actor_id=None)
    add_run(pack, "other", case_id="case_other", acting_unit_id="UB", acting_actor_id="planner_b")

    def view(run_id):
        with db.read() as conn:
            obs = replanning_observer.build_observation(conn, pack, run_id)
        assert "acting_unit_id" not in obs.data["run"]
        data = {k: v for k, v in obs.data.items() if k != "run"}
        return data, obs.hidden, obs.available

    assert view("child") == view("alone") == view("other")

    with db.read() as conn:
        seen = main_observer.build_observation(conn, pack, main).data
    assert "acting_unit_id" not in seen["run"]


# ── 종료 트랜잭션과 기동 복구 (ST-19) ──────────────────────────


def _crash_after_execute(monkeypatch, at_call):
    """Gateway가 Action을 커밋한 직후(그래프로 돌아가기 전) 죽는다."""
    real = runtime.StoreRunPort.execute
    calls = {"n": 0}

    def execute(self, run_id, step_no, message, meta):
        result = real(self, run_id, step_no, message, meta)
        calls["n"] += 1
        if calls["n"] == at_call:
            raise Crash
        return result

    monkeypatch.setattr(runtime.StoreRunPort, "execute", execute)


def _replanning_run(pack, *replies):
    """손으로 만든 Replanning Run을 한 번 부른다(메인 없이)."""
    with db.read() as conn:
        if get_run(conn, "r") is None:
            exists = False
        else:
            exists = True
    if not exists:
        add_run(pack, "r")
    return runtime.invoke(pack, {"run_id": "r"}, ScriptedChatModel(list(replies)))


def test_restart_after_committed_action_does_not_repeat_it(with_a, monkeypatch):
    pack = with_a
    _crash_after_execute(monkeypatch, at_call=1)
    with pytest.raises(Crash):
        _replanning_run(pack, solve("L0"))
    [run] = _runs()
    assert (run.status, run.steps_used, run.solver_calls_used) == ("RUNNING", 1, 1)

    recover_on_startup(pack)
    with db.read() as conn:
        [job] = [j for j in list_jobs(conn, pack.site_id) if j["kind"] == "CONTINUE_RUN"]
    assert (job["status"], job["run_id"]) == ("PENDING", run.run_id)
    run_until_idle(pack, model_factory=_factory(escalate()))

    [run] = _runs()
    steps = _steps(run.run_id)
    assert [(s["action"]["name"], s["status"]) for s in steps] == [
        ("SOLVE_WITH_SCOPE", "COMPLETED"),
        ("RETURN_RESULT", "COMPLETED"),
    ]
    assert (run.status, run.steps_used, run.solver_calls_used, run.restart_count) == (
        "BLOCKED",
        2,
        1,
        1,
    )
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM solver_job").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM command_result WHERE command_type = 'AGENT:SOLVE_WITH_SCOPE'"
            ).fetchone()[0]
            == 1
        )


def test_done_action_ends_run_in_the_same_transaction(with_a, monkeypatch):
    """종료 Action을 커밋한 직후 죽어도 Run은 이미 끝나 있다. 재기동 뒤 다시 행동하지 않는다."""
    pack = with_a
    _crash_after_execute(monkeypatch, at_call=2)
    with pytest.raises(Crash):
        _replanning_run(pack, solve("L0"), escalate())
    [run] = _runs()
    assert (run.status, run.steps_used) == ("BLOCKED", 2)

    recover_on_startup(pack)
    with db.read() as conn:
        assert [j for j in list_jobs(conn, pack.site_id) if j["kind"] == "CONTINUE_RUN"] == []
    run_until_idle(pack, model_factory=_factory())  # 모델을 부르면 script exhausted로 실패한다
    [run] = _runs()
    assert (run.status, run.steps_used, run.restart_count) == ("BLOCKED", 2, 0)


def test_restart_aborts_reserved_step_and_observes_again(with_a):
    """LLM 호출 중 죽으면 step은 예약만 남는다. 기동 때 정리하고 관찰부터 다시 한다."""
    pack = with_a

    def crash():
        raise Crash

    with pytest.raises(Crash):
        _replanning_run(pack, crash)
    [run] = _runs()
    assert (run.status, _steps(run.run_id)[0]["status"]) == ("RUNNING", "RESERVED")

    recover_on_startup(pack)
    run_until_idle(pack, model_factory=_factory(solve("L0"), escalate()))
    [run] = _runs()
    steps = _steps(run.run_id)
    assert [(s["status"], s["abort_reason"]) for s in steps[:2]] == [
        ("ABORTED", "RESTART"),
        ("COMPLETED", None),
    ]
    assert (run.status, run.steps_used, run.restart_count) == ("BLOCKED", 3, 1)
