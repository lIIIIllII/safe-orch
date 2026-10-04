"""작업 요청 폼.

critical field를 CONFIRMED(source_ref form:<form_id>)로 기록한다. Agent(Work Intake)를 대신하는 결정론
입력이며 값을 추정하지 않는다.
폼의 시간창은 사람이 구조화된 입력으로 넣은 가능 범위(Hard)다. 자연어 요청(Work Intake)의 시간은 기준
위치(요청한 시작 범위)가 되고 시간창은 Horizon 전체로 채워진다 (AG-35). 폼 요청에는 기준 위치가 없다.
"""

import sqlite3
from typing import Any

from pydantic import Field

from app.commands.service import Body, CommandContext, CommandOutcome, Result, run_command
from app.domain.calendar import has_work_slot
from app.domain.eligibility import ResourceNeed, exclusion_reasons, requirement_error
from app.domain.ids import new_id
from app.domain.models import (
    Actor,
    Demand,
    Requirement,
    Site,
    Task,
    TaskBase,
    pool_for,
)
from app.packs.loader import LoadedPack, confirmed_fields
from app.store.repos.cases import (
    deliver_event,
    end_case_run,
    queued_task_ids,
    register_recheck,
    wake_run,
)
from app.store.repos.pins import list_active_pins
from app.store.repos.plans import get_current_plan
from app.store.repos.resources import list_pools, list_resources
from app.store.repos.runs import has_open_case, list_active_runs
from app.store.repos.schedules import insert_task_base
from app.store.repos.site import bump_context_version
from app.store.repos.tasks import insert_task_revision, list_current_tasks

COMMAND = "SUBMIT_TASK_REQUEST"


class PredecessorInput(Body):
    task_id: str
    min_lag: int = Field(default=0, ge=0)


class TaskRequestForm(Body):
    """unit_id·owner_actor_id는 받지 않는다(요청자로 채운다)."""

    task_id: str = Field(min_length=1)
    work_type: str
    zone_id: str
    duration: int = Field(gt=0)
    earliest_start: int
    latest_start: int
    latest_end: int
    required_resource_type: str | None = None
    requested_resource_id: str | None = None
    # 작업 값. 작업 유형 기본값에 더해진다(빼거나 낮출 수 없다, CV-11)
    resource_requirements: tuple[Requirement, ...] = ()
    # 작업 값. 작업 유형 기본 수요보다 낮출 수 없다(큰 쪽을 쓴다, CV-11)
    pool_demands: tuple[Demand, ...] = ()
    predecessors: tuple[PredecessorInput, ...] = ()
    hazard_tags: tuple[str, ...] = Field(default=(), exclude=True)  # 받으면 버린다


# 자원 적격성 사유 → 폼 거절 사유 (CV-20)
EXCLUSION_CODES = {
    "TYPE_MISMATCH": "RESOURCE_TYPE_MISMATCH",
    "NOT_ALLOWED": "RESOURCE_NOT_AUTHORIZED",
    "NO_AVAILABILITY": "RESOURCE_NO_AVAILABILITY",
    "ZONE_NOT_ALLOWED": "RESOURCE_ZONE_NOT_ALLOWED",
    "REQUIREMENT_NOT_MET": "RESOURCE_REQUIREMENT_NOT_MET",
}


