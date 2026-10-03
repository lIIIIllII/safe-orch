"""ApproveAndCommit·WAIVE·구조화 거절.

NOT_AUTHORIZED와 CANDIDATE_NOT_FOUND는 단독으로 반환하고, 나머지 사유는 해당하는 것을 모두 반환한다.
STALE은 STALE_PLAN·STALE_CONTEXT로만 보고한다(승인 8단계는 item만 본 상태로 판정).
"""

import sqlite3

from pydantic import Field

from app.commands.service import Body, CommandContext, CommandOutcome, Result, run_command
from app.domain.ids import new_id
from app.domain.models import Axis, Candidate, FeedbackConstraint, Plan, Validation
from app.packs.loader import LoadedPack
from app.store.repos.cases import deliver_event, end_candidate_runs, register_recheck, wake_run
from app.store.repos.consultations import CandidateState, candidate_state, consultation_view
from app.store.repos.decisions import insert_constraint, insert_decision
from app.store.repos.events import list_active_holds
from app.store.repos.plans import get_plan_by_candidate, insert_plan
from app.store.repos.records import get_candidate, list_validations
from app.store.repos.runs import get_run, run_for_solver_result
from app.store.repos.site import bump_context_version, bump_plan_revision
from app.store.repos.tasks import list_current_tasks

REJECT_REASONS = (
    "TASK_IMMOVABLE",
    "RESOURCE_UNAVAILABLE",
    "TIME_WINDOW_UNACCEPTABLE",
    "PREFERENCE",
    "OTHER",
)


class ApproveRequest(Body):
    candidate_id: str
    validation_id: str
    expected_context_version: int


class WaiveRequest(Body):
    candidate_id: str
    task_ids: tuple[str, ...] = Field(min_length=1)
    comment: str


class RejectRequest(Body):
    candidate_id: str
    validation_id: str
    reason_code: str
    target_task_ids: tuple[str, ...] = ()
    axes: tuple[Axis, ...] = ()
    comment: str = ""


def _pass_validation(
    tx: sqlite3.Connection, site_id: str, candidate_id: str, validation_id: str | None = None
) -> Validation | None:
    """validation_id가 있으면 그 PASS, 없으면 가장 최근 PASS."""
    passes = [
        v
        for v in list_validations(tx, site_id, candidate_id)
        if v.status == "PASS" and (validation_id is None or v.validation_id == validation_id)
    ]
    return passes[-1] if passes else None


def _stale(r: Result, state: CandidateState) -> None:
    if state.rejected:
        r.reject("CANDIDATE_REJECTED")
    if state.stale_plan:
        r.reject("STALE_PLAN")
    if state.stale_context:
        r.reject("STALE_CONTEXT")


def _load(
    tx: sqlite3.Connection, ctx: CommandContext, candidate_id: str, r: Result
) -> Candidate | None:
    candidate = get_candidate(tx, ctx.site_id, candidate_id)
    if candidate is None:
        r.reject("CANDIDATE_NOT_FOUND")
        return None
    if not ctx.has_role("SUPERVISOR"):
        r.reject("NOT_AUTHORIZED")
        return None
    return candidate


# ── ApproveAndCommit ────────────────────────────────────


