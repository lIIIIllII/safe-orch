"""대기와 재개·거절 후 재탐색·대기열 (설계서 §9.2·§11.3·§11.5, 부록 A.21 1단계).

T33·T36–T39·T41·T42, 대기열(QUEUED), 기본안 B E2E(새 Action 부분은 2단계까지 xfail).
"""

import threading
import uuid

import pytest
from scripted import ScriptedChatModel, call, escalate, solve

from app.commands.approval import (
    ApproveRequest,
    RejectRequest,
    WaiveRequest,
    approve_and_commit,
    reject_candidate,
    waive,
)
from app.commands.events import EventReport, receive_event
from app.commands.runs import CancelRun, cancel_run
from app.commands.task_request import (
    TaskRequestForm,
    TaskWithdraw,
    submit_task_request,
    withdraw_task_request,
)
from app.coordinator.dispatcher import run_until_idle
from app.domain.models import SolverResult
from app.solver import cpsat
from app.store import db
from app.store.repos.cases import claim_resume, queued_task_ids, wake_run
from app.store.repos.dispatch import list_jobs, register_job
from app.store.repos.records import list_validations
from app.store.repos.runs import get_run, list_steps
from app.store.repos.site import get_site
from app.store.repos.snapshots import build_snapshot_content
from app.store.repos.tasks import list_current_tasks

FORM_FIELDS = (
    "task_id",
    "work_type",
    "zone_id",
    "duration",
    "earliest_start",
    "latest_start",
    "latest_end",
    "required_resource_type",
    "requested_resource_id",
)


def _key():
    return uuid.uuid4().hex


def _factory(*replies):
    return lambda: ScriptedChatModel(list(replies))


def _site(pack):
    with db.read() as conn:
        return get_site(conn, pack.site_id)


def _runs():
    with db.read() as conn:
        ids = [r[0] for r in conn.execute("SELECT run_id FROM agent_run ORDER BY rowid")]
        return [get_run(conn, rid) for rid in ids]


def _run(run_id):
    with db.read() as conn:
        return get_run(conn, run_id)


def _jobs(pack, kind=None):
    with db.read() as conn:
        return [j for j in list_jobs(conn, pack.site_id) if kind is None or j["kind"] == kind]


def _steps(run_id):
    with db.read() as conn:
        return list_steps(conn, run_id)


def _task(pack, task_id):
    with db.read() as conn:
        return next(t for t in list_current_tasks(conn, pack.site_id, pack) if t.task_id == task_id)


def _submit_a(pack):
    a = pack.new_task.model_dump(exclude={"requested", "unit_id", "owner_actor_id", "movable"})
    return submit_task_request(pack, "planner_a", _key(), TaskRequestForm(**a))


def _submit(pack, task_id, **changes):
    d = next(x for x in pack.demo_requests if x.task_id == task_id)
    form = TaskRequestForm(**{**{k: getattr(d, k) for k in FORM_FIELDS}, **changes})
    return submit_task_request(pack, d.requester, _key(), form)


def _alpha_waiting(pack):
    """폼 A → L0·L1 → Alpha(WAIT). 대기 중인 Run을 돌려준다."""
    assert _submit_a(pack).status == "APPLIED"
    run_until_idle(pack, model_factory=_factory(solve("L0"), solve("L1")))
    [run] = _runs()
    assert run.status == "WAITING_HUMAN" and run.wait_generation == 1
    return run


def _validation(pack, cand_id):
    with db.read() as conn:
        [v] = list_validations(conn, pack.site_id, cand_id)
    return v


def _reject(pack, cand_id, reason="PREFERENCE", targets=(), axes=(), comment="", key=None):
    body = RejectRequest(
        candidate_id=cand_id,
        validation_id=_validation(pack, cand_id).validation_id,
        reason_code=reason,
        target_task_ids=targets,
        axes=axes,
        comment=comment,
    )
    return reject_candidate(pack, "supervisor", key or _key(), body)


def _reject_demo(pack, cand_id):
    """scenario의 기본안 B 거절(TASK_IMMOVABLE, C, TIME·RESOURCE)."""
    x = pack.demo_rejections[0]
    return _reject(pack, cand_id, x.reason_code, x.target_task_ids, x.axes, x.comment)


def _approve(pack, cand_id):
    body = ApproveRequest(
        candidate_id=cand_id,
        validation_id=_validation(pack, cand_id).validation_id,
        expected_context_version=_site(pack).context_version,
    )
    return approve_and_commit(pack, "supervisor", _key(), body)