def validate_task_request(
    tx: sqlite3.Connection,
    pack: LoadedPack,
    site: Site,
    actor: Actor,
    form: TaskRequestForm,
    existing: bool = False,
    batch: frozenset[str] = frozenset(),
) -> list[str]:
    """폼 검증. 폼과 Work Intake, 작업 카드에서 고치기, 일정 넣기가 같이 쓴다. 사유를 검사 순서대로 모은다.

    existing: 이미 있는 작업의 값을 고치는 검증이다(작업 ID 중복을 보지 않는다).
    batch: 같은 묶음(일정 문서)으로 함께 들어오는 작업 ID. 선행 작업으로 가리킬 수 있다."""
    r = Result()
    site_id = site.site_id
    if (
        not existing
        and tx.execute(
            "SELECT 1 FROM task WHERE site_id = ? AND task_id = ?", (site_id, form.task_id)
        ).fetchone()
    ):
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
        or end_min > site.horizon_minutes
    ):
        r.reject("INVALID_WINDOW")
    elif not has_work_slot(
        es, form.latest_start, form.latest_end, form.duration, pack.work_intervals
    ):
        # 시간창 어디에도 근무 구간 하나에 들어가는 시작이 없다. 받아도 이관으로 끝날 뿐이다.
        # 요청 시작만 근무시간 밖이면 접수하고 CALENDAR 충돌로 재계획한다.
        r.reject("WINDOW_OUTSIDE_WORK_HOURS")
    if (
        wt is not None
        and "resource" in wt.critical_fields
        and (form.required_resource_type is None or form.requested_resource_id is None)
    ):
        r.reject("FIELD_MISSING")
    # 요구 조건은 선언된 속성과 맞아야 하고, 자원을 쓰는 작업에만 붙는다
    if any(requirement_error(q, pack.resource_attributes) for q in form.resource_requirements) or (
        form.resource_requirements and form.required_resource_type is None
    ):
        r.reject("INVALID_REQUIREMENT")
    # 수요는 선언된 종류만, 종류마다 하나. 필수 직종은 요청자 Unit에 풀이 있어야 한다 (CV-23)
    kinds = [d.kind for d in form.pool_demands]
    if len(set(kinds)) != len(kinds) or any(k not in pack.pool_kinds for k in kinds):
        r.reject("INVALID_DEMAND")
    pools = list_pools(tx, site_id)
    if any(
        d.required and pool_for(pools, actor.unit_id, d.kind) is None
        for d in pack.default_demands(form.work_type)
    ):
        r.reject("REQUIRED_POOL_MISSING")
    if form.requested_resource_id is not None:
        res = {x.resource_id: x for x in list_resources(tx, site_id)}.get(
            form.requested_resource_id
        )
        if res is None:
            r.reject("UNKNOWN_RESOURCE")
        else:
            need = ResourceNeed(
                required_resource_type=form.required_resource_type,
                zone_id=form.zone_id,
                requirements=(
                    *pack.default_requirements(form.work_type),
                    *form.resource_requirements,
                ),
            )
            for code in dict.fromkeys(
                EXCLUSION_CODES[e.reason] for e in exclusion_reasons(need, res, actor.unit_id)
            ):
                r.reject(code)
    # 선행 작업은 현재 READY·QUEUED 작업만. 철회된 작업(NEEDS_INFO)은 없는 것으로 본다.
    current = {
        t.task_id
        for t in list_current_tasks(tx, site_id, pack)
        if t.lifecycle in ("READY", "QUEUED")
    }
    if any(p.task_id not in current | batch for p in form.predecessors):
        r.reject("PREDECESSOR_NOT_FOUND")
    return r.reason_codes


def waits_in_queue(tx: sqlite3.Connection, site_id: str) -> bool:
    """새 작업이 대기열(QUEUED)에 서는가: 열린 메인(Case)이 있거나 먼저 접수된 대기 요청이 있다.
    대기 중인 작업은 Snapshot·충돌 검사에 들어가지 않고, 메인이 끝날 때 READY가 된다 (AG-07)."""
    return has_open_case(tx, site_id) or bool(queued_task_ids(tx, site_id))


def insert_requested_task(
    tx: sqlite3.Connection,
    pack: LoadedPack,
    actor: Actor,
    form: TaskRequestForm,
    source_ref: str,
    queued: bool,
    context_version: int,
    origins: dict[str, str] | None = None,
    base: TaskBase | None = None,
    schedule_id: str | None = None,
) -> dict[str, Any]:
    """검증을 통과한 요청으로 작업 하나를 만든다. 폼·Work Intake·일정 넣기가 같이 쓴다.

    critical field CONFIRMED(source_ref). 현장 버전·재확인·사건은 부르는 쪽이 한다(일정 넣기는 여러
    작업에 한 번만 한다).
    origins(값 이름 → 출처, Work Intake): Agent가 정한 값은 기록에 적는다 (AG-32).
    base: 새 작업의 기준 위치(요청한 시작 범위, 일정은 문서의 배정과 schedule_id). 없으면 기준이 없다:
    폼 요청은 시간창 안 어디든 변경도 지연도 아니다 (CV-29).
    """
    site_id = pack.site_id
    wt = pack.work_types[form.work_type]
    data = form.model_dump()
    task = Task(
        **data,
        revision=1,
        unit_id=actor.unit_id,
        owner_actor_id=actor.actor_id,
        hazard_tags=pack.hazard_tags(form.work_type),
        default_requirements=pack.default_requirements(form.work_type),
        default_demands=pack.default_demands(form.work_type),
        fields=confirmed_fields(data, wt.critical_fields, source_ref, origins),
        lifecycle="QUEUED" if queued else "READY",
    )
    insert_task_revision(tx, site_id, task)
    if base is not None:
        insert_task_base(tx, site_id, base, schedule_id, context_version)
    return {
        "task_id": task.task_id,
        "revision": 1,
        "queued": queued,
    }


