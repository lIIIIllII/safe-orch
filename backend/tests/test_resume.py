"""대기와 재개·거절 후 재탐색·대기열·담당자 확인.

메인이 전문 Agent를 부른다(사건 → 메인 자동 시작을 켠다). 메인과 Coordination은 스크립트의 기본 응답으로
돌고, 이 파일의 스크립트는 Replanning의 응답이다. Replanning은 검증까지만 살고, 거절 뒤에는 메인이
같은 Case에서 새 Run으로 다시 부른다.

담당자 확인은 Replanning이 하지 않는다: 막힌 결과의 need를 메인이 Coordination 사전 확인으로 넘긴다
(AG-09). 스크립트의 막힌 결과에는 길이 없으므로 메인의 기본 응답은 서버가 붙인 열 수 있는 것으로 부른다.

1단계: T33·T36–T39·T41·T42, 대기열(QUEUED). 2단계: 기본안 B E2E, T02·T17·T23–T26·T38(답변)·T40,
LIST·TRY 사용 조건과 사전 확인, Consent 복사, 거절한 값, state inbox.
"""

import threading
import uuid
from types import SimpleNamespace

import pytest
from conftest import choose
from scripted import (
    Router,
    blocked,
    call,
    escalate,
    solve,
)

from app.api.state import build_state
from app.commands.approval import (
    ApproveRequest,
    RejectRequest,
    WaiveRequest,
    approve_and_commit,
    reject_candidate,
    waive,
)
from app.commands.events import EventReport, receive_event
from app.commands.messages import (
    ReplyRequest,
    reply_message,
)
from app.commands.pins import TaskRef, pin_task
from app.commands.runs import CancelRun, cancel_run
from app.commands.task_request import (
    TaskRequestForm,
    TaskWithdraw,
    submit_task_request,
    withdraw_task_request,
)
from app.coordinator.dispatcher import run_until_idle
from app.store import db
from app.store.repos.cases import claim_resume, queued_task_ids, wake_run
from app.store.repos.dispatch import list_jobs, register_job
from app.store.repos.messages import get_message, get_proposal
from app.store.repos.records import (
    list_validations,
)
from app.store.repos.runs import get_run, list_steps
from app.store.repos.site import get_site
from app.store.repos.snapshots import build_snapshot_content
from app.store.repos.tasks import list_current_tasks

pytestmark = pytest.mark.usefixtures("main_on")

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
    """Replanning 스크립트. Run이 깨어날 때마다 모델이 새로 만들어져도 응답은 순서대로 이어진다."""
    return Router(replanning=replies).factory()


def _site(pack):
    with db.read() as conn:
        return get_site(conn, pack.site_id)


def _runs(agent_type=None):
    with db.read() as conn:
        ids = [r[0] for r in conn.execute("SELECT run_id FROM agent_run ORDER BY rowid")]
        runs = [get_run(conn, rid) for rid in ids]
    return [r for r in runs if agent_type is None or r.agent_type == agent_type]


def _last(agent_type="REPLANNING"):
    """그 종류의 가장 최근 Run."""
    return _runs(agent_type)[-1]


def _cand(run_id):
    """그 Replanning Run이 마지막으로 등록한 후보."""
    ids = [(s["tool_result"] or {}).get("candidate_id") for s in _steps(run_id)]
    return [c for c in ids if c][-1]


def _resumes(pack, run_id, status=None):
    return [
        j
        for j in _jobs(pack, "RESUME_RUN")
        if j["run_id"] == run_id and status in (None, j["status"])
    ]


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
    """폼 A → 메인 → Replanning(L0·L1 → Alpha → 검증 → DONE) → 협의 Run이 C 담당자 답을 기다린다.

    run_id = 후보를 만든 Replanning Run, wait_ref = Alpha 후보, main_id·consult_id = 메인과 협의 Run."""
    assert _submit_a(pack).status == "APPLIED"
    run_until_idle(pack, model_factory=_factory(solve("L0"), solve("L1")))
    choose(pack)  # Supervisor가 고른 안만 협의한다 (AG-29)
    run_until_idle(pack, model_factory=_factory())
    run, main, consult = _last(), _last("MAIN"), _last("COORDINATION")
    assert (run.status, run.end_reason) == ("SUCCEEDED", "RETURN_DONE")
    assert (main.status, main.wait_kind, main.wait_ref) == (
        "WAITING_HUMAN",
        "CHILD_RUN",
        consult.run_id,
    )
    assert consult.status == "WAITING_HUMAN" and consult.wait_generation == 1
    return SimpleNamespace(
        run_id=run.run_id,
        wait_ref=_cand(run.run_id),
        main_id=main.run_id,
        consult_id=consult.run_id,
    )