# ── 기본안 B ───────────────────────────────────────────────────


def test_plan_b_reject_with_constraint_wakes_and_resumes(seeded):
    """Alpha를 TASK_IMMOVABLE(C)로 거절 → Context +1, Run wake → 재개 → C 고정 관찰 → L0 INFEASIBLE."""
    pack = seeded
    run = _alpha_waiting(pack)
    ctx = _site(pack).context_version
    out = _reject_demo(pack, run.wait_ref)
    assert out.status == "APPLIED" and out.result_refs["run_id"] == run.run_id
    woke = _run(run.run_id)
    assert (_site(pack).context_version, woke.wake_seq, woke.status) == (
        ctx + 1,
        1,
        "WAITING_HUMAN",
    )
    [resume] = _jobs(pack, "RESUME_RUN")
    assert (resume["status"], resume["dedupe_key"]) == ("PENDING", f"RESUME_RUN:{run.run_id}:1")

    # 2단계 전에는 SOLVE·ESCALATE뿐이다: C 고정으로 L0 INFEASIBLE → 이관
    run_until_idle(pack, model_factory=_factory(solve("L0", "C 고정 반영"), escalate()))
    done = _run(run.run_id)
    assert (done.status, done.end_reason) == ("ESCALATED", "ESCALATE_NO_SOLUTION")
    assert (done.handled_wake_seq, done.wait_generation) == (1, 1)
    s3, s4 = _steps(run.run_id)[2:]
    obs = s3["observation"]
    assert obs["rejections"] == [
        {
            "candidate_id": run.wait_ref,
            "reason_code": "TASK_IMMOVABLE",
            "target_task_ids": ["C"],
            "axes": ["RESOURCE", "TIME"],
            "has_constraint": True,
            "quoted_comment": "작업발판 연계 공정 확정",
        }
    ]
    assert [c["task_id"] for c in obs["constraints"]] == ["C"]
    assert s3["tool_result"]["stage1"]["status"] == "INFEASIBLE"
    assert s4["action"]["name"] == "ESCALATE_NO_SOLUTION"


@pytest.mark.xfail(strict=True, reason="A.21 2단계: LIST·ASK·TRY·답변 명령")
def test_plan_b_full_e2e(seeded):
    """기본안 B 전체: 거절 → 재개 → L0 → LIST → ASK → 수락 → 재개 → TRY → Beta → 승인 R1."""
    from app.commands.messages import ReplyRequest, reply_message  # 2단계에서 생긴다

    pack = seeded
    run = _alpha_waiting(pack)
    _reject_demo(pack, run.wait_ref)
    run_until_idle(
        pack,
        model_factory=_factory(
            solve("L0", "C 고정 반영"),
            call("LIST_ASSIGNABLE_RESOURCES", "A 자원 조회", task_id="A"),
            call(
                "ASK_TASK_OWNER",
                "자원 축 확인",
                task_id="A",
                axis="RESOURCE",
                allowed_values=["SITE-CR-01"],
                question="SITE-CR-01을 써도 되나요?",
            ),
        ),
    )
    waiting = _run(run.run_id)
    assert (waiting.status, waiting.wait_kind) == ("WAITING_HUMAN", "MESSAGE")
    reply = ReplyRequest(message_id=waiting.wait_ref, decision="ACCEPT", comment="")
    assert reply_message(pack, "planner_a", _key(), reply).status == "APPLIED"
    run_until_idle(
        pack,
        model_factory=_factory(
            call(
                "TRY_ALTERNATIVE_RESOURCE", "SITE-CR-01 시도", task_id="A", resource_id="SITE-CR-01"
            )
        ),
    )
    beta = _run(run.run_id).wait_ref
    assert _approve(pack, beta).status == "APPLIED"
    assert _run(run.run_id).status == "SUCCEEDED"


# ── T33: 제약 없는 거절 ─────────────────────────────────────────


def _n1_waiting(pack):
    assert _submit(pack, "N1").status == "APPLIED"
    run_until_idle(pack, model_factory=_factory(solve("L0")))
    [run] = _runs()
    assert run.status == "WAITING_HUMAN"
    return run


