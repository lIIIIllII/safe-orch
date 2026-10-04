"""Run과 도메인 연결. 게이트 경로 자동화(사건 → 메인 → 전문 Agent)."""

import time
import uuid

import pytest
from conftest import choose
from scripted import Router, ScriptedChatModel, blocked, escalate, solve

from app.commands.approval import ApproveRequest, WaiveRequest, approve_and_commit, waive
from app.commands.events import EventReport, receive_event
from app.commands.task_request import TaskRequestForm, submit_task_request
from app.coordinator import dispatcher, transitions
from app.coordinator.dispatcher import DispatchWorker, run_until_idle
from app.domain.models import ValidationCheck
from app.store import db
from app.store.repos.case_events import list_case_events
from app.store.repos.consultations import consultation_view, list_review_queue
from app.store.repos.dispatch import list_jobs, register_job
from app.store.repos.records import list_validations
from app.store.repos.runs import get_run, list_steps
from app.store.repos.site import bump_context_version, get_site

pytestmark = pytest.mark.usefixtures("main_on")

GATE_SCRIPT = [solve("L0", "최소 변경부터"), solve("L1", "L0 불가, 범위를 넓힌다")]


def _key():
    return uuid.uuid4().hex


def _factory(replies=GATE_SCRIPT):
    return lambda: ScriptedChatModel(list(replies))


def _submit_a(pack):
    data = pack.new_task.model_dump(exclude={"requested", "unit_id", "owner_actor_id", "movable"})
    assert (
        submit_task_request(pack, "planner_a", _key(), TaskRequestForm(**data)).status == "APPLIED"
    )


def _jobs(pack, kind=None):
    with db.read() as conn:
        return [j for j in list_jobs(conn, pack.site_id) if kind is None or j["kind"] == kind]


def _runs(agent_type=None):
    with db.read() as conn:
        ids = [r[0] for r in conn.execute("SELECT run_id FROM agent_run ORDER BY rowid")]
        runs = [get_run(conn, rid) for rid in ids]
    return [r for r in runs if agent_type is None or r.agent_type == agent_type]


def list_steps_of(run_id):
    with db.read() as conn:
        return list_steps(conn, run_id)


def _count(table):
    with db.read() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _site(pack):
    with db.read() as conn:
        return get_site(conn, pack.site_id)


def _event(pack, source="rep-1"):
    body = EventReport(source_event_id=source, event_type="DELAY", text="도장 준비 지연")
    out = receive_event(pack, "reporter", _key(), body)
    assert out.status == "APPLIED"
    return out.result_refs["event_id"]


def _alpha(pack):
    """폼 A → 메인 → Replanning(L0·L1 → 검증 → DONE) → 협의 Run이 C 담당자 답을 기다린다.

    (메인, Replanning Run, 협의 Run, Alpha 후보 ID)."""
    _submit_a(pack)
    run_until_idle(pack, model_factory=_factory())
    choose(pack)  # Supervisor가 고른 안만 협의한다
    run_until_idle(pack, model_factory=_factory())
    [main] = _runs("MAIN")
    [replanning] = _runs("REPLANNING")
    [consult] = _runs("COORDINATION")
    assert (main.status, main.wait_kind, main.wait_ref) == (
        "WAITING_HUMAN",
        "CHILD_RUN",
        consult.run_id,
    )
    return main, replanning, consult, consult.input_ref["candidate_id"]


# ── 게이트 경로 자동화 ─────────────────────────────────────────


