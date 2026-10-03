"""Event 접수·즉시 Hold와 Hold 해제.

접수는 Event 저장 + context +1 + Hold 생성 + Audit를 한 트랜잭션에서 한다.
설정 EVENT_RESPONSE_ENABLED가 켜졌고 지연 신고(DELAY)면 같은 tx에서 Event Response START_RUN을 등록한다.
Hold는 Agent와 무관하게 이미 걸려 있다. 해제는 NO_CHANGE와 FACT_CONFIRMED(그 Event의 사실 수정이
확정되었을 때)다.
"""

import sqlite3
from typing import Literal

from pydantic import Field

from app.commands.service import Body, CommandContext, CommandOutcome, Result, run_command
from app.config import get_settings
from app.domain.canonical import canonical_hash
from app.domain.ids import new_id
from app.packs.loader import LoadedPack
from app.store.repos.cases import (
    end_case_run,
    register_event_response,
    register_recheck,
    stale_active_runs,
)
from app.store.repos.events import (
    get_event_by_source,
    get_hold,
    insert_event,
    insert_hold,
    list_active_holds,
    release_hold,
)
from app.store.repos.messages import decide_proposal, list_fact_updates
from app.store.repos.runs import list_active_runs
from app.store.repos.site import bump_context_version
from app.store.repos.tasks import list_current_tasks


class EventReport(Body):
    source_event_id: str = Field(min_length=1)
    event_type: Literal["DELAY", "OTHER"]
    text: str
    target_task_id: str | None = None


# Hold 해제 사유. API 본문(api/commands.ReleaseBody)도 이 정의를 쓴다
Resolution = Literal["FACT_CONFIRMED", "NO_CHANGE"]


class HoldRelease(Body):
    hold_id: str
    resolution: Resolution
    expected_context_version: int
    comment: str = ""


def _receive(tx: sqlite3.Connection, ctx: CommandContext, body: EventReport) -> Result:
    r = Result()
    if not ctx.has_role("REPORTER", "SUPERVISOR"):
        r.reject("NOT_AUTHORIZED")
        return r
    site_id = ctx.site_id
    body_hash = canonical_hash(
        {
            "event_type": body.event_type,
            "text": body.text,
            "target_task_id": body.target_task_id,
            "reporter_actor_id": ctx.actor_id,
        }
    )
    existing = get_event_by_source(tx, site_id, body.source_event_id)
    if existing is not None:
        if existing["body_hash"] != body_hash:
            r.reject("SOURCE_BODY_MISMATCH")
            return r
        r.replayed = True
        r.refs = {
            "event_id": existing["event_id"],
            "hold_id": existing["hold_id"],
            "event_context_version": existing["context_version"],
        }
        return r

    context_version = bump_context_version(tx, site_id)
    event_id = new_id("evt")
    insert_event(
        tx,
        site_id,
        event_id,
        body.source_event_id,
        body.event_type,
        ctx.actor_id,
        body.text,
        body.target_task_id,
        body_hash,
        context_version,
    )
    ready = {t.task_id for t in list_current_tasks(tx, site_id, ctx.pack) if t.lifecycle == "READY"}
    task_scoped = body.target_task_id is not None and body.target_task_id in ready
    hold_id = new_id("hold")
    insert_hold(
        tx,
        site_id,
        hold_id,
        event_id,
        "TASK" if task_scoped else "SITE",
        body.target_task_id if task_scoped else None,
        context_version,
    )
    # 열린 Case의 Run을 STALE로. 실행 중인 그래프는 다음 RUNNING 확인에서 멈춘다.
    # 보낸 요청은 CANCELLED, Case가 닫히면 대기열 1건이 READY로 올라간다(RECHECK는 Hold로 건너뜀).
    # 응답은 재전송 때와 같아야 하므로 run_id는 넣지 않는다(end_reason EVENT:<event_id>로 찾는다).
    stale_active_runs(tx, ctx.pack, f"EVENT:{event_id}")
    # 지연 신고면 Event Response가 대상 작업·사실 수정안을 찾는다. 다른 유형은 Hold만
    if get_settings().event_response_enabled and body.event_type == "DELAY":
        register_event_response(tx, ctx.pack, event_id, hold_id)
    r.refs = {"event_id": event_id, "hold_id": hold_id, "event_context_version": context_version}
    return r


def receive_event(
    pack: LoadedPack, actor_id: str, idempotency_key: str, body: EventReport
) -> CommandOutcome:
    return run_command(pack, "RECEIVE_EVENT", actor_id, idempotency_key, body, _receive)


def _release(tx: sqlite3.Connection, ctx: CommandContext, body: HoldRelease) -> Result:
    r = Result()
    if not ctx.has_role("SUPERVISOR"):
        r.reject("NOT_AUTHORIZED")
        return r
    site_id = ctx.site_id
    hold = get_hold(tx, site_id, body.hold_id)
    if hold is None:
        r.reject("HOLD_NOT_FOUND")
        return r
    if hold["status"] != "ACTIVE":
        r.reject("HOLD_NOT_ACTIVE")
    facts = list_fact_updates(tx, site_id, event_id=hold["event_id"])
    # FACT_CONFIRMED는 그 Event의 사실 수정이 확정되었을 때만
    if body.resolution == "FACT_CONFIRMED" and not any(p["status"] == "CONFIRMED" for p in facts):
        r.reject("FACT_NOT_CONFIRMED")
    if body.expected_context_version != ctx.site.context_version:
        r.reject("STALE_CONTEXT")
    if r.reason_codes:
        return r

    if body.resolution == "NO_CHANGE":
        # 변경 없음: 대기 중 사실 수정안은 폐기, 그 Event의 Event Response Run은 끝낸다
        for p in facts:
            if p["status"] == "PENDING":
                decide_proposal(
                    tx, p["proposal_id"], "DISCARDED", ctx.actor_id, ctx.site.context_version
                )
        for run in list_active_runs(tx, site_id):
            if (
                run.agent_type == "EVENT_RESPONSE"
                and run.input_ref.get("event_id") == hold["event_id"]
            ):
                end_case_run(tx, ctx.pack, run.run_id, "STALE", f"HOLD_RELEASED:{body.hold_id}")
    context_version = bump_context_version(tx, site_id)
    release_hold(tx, site_id, body.hold_id, body.resolution, ctx.actor_id, context_version)
    recheck = not list_active_holds(tx, site_id)
    if recheck:
        cause = {"kind": "HOLD_RELEASE", "hold_id": body.hold_id, "task_id": hold["task_id"]}
        register_recheck(tx, site_id, cause)
    r.refs = {"hold_id": body.hold_id, "recheck": recheck}
    r.audit_reason = body.resolution
    return r


def release_hold_command(
    pack: LoadedPack, actor_id: str, idempotency_key: str, body: HoldRelease
) -> CommandOutcome:
    return run_command(pack, "RELEASE_HOLD", actor_id, idempotency_key, body, _release)