def _validation(pack, cand_id):
    with db.read() as conn:
        [v] = list_validations(conn, pack.site_id, cand_id)
    return v


def _reject(pack, cand_id, reason="PREFERENCE", targets=(), comment="", key=None):
    body = RejectRequest(
        candidate_id=cand_id,
        validation_id=_validation(pack, cand_id).validation_id,
        reason_code=reason,
        target_task_ids=targets,
        comment=comment,
    )
    return reject_candidate(pack, "supervisor", key or _key(), body)


def _reject_demo(pack, cand_id):
    """기본안 B: scenario의 거절(C를 옮길 수 없다) 뒤 Supervisor가 C를 고정한다 (AG-27)."""
    x = pack.demo_rejections[0]
    out = _reject(pack, cand_id, x.reason_code, x.target_task_ids, x.comment)
    assert pin_task(pack, "supervisor", _key(), TaskRef(task_id="C")).status == "APPLIED"
    return out


def _approve(pack, cand_id):
    body = ApproveRequest(
        candidate_id=cand_id,
        validation_id=_validation(pack, cand_id).validation_id,
        expected_context_version=_site(pack).context_version,
    )
    return approve_and_commit(pack, "supervisor", _key(), body)


# ── 기본안 B ───────────────────────────────────────────────────


def test_plan_b_reject_and_pin_recalls_replanning(seeded):
    """Alpha를 거절하고 Supervisor가 C를 고정 → Context +1, 메인이 깨어나 재계획을 다시 부른다 → 새 Run이 같은
    Case의 이전 계산과 C 고정을 본다 → 미시도 범위 없음.

    C 고정으로 L1·L2가 L0와 같은 탐색(같은 실효 탐색 키)이 되므로 계산을 반복하지 않는다. 막힌 결과에
    서버가 열 수 있는 것을 붙이고, 메인이 그 need로 Coordination 사전 확인을 부른다.
    """
    pack = seeded
    run = _alpha_waiting(pack)
    ctx = _site(pack).context_version
    out = _reject_demo(pack, run.wait_ref)
    assert out.status == "APPLIED"
    # 거절로 협의 Run이 끝나 메인이 깨어난다. 거절 결과와 고정은 메인의 사건이다
    woke = _run(run.main_id)
    assert (_site(pack).context_version, woke.wake_seq, woke.status) == (
        ctx + 1,
        woke.handled_wake_seq + 1,  # 고정은 재개를 기다리는 메인을 한 번 더 깨우지 않는다
        "WAITING_HUMAN",
    )
    with db.read() as conn:
        kinds = [r[0] for r in conn.execute("SELECT kind FROM case_event ORDER BY seq")]
    assert kinds[-2:] == ["CANDIDATE_DECIDED", "TASK_PINNED"]
    assert _run(run.consult_id).end_reason == f"REJECTED:{run.wait_ref}"
    [resume] = _resumes(pack, run.main_id, "PENDING")
    assert resume["dedupe_key"] == f"RESUME_RUN:{run.main_id}:{woke.wait_generation}"

    run_until_idle(pack, model_factory=_factory(escalate()))
    first, second = _runs("REPLANNING")
    assert (second.status, second.end_reason) == ("BLOCKED", "RETURN_BLOCKED")
    assert (second.case_id, second.parent_run_id) == (first.case_id, run.main_id)
    assert (first.solver_calls_used, second.solver_calls_used) == (2, 0)
    [s3] = _steps(second.run_id)
    obs = s3["observation"]
    assert obs["rejections"] == [
        {
            "candidate_id": run.wait_ref,
            "reason_code": "TIME_WINDOW_UNACCEPTABLE",
            "target_task_ids": ["C"],
            "quoted_comment": "작업발판 연계 공정 확정",
        }
    ]
    facts = obs["rejection_facts"]
    assert (facts["count"], facts["untried_remaining"]) == (1, False)
    # 이전 계산은 Case 단위다: 앞 Run의 L0·L1이 보인다
    assert [(a["scope_level"], a["this_run"]) for a in obs["attempts"]] == [
        ("L0", False),
        ("L1", False),
    ]
    assert obs["latest_validation"]["live"] is False
    pinned = {t["task_id"]: t["pinned"] for t in obs["acting_tasks"]}
    assert pinned["C"] == {"pinned_by": "supervisor", "by_role": "SUPERVISOR"}
    assert pinned["A"] is None
    assert obs["untried_levels"] == []
    assert _names(s3) == [  # 사람 도구는 없다
        "SOLVE_WITH_CONDITIONS",
        "LIST_ASSIGNABLE_RESOURCES",
        "RETURN_RESULT",
    ]
    assert s3["action"]["name"] == "RETURN_RESULT"
    # 열 수 있는 것: 권한 때문에 제외된 자원과, 모든 범위에서 해가 없는 요청 작업의 시간창이 남는다
    openers = [
        {
            "need_id": f"{second.run_id}:s:0",
            "kind": "FACT_CHANGE",
            "field": "PERMISSION",
            "resource_id": "B-CR-01",
        },
        {
            "need_id": f"{second.run_id}:s:1",
            "kind": "FACT_CHANGE",
            "field": "WINDOW",
            "task_id": "A",
        },
    ]
    assert (s3["tool_result"]["paths"], s3["tool_result"]["openers"]) == ([], openers)
    # 사전 확인은 없다: 메인은 막힌 결과를 받고 담당자에게 묻는 Run을 부르지 않는다
    assert _runs("COORDINATION") == [] or all(
        r.input_ref["phase"] != "ASK" for r in _runs("COORDINATION")
    )
    woke = _steps(run.main_id)[-1]["observation"]
    unit = next(u for u in woke["groups"][0]["units"] if u["unit_id"] == "UA")
    assert (unit["last_result"]["paths"], unit["last_result"]["openers"]) == ([], openers)
    assert not [c for c in woke["calls"] if c["agent"] == "COORDINATION"]


