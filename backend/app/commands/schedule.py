"""일정 꺼내기.

지금 확정 계획의 작업과 배정, 희망 영역과 출처, 고정(표시용)을 일정 문서로 만들어 기록에 남긴다.
취소·철회·대기열 작업, 계획 밖 요청, 동의·Hold는 담지 않는다. 현장 사실은 바꾸지 않지만 기록을 남기므로
명령이다 (ST-23). 문서는 GET /schedules/{id}로 받는다.
"""

import sqlite3
from datetime import UTC, datetime

from app.commands.service import Body, CommandContext, CommandOutcome, Result, run_command
from app.domain.ids import new_id
from app.domain.schedule import Hope, Schedule, ScheduleTask, to_document
from app.packs.loader import LoadedPack
from app.store.repos.pins import list_active_pins, preferred_windows
from app.store.repos.plans import get_current_plan
from app.store.repos.schedules import insert_schedule
from app.store.repos.site import get_site
from app.store.repos.tasks import list_current_tasks

EXPORT_COMMAND = "EXPORT_SCHEDULE"

TASK_VALUES = (
    "task_id",
    "unit_id",
    "owner_actor_id",
    "work_type",
    "zone_id",
    "duration",
    "required_resource_type",
    "requested_resource_id",
    "resource_requirements",
    "pool_demands",
    "predecessors",
)


class ExportRequest(Body):
    """본문이 없다."""


def current_schedule(
    conn: sqlite3.Connection, pack: LoadedPack, schedule_id: str, actor_id: str, at: str
) -> Schedule:
    """지금 확정 계획의 일정. 계획에 있는 READY 작업만 담는다(작업 ID순)."""
    site_id = pack.site_id
    site = get_site(conn, site_id)
    if site is None:
        raise LookupError(f"site {site_id} not found")
    plan = get_current_plan(conn, site_id)
    ready = {
        t.task_id: t for t in list_current_tasks(conn, site_id, pack) if t.lifecycle == "READY"
    }
    placed = sorted(
        (a for a in (plan.assignments if plan else ()) if a.task_id in ready),
        key=lambda a: a.task_id,
    )
    pinned = {p.task_id for p in list_active_pins(conn, site_id)}
    hopes = preferred_windows(conn, site_id)
    tasks = []
    for a in placed:
        task, hope = ready[a.task_id], hopes.get(a.task_id)
        tasks.append(
            ScheduleTask(
                **{k: getattr(task, k) for k in TASK_VALUES},
                preferred_window=None
                if hope is None
                else Hope(start=hope["start"], end=hope["end"], origin=hope["origin"]),
                pinned=task.task_id in pinned,
                origins={
                    name: origin
                    for record in task.fields.values()
                    for name, origin in sorted(record.origins.items())
                },
            )
        )
    return Schedule(
        schedule_id=schedule_id,
        site_id=site_id,
        pack_hash=site.pack_hash,
        plan_revision=site.plan_revision,
        exported_by=actor_id,
        exported_at=at,
        tasks=tuple(tasks),
        assignments=tuple(placed),
    )


def _export(tx: sqlite3.Connection, ctx: CommandContext, body: ExportRequest) -> Result:
    r = Result()
    if ctx.actor is None:
        r.reject("NOT_AUTHORIZED")
        return r
    at = datetime.now(UTC).isoformat(timespec="seconds")
    schedule = current_schedule(tx, ctx.pack, new_id("sch"), ctx.actor_id, at)
    document = to_document(schedule, ctx.pack.horizon_start_utc, ctx.pack.timezone)
    digest = insert_schedule(
        tx,
        ctx.site_id,
        schedule.schedule_id,
        "EXPORT",
        schedule.plan_revision,
        ctx.actor_id,
        at,
        document,
    )
    r.refs = {
        "schedule_id": schedule.schedule_id,
        "content_hash": digest,
        "plan_revision": schedule.plan_revision,
        "task_count": len(schedule.tasks),
    }
    return r


def export_schedule(pack: LoadedPack, actor_id: str, idempotency_key: str) -> CommandOutcome:
    return run_command(pack, EXPORT_COMMAND, actor_id, idempotency_key, ExportRequest(), _export)
