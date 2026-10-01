"""Event 접수·즉시 Hold와 Hold 해제 (설계서 §10, 부록 A.14).

접수는 Event 저장 + context +1 + Hold 생성 + Audit를 한 트랜잭션에서 한다(I-05).
Event Response Agent가 없으므로 Run을 시작하지 않는다. 해제는 NO_CHANGE만 지원한다.
"""

import sqlite3
from typing import Literal

from pydantic import Field

from app.commands.service import Body, CommandContext, CommandOutcome, Result, run_command
from app.domain.canonical import canonical_hash
from app.domain.ids import new_id
from app.packs.loader import LoadedPack
from app.store.repos.cases import register_recheck, stale_active_runs
from app.store.repos.events import (
    get_event_by_source,
    get_hold,
    insert_event,
    insert_hold,
    list_active_holds,
    release_hold,
)
from app.store.repos.site import bump_context_version
from app.store.repos.tasks import list_current_tasks


class EventReport(Body):
    source_event_id: str = Field(min_length=1)
    event_type: Literal["DELAY", "OTHER"]
    text: str
    target_task_id: str | None = None


class HoldRelease(Body):
    hold_id: str
    resolution: Literal["FACT_CONFIRMED", "NO_CHANGE"]
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
    # 열린 Case의 Run을 STALE로 (§10, §11.5). 실행 중인 그래프는 다음 RUNNING 확인에서 멈춘다.
    # 보낸 요청은 CANCELLED, Case가 닫히면 대기열 1건이 READY로 올라간다(RECHECK는 Hold로 건너뜀, A.21).
    # 응답은 재전송 때와 같아야 하므로 run_id는 넣지 않는다(end_reason EVENT:<event_id>로 찾는다).
    stale_active_runs(tx, ctx.pack, f"EVENT:{event_id}")
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
    if body.resolution != "NO_CHANGE":
        r.reject("RESOLUTION_NOT_SUPPORTED")
    if body.expected_context_version != ctx.site.context_version:
        r.reject("STALE_CONTEXT")
    if r.reason_codes:
        return r

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