def test_gate_path_automated(seeded):
    pack = seeded
    main, run, _, alpha = _alpha(pack)

    assert main.case_id.startswith("case_") and main.exec_contract_version == "main-c6"
    assert (run.parent_run_id, run.case_id) == (main.run_id, main.case_id)
    # 재계획 Run에는 주체 Unit도 대신 움직이는 Actor도 없다 (AG-24)
    assert (run.acting_unit_id, run.acting_actor_id) == (None, None)
    # Replanning은 검증까지만 산다: L0 → L1(후보) → 검증 결과로 깨어나 DONE
    assert (run.status, run.end_reason, run.steps_used, run.solver_calls_used) == (
        "SUCCEEDED",
        "RETURN_DONE",
        3,
        2,
    )
    assert {j["kind"]: j["status"] for j in _jobs(pack)} == {
        "RECHECK": "DONE",
        "CONTINUE_RUN": "DONE",
        "START_RUN": "DONE",
        "VALIDATE": "DONE",
        "BUILD_CONSULTATION": "DONE",
        "RESUME_RUN": "DONE",
    }

    with db.read() as conn:
        [v] = list_validations(conn, pack.site_id, alpha)
        view = consultation_view(conn, pack.site_id, alpha)
        assert list_review_queue(conn, pack.site_id) == [alpha]
    assert v.status == "PASS"
    assert [i.task_id for i in view.items] == ["C"]

    # Supervisor가 수용하면 답을 기다리던 협의 Run이 깨어나 끝나고, 메인은 승인을 기다린다
    body = WaiveRequest(candidate_id=alpha, task_ids=("C",), comment="작업발판 일정 확인됨")
    assert waive(pack, "supervisor", _key(), body).status == "APPLIED"
    run_until_idle(pack, model_factory=_factory())
    assert _runs("COORDINATION")[0].status == "SUCCEEDED"
    [main] = _runs("MAIN")
    assert (main.status, main.wait_kind, main.wait_ref) == (
        "WAITING_HUMAN",
        "HUMAN_DECISION",
        alpha,
    )

    approve = ApproveRequest(
        candidate_id=alpha,
        validation_id=v.validation_id,
        expected_context_version=_site(pack).context_version,
    )
    assert approve_and_commit(pack, "supervisor", _key(), approve).status == "APPLIED"
    # 확정 뒤 통지도 메인이 부른다. 통지가 끝나야 열린 일이 없어 CLOSE할 수 있다
    run_until_idle(pack, model_factory=_factory())
    [main] = _runs("MAIN")
    assert (main.status, main.end_reason, main.agent_calls_used) == ("SUCCEEDED", "CLOSE", 3)
    assert [(r.input_ref["phase"], r.status) for r in _runs("COORDINATION")] == [
        ("CONSULT", "SUCCEEDED"),
        ("NOTICE", "SUCCEEDED"),
    ]


# ── Event·Validator와 Run ──────────────────────────────────────


def test_event_while_waiting_stales_child_and_keeps_main(seeded):
    _, _, _, alpha = _alpha(seeded)
    event_id = _event(seeded)
    [stale] = _runs("COORDINATION")
    assert (stale.status, stale.end_reason, stale.wait_kind) == ("STALE", f"EVENT:{event_id}", None)
    # 메인은 무효가 되지 않는다. 하위 Run이 끝나 깨어나고, 신고는 메인의 사건이다
    [main] = _runs("MAIN")
    assert main.status == "WAITING_HUMAN" and _jobs(seeded, "RESUME_RUN")[-1]["status"] == "PENDING"
    with db.read() as conn:
        assert consultation_view(conn, seeded.site_id, alpha).status == "CANCELLED"
        kinds = [e["kind"] for e in list_case_events(conn, seeded.site_id)]
    assert kinds[-2:] == ["CHILD_RUN_ENDED", "EVENT_REPORTED"]


def test_solver_candidate_fail_marks_run_error(seeded, monkeypatch):
    real = transitions.validate

    def inject_fail(*args):
        v = real(*args)
        bad = ValidationCheck(
            check_id="C03", status="FAIL", task_ids=("A",), reason_code="DURATION"
        )
        return v.model_copy(update={"status": "FAIL", "checks": (bad,)})

    monkeypatch.setattr(transitions, "validate", inject_fail)
    _submit_a(seeded)
    run_until_idle(seeded, model_factory=_factory())
    [run] = _runs("REPLANNING")
    assert (run.status, run.end_reason) == ("ERROR", "MODEL_VALIDATION_MISMATCH")
    assert not _jobs(seeded, "BUILD_CONSULTATION")
    # 메인은 서버가 끝낸 하위 Run의 결과를 받고 이관한다
    [main] = _runs("MAIN")
    assert (main.status, main.end_reason) == ("ESCALATED", "ESCALATE")


