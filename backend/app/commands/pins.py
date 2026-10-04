"""작업 고정·고정 해제와 희망 영역 (AG-27). 사람만 한다. Agent·Tool Gateway에는 이 함수가 없다.

- 고정: 담당자는 자기 작업, Supervisor는 모든 작업. 고정된 작업은 시각·자원 모두 기준 배정에 묶인다.
  Supervisor가 건 고정은 Supervisor만 푼다. 고정·해제는 context +1(검토 중인 후보는 STALE)이고, 열린
  메인이 있으면 사건으로 전한다(없으면 사건을 만들지 않는다). 열린 재계획·협의 Run은 다시 관찰하게 깨운다.
- 희망 영역: 담당자만 그리고 지운다. 작업당 시각 구간 하나. context를 올리지 않고 사건도 없다.
  서버는 강제하지 않는다.
"""

import sqlite3
from datetime import UTC, datetime

from pydantic import Field

from app.commands.service import Body, CommandContext, CommandOutcome, Result, run_command
from app.domain.ids import new_id
from app.domain.models import Pin, Task
from app.packs.loader import LoadedPack
from app.store.repos.cases import deliver_to_open_main, wake_run
from app.store.repos.pins import (
    clear_preferred_window,
    insert_pin,
    insert_preferred_window,
    list_active_pins,
    release_pin,
)
from app.store.repos.runs import list_active_runs
from app.store.repos.site import bump_context_version
from app.store.repos.tasks import list_current_tasks


class TaskRef(Body):
    task_id: str = Field(min_length=1)


class PreferredWindow(Body):
    task_id: str = Field(min_length=1)
    start: int
    end: int


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _ready_task(tx: sqlite3.Connection, ctx: CommandContext, task_id: str) -> Task | None:
    return next(
        (
            t
            for t in list_current_tasks(tx, ctx.site_id, ctx.pack)
            if t.task_id == task_id and t.lifecycle == "READY"
        ),
        None,
    )


def _notify(tx: sqlite3.Connection, ctx: CommandContext, kind: str, pin: Pin) -> None:
    """고정·해제 뒤: 열린 재계획·협의 Run은 다시 관찰하게 깨우고, 열린 메인에 사건을 전한다."""
    for run in list_active_runs(tx, ctx.site_id):
        if run.agent_type in ("REPLANNING", "COORDINATION"):
            wake_run(tx, ctx.site_id, run.run_id)
    deliver_to_open_main(
        tx,
        ctx.pack,
        kind,
        f"{kind}:{pin.pin_id}",
        {"pin_id": pin.pin_id, "task_id": pin.task_id, "actor_id": ctx.actor_id},
    )


def _pin(tx: sqlite3.Connection, ctx: CommandContext, body: TaskRef) -> Result:
    r = Result()
    task = _ready_task(tx, ctx, body.task_id)
    if task is None:
        r.reject("TASK_NOT_FOUND")
        return r
    supervisor = ctx.has_role("SUPERVISOR")
    if ctx.actor_id != task.owner_actor_id and not supervisor:
        r.reject("NOT_AUTHORIZED")
        return r
    if any(p.task_id == task.task_id for p in list_active_pins(tx, ctx.site_id)):
        r.reject("ALREADY_PINNED")
        return r
    context_version = bump_context_version(tx, ctx.site_id)
    pin = Pin(
        pin_id=new_id("pin"),
        task_id=task.task_id,
        pinned_by=ctx.actor_id,
        by_role="SUPERVISOR" if supervisor else "OWNER",
    )
    insert_pin(tx, ctx.site_id, pin, _now(), context_version)
    _notify(tx, ctx, "TASK_PINNED", pin)
    r.refs = {"pin_id": pin.pin_id, "task_id": task.task_id, "by_role": pin.by_role}
    return r


def pin_task(
    pack: LoadedPack, actor_id: str, idempotency_key: str, body: TaskRef
) -> CommandOutcome:
    return run_command(pack, "PIN_TASK", actor_id, idempotency_key, body, _pin)


def _unpin(tx: sqlite3.Connection, ctx: CommandContext, body: TaskRef) -> Result:
    r = Result()
    pin = next((p for p in list_active_pins(tx, ctx.site_id) if p.task_id == body.task_id), None)
    if pin is None:
        r.reject("PIN_NOT_FOUND")
        return r
    supervisor = ctx.has_role("SUPERVISOR")
    owner = next(
        (
            t.owner_actor_id
            for t in list_current_tasks(tx, ctx.site_id, ctx.pack)
            if t.task_id == body.task_id
        ),
        None,
    )
    # Supervisor가 건 고정은 Supervisor만 푼다. 담당자가 건 고정은 담당자와 Supervisor가 푼다
    if not supervisor and (pin.by_role == "SUPERVISOR" or ctx.actor_id != owner):
        r.reject("NOT_AUTHORIZED")
        return r
    context_version = bump_context_version(tx, ctx.site_id)
    release_pin(tx, ctx.site_id, pin.pin_id, ctx.actor_id, _now(), context_version)
    _notify(tx, ctx, "TASK_UNPINNED", pin)
    r.refs = {"pin_id": pin.pin_id, "task_id": pin.task_id}
    return r


def unpin_task(
    pack: LoadedPack, actor_id: str, idempotency_key: str, body: TaskRef
) -> CommandOutcome:
    return run_command(pack, "UNPIN_TASK", actor_id, idempotency_key, body, _unpin)


# ── 희망 영역 ──────────────────────────────────────────────────


def _set_window(tx: sqlite3.Connection, ctx: CommandContext, body: PreferredWindow) -> Result:
    r = Result()
    task = _ready_task(tx, ctx, body.task_id)
    if task is None:
        r.reject("TASK_NOT_FOUND")
        return r
    if ctx.actor_id != task.owner_actor_id:
        r.reject("NOT_AUTHORIZED")
        return r
    if not 0 <= body.start < body.end <= ctx.site.horizon_minutes:
        r.reject("INVALID_WINDOW")
        return r
    now = _now()
    clear_preferred_window(tx, ctx.site_id, task.task_id, ctx.actor_id, now)
    window_id = new_id("pw")
    insert_preferred_window(
        tx, ctx.site_id, window_id, task.task_id, body.start, body.end, ctx.actor_id, now
    )
    r.refs = {"window_id": window_id, "task_id": task.task_id}
    return r


def set_preferred_window(
    pack: LoadedPack, actor_id: str, idempotency_key: str, body: PreferredWindow
) -> CommandOutcome:
    return run_command(pack, "SET_PREFERRED_WINDOW", actor_id, idempotency_key, body, _set_window)


def _clear_window(tx: sqlite3.Connection, ctx: CommandContext, body: TaskRef) -> Result:
    r = Result()
    task = _ready_task(tx, ctx, body.task_id)
    if task is None:
        r.reject("TASK_NOT_FOUND")
        return r
    if ctx.actor_id != task.owner_actor_id:
        r.reject("NOT_AUTHORIZED")
        return r
    if not clear_preferred_window(tx, ctx.site_id, task.task_id, ctx.actor_id, _now()):
        r.reject("PREFERRED_WINDOW_NOT_FOUND")
        return r
    r.refs = {"task_id": task.task_id}
    return r


def clear_preferred_window_command(
    pack: LoadedPack, actor_id: str, idempotency_key: str, body: TaskRef
) -> CommandOutcome:
    return run_command(
        pack, "CLEAR_PREFERRED_WINDOW", actor_id, idempotency_key, body, _clear_window
    )
