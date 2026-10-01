"""작업 요청 폼 (우선순위 문서 구현 범위 D3, 부록 A.14).

critical field를 CONFIRMED(source_ref form:<form_id>)로 기록하고 Consent(시작 범위, 요청 자원)를
만든다. Agent(Work Intake)를 대신하는 결정론 입력이며 값을 추정하지 않는다.
"""

import sqlite3

from pydantic import Field

from app.commands.service import Body, CommandContext, CommandOutcome, Result, run_command
from app.domain.calendar import has_work_slot
from app.domain.ids import new_id
from app.domain.models import Consent, Movable, Task
from app.packs.loader import LoadedPack, confirmed_fields
from app.store.repos.cases import end_case_run, register_recheck, wake_run
from app.store.repos.consents import insert_consent
from app.store.repos.plans import get_current_plan
from app.store.repos.resources import list_resources
from app.store.repos.runs import has_open_case, list_active_runs
from app.store.repos.site import bump_context_version
from app.store.repos.tasks import insert_task_revision, list_current_tasks

COMMAND = "SUBMIT_TASK_REQUEST"


class PredecessorInput(Body):
    task_id: str
    min_lag: int = Field(default=0, ge=0)


class TaskRequestForm(Body):
    """unit_id·owner_actor_id·movable은 받지 않는다(요청자와 고정값으로 채운다)."""

    task_id: str = Field(min_length=1)
    work_type: str
    zone_id: str
    duration: int = Field(gt=0)
    earliest_start: int
    latest_start: int
    latest_end: int
    required_resource_type: str | None = None
    requested_resource_id: str | None = None
    predecessors: tuple[PredecessorInput, ...] = ()
    hazard_tags: tuple[str, ...] = Field(default=(), exclude=True)  # 받으면 버린다 (I-14, A.4)


def _handle(tx: sqlite3.Connection, ctx: CommandContext, form: TaskRequestForm) -> Result:
    r = Result()
    if not ctx.has_role("UNIT_PLANNER") or ctx.actor is None:
        r.reject("NOT_AUTHORIZED")
        return r
    pack, site_id, actor = ctx.pack, ctx.site_id, ctx.actor

    if tx.execute(
        "SELECT 1 FROM task WHERE site_id = ? AND task_id = ?", (site_id, form.task_id)
    ).fetchone():
        r.reject("TASK_ID_EXISTS")
    wt = pack.work_types.get(form.work_type)
    if wt is None:
        r.reject("UNKNOWN_WORK_TYPE")
    if not tx.execute(
        "SELECT 1 FROM zone WHERE site_id = ? AND zone_id = ?", (site_id, form.zone_id)
    ).fetchone():
        r.reject("UNKNOWN_ZONE")
    es, end_min = form.earliest_start, form.earliest_start + form.duration
    if (
        es < 0
        or es > form.latest_start
        or end_min > form.latest_end
        or end_min > ctx.site.horizon_minutes
    ):
        r.reject("INVALID_WINDOW")
    elif not has_work_slot(
        es, form.latest_start, form.latest_end, form.duration, pack.work_intervals
    ):
        # 시간창 어디에도 근무 구간 하나에 들어가는 시작이 없다. 받아도 이관으로 끝날 뿐이다 (A.20).
        # 요청 시작만 근무시간 밖이면 접수하고 CALENDAR 충돌로 재계획한다.
        r.reject("WINDOW_OUTSIDE_WORK_HOURS")
    if (
        wt is not None
        and "resource" in wt.critical_fields
        and (form.required_resource_type is None or form.requested_resource_id is None)
    ):
        r.reject("FIELD_MISSING")
    if form.requested_resource_id is not None:
        res = {x.resource_id: x for x in list_resources(tx, site_id)}.get(
            form.requested_resource_id
        )
        if res is None:
            r.reject("UNKNOWN_RESOURCE")
        else:
            if res.resource_type != form.required_resource_type:
                r.reject("RESOURCE_TYPE_MISMATCH")
            if actor.unit_id not in res.allowed_unit_ids:
                r.reject("RESOURCE_NOT_AUTHORIZED")
    current = {t.task_id for t in list_current_tasks(tx, site_id, pack)}
    if any(p.task_id not in current for p in form.predecessors):
        r.reject("PREDECESSOR_NOT_FOUND")
    if r.reason_codes or wt is None:
        return r

    form_id = new_id("form")
    source_ref = f"form:{form_id}"
    data = form.model_dump()
    task = Task(
        **data,
        revision=1,
        unit_id=actor.unit_id,
        owner_actor_id=actor.actor_id,
        hazard_tags=pack.hazard_tags(form.work_type),
        movable=Movable(time=True, resource=False),  # 자원 축은 MOVABILITY로만 연다
        fields=confirmed_fields(data, wt.critical_fields, source_ref),
        lifecycle="READY",
    )
    # 열린 Replanning Case 중이면 접수 후 대기열(QUEUED): Snapshot·충돌 검사에 들어가지 않으므로
    # context를 올리지 않고 RECHECK도 없다. Case가 끝날 때 접수 순서로 READY가 된다 (A.21 0-1).
    queued = has_open_case(tx, site_id)
    if queued:
        task = task.model_copy(update={"lifecycle": "QUEUED"})
    insert_task_revision(tx, site_id, task)
    context_version = ctx.site.context_version if queued else bump_context_version(tx, site_id)

    consents = [
        Consent(
            consent_id=new_id("cns"),
            task_id=task.task_id,
            task_revision=1,
            owner_actor_id=actor.actor_id,
            axis="TIME",
            scope={"start_min": form.earliest_start, "start_max": form.latest_start},
            source_ref=source_ref,
        )
    ]
    if form.requested_resource_id is not None:
        consents.append(
            Consent(
                consent_id=new_id("cns"),
                task_id=task.task_id,
                task_revision=1,
                owner_actor_id=actor.actor_id,
                axis="RESOURCE",
                scope={"resource_ids": [form.requested_resource_id]},
                source_ref=source_ref,
            )
        )
    for c in consents:
        insert_consent(tx, site_id, c, context_version)
    if not queued:
        cause = {"kind": "FORM", "task_id": task.task_id, "actor_id": actor.actor_id}
        register_recheck(tx, site_id, cause)

    r.refs = {
        "task_id": task.task_id,
        "revision": 1,
        "queued": queued,
        "form_id": form_id,
        "consent_ids": [c.consent_id for c in consents],
    }
    return r