def _approve(tx: sqlite3.Connection, ctx: CommandContext, body: ApproveRequest) -> Result:
    r = Result()
    site_id, site = ctx.site_id, ctx.site
    # 2. 이 후보로 확정된 Plan이 있으면 버전 검사보다 먼저 기존 Plan을 돌려준다
    existing = get_plan_by_candidate(tx, site_id, body.candidate_id)
    if existing is not None:
        r.replayed = True
        r.refs = {"plan_revision": existing.plan_revision}
        return r
    candidate = _load(tx, ctx, body.candidate_id, r)
    if candidate is None:
        return r
    state = candidate_state(tx, site_id, candidate)
    if state.rejected:
        r.reject("CANDIDATE_REJECTED")
    validation = _pass_validation(tx, site_id, candidate.candidate_id, body.validation_id)
    if validation is None:
        r.reject("VALIDATION_NOT_PASS")
    if state.stale_plan:
        r.reject("STALE_PLAN")
    if state.stale_context or body.expected_context_version != site.context_version:
        r.reject("STALE_CONTEXT")
    if list_active_holds(tx, site_id):
        r.reject("HOLD_ACTIVE")
    view = consultation_view(tx, site_id, candidate.candidate_id)
    if view is None or view.items_status != "COMPLETE":
        r.reject("CONSULTATION_INCOMPLETE")
    if r.reason_codes or validation is None:
        return r

    plan_revision = bump_plan_revision(tx, site_id)
    insert_plan(
        tx,
        site_id,
        Plan(
            plan_revision=plan_revision,
            assignments=candidate.assignments,
            candidate_id=candidate.candidate_id,
            committed_context_version=site.context_version,
        ),
    )
    decision_id = new_id("dec")
    insert_decision(
        tx,
        site_id,
        decision_id,
        "APPROVE",
        candidate.candidate_id,
        validation.validation_id,
        ctx.actor_id,
        site.context_version,
    )
    # 이 후보의 협의 Run이 열려 있으면 끝낸다. 확정 뒤 남은 요청을 위해 RECHECK(plan 키)를 등록한다.
    # 승인 결과는 사건으로 메인에게 간다. 확정 뒤 통지는 메인이 부른다 (AG-26).
    end_candidate_runs(
        tx, ctx.pack, candidate.candidate_id, "SUCCEEDED", f"COMMITTED:{plan_revision}"
    )
    register_recheck(tx, site_id, {"kind": "COMMIT", "plan_revision": plan_revision})
    _deliver_decided(tx, ctx, candidate, decision_id, "APPROVE")
    r.refs = {"plan_revision": plan_revision, "decision_id": decision_id}
    return r


def approve_and_commit(
    pack: LoadedPack, actor_id: str, idempotency_key: str, body: ApproveRequest
) -> CommandOutcome:
    return run_command(pack, "APPROVE_AND_COMMIT", actor_id, idempotency_key, body, _approve)


# ── WAIVE ─────────────────────────────────────────────


def _waive(tx: sqlite3.Connection, ctx: CommandContext, body: WaiveRequest) -> Result:
    r = Result()
    candidate = _load(tx, ctx, body.candidate_id, r)
    if candidate is None:
        return r
    if not body.comment.strip():
        r.reject("COMMENT_REQUIRED")
    _stale(r, candidate_state(tx, ctx.site_id, candidate))
    view = consultation_view(tx, ctx.site_id, candidate.candidate_id)
    validation = _pass_validation(tx, ctx.site_id, candidate.candidate_id)
    if view is None or validation is None:
        r.reject("CONSULTATION_NOT_FOUND")
    else:
        for tid in body.task_ids:
            status = view.item_status.get(tid)
            if status is None:
                r.reject("ITEM_NOT_FOUND")
            elif status != "PENDING":
                r.reject("ITEM_NOT_WAIVABLE")
    if r.reason_codes or validation is None:
        return r

    decision_id = new_id("dec")
    insert_decision(
        tx,
        ctx.site_id,
        decision_id,
        "WAIVE",
        candidate.candidate_id,
        validation.validation_id,
        ctx.actor_id,
        ctx.site.context_version,
        target_task_ids=tuple(sorted(set(body.task_ids))),
        comment=body.comment,
    )
    r.refs = {"decision_id": decision_id}
    # 협의 항목이 사람 수용으로 닫혔다. 답을 기다리던 협의 Run이 다시 관찰하게 깨운다
    for (run_id,) in tx.execute(
        "SELECT run_id FROM agent_run WHERE site_id = ? AND agent_type = 'COORDINATION'"
        " AND status = 'WAITING_HUMAN' AND json_extract(input_ref, '$.candidate_id') = ?",
        (ctx.site_id, candidate.candidate_id),
    ).fetchall():
        wake_run(tx, ctx.site_id, run_id)
    return r


