"""작업 카드에서 값 고치기·정한 값 확인 (AG-33). 사람만 한다. Agent·Tool Gateway에는 이 함수가 없다.

담당자(요청자)가 자기 작업의 critical field 값을 고치거나, Work Intake가 정한 값을 그대로 확인한다.
- 고치면 새 revision이고 고친 값은 사람이 말한 값이 된다. 검증은 폼과 같다(validate_task_request).
- 확인(confirm)하면 남은 정한 값이 모두 말한 값이 된다. 작업 유형은 고칠 수 없고 확인만 한다.
- 동의: 바뀌지 않은 축의 Consent는 새 revision으로 복사하고, 사람이 말한 시작 범위·요청 자원에 Consent가
  없으면 만든다. 정한 값이 남아 있는 축에는 만들지 않는다.
- 계획에 있는 작업은 지금 배치가 깨지는 값으로는 고칠 수 없다(EDIT_BREAKS_PLAN): 먼저 옮기거나 없앤다.
- 고정·직접 이동과 같은 방식으로 다룬다: context +1, 열린 재계획·협의 Run 깨우기, 열린 메인이 있을 때만
  사건(TASK_EDITED), 재확인 등록. 대기열(QUEUED) 작업은 아직 사실이 아니라 새 revision만 남긴다.
"""

import sqlite3
from typing import Any

from pydantic import Field

from app.commands.service import Body, CommandContext, CommandOutcome, Result, run_command
from app.commands.task_request import TaskRequestForm, stated_consents, validate_task_request
from app.domain.canonical import canonical_hash
from app.domain.ids import new_id
from app.domain.models import FieldRecord, Snapshot, Task
from app.packs.loader import FIELD_VALUES, LoadedPack, confirmed_fields
from app.rules.engine import detect_conflicts
from app.store.repos.cases import copy_consents, deliver_to_open_main, register_recheck, wake_run
from app.store.repos.consents import insert_consent, list_current_consents
from app.store.repos.plans import get_current_plan
from app.store.repos.runs import list_active_runs
from app.store.repos.site import bump_context_version
from app.store.repos.snapshots import build_snapshot_content
from app.store.repos.tasks import insert_task_revision, list_current_tasks

EDITABLE = (
    "zone_id",
    "duration",
    "earliest_start",
    "latest_start",
    "latest_end",
    "required_resource_type",
    "requested_resource_id",
)


class EditRequest(Body):
    """주지 않은 값(None)은 그대로 둔다. confirm이면 남은 정한 값을 모두 확인한다."""

    task_id: str = Field(min_length=1)
    zone_id: str | None = None
    duration: int | None = Field(default=None, gt=0)
    earliest_start: int | None = None
    latest_start: int | None = None
    latest_end: int | None = None
    required_resource_type: str | None = None
    requested_resource_id: str | None = None
    confirm: bool = False


def _conflicts_of(tx: sqlite3.Connection, ctx: CommandContext, task_id: str) -> set[tuple]:
    """지금 사실에서 그 작업이 걸린 충돌 (저장하지 않는 Snapshot으로 본다)."""
    content = build_snapshot_content(tx, ctx.site_id, ctx.pack)
    probe = Snapshot(snapshot_id="edit", snapshot_hash=canonical_hash(content), content=content)
    found = detect_conflicts(probe, probe.facts().check_assignments(), ctx.pack)
    return {(c.rule_id, c.task_ids) for c in found if task_id in c.task_ids}


def _fields(
    task: Task, data: dict[str, Any], changed: set[str], confirm: bool, source_ref: str
) -> dict[str, FieldRecord]:
    """새 revision의 확인 기록. 값이 바뀌었거나 정한 값을 확인한 필드만 새 기록(출처 card)이 되고,
    나머지는 앞 revision의 기록을 그대로 둔다. 고친 값과 확인한 값은 사람이 말한 값이 된다."""
    critical = tuple(name for name in FIELD_VALUES if name in task.fields)
    fresh = confirmed_fields(data, critical, source_ref)
    out: dict[str, FieldRecord] = {}
    for name, old in task.fields.items():
        names = set(FIELD_VALUES.get(name, (name,)))
        origins = {} if confirm else {v: o for v, o in old.origins.items() if v not in changed}
        if name not in fresh or (not names & changed and origins == old.origins):
            out[name] = old
        else:
            out[name] = fresh[name].model_copy(update={"origins": origins})
    return out