def submit_task_request(
    pack: LoadedPack, actor_id: str, idempotency_key: str, form: TaskRequestForm
) -> CommandOutcome:
    return run_command(pack, COMMAND, actor_id, idempotency_key, form, _handle)


# ── 요청 철회 (부록 A.20) ──────────────────────────────────────

WITHDRAW_COMMAND = "WITHDRAW_TASK_REQUEST"


class TaskWithdraw(Body):
    task_id: str = Field(min_length=1)
    comment: str = ""


def _withdraw(tx: sqlite3.Connection, ctx: CommandContext, body: TaskWithdraw) -> Result:
    """해결하지 못한 요청(Plan에 없는 READY 작업) 또는 대기열(QUEUED) 작업을 계산 대상에서 뺀다.

    READY를 남겨 두면 기준 위치에 고정 상수로 남아 이후 모든 Solver 호출이 INFEASIBLE이 된다.
    - READY: 새 revision(NEEDS_INFO) + context +1 + RECHECK. 이 작업을 다루는 열린 Run은 STALE(그 Case가
      끝나 대기열이 올라감), 다른 열린 Run은 wake(고정 충돌이 사라져 다시 풀 수 있다, A.21 3).
    - QUEUED: 사실에 들어간 적이 없으므로 새 revision(NEEDS_INFO)만. context·RECHECK·Run 영향 없음.
    """
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
    if task is None:
        r.reject("TASK_NOT_FOUND")
        return r
    if ctx.actor_id != task.owner_actor_id and not ctx.has_role("SUPERVISOR"):
        r.reject("NOT_AUTHORIZED")
        return r
    plan = get_current_plan(tx, site_id)
    if plan is not None and any(a.task_id == task.task_id for a in plan.assignments):
        r.reject("TASK_IN_PLAN")
        return r

    revision = task.revision + 1
    insert_task_revision(
        tx, site_id, task.model_copy(update={"revision": revision, "lifecycle": "NEEDS_INFO"})
    )
    r.refs = {"task_id": task.task_id, "revision": revision, "queued": task.lifecycle == "QUEUED"}
    if task.lifecycle == "QUEUED":
        return r
    bump_context_version(tx, site_id)
    for run in list_active_runs(tx, site_id):
        if task.task_id in (run.input_ref.get("conflict") or {}).get("task_ids", []):
            end_case_run(tx, ctx.pack, run.run_id, "STALE", f"WITHDRAW:{task.task_id}")
        else:
            wake_run(tx, site_id, run.run_id)
    cause = {"kind": "WITHDRAW", "task_id": task.task_id, "actor_id": ctx.actor_id}
    register_recheck(tx, site_id, cause)
    return r


def withdraw_task_request(
    pack: LoadedPack, actor_id: str, idempotency_key: str, body: TaskWithdraw
) -> CommandOutcome:
    return run_command(pack, WITHDRAW_COMMAND, actor_id, idempotency_key, body, _withdraw)