def test_t33_reject_without_constraint_wakes_and_blocks_same_assignments(seeded):
    pack = seeded
    run = _n1_waiting(pack)
    ctx = _site(pack).context_version
    assert _reject(pack, run.wait_ref, "PREFERENCE", comment="오후가 좋다").status == "APPLIED"
    assert _site(pack).context_version == ctx  # 제약 없는 거절은 Context를 바꾸지 않는다
    assert _run(run.run_id).wake_seq == 1
    # 재개: L2는 C까지 넣지만 같은 배정(N1 11:00)이 나온다 → Guard가 후보를 만들지 않는다
    run_until_idle(pack, model_factory=_factory(solve("L2"), escalate()))
    s2, s3 = _steps(run.run_id)[1:]
    assert s2["observation"]["rejections"][0]["has_constraint"] is False
    assert s2["observation"]["rejections"][0]["quoted_comment"] == "오후가 좋다"
    assert s2["guard"] == {"verdict": "REJECTED", "reason_code": "DUPLICATE_REJECTED"}
    assert (s2["result_kind"], s2["tool_result"]["candidate_id"]) == ("CONTINUE", None)
    assert s3["action"]["name"] == "ESCALATE_NO_SOLUTION"
    with db.read() as conn:
        n = conn.execute("SELECT COUNT(*) FROM candidate").fetchone()[0]
    assert n == 1


def test_t33_second_plain_rejection_escalates(seeded, monkeypatch):
    """제약 없는 거절이 Case의 2번째면 깨우지 않고 이관 (§9.2·T33, A.21 0-3)."""
    pack = seeded
    run = _n1_waiting(pack)
    _reject(pack, run.wait_ref, "PREFERENCE")
    real = cpsat.solve

    def shifted(*args):  # 두 번째 후보가 다른 배정이 되게 N1을 10분 늦춘다(검증은 통과하는 값)
        r: SolverResult = real(*args)
        sol = [
            {**a, "start": a["start"] + 10, "end": a["end"] + 10} if a["task_id"] == "N1" else a
            for a in r.stage2["solution"]
        ]
        return r.model_copy(update={"stage2": {**r.stage2, "solution": sol}})

    monkeypatch.setattr(cpsat, "solve", shifted)
    run_until_idle(pack, model_factory=_factory(solve("L2")))
    second = _run(run.run_id)
    assert second.status == "WAITING_HUMAN" and second.wait_ref != run.wait_ref
    out = _reject(pack, second.wait_ref, "OTHER")
    assert out.status == "APPLIED"
    ended = _run(run.run_id)
    assert (ended.status, ended.end_reason) == ("ESCALATED", "REJECTED_TWICE")
    assert [j for j in _jobs(pack, "RESUME_RUN") if j["status"] == "PENDING"] == []


# ── T36–T39·T41·T42: 대기와 재개 ───────────────────────────────


def test_t36_change_before_wait_is_not_lost(seeded):
    """답변(변화)이 RUNNING 중에 오면 대기하지 않고 다시 관찰한다 (NEW_CHANGE_BEFORE_WAIT)."""
    pack = seeded

    def wake_then_l1():
        with db.write() as tx:
            [rid] = [
                r[0] for r in tx.execute("SELECT run_id FROM agent_run WHERE status='RUNNING'")
            ]
            wake_run(tx, pack.site_id, rid)
        return solve("L1")

    assert _submit_a(pack).status == "APPLIED"
    run_until_idle(pack, model_factory=_factory(solve("L0"), wake_then_l1, escalate()))
    [run] = _runs()
    s2 = _steps(run.run_id)[1]
    assert (s2["result_kind"], s2["guard"]["reason_code"]) == ("CONTINUE", "NEW_CHANGE_BEFORE_WAIT")
    assert (run.status, run.wait_generation, run.handled_wake_seq) == ("ESCALATED", 0, 1)
    assert _jobs(pack, "RESUME_RUN") == []


def test_t37_old_generation_resume_is_void(seeded):
    pack = seeded
    run = _alpha_waiting(pack)  # wait_generation 1
    with db.write() as tx:
        register_job(
            tx,
            pack.site_id,
            "RESUME_RUN",
            f"RESUME_RUN:{run.run_id}:0",
            run_id=run.run_id,
            wait_generation=0,
        )
    run_until_idle(pack, model_factory=_factory(pytest.fail))
    assert _run(run.run_id).status == "WAITING_HUMAN"
    [job] = _jobs(pack, "RESUME_RUN")
    assert job["status"] == "DONE"


