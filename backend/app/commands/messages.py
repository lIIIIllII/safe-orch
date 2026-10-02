"""메시지 답변·제안 확인/폐기 (설계서 §9.4·§12, I-13, 부록 A.21 2).

reply(ACCEPT|DECLINE)와 proposals/{pid}/confirm·discard는 같은 처리(_answer)를 쓴다. 제안이 붙은
메시지면 ACCEPT = 확인, DECLINE = 폐기다. 검사 순서(단독 반환 규칙은 A.14):
① *_NOT_FOUND ② NOT_AUTHORIZED ③ 메시지 CANCELLED·제안 STALE → LATE(기록만, 도메인 변화·wake 없음)
④ 이미 답함: 같은 결정 REPLAYED, 다른 결정 ALREADY_ANSWERED ⑤ STALE_PROPOSAL ⑥ INVALID_VALUES.
comment는 Observation에 quoted_comment로만 들어간다(A.17).
"""

import sqlite3
from datetime import UTC, datetime
from typing import Any, Literal

from app.commands.service import Body, CommandContext, CommandOutcome, Result, run_command
from app.domain import fact_update
from app.domain.ids import new_id
from app.domain.models import Consent, FeedbackConstraint, FieldRecord, Movable
from app.packs.loader import LoadedPack
from app.store.repos.cases import copy_consents, end_candidate_runs, end_case_run, wake_run
from app.store.repos.consents import insert_consent
from app.store.repos.decisions import insert_constraint
from app.store.repos.events import get_hold
from app.store.repos.messages import (
    decide_proposal,
    get_message,
    get_proposal,
    message_for_proposal,
    set_message_reply,
)
from app.store.repos.records import get_candidate
from app.store.repos.runs import run_for_solver_result
from app.store.repos.site import bump_context_version
from app.store.repos.tasks import insert_task_revision, list_current_tasks

# ANSWER: 자유 텍스트 답 (A.26 3). API 본문(api/commands.ReplyBody)도 이 정의를 쓴다 (A.26 후속)
Decision = Literal["ACCEPT", "DECLINE", "ANSWER"]


class ReplyRequest(Body):
    message_id: str
    decision: Decision
    values: tuple[str, ...] | None = None  # 생략하면 allowed_values 전부
    comment: str = ""


class ProposalDecision(Body):
    proposal_id: str
    comment: str = ""