def create_requested_task(
    tx: sqlite3.Connection,
    pack: LoadedPack,
    site: Site,
    actor: Actor,
    form: TaskRequestForm,
    source_ref: str,
    cause_kind: str = "FORM",
    origins: dict[str, str] | None = None,
    base: TaskBase | None = None,
) -> dict[str, Any]:
    """작업 하나의 접수(폼·Work Intake): 작업을 만들고, 대기열이 아니면 Context +1, RECHECK,
    작업 준비됨 사건. source_ref만 다르면 같은 작업이 된다. base는 Work Intake가 낸 기준 위치다."""
    site_id = site.site_id
    queued = waits_in_queue(tx, site_id)
    context_version = site.context_version if queued else bump_context_version(tx, site_id)
    refs = insert_requested_task(
        tx, pack, actor, form, source_ref, queued, context_version, origins, base=base
    )
    if not queued:
        cause = {"kind": cause_kind, "task_id": form.task_id, "actor_id": actor.actor_id}
        register_recheck(tx, site_id, cause)
        deliver_event(tx, pack, "TASK_READY", f"TASK_READY:{form.task_id}:1", cause)
    return refs


def _handle(tx: sqlite3.Connection, ctx: CommandContext, form: TaskRequestForm) -> Result:
    r = Result()
    if not ctx.has_role("UNIT_PLANNER") or ctx.actor is None:
        r.reject("NOT_AUTHORIZED")
        return r
    for code in validate_task_request(tx, ctx.pack, ctx.site, ctx.actor, form):
        r.reject(code)
    if r.reason_codes:
        return r
    form_id = new_id("form")
    refs = create_requested_task(tx, ctx.pack, ctx.site, ctx.actor, form, f"form:{form_id}")
    r.refs = {
        "task_id": refs["task_id"],
        "revision": refs["revision"],
        "queued": refs["queued"],
        "form_id": form_id,
    }
    return r


def submit_task_request(
    pack: LoadedPack, actor_id: str, idempotency_key: str, form: TaskRequestForm
) -> CommandOutcome:
    return run_command(pack, COMMAND, actor_id, idempotency_key, form, _handle)


# ── 요청 철회 ──────────────────────────────────────

WITHDRAW_COMMAND = "WITHDRAW_TASK_REQUEST"


class TaskWithdraw(Body):
    task_id: str = Field(min_length=1)
    comment: str = ""


def _withdraw(tx: sqlite3.Connection, ctx: CommandContext, body: TaskWithdraw) -> Result:
    """해결하지 못한 요청(Plan에 없는 READY 작업) 또는 대기열(QUEUED) 작업을 계산 대상에서 뺀다.

    READY를 남겨 두면 기준 위치에 고정 상수로 남아 이후 모든 Solver 호출이 INFEASIBLE이 된다.
    고정된 작업은 철회할 수 없다(TASK_PINNED): 먼저 고정을 푼다.
    - READY: 새 revision(NEEDS_INFO) + context +1 + RECHECK. 열린 협의 Run은 STALE, 열린 재계획 Run은
      wake(충돌 전체를 다시 관찰한다).
    - QUEUED: 사실에 들어간 적이 없으므로 새 revision(NEEDS_INFO)만. context·RECHECK·Run 영향 없음.
    """
    r = Result()
    site_id = ctx.site_id
    active = [
        t for t in list_current_tasks(tx, site_id, ctx.pack) if t.lifecycle in ("READY", "QUEUED")
    ]
    task = next((t for t in active if t.task_id == body.task_id), None)
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
    # 고정된 작업은 먼저 고정을 풀어야 한다: 철회로 고정(특히 Supervisor가 건 것)을 우회하지 않는다 (AG-27)
    if any(p.task_id == task.task_id for p in list_active_pins(tx, site_id)):
        r.reject("TASK_PINNED")
        return r
    # 이 작업을 선행으로 가진 READY·QUEUED 작업이 있으면 후속 요청을 먼저 철회해야 한다.
    if any(p.task_id == task.task_id for t in active for p in t.predecessors):
        r.reject("TASK_HAS_SUCCESSORS")
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
        if run.agent_type == "COORDINATION":
            # Context가 올라 협의 중인 후보가 무효다
            end_case_run(tx, ctx.pack, run.run_id, "STALE", f"WITHDRAW:{task.task_id}")
        elif run.agent_type == "REPLANNING":
            # 다른 작업의 철회는 Event Response·Intake Run의 판단 근거가 아니다(깨우지 않는다)
            wake_run(tx, site_id, run.run_id)
    cause = {"kind": "WITHDRAW", "task_id": task.task_id, "actor_id": ctx.actor_id}
    key = f"TASK_REQUEST_WITHDRAWN:{task.task_id}:{revision}"
    register_recheck(tx, site_id, cause)
    deliver_event(tx, ctx.pack, "TASK_REQUEST_WITHDRAWN", key, cause)
    return r


def withdraw_task_request(
    pack: LoadedPack, actor_id: str, idempotency_key: str, body: TaskWithdraw
) -> CommandOutcome:
    return run_command(pack, WITHDRAW_COMMAND, actor_id, idempotency_key, body, _withdraw)