def test_t38_same_reply_replayed_without_wake(seeded):
    """같은 키·같은 본문 재전송은 REPLAYED, wake·job 추가 없음. 같은 키 다른 본문은 거절."""
    pack = seeded
    run = _alpha_waiting(pack)
    key = _key()
    assert _reject(pack, run.wait_ref, "PREFERENCE", key=key).status == "APPLIED"
    again = _reject(pack, run.wait_ref, "PREFERENCE", key=key)
    assert again.status == "REPLAYED"
    assert _run(run.run_id).wake_seq == 1
    assert len(_jobs(pack, "RESUME_RUN")) == 1
    other = _reject(pack, run.wait_ref, "OTHER", key=key)
    assert other.reason_codes == ("IDEMPOTENCY_MISMATCH",)


def test_t39_resume_claimed_once(seeded):
    pack = seeded
    run = _alpha_waiting(pack)
    with db.write() as tx:
        assert claim_resume(tx, run.run_id, 1) is True
    with db.write() as tx:
        assert claim_resume(tx, run.run_id, 1) is False


def test_t39_concurrent_claim_from_two_connections(seeded):
    pack = seeded
    run = _alpha_waiting(pack)
    barrier = threading.Barrier(2)
    results = []

    def claim():
        barrier.wait()
        with db.write() as tx:
            results.append(claim_resume(tx, run.run_id, 1))
        db.close()

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [False, True]


def test_t41_two_changes_one_resume(seeded):
    pack = seeded
    run = _alpha_waiting(pack)
    with db.write() as tx:
        assert wake_run(tx, pack.site_id, run.run_id)
        assert wake_run(tx, pack.site_id, run.run_id)
    pending = [j for j in _jobs(pack, "RESUME_RUN") if j["status"] == "PENDING"]
    assert len(pending) == 1 and _run(run.run_id).wake_seq == 2
    run_until_idle(pack, model_factory=_factory(escalate()))
    resumed = _run(run.run_id)
    assert (resumed.handled_wake_seq, resumed.status) == (2, "ESCALATED")
    assert _steps(run.run_id)[-1]["observed_wake_seq"] == 2


def test_t42_event_while_waiting_stales_and_resume_is_void(seeded):
    pack = seeded
    run = _alpha_waiting(pack)
    with db.write() as tx:
        wake_run(tx, pack.site_id, run.run_id)  # PENDING RESUME
    body = EventReport(source_event_id="e1", event_type="DELAY", text="지연")
    assert receive_event(pack, "reporter", _key(), body).status == "APPLIED"
    assert _run(run.run_id).status == "STALE"
    run_until_idle(pack, model_factory=_factory(pytest.fail))
    assert _run(run.run_id).status == "STALE"
    assert {j["status"] for j in _jobs(pack, "RESUME_RUN")} == {"DONE"}


# ── 대기열 (A.21 0-1) ──────────────────────────────────────────


def test_form_during_open_case_is_queued_then_promoted_on_commit(seeded):
    pack = seeded
    run = _alpha_waiting(pack)
    ctx = _site(pack).context_version
    rechecks = len(_jobs(pack, "RECHECK"))
    out = _submit(pack, "N1")
    assert out.status == "APPLIED" and out.result_refs["queued"] is True
    assert _site(pack).context_version == ctx  # 대기열은 사실이 아니다
    assert len(_jobs(pack, "RECHECK")) == rechecks
    n1 = _task(pack, "N1")
    assert (n1.lifecycle, n1.revision) == ("QUEUED", 1)
    with db.read() as conn:
        content = build_snapshot_content(conn, pack.site_id, pack)
    assert "N1" not in [t["task_id"] for t in content["tasks"]]
    assert _run(run.run_id).wake_seq == 0  # 폼은 열린 Case를 깨우지 않는다

    body = WaiveRequest(candidate_id=run.wait_ref, task_ids=("C",), comment="확인")
    assert waive(pack, "supervisor", _key(), body).status == "APPLIED"
    assert _approve(pack, run.wait_ref).status == "APPLIED"
    n1 = _task(pack, "N1")
    assert (n1.lifecycle, n1.revision) == ("READY", 2)
    assert _site(pack).context_version == ctx + 1
    with db.read() as conn:
        consents = conn.execute(
            "SELECT axis, task_revision FROM consent WHERE task_id = 'N1' ORDER BY rowid"
        ).fetchall()
    assert [tuple(c) for c in consents] == [
        ("TIME", 1),
        ("RESOURCE", 1),
        ("TIME", 2),
        ("RESOURCE", 2),
    ]
    # 승격 RECHECK와 확정 RECHECK는 같은 (ctx, plan) 키라 하나만 남는다(먼저 등록한 승격)
    last = _jobs(pack, "RECHECK")[-1]
    assert last["dedupe_key"] == f"RECHECK:ctx{ctx + 1}:plan1"
    assert last["payload"]["cause"] == {"kind": "QUEUE", "task_id": "N1", "actor_id": "planner_a"}
    # 올라간 요청은 요청자로 재계획된다
    run_until_idle(pack, model_factory=_factory(solve("L0")))
    n1_run = _runs()[-1]
    assert (n1_run.status, n1_run.acting_actor_id) == ("WAITING_HUMAN", "planner_a")