def _reply(pack, message_id, decision="ACCEPT", actor="planner_a", key=None, **kw):
    body = ReplyRequest(message_id=message_id, decision=decision, **kw)
    return reply_message(pack, actor, key or _key(), body)


def _message(pack, message_id):
    with db.read() as conn:
        return get_message(conn, pack.site_id, message_id)


def _proposal(pack, proposal_id):
    with db.read() as conn:
        return get_proposal(conn, pack.site_id, proposal_id)


def _names(step):
    return [t["function"]["name"] for t in step["available_actions"]]


def _enum(step, name, arg):
    tool = next(t["function"] for t in step["available_actions"] if t["function"]["name"] == name)
    return tool["parameters"]["properties"][arg]["enum"]


# ── T33: 제약 없는 거절 ─────────────────────────────────────────


def _n1_waiting(pack):
    assert _submit(pack, "N1").status == "APPLIED"
    run_until_idle(pack, model_factory=_factory(solve("L0")))
    run = _last()
    assert run.status == "SUCCEEDED"
    return SimpleNamespace(
        run_id=run.run_id, wait_ref=_cand(run.run_id), main_id=_last("MAIN").run_id
    )


def test_t33_reject_without_constraint_recalls_and_blocks_same_assignments(seeded):
    pack = seeded
    run = _n1_waiting(pack)
    ctx = _site(pack).context_version
    assert _reject(pack, run.wait_ref, "PREFERENCE", comment="오후가 좋다").status == "APPLIED"
    assert _site(pack).context_version == ctx  # 제약 없는 거절은 Context를 바꾸지 않는다
    # 메인이 다시 부른다(거절로 사실 지문이 바뀌었다). L2는 C까지 넣지만 같은 배정(N1 11:00)이 나온다
    # → Guard가 후보를 만들지 않는다
    run_until_idle(pack, model_factory=_factory(solve("L2"), escalate()))
    second = _last()
    assert second.run_id != run.run_id
    s2, s3 = _steps(second.run_id)
    assert s2["observation"]["rejections"][0]["quoted_comment"] == "오후가 좋다"
    assert s2["observation"]["rejection_facts"]["count"] == 1
    assert s2["guard"] == {"verdict": "REJECTED", "reason_code": "DUPLICATE_REJECTED"}
    assert (s2["result_kind"], s2["tool_result"]["candidate_id"]) == ("CONTINUE", None)
    assert s3["action"]["name"] == "RETURN_RESULT"
    with db.read() as conn:
        n = conn.execute("SELECT COUNT(*) FROM candidate").fetchone()[0]
    assert n == 1