def _reply_record(ctx: CommandContext, decision: str, values: list[str], comment: str) -> dict:
    return {
        "decision": decision,
        "values": values,
        "comment": comment,
        "actor_id": ctx.actor_id,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def _answer(
    tx: sqlite3.Connection,
    ctx: CommandContext,
    message: dict[str, Any],
    proposal: dict[str, Any] | None,
    decision: str,
    values: tuple[str, ...] | None,
    comment: str,
) -> Result:
    """③–⑥ 검사와 효과. ①·②는 호출한 쪽이 한다."""
    r = Result()
    # 자유 텍스트 질문(제안 없는 QUESTION)에는 ANSWER만, 다른 메시지에는 ANSWER 불가 (A.26 3)
    free_text = message["type"] == "QUESTION" and proposal is None
    if free_text != (decision == "ANSWER"):
        r.reject("INVALID_DECISION")
        return r
    if decision == "ANSWER" and not comment.strip():
        r.reject("COMMENT_REQUIRED")
        return r
    site_id = ctx.site_id
    refs: dict[str, Any] = {
        "message_id": message["message_id"],
        "proposal_id": None if proposal is None else proposal["proposal_id"],
        "decision": decision,
    }
    # ③ 늦은 답: Run이 끝나 요청이 효력을 잃었다. 답은 기록하되 도메인 변화·wake 없음 (T40)
    if message["status"] in ("CANCELLED", "LATE") or (
        proposal is not None and proposal["status"] == "STALE"
    ):
        if message["status"] == "LATE":  # 이미 늦은 답을 기록했다: ④와 같은 규칙
            if (message["reply"] or {}).get("decision") != decision:
                r.reject("ALREADY_ANSWERED")
                return r
            r.replayed = True
        else:
            set_message_reply(
                tx,
                message["message_id"],
                "LATE",
                _reply_record(ctx, decision, list(values or ()), comment),
                ctx.site.context_version,
            )
        r.refs = {**refs, "late": True}
        return r
    # ④ 이미 답함: 같은 결정이면 기존 결과(효과 1회, T26), 다른 결정이면 거절
    if message["status"] == "ANSWERED":
        if (message["reply"] or {}).get("decision") != decision:
            r.reject("ALREADY_ANSWERED")
            return r
        r.replayed = True
        r.refs = {**refs, **((proposal or {}).get("result_ref") or {})}
        return r
    task = None
    allowed: list[str] = []
    if proposal is not None:
        # ⑤ 제안 이후 작업이 바뀌었다 (T25)
        task = next(
            (
                t
                for t in list_current_tasks(tx, site_id, ctx.pack)
                if t.task_id == proposal["target_task_id"]
            ),
            None,
        )
        if task is None or task.revision != proposal["base_task_revision"]:
            r.reject("STALE_PROPOSAL")
            return r
        if proposal["type"] == "FACT_UPDATE" and decision == "ACCEPT":
            # 사실 수정: 바꿀 필드의 현재 값 == old_value 전부 (A.25 3). Hold 조건은 origin EVENT만 (A.29 3)
            payload = proposal["payload"]
            if any(
                getattr(task, c["field"]) != c["old_value"] for c in fact_update.changes(payload)
            ):
                r.reject("STALE_PROPOSAL")
                return r
            if fact_update.rule(payload).needs_hold:
                hold = get_hold(tx, site_id, payload.get("hold_id") or "")
                if hold is None or hold["status"] != "ACTIVE":
                    r.reject("HOLD_NOT_ACTIVE")
                    return r
        # ⑥ values는 allowed_values의 비어 있지 않은 부분집합
        allowed = list(proposal["payload"].get("allowed_values", []))
        if (
            decision == "ACCEPT"
            and values is not None
            and (not values or not set(values) <= set(allowed))
        ):
            r.reject("INVALID_VALUES")
            return r

    # 변경 요청의 이견(DECLINE)에는 사유가 필요하다. 제약 초안의 근거가 된다 (A.24 5)
    if message["type"] == "CHANGE_REQUEST" and decision == "DECLINE" and not comment.strip():
        r.reject("COMMENT_REQUIRED")
        return r

    chosen = [v for v in allowed if values is None or v in values] if decision == "ACCEPT" else []
    context_version = ctx.site.context_version
    constraint = None
    fact = None
    if proposal is not None and decision == "ACCEPT" and proposal["type"] == "FACT_UPDATE":
        assert task is not None
        fact = _confirm_fact_update(tx, ctx, task, proposal, message["message_id"])
        context_version = fact.pop("context_version")
        decide_proposal(
            tx, proposal["proposal_id"], "CONFIRMED", ctx.actor_id, context_version, fact
        )
        refs.update(fact)
    elif (
        proposal is not None and decision == "ACCEPT" and proposal["type"] == "FEEDBACK_CONSTRAINT"
    ):
        constraint = _confirm_constraint(tx, ctx, proposal)
        context_version = constraint.pop("context_version")
        decide_proposal(
            tx, proposal["proposal_id"], "CONFIRMED", ctx.actor_id, context_version, constraint
        )
        refs.update(constraint)
    elif proposal is not None and decision == "ACCEPT":
        assert task is not None
        result_ref = _confirm_movability(tx, ctx, task, message["message_id"], chosen)
        context_version = result_ref.pop("context_version")
        decide_proposal(
            tx, proposal["proposal_id"], "CONFIRMED", ctx.actor_id, context_version, result_ref
        )
        refs.update(result_ref)
    elif proposal is not None:
        decide_proposal(tx, proposal["proposal_id"], "DISCARDED", ctx.actor_id, context_version)
    set_message_reply(
        tx,
        message["message_id"],
        "ANSWERED",
        _reply_record(ctx, decision, chosen, comment),
        context_version,
    )
    if fact is not None and fact_update.rule(proposal["payload"]).ends_run:
        # 사실 수정 확정(origin EVENT): Event Response Run은 할 일을 마쳤다(도메인 사실에 의한 종료, A.25 3).
        # origin OWNER는 끝내지 않고 아래에서 제안을 만든 Replanning Run을 깨운다 (A.29 3)
        assert proposal is not None
        end_case_run(
            tx,
            ctx.pack,
            message["run_id"],
            "SUCCEEDED",
            f"FACT_CONFIRMED:{proposal['proposal_id']}",
        )
    if constraint is not None:
        # 제약 확정: 후보가 무효가 되므로 협의 Run을 끝내고 후보의 Replanning Run을 깨운다 (A.24 5·6)
        assert proposal is not None
        candidate_id = proposal["payload"]["candidate_id"]
        end_candidate_runs(
            tx, ctx.pack, candidate_id, "STALE", f"CONSTRAINT:{constraint['constraint_id']}"
        )
        candidate = get_candidate(tx, site_id, candidate_id)
        replanning = (
            run_for_solver_result(tx, candidate.solver_result_id)
            if candidate is not None and candidate.solver_result_id
            else None
        )
        if replanning is not None:
            wake_run(tx, site_id, replanning)
        refs["replanning_run_id"] = replanning
    # 메시지를 만든 Run을 깨운다 (§11.3 표, A.21 3). 이미 끝났으면 아무것도 하지 않는다.
    woke = wake_run(tx, site_id, message["run_id"])
    r.refs = {**refs, "run_id": message["run_id"], "woke": woke}
    r.audit_reason = decision
    return r


def _confirm_fact_update(
    tx: sqlite3.Connection,
    ctx: CommandContext,
    task: Any,
    proposal: dict[str, Any],
    message_id: str,
) -> dict[str, Any]:
    """사실 수정 확정 (§18.2.3, A.25 3·A.29 3). 새 task revision(바꿀 필드 = 새 값) → Context +1.

    시간창이 바뀌었으므로 TIME Consent는 복사하지 않고 RESOURCE만 복사한다(A.14 C1). origin OWNER는
    담당자가 넓힌 창에 동의한 것이므로 새 TIME Consent(시작 범위 = 새 창, 출처 message:<mid>)를 만든다.
    critical field window의 확인 값도 새 값으로 바꾼다(출처 proposal:<id>. 바꾸지 않으면
    Validator C11이 CONFIRMED_VALUE_MISMATCH로 막는다, A.8·A.25).
    """
    payload = proposal["payload"]
    rule = fact_update.rule(payload)
    updates = {c["field"]: c["new_value"] for c in fact_update.changes(payload)}
    # 바꿀 수 있는 필드는 origin이 정한다. 제안은 서버가 만들므로 어긋나면 코드 오류다.
    assert set(updates) <= rule.fields, (fact_update.origin(payload), sorted(updates))
    revision = task.revision + 1
    fields = dict(task.fields)
    if "window" in fields:
        window = {**fields["window"].value, **updates}
        fields["window"] = FieldRecord(
            value=window, status="CONFIRMED", source_ref=f"proposal:{proposal['proposal_id']}"
        )
    new_task = task.model_copy(update={"revision": revision, **updates, "fields": fields})
    insert_task_revision(tx, ctx.site_id, new_task)
    context_version = bump_context_version(tx, ctx.site_id)
    consent_ids = copy_consents(
        tx, ctx.site_id, task.task_id, task.revision, revision, context_version, ("RESOURCE",)
    )
    if rule.new_time_consent:
        consent = Consent(
            consent_id=new_id("cns"),
            task_id=task.task_id,
            task_revision=revision,
            owner_actor_id=task.owner_actor_id,
            axis="TIME",
            scope={"start_min": new_task.earliest_start, "start_max": new_task.latest_start},
            source_ref=f"message:{message_id}",
        )
        insert_consent(tx, ctx.site_id, consent, context_version)
        consent_ids = [*consent_ids, consent.consent_id]
    return {
        "task_revision": revision,
        "consent_ids": consent_ids,
        "context_version": context_version,
    }


def _confirm_constraint(
    tx: sqlite3.Connection, ctx: CommandContext, proposal: dict[str, Any]
) -> dict[str, Any]:
    """제약 초안 확정 (§18.2.2, A.24 5). 이견을 낸 담당자가 확인했을 때만 제약이 생긴다(I-13).

    FeedbackConstraint(frozen_axes = 초안 축, source PROPOSAL) → context +1. 효과는 Supervisor
    구조화 거절(source DECISION)과 같다: 후보 STALE, Replanning 재탐색에서 Hard 제약.
    """
    context_version = bump_context_version(tx, ctx.site_id)
    fc = FeedbackConstraint(
        constraint_id=new_id("fc"),
        task_id=proposal["target_task_id"],
        frozen_axes=tuple(proposal["payload"]["axes"]),
        source_type="PROPOSAL",
        source_id=proposal["proposal_id"],
    )
    insert_constraint(tx, ctx.site_id, fc, context_version)
    return {"constraint_id": fc.constraint_id, "context_version": context_version}


def _confirm_movability(
    tx: sqlite3.Connection,
    ctx: CommandContext,
    task: Any,
    message_id: str,
    values: list[str],
) -> dict[str, Any]:
    """MOVABILITY 확인 (§9.4, A.21 2). 한 tx:

    새 task revision(movable.resource = true, 나머지 그대로) → 기존 Consent를 같은 source_ref로 복사
    (A.14 C1) + RESOURCE Consent [values](source_ref message:<mid>) → context +1.
    """
    site_id = ctx.site_id
    revision = task.revision + 1
    movable = Movable(time=task.movable.time, resource=True)
    insert_task_revision(
        tx, site_id, task.model_copy(update={"revision": revision, "movable": movable})
    )
    context_version = bump_context_version(tx, site_id)
    consent_ids = copy_consents(tx, site_id, task.task_id, task.revision, revision, context_version)
    consent = Consent(
        consent_id=new_id("cns"),
        task_id=task.task_id,
        task_revision=revision,
        owner_actor_id=task.owner_actor_id,
        axis="RESOURCE",
        scope={"resource_ids": values},
        source_ref=f"message:{message_id}",
    )
    insert_consent(tx, site_id, consent, context_version)
    return {
        "task_revision": revision,
        "consent_ids": [*consent_ids, consent.consent_id],
        "context_version": context_version,
    }


# ── reply ──────────────────────────────────────────────────────


def _reply(tx: sqlite3.Connection, ctx: CommandContext, body: ReplyRequest) -> Result:
    message = get_message(tx, ctx.site_id, body.message_id)
    if message is None:
        r = Result()
        r.reject("MESSAGE_NOT_FOUND")
        return r
    if ctx.actor_id != message["to_actor_id"]:
        r = Result()
        r.reject("NOT_AUTHORIZED")
        return r
    proposal = (
        get_proposal(tx, ctx.site_id, message["proposal_id"]) if message["proposal_id"] else None
    )
    return _answer(tx, ctx, message, proposal, body.decision, body.values, body.comment)


def reply_message(
    pack: LoadedPack, actor_id: str, idempotency_key: str, body: ReplyRequest
) -> CommandOutcome:
    return run_command(pack, "REPLY_MESSAGE", actor_id, idempotency_key, body, _reply)


# ── proposals/{pid}/confirm·discard (§12) ──────────────────────


def _proposal_handler(decision: str):
    def handle(tx: sqlite3.Connection, ctx: CommandContext, body: ProposalDecision) -> Result:
        r = Result()
        proposal = get_proposal(tx, ctx.site_id, body.proposal_id)
        message = None if proposal is None else message_for_proposal(tx, body.proposal_id)
        if proposal is None or message is None:
            r.reject("PROPOSAL_NOT_FOUND")
            return r
        if ctx.actor_id != proposal["confirmer_actor_id"]:
            r.reject("NOT_AUTHORIZED")
            return r
        return _answer(tx, ctx, message, proposal, decision, None, body.comment)

    return handle


def confirm_proposal(
    pack: LoadedPack, actor_id: str, idempotency_key: str, body: ProposalDecision
) -> CommandOutcome:
    handler = _proposal_handler("ACCEPT")
    return run_command(pack, "CONFIRM_PROPOSAL", actor_id, idempotency_key, body, handler)


def discard_proposal(
    pack: LoadedPack, actor_id: str, idempotency_key: str, body: ProposalDecision
) -> CommandOutcome:
    handler = _proposal_handler("DECLINE")
    return run_command(pack, "DISCARD_PROPOSAL", actor_id, idempotency_key, body, handler)