def _edit(tx: sqlite3.Connection, ctx: CommandContext, body: EditRequest) -> Result:
    r = Result()
    site_id = ctx.site_id
    task = next(
        (
            t
            for t in list_current_tasks(tx, site_id, ctx.pack)
            if t.task_id == body.task_id and t.lifecycle in ("READY", "QUEUED")
        ),
        None,
    )
    if task is None or ctx.actor is None:
        r.reject("TASK_NOT_FOUND")
        return r
    if ctx.actor_id != task.owner_actor_id:
        r.reject("NOT_AUTHORIZED")
        return r
    given = {k: getattr(body, k) for k in EDITABLE if getattr(body, k) is not None}
    changes = {k: v for k, v in given.items() if v != getattr(task, k)}
    if not changes and not (body.confirm and task.decided_values):
        r.reject("NO_CHANGE")
        return r

    data = {**task.model_dump(), **changes}
    form = TaskRequestForm(**{k: data[k] for k in TaskRequestForm.model_fields if k in data})
    for code in validate_task_request(tx, ctx.pack, ctx.site, ctx.actor, form, existing=True):
        r.reject(code)
    if r.reason_codes:
        return r

    ready = task.lifecycle == "READY"
    plan = get_current_plan(tx, site_id)
    in_plan = plan is not None and any(a.task_id == task.task_id for a in plan.assignments)
    before = _conflicts_of(tx, ctx, task.task_id) if in_plan else set()
    revision = task.revision + 1
    edit_id = new_id("edit")
    source = f"card:{edit_id}"
    changed = set(changes)
    updated = task.model_copy(
        update={
            **changes,
            "revision": revision,
            "fields": _fields(task, data, changed, body.confirm, source),
        }
    )
    insert_task_revision(tx, site_id, updated)
    # 계획에 있는 작업: 고친 값으로 지금 배치가 깨지면 받지 않는다(쓰기는 거절과 함께 되돌려진다, ST-04)
    if in_plan and _conflicts_of(tx, ctx, task.task_id) - before:
        r.reject("EDIT_BREAKS_PLAN")
        return r
    context_version = bump_context_version(tx, site_id) if ready else ctx.site.context_version

    # 동의: 값이 바뀌지 않은 축은 복사하고, 사람이 말한 값에 동의가 없으면 만든다
    axes = tuple(
        axis
        for axis, names in (
            ("TIME", {"earliest_start", "latest_start"}),
            ("RESOURCE", {"requested_resource_id"}),
        )
        if not names & changed
    )
    consent_ids = copy_consents(
        tx, site_id, task.task_id, task.revision, revision, context_version, axes
    )
    have = [c for c in list_current_consents(tx, site_id) if c.task_id == task.task_id]
    for consent in stated_consents(updated, source):
        if not any(c.axis == consent.axis and c.scope == consent.scope for c in have):
            insert_consent(tx, site_id, consent, context_version)
            consent_ids.append(consent.consent_id)

    r.refs = {
        "task_id": task.task_id,
        "revision": revision,
        "edit_id": edit_id,
        "changed": sorted(changed),
        "confirmed": body.confirm,
        "consent_ids": consent_ids,
        "queued": not ready,
    }
    if not ready:
        return r
    # 고정·직접 이동과 같은 방식: 열린 재계획·협의 Run은 다시 관찰하게 깨우고, 열린 메인에만 사건을 전한다
    for run in list_active_runs(tx, site_id):
        if run.agent_type in ("REPLANNING", "COORDINATION"):
            wake_run(tx, site_id, run.run_id)
    ref = {
        "task_id": task.task_id,
        "actor_id": ctx.actor_id,
        "revision": revision,
        "changed": sorted(changed),
        "confirmed": body.confirm,
    }
    deliver_to_open_main(tx, ctx.pack, "TASK_EDITED", f"TASK_EDITED:{task.task_id}:{revision}", ref)
    register_recheck(tx, site_id, {"kind": "EDIT", "task_id": task.task_id})
    return r


def edit_task(
    pack: LoadedPack, actor_id: str, idempotency_key: str, body: EditRequest
) -> CommandOutcome:
    return run_command(pack, "EDIT_TASK", actor_id, idempotency_key, body, _edit)
