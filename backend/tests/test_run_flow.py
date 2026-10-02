"""Run과 도메인 연결 3b (설계서 §8·§10·§11.3(7)·§11.5, 부록 A.16). 게이트 경로 자동화."""

import time
import uuid

from scripted import ScriptedChatModel, escalate, solve

from app.commands.approval import ApproveRequest, WaiveRequest, approve_and_commit, waive
from app.commands.events import EventReport, receive_event
from app.commands.task_request import TaskRequestForm, submit_task_request
from app.coordinator import dispatcher, transitions
from app.coordinator.dispatcher import DispatchWorker, run_until_idle
from app.domain.models import ValidationCheck
from app.store import db
from app.store.repos.consultations import consultation_view, list_review_queue
from app.store.repos.dispatch import list_jobs, register_job
from app.store.repos.records import list_validations
from app.store.repos.runs import get_run
from app.store.repos.site import bump_context_version, get_site

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


def _runs():
    with db.read() as conn:
        ids = [r[0] for r in conn.execute("SELECT run_id FROM agent_run ORDER BY rowid")]
        return [get_run(conn, rid) for rid in ids]


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


def _waiting_run(pack):
    """폼 A → 워커(START_RUN → 스크립트 L0·L1 → WAIT → VALIDATE → Consultation)."""
    _submit_a(pack)
    run_until_idle(pack, model_factory=_factory())
    [run] = _runs()
    assert run.status == "WAITING_HUMAN"
    return run


# ── 게이트 경로 자동화 ─────────────────────────────────────────


def test_gate_path_automated(seeded):
    pack = seeded
    run = _waiting_run(pack)

    [start] = _jobs(pack, "START_RUN")
    assert (start["status"], start["run_id"]) == ("DONE", run.run_id)
    assert run.case_id.startswith("case_") and run.input_ref["job_id"] == start["job_id"]
    assert (run.acting_unit_id, run.acting_actor_id) == ("UA", "planner_a")
    assert (run.wait_kind, run.steps_used, run.solver_calls_used) == ("CANDIDATE_OUTCOME", 2, 2)
    assert {j["kind"]: j["status"] for j in _jobs(pack)} == {
        "RECHECK": "DONE",
        "START_RUN": "DONE",
        "VALIDATE": "DONE",
        "BUILD_CONSULTATION": "DONE",
    }

    alpha = run.wait_ref
    with db.read() as conn:
        [v] = list_validations(conn, pack.site_id, alpha)
        view = consultation_view(conn, pack.site_id, alpha)
        assert list_review_queue(conn, pack.site_id) == [alpha]
    assert v.status == "PASS"
    assert {i.task_id: i.base_status for i in view.items} == {"A": "COVERED", "C": "PENDING"}

    body = WaiveRequest(candidate_id=alpha, task_ids=("C",), comment="작업발판 일정 확인됨")
    assert waive(pack, "supervisor", _key(), body).status == "APPLIED"
    approve = ApproveRequest(
        candidate_id=alpha,
        validation_id=v.validation_id,
        expected_context_version=_site(pack).context_version,
    )
    out = approve_and_commit(pack, "supervisor", _key(), approve)
    assert out.status == "APPLIED" and out.result_refs["succeeded_run_id"] == run.run_id
    [done] = _runs()
    assert (done.status, done.end_reason) == ("SUCCEEDED", "COMMITTED:1")


# ── Event·Validator와 Run ──────────────────────────────────────


def test_event_while_waiting_stales_run(seeded):
    run = _waiting_run(seeded)
    event_id = _event(seeded)
    [stale] = _runs()
    assert (stale.status, stale.end_reason, stale.wait_kind) == ("STALE", f"EVENT:{event_id}", None)
    with db.read() as conn:
        assert consultation_view(conn, seeded.site_id, run.wait_ref).status == "CANCELLED"


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
    [run] = _runs()
    assert (run.status, run.end_reason) == ("ERROR", "MODEL_VALIDATION_MISMATCH")
    assert not _jobs(seeded, "BUILD_CONSULTATION")


def test_incomplete_only_does_not_error(seeded, monkeypatch, solver_limit):
    solver_limit(2)  # Alpha(L0·L1) 뒤 이관만 열린다 (A.30)
    real = transitions.validate

    def inject_incomplete(*args):
        v = real(*args)
        c11 = ValidationCheck(
            check_id="C11", status="INCOMPLETE", task_ids=("A",), reason_code="FIELD_NOT_CONFIRMED"
        )
        return v.model_copy(update={"status": "INCOMPLETE", "checks": (c11,)})

    monkeypatch.setattr(transitions, "validate", inject_incomplete)
    _submit_a(seeded)
    # C11만 걸린 비PASS는 ERROR가 아니라 Run을 깨운다(§11.5, A.21). 재개 뒤 남은 행동은 이관뿐이다.
    run_until_idle(seeded, model_factory=_factory([*GATE_SCRIPT, escalate()]))
    [run] = _runs()
    assert (run.status, run.end_reason) == ("ESCALATED", "ESCALATE_NO_SOLUTION")
    assert (run.wake_seq, run.handled_wake_seq, run.wait_generation) == (1, 1, 1)


# ── 열린 Case·START_RUN 재확인 ─────────────────────────────────


def test_recheck_skips_while_case_open(seeded):
    _waiting_run(seeded)
    with db.write() as tx:  # 열린 Case 중 Context 변경 + RECHECK
        bump_context_version(tx, seeded.site_id)
        register_job(tx, seeded.site_id, "RECHECK", "RECHECK:manual", {})
    snapshots = _count("snapshot")
    run_until_idle(seeded, model_factory=_factory())
    assert _jobs(seeded, "RECHECK")[-1]["status"] == "DONE"
    assert len(_jobs(seeded, "START_RUN")) == 1 and _count("snapshot") == snapshots
    assert _count("candidate") == 1  # Alpha만, RECONFIRM 없음


def test_start_run_skipped_when_hold_active(seeded):
    _submit_a(seeded)
    run_until_idle(seeded)  # model_factory 없음: START_RUN은 PENDING
    _event(seeded)
    run_until_idle(seeded, model_factory=_factory())
    [start] = _jobs(seeded, "START_RUN")
    assert (start["status"], start["run_id"]) == ("DONE", None) and _runs() == []


def test_start_run_skipped_when_context_moved(seeded):
    _submit_a(seeded)
    run_until_idle(seeded)
    with db.write() as tx:
        bump_context_version(tx, seeded.site_id)
    run_until_idle(seeded, model_factory=_factory())
    assert _jobs(seeded, "START_RUN")[0]["status"] == "DONE" and _runs() == []


def test_start_run_skipped_when_case_open(seeded):
    run = _waiting_run(seeded)
    [first] = _jobs(seeded, "START_RUN")
    with db.write() as tx:
        register_job(tx, seeded.site_id, "START_RUN", "START_RUN:manual", first["payload"])

    def must_not_call():
        raise AssertionError("model requested for a second run")

    run_until_idle(seeded, model_factory=must_not_call)
    assert [r.run_id for r in _runs()] == [run.run_id]
    assert _jobs(seeded, "START_RUN")[-1]["status"] == "DONE"


def test_model_unavailable_marks_run_error(seeded):
    def no_key():
        raise RuntimeError("OPENAI_API_KEY not configured")

    _submit_a(seeded)
    run_until_idle(seeded, model_factory=no_key)
    [run] = _runs()
    assert (run.status, run.end_reason) == ("ERROR", "MODEL_UNAVAILABLE: RuntimeError")
    assert _jobs(seeded, "START_RUN")[0]["status"] == "DONE"


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