def waive(
    pack: LoadedPack, actor_id: str, idempotency_key: str, body: WaiveRequest
) -> CommandOutcome:
    return run_command(pack, "WAIVE", actor_id, idempotency_key, body, _waive)


# ── 구조화 거절 ─────────────────────────────────────────


def _reject(tx: sqlite3.Connection, ctx: CommandContext, body: RejectRequest) -> Result:
    r = Result()
    candidate = _load(tx, ctx, body.candidate_id, r)
    if candidate is None:
        return r
    site_id = ctx.site_id
    validation = _pass_validation(tx, site_id, candidate.candidate_id, body.validation_id)
    if validation is None:
        r.reject("VALIDATION_NOT_PASS")
    _stale(r, candidate_state(tx, site_id, candidate))
    if body.reason_code not in REJECT_REASONS:
        r.reject("INVALID_REASON_CODE")
    immovable = body.reason_code == "TASK_IMMOVABLE"
    if immovable and not (body.target_task_ids and body.axes):
        r.reject("TARGET_REQUIRED")
    ready = {t.task_id for t in list_current_tasks(tx, site_id, ctx.pack) if t.lifecycle == "READY"}
    if any(t not in ready for t in body.target_task_ids):
        r.reject("TASK_NOT_FOUND")
    if r.reason_codes or validation is None:
        return r

    targets = tuple(sorted(set(body.target_task_ids)))
    axes = tuple(sorted(set(body.axes)))
    decision_id = new_id("dec")
    insert_decision(
        tx,
        site_id,
        decision_id,
        "REJECT",
        candidate.candidate_id,
        validation.validation_id,
        ctx.actor_id,
        ctx.site.context_version,
        reason_code=body.reason_code,
        target_task_ids=targets,
        axes=axes,
        comment=body.comment,
    )
    constraint_ids = []
    if immovable:
        context_version = bump_context_version(tx, site_id)
        for tid in targets:
            fc = FeedbackConstraint(
                constraint_id=new_id("fc"),
                task_id=tid,
                frozen_axes=axes,
                source_type="DECISION",
                source_id=decision_id,
            )
            insert_constraint(tx, site_id, fc, context_version)
            constraint_ids.append(fc.constraint_id)
    r.refs = {"decision_id": decision_id, "constraint_ids": constraint_ids}
    r.audit_reason = body.reason_code
    # 후보가 거절되었으므로 그 후보의 협의 Run을 끝낸다(보낸 요청 정리)
    end_candidate_runs(
        tx, ctx.pack, candidate.candidate_id, "STALE", f"REJECTED:{candidate.candidate_id}"
    )
    # 거절 결과는 사건으로 메인에게 간다. 다시 재계획할지 이관할지는 메인이 판단한다 (AG-25)
    _deliver_decided(tx, ctx, candidate, decision_id, "REJECT")
    return r


def _deliver_decided(
    tx: sqlite3.Connection,
    ctx: CommandContext,
    candidate: Candidate,
    decision_id: str,
    decision: str,
) -> None:
    """후보 승인·거절 결과는 사건이다. 원래 Case는 그 후보를 만든 Run의 Case다."""
    maker_id = (
        run_for_solver_result(tx, candidate.solver_result_id)
        if candidate.solver_result_id
        else None
    )
    maker = get_run(tx, maker_id) if maker_id else None
    deliver_event(
        tx,
        ctx.pack,
        "CANDIDATE_DECIDED",
        f"CANDIDATE_DECIDED:{decision_id}",
        {"candidate_id": candidate.candidate_id, "decision_id": decision_id, "type": decision},
        None if maker is None else maker.case_id,
    )


def reject_candidate(
    pack: LoadedPack, actor_id: str, idempotency_key: str, body: RejectRequest
) -> CommandOutcome:
    return run_command(pack, "REJECT_CANDIDATE", actor_id, idempotency_key, body, _reject)
