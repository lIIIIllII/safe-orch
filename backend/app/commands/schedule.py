"""일정 꺼내기와 넣기.

꺼내기: 지금 확정 계획의 작업과 배정, 희망 영역과 출처, 고정(표시용)을 일정 문서로 만들어 기록에 남긴다.
취소·철회·대기열 작업, 계획 밖 요청, 동의·Hold는 담지 않는다. 현장 사실은 바꾸지 않지만 기록을 남기므로
명령이다 (ST-23). 문서는 GET /schedules/{id}로 받는다.

넣기 (ST-24): 넣는 사람은 자기 Unit의 작업만 넣고, 넣은 값은 담당자가 말한 값이다. 문서의 시각은 Soft다:
새 작업의 시간창은 Horizon 전체이고 문서의 희망 영역(없으면 배정 구간)이 희망 영역이 된다 (AG-35). 새 작업의
기준 배정은 문서의 배정이다. 이미 있는 자기 작업은 값이 다르면 작업 카드에서 고친 것과, 배정이 다르면 희망
영역을 그린 것과 같은 처리다. 넣을 수 없는 작업이 하나라도 있으면 전체를 거절하고, 사람이 빼기로 고른 작업만
뺀다. 넣기 하나는 사건 하나다. 열린 메인이 있으면 새 작업은 대기열에 선다 (AG-07).
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.commands.pins import replace_preferred_window
from app.commands.service import Body, CommandContext, CommandOutcome, Result, run_command
from app.commands.task_edit import needs_agent, revise_task
from app.commands.task_request import (
    EXCLUSION_CODES,
    TaskRequestForm,
    insert_requested_task,
    validate_task_request,
    waits_in_queue,
)
from app.domain.eligibility import ResourceNeed, exclusion_reasons
from app.domain.ids import new_id
from app.domain.models import Actor, SnapshotContent, TaskBase
from app.domain.schedule import (
    Hope,
    Schedule,
    ScheduleError,
    ScheduleTask,
    read_document,
    to_document,
)
from app.packs.loader import LoadedPack
from app.store.repos.cases import deliver_event, deliver_to_open_main, register_recheck, wake_run
from app.store.repos.pins import list_active_pins, preferred_windows
from app.store.repos.plans import get_current_plan
from app.store.repos.resources import list_resources
from app.store.repos.runs import list_active_runs
from app.store.repos.schedules import insert_schedule, insert_task_base, list_task_bases
from app.store.repos.site import bump_context_version, get_site
from app.store.repos.snapshots import build_snapshot_content
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


# ── 일정 넣기 ──────────────────────────────────────────────────

IMPORT_COMMAND = "IMPORT_SCHEDULE"

# 이미 있는 작업에서 문서로 고칠 수 있는 값(작업 카드와 같다, AG-33)과 고칠 수 없는 값
EDITABLE_VALUES = ("zone_id", "duration", "required_resource_type", "requested_resource_id")
FIXED_VALUES = ("work_type", "resource_requirements", "pool_demands", "predecessors")


class ImportRequest(Body):
    document: dict[str, Any]
    exclude: tuple[str, ...] = ()  # 사람이 미리보기에서 빼기로 고른 작업


@dataclass
class Item:
    """문서의 작업 하나에 대한 판정. verdict: NEW 새 작업, UNCHANGED 바뀌는 것 없음, HOPE_CHANGED 희망이
    바뀜, VALUE_CHANGED 값이 바뀜(희망도 바뀔 수 있다), REJECTED 넣을 수 없음."""

    task_id: str
    verdict: str
    excluded: bool = False
    reasons: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)
    changes: dict[str, Any] = field(default_factory=dict)  # 바뀌는 값 (VALUE_CHANGED)
    hope: tuple[int, int] | None = None  # 새로 서는 희망 영역 [start, end)
    form: TaskRequestForm | None = None  # 새 작업의 검증한 값
    base: TaskBase | None = None  # 새 작업의 기준 배정


@dataclass
class Judgement:
    """넣기 판정 전체. reasons가 있으면 문서 전체를 넣을 수 없다."""

    items: list[Item] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)
    pack_mismatch: bool = False

    @property
    def acceptable(self) -> bool:
        return not self.reasons and all(
            i.verdict != "REJECTED" for i in self.items if not i.excluded
        )


def _horizon_form(task: ScheduleTask, horizon: int) -> TaskRequestForm:
    """새 작업의 폼 값. 시간창은 Horizon 전체다: 문서의 시각은 희망 영역이 된다 (AG-35)."""
    return TaskRequestForm(
        **{
            k: getattr(task, k)
            for k in TASK_VALUES
            if k not in ("unit_id", "owner_actor_id", "predecessors")
        },
        predecessors=tuple(p.model_dump() for p in task.predecessors),
        earliest_start=0,
        latest_start=horizon - task.duration,
        latest_end=horizon,
    )


def judge(
    conn: sqlite3.Connection,
    pack: LoadedPack,
    actor: Actor,
    document: Any,
    exclude: tuple[str, ...] = (),
) -> Judgement:
    """문서의 작업마다 넣을 수 있는지와 무엇이 바뀌는지 판정한다. 읽기만 한다. 미리보기와 넣기 명령이
    같은 함수를 쓴다(넣기는 쓰기 트랜잭션 안에서 다시 판정한다)."""
    out = Judgement()
    site_id = pack.site_id
    site = get_site(conn, site_id)
    if site is None:
        raise LookupError(f"site {site_id} not found")
    horizon = site.horizon_minutes
    try:
        parsed = read_document(document, pack.horizon_start_utc, pack.timezone, horizon)
    except ScheduleError as e:
        out.reasons, out.details = ["SCHEDULE_MALFORMED"], list(e.reasons)
        return out
    if parsed.site_id != site_id:
        out.reasons = ["SITE_MISMATCH"]
        return out
    out.pack_mismatch = parsed.pack_hash != pack.pack_hash

    tasks = {t.task_id: t for t in list_current_tasks(conn, site_id, pack)}
    resources = {r.resource_id: r for r in list_resources(conn, site_id)}
    hopes = preferred_windows(conn, site_id)
    # 지금 자리: 계획에 있으면 계획의 배정, 없으면 기준 배정
    facts = SnapshotContent.model_validate(build_snapshot_content(conn, site_id, pack))
    positions = {tid: a.start for tid, a in facts.base_assignments().items()}
    for b in list_task_bases(conn, site_id):
        positions.setdefault(b.task_id, b.start)
    excluded = set(exclude)
    batch = frozenset(e.task_id for e in parsed.entries if e.task_id not in excluded)

    for entry in parsed.entries:
        item = Item(task_id=entry.task_id, verdict="REJECTED", excluded=entry.task_id in excluded)
        out.items.append(item)
        task, placed = entry.task, entry.assignment
        if task is None or placed is None:
            item.reasons, item.details = list(entry.codes), list(entry.details)
            continue
        if task.unit_id != actor.unit_id:
            item.reasons.append("OTHER_UNIT_TASK")  # 자기 Unit의 작업만 넣는다
            continue
        span = (placed.start, placed.end)
        wanted = task.preferred_window
        target = (wanted.start, wanted.end) if wanted is not None else None
        existing = tasks.get(task.task_id)
        if existing is None:
            form = _horizon_form(task, horizon)
            codes = validate_task_request(conn, pack, site, actor, form, batch=batch)
            if task.required_resource_type is not None and placed.resource_id is None:
                codes.append("FIELD_MISSING")
            elif (
                placed.resource_id is not None and placed.resource_id != task.requested_resource_id
            ):
                # 배정의 자원이 요청 자원과 다르면 그 자원도 같은 적격성 함수로 본다 (CV-20)
                resource = resources.get(placed.resource_id)
                need = ResourceNeed(
                    required_resource_type=task.required_resource_type,
                    zone_id=task.zone_id,
                    requirements=(
                        *pack.default_requirements(task.work_type),
                        *task.resource_requirements,
                    ),
                )
                if resource is None:
                    codes.append("UNKNOWN_RESOURCE")
                else:
                    codes += [
                        EXCLUSION_CODES[e.reason]
                        for e in exclusion_reasons(need, resource, actor.unit_id)
                    ]
            if codes:
                item.reasons = list(dict.fromkeys(codes))
                continue
            item.verdict, item.form, item.hope = "NEW", form, target or span
            item.base = TaskBase(
                task_id=task.task_id, start=placed.start, resource_id=placed.resource_id
            )
            continue
        if existing.lifecycle not in ("READY", "QUEUED"):
            item.reasons.append("TASK_ID_RETIRED")  # 취소·철회된 ID는 다시 쓰지 못한다
            continue
        if existing.owner_actor_id != actor.actor_id:
            item.reasons.append("NOT_OWNER")
            continue
        if any(getattr(task, k) != getattr(existing, k) for k in FIXED_VALUES):
            item.reasons.append("VALUE_NOT_EDITABLE")
            continue
        changes = {
            k: getattr(task, k) for k in EDITABLE_VALUES if getattr(task, k) != getattr(existing, k)
        }
        if changes:
            data = {**existing.model_dump(), **changes}
            form = TaskRequestForm(
                **{k: data[k] for k in TaskRequestForm.model_fields if k in data}
            )
            codes = validate_task_request(conn, pack, site, actor, form, existing=True)
            if codes:
                item.reasons = codes
                continue
            item.changes = changes
        # 희망: 문서에 희망 영역이 있으면 그것, 없으면 배정이 지금 자리와 다를 때 그 배정 구간
        position = positions.get(task.task_id)
        if target is None and position is not None and placed.start != position:
            target = span
        current = hopes.get(task.task_id)
        if target is not None and (current is None or (current["start"], current["end"]) != target):
            item.hope = target
        if changes:
            item.verdict = "VALUE_CHANGED"
        else:
            item.verdict = "HOPE_CHANGED" if item.hope is not None else "UNCHANGED"
    return out


def preview_import(
    conn: sqlite3.Connection,
    pack: LoadedPack,
    actor: Actor,
    document: Any,
    exclude: tuple[str, ...],
) -> dict[str, Any]:
    """넣기 미리보기(읽기 전용, 기록 없음): 문서 전체의 판정과 작업별 판정."""
    judged = judge(conn, pack, actor, document, exclude)
    return {
        "acceptable": judged.acceptable,
        "authorized": "UNIT_PLANNER" in actor.roles,
        "reasons": judged.reasons,
        "details": judged.details,
        # Pack이 달라도 거절하지 않는다. 값은 지금 Pack으로 검증한다
        "pack_mismatch": judged.pack_mismatch,
        "tasks": [
            {
                "task_id": i.task_id,
                "verdict": i.verdict,
                "excluded": i.excluded,
                "reasons": i.reasons,
                "details": i.details,
                "changed": sorted(i.changes),
                "hope_changed": i.hope is not None and i.verdict != "NEW",
            }
            for i in judged.items
        ],
    }


def _import(tx: sqlite3.Connection, ctx: CommandContext, body: ImportRequest) -> Result:
    r = Result()
    if not ctx.has_role("UNIT_PLANNER") or ctx.actor is None:
        r.reject("NOT_AUTHORIZED")
        return r
    site_id = ctx.site_id
    judged = judge(tx, ctx.pack, ctx.actor, body.document, body.exclude)
    for code in judged.reasons:
        r.reject(code)
    if r.reason_codes:
        r.refs = {"details": judged.details}
        return r
    rejected = [i for i in judged.items if i.verdict == "REJECTED" and not i.excluded]
    if rejected:
        # 넣을 수 없는 작업이 하나라도 있으면 전체를 거절한다. 사람이 빼기로 고른 작업만 뺀다
        r.reject("TASKS_NOT_IMPORTABLE")
        r.refs = {"tasks": [{"task_id": i.task_id, "reasons": i.reasons} for i in rejected]}
        return r

    at = datetime.now(UTC).isoformat(timespec="seconds")
    schedule_id = new_id("sch")
    digest = insert_schedule(
        tx, site_id, schedule_id, "IMPORT", ctx.site.plan_revision, ctx.actor_id, at, body.document
    )
    included = [i for i in judged.items if not i.excluded]
    new = [i for i in included if i.verdict == "NEW"]
    changed = [i for i in included if i.verdict in ("HOPE_CHANGED", "VALUE_CHANGED")]
    tasks = {t.task_id: t for t in list_current_tasks(tx, site_id, ctx.pack)}
    queued = waits_in_queue(tx, site_id)
    # 대기 중인(QUEUED) 작업은 아직 사실이 아니다: 새 revision·희망만 남기고 현장 버전은 올리지 않는다
    live = [i for i in changed if tasks[i.task_id].lifecycle == "READY"]
    applied = bool(live) or (bool(new) and not queued)
    context_version = bump_context_version(tx, site_id) if applied else ctx.site.context_version
    source = f"schedule:{schedule_id}"

    for item in new:
        assert item.form is not None and item.hope is not None and item.base is not None
        insert_requested_task(
            tx,
            ctx.pack,
            ctx.actor,
            item.form,
            source,
            queued,
            context_version,
            hope=(*item.hope, "STATED"),
            hope_made_by="OWNER",
        )
        insert_task_base(tx, site_id, item.base, schedule_id)
    for item in changed:
        if item.changes:
            revise_task(
                tx, site_id, tasks[item.task_id], item.changes, False, source, context_version
            )
        if item.hope is not None:
            replace_preferred_window(tx, ctx, item.task_id, *item.hope)

    r.refs = {
        "schedule_id": schedule_id,
        "content_hash": digest,
        "queued": bool(new) and queued,
        "new_task_ids": [i.task_id for i in new],
        "changed_task_ids": [i.task_id for i in changed],
        "unchanged": sum(1 for i in included if i.verdict == "UNCHANGED"),
        "excluded": sorted(i.task_id for i in judged.items if i.excluded),
    }
    if not applied:
        return r  # 바뀌는 것이 없거나 대기열에만 섰다: 기록만 남긴다
    for run in list_active_runs(tx, site_id):
        if run.agent_type in ("REPLANNING", "COORDINATION"):
            wake_run(tx, site_id, run.run_id)
    # 넣기 하나는 사건 하나다. 메인은 이 작업들이 한 일정에서 왔다는 것을 안다 (ST-24)
    ref = {
        "kind": "IMPORT",
        "schedule_id": schedule_id,
        "task_ids": [] if queued else [i.task_id for i in new],
        "changed_task_ids": [i.task_id for i in live],
        "actor_id": ctx.actor_id,
    }
    key = f"SCHEDULE_IMPORTED:{schedule_id}"
    if ref["task_ids"] or needs_agent(tx, ctx):
        deliver_event(tx, ctx.pack, "SCHEDULE_IMPORTED", key, ref)
    else:
        deliver_to_open_main(tx, ctx.pack, "SCHEDULE_IMPORTED", key, ref)
    register_recheck(tx, site_id, {"kind": "SCHEDULE", "schedule_id": schedule_id})
    return r


def import_schedule(
    pack: LoadedPack, actor_id: str, idempotency_key: str, body: ImportRequest
) -> CommandOutcome:
    return run_command(pack, IMPORT_COMMAND, actor_id, idempotency_key, body, _import)
