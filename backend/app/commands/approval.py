"""ApproveAndCommit·WAIVE·구조화 거절 (설계서 §9.1–§9.3, 부록 A.14).

NOT_AUTHORIZED와 CANDIDATE_NOT_FOUND는 단독으로 반환하고, 나머지 사유는 해당하는 것을 모두 반환한다.
STALE은 STALE_PLAN·STALE_CONTEXT로만 보고한다(승인 8단계는 item만 본 상태로 판정).
"""

import sqlite3

from pydantic import Field

from app.commands.service import Body, CommandContext, CommandOutcome, Result, run_command
from app.domain.ids import new_id
from app.domain.models import Axis, Candidate, FeedbackConstraint, Plan, Validation
from app.packs.loader import LoadedPack
from app.store.repos.consultations import CandidateState, candidate_state, consultation_view
from app.store.repos.decisions import insert_constraint, insert_decision
from app.store.repos.events import list_active_holds
from app.store.repos.plans import get_plan_by_candidate, insert_plan
from app.store.repos.records import get_candidate, list_validations
from app.store.repos.runs import ACTIVE, end_run, run_for_solver_result
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


# ── ApproveAndCommit (§9.1) ────────────────────────────────────


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
    # 후보를 만든 Replanning Run을 SUCCEEDED로 (§11.3(7), A.16). RECONFIRM 후보에는 Run이 없다.
    run_id = None
    if candidate.solver_result_id is not None:
        run_id = run_for_solver_result(tx, candidate.solver_result_id)
        if run_id is not None and not end_run(
            tx, run_id, "SUCCEEDED", f"COMMITTED:{plan_revision}", ACTIVE
        ):
            run_id = None
    r.refs = {
        "plan_revision": plan_revision,
        "decision_id": decision_id,
        "succeeded_run_id": run_id,
    }
    return r


def approve_and_commit(
    pack: LoadedPack, actor_id: str, idempotency_key: str, body: ApproveRequest
) -> CommandOutcome:
    return run_command(pack, "APPROVE_AND_COMMIT", actor_id, idempotency_key, body, _approve)


# ── WAIVE (§9.3-4) ─────────────────────────────────────────────


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
    return r


def waive(
    pack: LoadedPack, actor_id: str, idempotency_key: str, body: WaiveRequest
) -> CommandOutcome:
    return run_command(pack, "WAIVE", actor_id, idempotency_key, body, _waive)


# ── 구조화 거절 (§9.2) ─────────────────────────────────────────


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
    return r


def reject_candidate(
    pack: LoadedPack, actor_id: str, idempotency_key: str, body: RejectRequest
) -> CommandOutcome:
    return run_command(pack, "REJECT_CANDIDATE", actor_id, idempotency_key, body, _reject)