def test_incomplete_only_does_not_error(seeded, monkeypatch):
    real = transitions.validate

    def inject_incomplete(*args):
        v = real(*args)
        c11 = ValidationCheck(
            check_id="C11", status="INCOMPLETE", task_ids=("A",), reason_code="FIELD_NOT_CONFIRMED"
        )
        return v.model_copy(update={"status": "INCOMPLETE", "checks": (c11,)})

    monkeypatch.setattr(transitions, "validate", inject_incomplete)
    _submit_a(seeded)
    # C11만 걸린 비PASS는 ERROR가 아니라 Run을 깨운다. 재개 뒤 남은 행동은 막힌 결과뿐이다.
    run_until_idle(seeded, model_factory=_factory([*GATE_SCRIPT, escalate()]))
    [run] = _runs("REPLANNING")
    assert (run.status, run.end_reason) == ("BLOCKED", "RETURN_BLOCKED")
    assert (run.wake_seq, run.handled_wake_seq, run.wait_generation) == (1, 1, 1)


# ── RECHECK·하위 Run 시작 재확인 ───────────────────────────────


def test_recheck_does_not_start_replanning(seeded):
    """충돌이 있으면 RECHECK는 아무것도 만들지 않는다. 재계획을 부를지는 메인이 판단한다."""
    _alpha(seeded)
    with db.write() as tx:  # Context 변경 + RECHECK
        bump_context_version(tx, seeded.site_id)
        register_job(tx, seeded.site_id, "RECHECK", "RECHECK:manual", {})
    run_until_idle(seeded, model_factory=_factory())
    assert _jobs(seeded, "RECHECK")[-1]["status"] == "DONE"
    assert len(_jobs(seeded, "START_RUN")) == 2  # 재계획과 협의, 둘 다 메인이 불렀다
    assert _count("candidate") == 1  # Alpha만, RECONFIRM 없음


def test_no_replanning_while_hold_active(seeded):
    _submit_a(seeded)
    run_until_idle(seeded)  # model_factory 없음: 메인은 아직 돌지 않았다
    _event(seeded)
    # 신고는 열린 메인의 사건이다. Hold 중에는 재계획 호출이 받아들여지지 않는다
    run_until_idle(seeded, model_factory=Router(event_response=[blocked()]).factory())
    assert _runs("REPLANNING") == []
    [main] = _runs("MAIN")
    assert (main.status, main.wait_kind) == ("WAITING_HUMAN", "HUMAN_DECISION")
    first = list_steps_of(main.run_id)[0]["observation"]
    assert [c["agent"] for c in first["calls"]] == ["EVENT_RESPONSE"]


def test_start_run_skipped_when_parent_does_not_wait_for_it(seeded):
    _, run, _, _ = _alpha(seeded)
    [first] = [j for j in _jobs(seeded, "START_RUN") if j["run_id"] == run.run_id]
    with db.write() as tx:
        payload = {**first["payload"], "run_id": "run_other"}
        register_job(tx, seeded.site_id, "START_RUN", "START_RUN:manual", payload)

    def must_not_call():
        raise AssertionError("model requested for a second run")

    run_until_idle(seeded, model_factory=must_not_call)
    assert [r.run_id for r in _runs("REPLANNING")] == [run.run_id]
    assert _jobs(seeded, "START_RUN")[-1]["status"] == "DONE"


def test_model_unavailable_marks_main_error_and_notifies_supervisor(seeded):
    def no_key():
        raise RuntimeError("OPENAI_API_KEY not configured")

    _submit_a(seeded)
    run_until_idle(seeded, model_factory=no_key)
    [run] = _runs()
    assert (run.agent_type, run.status, run.end_reason) == (
        "MAIN",
        "ERROR",
        "MODEL_UNAVAILABLE: RuntimeError",
    )
    with db.read() as conn:
        [notice] = conn.execute("SELECT to_actor_id, type, body FROM message").fetchall()
    assert notice[:2] == ("supervisor", "NOTICE") and "넘겨지지 않습니다" in notice[2]


# ── 워커 루프 ──────────────────────────────────────────────────


def test_worker_loop_survives_exception(seeded, monkeypatch):
    real = dispatcher.process_next
    calls = {"n": 0}

    def flaky(pack, model_factory=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real(pack, model_factory)

    monkeypatch.setattr(dispatcher, "process_next", flaky)
    _submit_a(seeded)
    worker = DispatchWorker(seeded, poll_s=0.05)
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while _jobs(seeded, "RECHECK")[0]["status"] != "DONE":
            assert time.monotonic() < deadline, "worker stopped after an exception"
            time.sleep(0.05)
    finally:
        worker.stop()
    assert calls["n"] >= 2 and not worker._thread.is_alive()