def test_cv13_rejected_candidate_search_stays_tried_after_context_change(seeded):
    """거절된 후보의 탐색은 현장 버전이 바뀌어도 다시 계산하지 않는다(같은 배정이 나온다).
    무효가 된 후보의 탐색만 미시도로 돌아온다 (CV-13)."""
    pack = seeded
    run = _n1_waiting(pack)
    assert _reject(pack, run.wait_ref, "PREFERENCE").status == "APPLIED"
    run_until_idle(pack, model_factory=_factory(escalate()))  # 다시 부른 Run은 막힘, 메인은 이관
    first = _steps(_last().run_id)[0]["observation"]
    assert "L0" not in first["untried_levels"]
    # 관계없는 변화로 현장 버전이 오른다. 거절된 후보는 무효이기도 하지만 탐색은 열리지 않는다
    with db.write() as tx:
        tx.execute("UPDATE site SET context_version = context_version + 1")
    from app.store.repos.runs import tried_search_keys

    with db.read() as conn:
        keys = tried_search_keys(conn, pack.site_id, _run(run.run_id).case_id)
        [l0] = conn.execute(
            "SELECT s.search_key FROM solver_job j JOIN search_spec s"
            " ON s.search_spec_id = j.search_spec_id WHERE j.run_id = ?",
            (run.run_id,),
        ).fetchone()
    assert l0 in keys


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
    [run] = _runs("REPLANNING")
    s2 = _steps(run.run_id)[1]
    assert (s2["result_kind"], s2["guard"]["reason_code"]) == ("CONTINUE", "NEW_CHANGE_BEFORE_WAIT")
    assert (run.status, run.wait_generation, run.handled_wake_seq) == ("BLOCKED", 0, 1)
    assert _resumes(pack, run.run_id) == []


def test_t37_old_generation_resume_is_void(seeded):
    pack = seeded
    waiting = _alpha_waiting(pack).consult_id  # 답을 기다리는 협의 Run, wait_generation 1
    with db.write() as tx:
        register_job(
            tx,
            pack.site_id,
            "RESUME_RUN",
            f"RESUME_RUN:{waiting}:0",
            run_id=waiting,
            wait_generation=0,
        )
    steps = len(_steps(waiting))
    run_until_idle(pack, model_factory=_factory())
    assert _run(waiting).status == "WAITING_HUMAN" and len(_steps(waiting)) == steps
    [job] = _resumes(pack, waiting)
    assert job["status"] == "DONE"


def test_t38_same_reply_replayed_without_wake(seeded):
    """같은 키·같은 본문 재전송은 REPLAYED, wake·job 추가 없음. 같은 키 다른 본문은 거절."""
    pack = seeded
    run = _alpha_waiting(pack)
    key = _key()
    assert _reject(pack, run.wait_ref, "PREFERENCE", key=key).status == "APPLIED"
    wake = _run(run.main_id).wake_seq
    again = _reject(pack, run.wait_ref, "PREFERENCE", key=key)
    assert again.status == "REPLAYED"
    assert _run(run.main_id).wake_seq == wake
    assert len(_resumes(pack, run.main_id, "PENDING")) == 1
    other = _reject(pack, run.wait_ref, "OTHER", key=key)
    assert other.reason_codes == ("IDEMPOTENCY_MISMATCH",)


