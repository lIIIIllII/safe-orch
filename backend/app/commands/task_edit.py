"""작업 카드에서 값 고치기·정한 값 확인 (AG-33). 사람만 한다. Agent·Tool Gateway에는 이 함수가 없다.

담당자(요청자)가 자기 작업의 critical field 값을 고치거나, Work Intake가 정한 값을 그대로 확인한다.
- 고치면 새 revision이고 고친 값은 사람이 말한 값이 된다. 검증은 폼과 같다(validate_task_request).
- 확인(confirm)하면 남은 정한 값이 모두 말한 값이 된다. 작업 유형은 고칠 수 없고 확인만 한다.
  Work Intake가 정한 희망 영역도 확인으로 말한 희망이 된다(그때부터 그 범위 안은 묻지 않는다, ST-22).
  정한 값이 희망 영역뿐이면 새 revision을 만들지 않는다.
- 카드에서 고치는 시간창은 가능 범위(Hard)다. 희망 영역은 타임라인에서 그리고 지운다.
- 동의: 바뀌지 않은 축의 Consent는 새 revision으로 복사한다. 시작 범위를 고치면 그 범위의 Consent를,
  사람이 말한 요청 자원에 Consent가 없으면 그 Consent를 만든다. 정한 값이 남아 있는 축에는 만들지 않는다.
- 계획에 있는 작업도 지금 배치가 깨지는 값으로 고칠 수 있다. 막지 않고 Agent가 다시 풀게 한다.
- context +1, 열린 재계획·협의 Run 깨우기, 재확인 등록. 사건(TASK_EDITED)은 고친 뒤 충돌이 있거나 계획 밖
  READY 작업이 남으면 작업 준비됨과 같은 방식으로 전하고(열린 메인이 없으면 메인이 뜬다), 그렇지 않으면
  고정·직접 이동처럼 열린 메인이 있을 때만 전한다. 대기열(QUEUED) 작업은 아직 사실이 아니라 새 revision만
  남긴다.
"""

import sqlite3
from typing import Any

from pydantic import Field

from app.commands.pins import confirm_preferred_window
from app.commands.service import Body, CommandContext, CommandOutcome, Result, run_command
from app.commands.task_request import TaskRequestForm, stated_consents, validate_task_request
from app.domain.canonical import canonical_hash
from app.domain.ids import new_id
from app.domain.models import FieldRecord, Snapshot, Task
from app.packs.loader import FIELD_VALUES, LoadedPack, confirmed_fields
from app.rules.engine import detect_conflicts
from app.store.repos.cases import (
    copy_consents,
    deliver_event,
    deliver_to_open_main,
    register_recheck,
    wake_run,
)
from app.store.repos.consents import insert_consent, list_current_consents
from app.store.repos.pins import preferred_windows
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


def _needs_agent(tx: sqlite3.Connection, ctx: CommandContext) -> bool:
    """고친 뒤 Agent가 풀 일이 남았는가: 서버의 충돌 검사에 걸린 충돌이 있거나 계획 밖 READY 작업이 있다.
    저장하지 않는 Snapshot으로 본다."""
    content = build_snapshot_content(tx, ctx.site_id, ctx.pack)
    probe = Snapshot(snapshot_id="edit", snapshot_hash=canonical_hash(content), content=content)
    facts = probe.facts()
    in_plan = {a.task_id for a in facts.plan.assignments}
    unplanned = any(t.task_id not in in_plan for t in facts.tasks)
    return unplanned or bool(detect_conflicts(probe, facts.check_assignments(), ctx.pack))


def edited_fields(
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
    hope = preferred_windows(tx, site_id).get(task.task_id)
    confirm_hope = body.confirm and hope is not None and hope["origin"] == "DECIDED"
    # 새 revision은 값이 바뀌거나 정한 값을 확인할 때만. 정한 희망만 확인하면 작업 값은 그대로다
    revise = bool(changes) or (body.confirm and bool(task.decided_values))
    if not revise and not confirm_hope:
        r.reject("NO_CHANGE")
        return r

    data = {**task.model_dump(), **changes}
    form = TaskRequestForm(**{k: data[k] for k in TaskRequestForm.model_fields if k in data})
    for code in validate_task_request(tx, ctx.pack, ctx.site, ctx.actor, form, existing=True):
        r.reject(code)
    if r.reason_codes:
        return r

    ready = task.lifecycle == "READY"
    revision = task.revision + 1 if revise else task.revision
    edit_id = new_id("edit")
    source = f"card:{edit_id}"
    changed = set(changes)
    context_version = bump_context_version(tx, site_id) if ready else ctx.site.context_version
    consent_ids: list[str] = []
    if revise:
        updated = task.model_copy(
            update={
                **changes,
                "revision": revision,
                "fields": edited_fields(task, data, changed, body.confirm, source),
            }
        )
        insert_task_revision(tx, site_id, updated)

        # 동의: 값이 바뀌지 않은 축은 복사한다. 시작 범위는 사람이 고쳤을 때만, 요청 자원은 말한 값에
        # 동의가 없으면 만든다(자연어 요청의 시간창은 사람이 넣은 값이 아니다, ST-22)
        timed = bool({"earliest_start", "latest_start"} & changed)
        axes = tuple(
            axis
            for axis, moved in (("TIME", timed), ("RESOURCE", "requested_resource_id" in changed))
            if not moved
        )
        consent_ids = copy_consents(
            tx, site_id, task.task_id, task.revision, revision, context_version, axes
        )
        have = [c for c in list_current_consents(tx, site_id) if c.task_id == task.task_id]
        for consent in stated_consents(updated, source, time=timed):
            if not any(c.axis == consent.axis and c.scope == consent.scope for c in have):
                insert_consent(tx, site_id, consent, context_version)
                consent_ids.append(consent.consent_id)
    if confirm_hope:
        confirm_preferred_window(tx, ctx, task.task_id)

    r.refs = {
        "task_id": task.task_id,
        "revision": revision,
        "edit_id": edit_id,
        "changed": sorted(changed),
        "confirmed": body.confirm,
        "consent_ids": consent_ids,
        "queued": not ready,
        "preferred_window_confirmed": confirm_hope,
    }
    if not ready:
        return r
    # 열린 재계획·협의 Run은 다시 관찰하게 깨운다
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
    # 희망만 확인하면 revision이 그대로라 현장 버전으로 구분한다
    key = f"TASK_EDITED:{task.task_id}:{revision}" + ("" if revise else f":{context_version}")
    if _needs_agent(tx, ctx):
        # 풀 일이 남았다: 작업 준비됨과 같은 방식으로 전한다(열린 메인이 없으면 메인이 뜬다)
        deliver_event(tx, ctx.pack, "TASK_EDITED", key, ref)
    else:
        deliver_to_open_main(tx, ctx.pack, "TASK_EDITED", key, ref)
    register_recheck(tx, site_id, {"kind": "EDIT", "task_id": task.task_id})
    return r


def edit_task(
    pack: LoadedPack, actor_id: str, idempotency_key: str, body: EditRequest
) -> CommandOutcome:
    return run_command(pack, "EDIT_TASK", actor_id, idempotency_key, body, _edit)