def _queue_n1_during_alpha(pack):
    run = _alpha_waiting(pack)
    assert _submit(pack, "N1").result_refs["queued"] is True
    return run


def test_queue_promoted_when_case_escalates(seeded):
    pack = seeded
    run = _queue_n1_during_alpha(pack)
    _reject(pack, run.wait_ref, "PREFERENCE")
    run_until_idle(pack, model_factory=_factory(escalate()))
    assert _run(run.run_id).status == "ESCALATED"
    assert _task(pack, "N1").lifecycle == "READY"


def test_queue_promoted_when_case_cancelled(seeded):
    pack = seeded
    run = _queue_n1_during_alpha(pack)
    out = cancel_run(pack, "supervisor", _key(), CancelRun(run_id=run.run_id))
    assert out.status == "APPLIED"
    assert _task(pack, "N1").lifecycle == "READY"


def test_queue_promoted_when_event_stales_case(seeded):
    pack = seeded
    run = _queue_n1_during_alpha(pack)
    body = EventReport(source_event_id="e1", event_type="DELAY", text="지연")
    out = receive_event(pack, "reporter", _key(), body)
    assert out.status == "APPLIED"
    assert _run(run.run_id).status == "STALE"
    assert _task(pack, "N1").lifecycle == "READY"
    # Hold가 있으므로 RECHECK는 재계획하지 않는다
    run_until_idle(pack, model_factory=_factory(pytest.fail))
    assert len(_runs()) == 1


def test_queue_order_and_withdraw_queued(seeded):
    pack = seeded
    run = _alpha_waiting(pack)
    assert _submit(pack, "N2").result_refs["queued"] is True
    assert _submit(pack, "N1").result_refs["queued"] is True
    with db.read() as conn:
        assert queued_task_ids(conn, pack.site_id) == ["N2", "N1"]  # 접수 순서
    ctx = _site(pack).context_version
    out = withdraw_task_request(pack, "planner_a", _key(), TaskWithdraw(task_id="N2"))
    assert out.status == "APPLIED" and out.result_refs["queued"] is True
    assert _site(pack).context_version == ctx  # 대기열 철회는 사실을 바꾸지 않는다
    assert _task(pack, "N2").lifecycle == "NEEDS_INFO"
    assert _run(run.run_id).wake_seq == 0
    cancel_run(pack, "supervisor", _key(), CancelRun(run_id=run.run_id))
    assert (_task(pack, "N1").lifecycle, _task(pack, "N2").lifecycle) == ("READY", "NEEDS_INFO")


# ── 철회와 열린 Case ───────────────────────────────────────────


def test_withdraw_other_request_wakes_open_case(seeded):
    """Case 밖의 Plan 밖 요청을 철회하면 열린 Run을 깨운다(고정 충돌이 사라짐, A.21 3)."""
    pack = seeded
    # 충돌 없는 요청 N1(11:00 시작) → RECONFIRM 후보만 생기고 Run은 없다
    assert _submit(pack, "N1", earliest_start=1560).status == "APPLIED"
    run_until_idle(pack, model_factory=_factory(pytest.fail))
    run = _alpha_waiting(pack)
    out = withdraw_task_request(pack, "planner_a", _key(), TaskWithdraw(task_id="N1"))
    assert out.status == "APPLIED"
    woke = _run(run.run_id)
    assert (woke.status, woke.wake_seq) == ("WAITING_HUMAN", 1)


def test_withdraw_case_request_stales_case(seeded):
    pack = seeded
    run = _alpha_waiting(pack)
    out = withdraw_task_request(pack, "planner_a", _key(), TaskWithdraw(task_id="A"))
    assert out.status == "APPLIED"
    ended = _run(run.run_id)
    assert (ended.status, ended.end_reason) == ("STALE", "WITHDRAW:A")