def test_t39_resume_claimed_once(seeded):
    pack = seeded
    waiting = _alpha_waiting(pack).consult_id
    with db.write() as tx:
        assert claim_resume(tx, waiting, 1) is True
    with db.write() as tx:
        assert claim_resume(tx, waiting, 1) is False


def test_t39_concurrent_claim_from_two_connections(seeded):
    pack = seeded
    waiting = _alpha_waiting(pack).consult_id
    barrier = threading.Barrier(2)
    results = []

    def claim():
        barrier.wait()
        with db.write() as tx:
            results.append(claim_resume(tx, waiting, 1))
        db.close()

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [False, True]


def test_t41_two_changes_one_resume(seeded):
    pack = seeded
    waiting = _alpha_waiting(pack).consult_id
    with db.write() as tx:
        assert wake_run(tx, pack.site_id, waiting)
        assert wake_run(tx, pack.site_id, waiting)
    assert len(_resumes(pack, waiting, "PENDING")) == 1 and _run(waiting).wake_seq == 2
    run_until_idle(pack, model_factory=_factory())
    resumed = _run(waiting)
    # 두 변화를 한 번의 재개로 본다. 답이 아직 없으므로 다시 기다린다
    assert (resumed.handled_wake_seq, resumed.status, resumed.wait_generation) == (
        2,
        "WAITING_HUMAN",
        2,
    )
    assert _steps(waiting)[-1]["observed_wake_seq"] == 2


def test_t42_event_while_waiting_stales_and_resume_is_void(seeded):
    pack = seeded
    waiting = _alpha_waiting(pack).consult_id
    with db.write() as tx:
        wake_run(tx, pack.site_id, waiting)  # PENDING RESUME
    body = EventReport(source_event_id="e1", event_type="OTHER", text="지연")
    assert receive_event(pack, "reporter", _key(), body).status == "APPLIED"
    assert _run(waiting).status == "STALE"
    steps = len(_steps(waiting))
    run_until_idle(pack, model_factory=_factory())
    assert _run(waiting).status == "STALE" and len(_steps(waiting)) == steps
    assert {j["status"] for j in _resumes(pack, waiting)} == {"DONE"}


# ── 대기열 ──────────────────────────────────────────


def test_form_during_open_case_is_queued_then_promoted_on_commit(seeded):
    pack = seeded
    run = _alpha_waiting(pack)
    ctx = _site(pack).context_version
    rechecks = len(_jobs(pack, "RECHECK"))
    wake = _run(run.main_id).wake_seq
    out = _submit(pack, "N1")
    assert out.status == "APPLIED" and out.result_refs["queued"] is True
    assert _site(pack).context_version == ctx  # 대기열은 사실이 아니다
    assert len(_jobs(pack, "RECHECK")) == rechecks
    n1 = _task(pack, "N1")
    assert (n1.lifecycle, n1.revision) == ("QUEUED", 1)
    with db.read() as conn:
        content = build_snapshot_content(conn, pack.site_id, pack)
    assert "N1" not in [t["task_id"] for t in content["tasks"]]
    assert _run(run.main_id).wake_seq == wake  # 폼은 열린 메인을 깨우지 않는다

    body = WaiveRequest(candidate_id=run.wait_ref, task_ids=("C",), comment="확인")
    assert waive(pack, "supervisor", _key(), body).status == "APPLIED"
    assert _approve(pack, run.wait_ref).status == "APPLIED"
    assert _task(pack, "N1").lifecycle == "QUEUED"  # 메인이 끝나야 대기열이 올라간다
    # 메인: 통지 → CLOSE → 대기열 1건 승격 → 새 메인이 올라온 요청을 요청자로 재계획한다
    run_until_idle(pack, model_factory=_factory(solve("L0")))
    assert (_run(run.main_id).status, _run(run.main_id).end_reason) == ("SUCCEEDED", "CLOSE")
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
    last = _jobs(pack, "RECHECK")[-1]
    assert last["dedupe_key"] == f"RECHECK:ctx{ctx + 1}:plan1"
    assert last["payload"]["cause"] == {"kind": "QUEUE", "task_id": "N1", "actor_id": "planner_a"}
    n1_run, n1_main = _last(), _last("MAIN")
    assert n1_main.run_id != run.main_id and n1_main.status == "WAITING_HUMAN"
    assert (n1_run.parent_run_id, n1_run.status, n1_run.acting_actor_id) == (
        n1_main.run_id,
        "SUCCEEDED",
        "planner_a",
    )


def _queue_n1_during_alpha(pack):
    run = _alpha_waiting(pack)
    assert _submit(pack, "N1").result_refs["queued"] is True
    return run


def test_queue_promoted_when_main_escalates(seeded):
    pack = seeded
    run = _queue_n1_during_alpha(pack)
    _reject(pack, run.wait_ref, "PREFERENCE")
    # 다시 부른 재계획이 막히면 메인이 이관한다. 대기열에서 1건이 올라가 새 메인이 받는다
    run_until_idle(pack, model_factory=_factory(escalate(), escalate()))
    assert _run(run.main_id).status == "ESCALATED"
    assert _task(pack, "N1").lifecycle == "READY"
    assert len(_runs("MAIN")) == 2


def test_queue_promoted_when_main_cancelled(seeded):
    pack = seeded
    run = _queue_n1_during_alpha(pack)
    out = cancel_run(pack, "supervisor", _key(), CancelRun(run_id=run.main_id))
    assert out.status == "APPLIED"
    assert _run(run.consult_id).status == "CANCELLED"  # 하위 Run도 같이 끝난다
    assert _task(pack, "N1").lifecycle == "READY"


def test_event_keeps_main_open_and_queue_waits(seeded):
    """신고는 열린 전문 Agent Run만 무효로 만든다. 메인이 남아 있으므로 대기열은 올라가지 않는다."""
    pack = seeded
    run = _queue_n1_during_alpha(pack)
    body = EventReport(source_event_id="e1", event_type="OTHER", text="지연")
    out = receive_event(pack, "reporter", _key(), body)
    assert out.status == "APPLIED"
    assert _run(run.consult_id).status == "STALE"
    assert _run(run.main_id).status == "WAITING_HUMAN"
    assert _task(pack, "N1").lifecycle == "QUEUED"
    # Hold가 있으므로 메인은 재계획을 부르지 못하고 Hold 해제를 기다린다
    run_until_idle(pack, model_factory=_factory())
    main = _run(run.main_id)
    assert (main.status, main.wait_kind) == ("WAITING_HUMAN", "HUMAN_DECISION")
    assert len(_runs("REPLANNING")) == 1


def test_queue_order_and_withdraw_queued(seeded):
    pack = seeded
    run = _alpha_waiting(pack)
    assert _submit(pack, "N2").result_refs["queued"] is True
    assert _submit(pack, "N1").result_refs["queued"] is True
    with db.read() as conn:
        assert queued_task_ids(conn, pack.site_id) == ["N2", "N1"]  # 접수 순서
    ctx, wake = _site(pack).context_version, _run(run.main_id).wake_seq
    out = withdraw_task_request(pack, "planner_a", _key(), TaskWithdraw(task_id="N2"))
    assert out.status == "APPLIED" and out.result_refs["queued"] is True
    assert _site(pack).context_version == ctx  # 대기열 철회는 사실을 바꾸지 않는다
    assert _task(pack, "N2").lifecycle == "NEEDS_INFO"
    assert _run(run.main_id).wake_seq == wake
    cancel_run(pack, "supervisor", _key(), CancelRun(run_id=run.main_id))
    assert (_task(pack, "N1").lifecycle, _task(pack, "N2").lifecycle) == ("READY", "NEEDS_INFO")


# ── 철회와 열린 Case ───────────────────────────────────────────


def test_withdraw_case_request_stales_child_and_wakes_main(seeded):
    pack = seeded
    run = _alpha_waiting(pack)
    wake = _run(run.main_id).wake_seq
    out = withdraw_task_request(pack, "planner_a", _key(), TaskWithdraw(task_id="A"))
    assert out.status == "APPLIED"
    ended = _run(run.consult_id)
    assert (ended.status, ended.end_reason) == ("STALE", "WITHDRAW:A")
    assert _run(run.main_id).wake_seq == wake + 1  # 하위 Run이 끝나 메인이 깨어난다


# ── 2단계: 답변·확인 명령 ────────────────────────


# ── 2단계: LIST·TRY 사용 조건과 사전 확인 ─────────────────


# ── 대기열 순서 ─────────────────────────────────────


def test_form_waits_behind_queue_while_reconfirm_pending(seeded):
    """재확인 후보의 승인을 기다리는 메인이 열려 있는 동안 새 폼은 대기열 뒤에 선다."""
    pack = seeded
    run = _alpha_waiting(pack)
    assert _submit(pack, "N1", earliest_start=1560).result_refs["queued"] is True
    assert _submit(pack, "N2").result_refs["queued"] is True
    body = WaiveRequest(candidate_id=run.wait_ref, task_ids=("C",), comment="확인")
    waive(pack, "supervisor", _key(), body)
    assert _approve(pack, run.wait_ref).status == "APPLIED"
    # 메인 CLOSE → N1 승격: 충돌 없음 → RECONFIRM 후보, 새 메인은 승인을 기다린다(재계획 없음)
    run_until_idle(pack, model_factory=_factory())
    with db.read() as conn:
        [reconfirm] = [
            r[0] for r in conn.execute("SELECT candidate_id FROM candidate WHERE kind='RECONFIRM'")
        ]
        assert queued_task_ids(conn, pack.site_id) == ["N2"]
    main = _last("MAIN")
    assert _task(pack, "N1").lifecycle == "READY" and len(_runs("REPLANNING")) == 1
    assert (main.status, main.wait_kind, main.wait_ref) == (
        "WAITING_HUMAN",
        "HUMAN_DECISION",
        reconfirm,
    )
    out = _submit(pack, "N4")
    assert out.status == "APPLIED" and out.result_refs["queued"] is True
    with db.read() as conn:
        assert queued_task_ids(conn, pack.site_id) == ["N2", "N4"]
    assert _approve(pack, reconfirm).status == "APPLIED"
    # 승인 뒤 메인 CLOSE → 먼저 접수된 N2가 올라간다
    run_until_idle(pack, model_factory=_factory(escalate()))
    assert _task(pack, "N2").lifecycle == "READY"
    with db.read() as conn:
        ready = [
            r[0]
            for r in conn.execute(
                "SELECT json_extract(ref, '$.task_id') FROM case_event"
                " WHERE kind = 'TASK_READY' ORDER BY seq"
            )
        ]
    assert ready[:3] == ["A", "N1", "N2"]


# ── state inbox ───────────────────────────────────────


# ── state: 화면용 값 ──────────────────────────────────


def test_state_shows_rejection_resume_count_and_queue(seeded):
    pack = seeded
    run = _alpha_waiting(pack)
    alpha = run.wait_ref
    assert _submit(pack, "N2").result_refs["queued"] is True
    assert _submit(pack, "N1").result_refs["queued"] is True
    with db.read() as conn:
        assert build_state(conn, pack, "supervisor")["task_queue"] == ["N2", "N1"]
    _reject_demo(pack, alpha)
    run_until_idle(
        pack,
        model_factory=_factory(
            call("LIST_ASSIGNABLE_RESOURCES", "A 자원 조회", task_id="A"),
            blocked(),
        ),
    )
    with db.read() as conn:
        state = build_state(conn, pack, "supervisor")
    view = next(c for c in state["candidates"] if c["candidate_id"] == alpha)
    rej = view["rejection"]
    assert (rej["reason_code"], rej["target_task_ids"], rej["actor_id"]) == (
        "TIME_WINDOW_UNACCEPTABLE",
        ["C"],
        "supervisor",
    )
    c = next(t for t in state["tasks"] if t["task_id"] == "C")
    assert (c["pin"]["pinned_by"], c["pin"]["by_role"]) == ("supervisor", "SUPERVISOR")
    assert all(c["rejection"] is None for c in state["candidates"] if c["candidate_id"] != alpha)
