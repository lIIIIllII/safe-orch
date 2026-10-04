"""담당자의 직접 이동과 작업 없애기 (AG-31). 사람만 한다. Agent·Tool Gateway에는 이 함수가 없다.

담당자가 타임라인에서 자기 작업의 시각을 옮기거나, 작업 카드에서 자원을 바꾸거나, 자기 작업을 없앤다. 한 트랜잭션에서 사람이 만든 후보
(MOVE·REMOVE) + 검증 + Plan을 남기고 바로 확정한다(Supervisor 승인 없음). 검토 대기열에 오르지 않는다.
- 할 수 없는 것: 남의 작업, 고정된 작업, Plan에 없는 작업, ACTIVE Hold 중(ST-13), 검증을 통과하지
  못하는 배치(이동: 시간창 밖, 근무 달력, 다른 작업과의 Rule 위반 / 없애기: 뒤에 이어지는 작업이 있음).
- 놓을 수 있는 시작 구간(move_range)과 놓은 자리의 판정(move_check), 확정(move_task)은 같은 함수
  (_judge)로 판정한다. 없애기의 확인(remove_check)과 확정(remove_task)도 같은 함수(_remove_verdict)다.
  확인은 읽기 전용이다.
- 자원 바꾸기(change_resource)는 지금 시각 그대로 자원만 바꾼 배치를 같은 함수로 판정한다. 확정하면
  요청 자원도 새 자원으로 바뀌고 출처는 말함이 된다. Plan에 없는 요청은 값 고치기(EDIT_TASK)로 한다.
- 없앤 작업은 지우지 않고 새 revision에 CANCELLED로 남긴다(Snapshot·충돌·관찰에서 빠진다). Plan에 없는
  요청 작업은 이 명령이 아니라 요청 철회(withdraw_task_request)로 없앤다.
- 확정되면 Plan revision이 올라 살아 있는 후보는 기존 판정으로 무효가 된다. 열린 재계획·협의 Run은
  깨우고, 열린 메인이 있을 때만 사건(TASK_MOVED·TASK_REMOVED)을 전하고, 재확인을 등록한다. 그 작업과
  안전 규칙으로 엮인 작업의 담당자에게 서버 문구로 통지한다. 옮긴 작업을 고정하지 않는다.
"""

import sqlite3
from collections.abc import Callable
from typing import Any

from pydantic import Field

from app.commands.service import Body, CommandContext, CommandOutcome, Result, run_command
from app.commands.task_edit import edited_fields
from app.domain.calendar import local_clock
from app.domain.canonical import canonical_hash
from app.domain.hashes import candidate_hash
from app.domain.ids import new_id
from app.domain.models import Assignment, Candidate, Plan, Snapshot, SnapshotContent, Validation
from app.packs.loader import LoadedPack
from app.rules.engine import separation_links
from app.store.repos.cases import (
    deliver_to_open_main,
    register_recheck,
    wake_run,
)
from app.store.repos.consultations import list_review_queue
from app.store.repos.messages import insert_message
from app.store.repos.plans import insert_plan
from app.store.repos.records import insert_candidate, insert_validation
from app.store.repos.runs import list_active_runs
from app.store.repos.site import bump_context_version, bump_plan_revision
from app.store.repos.snapshots import build_snapshot_content, create_snapshot
from app.store.repos.tasks import insert_task_revision, list_current_tasks
from app.validator.validator import validate

SNAP_MIN = 5  # 놓을 수 있는 시작의 격자(분). 화면의 맞춤 단위와 같다


class MoveRequest(Body):
    task_id: str = Field(min_length=1)
    start: int


class RemoveRequest(Body):
    task_id: str = Field(min_length=1)


class ResourceRequest(Body):
    task_id: str = Field(min_length=1)
    resource_id: str = Field(min_length=1)


def _refusal(facts: SnapshotContent, task_id: str, actor_id: str) -> str | None:
    """자리와 관계없이 옮길 수 없는 사유."""
    task = facts.task_map().get(task_id)
    if task is None:
        return "TASK_NOT_FOUND"
    if task.owner_actor_id != actor_id:
        return "NOT_AUTHORIZED"
    if facts.holds:
        return "HOLD_ACTIVE"
    if task_id in facts.pinned_task_ids():
        return "TASK_PINNED"
    if all(a.task_id != task_id for a in facts.plan.assignments):
        return "TASK_NOT_IN_PLAN"
    return None


def _judge(
    snapshot: Snapshot,
    facts: SnapshotContent,
    pack: LoadedPack,
    task_id: str,
    start: int,
    actor_id: str,
    resource_id: str | None = None,
) -> tuple[Candidate, Validation]:
    """현재 Plan에서 그 작업의 시작(또는 자원)만 바꾼 배치를 Validator로 본다. 구간 계산·놓기·확정과
    카드의 자원 바꾸기가 같이 쓴다. resource_id를 주지 않으면 자원은 그대로다."""
    tasks = facts.task_map()
    assignments = tuple(
        a
        if a.task_id != task_id
        else Assignment(
            task_id=task_id,
            start=start,
            end=start + tasks[task_id].duration,
            resource_id=resource_id or a.resource_id,
        )
        for a in facts.plan.assignments
        if a.task_id in tasks
    )
    candidate = Candidate(
        candidate_id=new_id("cand"),
        snapshot_id=snapshot.snapshot_id,
        search_spec_id=None,
        search_spec_hash=None,
        solver_result_id=None,
        base_plan_revision=facts.plan_revision,
        context_version=facts.context_version,
        pack_hash=facts.pack_hash,
        assignments=assignments,
        candidate_hash=candidate_hash(
            assignments,
            facts.plan_revision,
            facts.context_version,
            snapshot.snapshot_hash,
            None,
            facts.pack_hash,
        ),
        kind="MOVE",
        made_by=actor_id,
    )
    return candidate, validate(snapshot, candidate, None, pack)


def _verdict(
    snapshot: Snapshot,
    facts: SnapshotContent,
    pack: LoadedPack,
    task_id: str,
    start: int,
    actor_id: str,
) -> tuple[list[str], Candidate | None, Validation | None]:
    """그 자리로 옮길 수 있는지. 사유가 비어 있으면 옮길 수 있다."""
    refusal = _refusal(facts, task_id, actor_id)
    if refusal is not None:
        return [refusal], None, None
    current = next(a for a in facts.plan.assignments if a.task_id == task_id)
    if start == current.start:
        return ["NO_CHANGE"], None, None
    candidate, validation = _judge(snapshot, facts, pack, task_id, start, actor_id)
    if validation.status != "PASS":
        failed = [c.reason_code for c in validation.checks if c.reason_code is not None]
        return ["MOVE_NOT_VALID", *dict.fromkeys(failed)], candidate, validation
    return [], candidate, validation


def _probe(conn: sqlite3.Connection, pack: LoadedPack) -> tuple[Snapshot, SnapshotContent]:
    """저장하지 않는 Snapshot (읽기 전용 판정용)."""
    content = build_snapshot_content(conn, pack.site_id, pack)
    snapshot = Snapshot(snapshot_id="move", snapshot_hash=canonical_hash(content), content=content)
    return snapshot, snapshot.facts()


def move_range(
    conn: sqlite3.Connection, pack: LoadedPack, actor_id: str, task_id: str
) -> dict[str, Any]:
    """그 작업을 놓을 수 있는 시작 구간 (읽기 전용). 시간창 안의 격자 시작마다 _judge로 본다.

    reason_codes가 있으면 어디에도 놓을 수 없다. invalidates는 확정하면 무효가 될 검토 중인 안이다.
    """
    snapshot, facts = _probe(conn, pack)
    out: dict[str, Any] = {
        "task_id": task_id,
        "snap": SNAP_MIN,
        "context_version": facts.context_version,
        "plan_revision": facts.plan_revision,
        "reason_codes": [],
        "ranges": [],
        "invalidates": list_review_queue(conn, pack.site_id),
    }
    refusal = _refusal(facts, task_id, actor_id)
    if refusal is not None:
        out["reason_codes"] = [refusal]
        return out
    task = facts.task_map()[task_id]
    first = -(-task.earliest_start // SNAP_MIN) * SNAP_MIN
    for start in range(first, task.latest_start + 1, SNAP_MIN):
        _, validation = _judge(snapshot, facts, pack, task_id, start, actor_id)
        if validation.status != "PASS":
            continue
        if out["ranges"] and out["ranges"][-1]["start_max"] == start - SNAP_MIN:
            out["ranges"][-1]["start_max"] = start
        else:
            out["ranges"].append({"start_min": start, "start_max": start})
    return out


def move_check(
    conn: sqlite3.Connection, pack: LoadedPack, actor_id: str, task_id: str, start: int
) -> dict[str, Any]:
    """놓은 자리의 판정 (읽기 전용). 확정과 같은 판정이다."""
    snapshot, facts = _probe(conn, pack)
    codes, _, _ = _verdict(snapshot, facts, pack, task_id, start, actor_id)
    return {
        "task_id": task_id,
        "start": start,
        "ok": not codes,
        "reason_codes": codes,
        "invalidates": list_review_queue(conn, pack.site_id),
    }


def _clock(pack: LoadedPack, minute: int) -> str:
    return local_clock(pack.horizon_start_utc, pack.timezone, minute)


def _linked(links: list[tuple[str, str]]) -> str:
    return ", ".join(f"{task_id}(안전 규칙 '{rule}')" for task_id, rule in links)


def move_notice_text(
    pack: LoadedPack,
    plan_revision: int,
    moved: str,
    before: Assignment,
    after: Assignment,
    links: list[tuple[str, str]],
) -> str:
    """직접 이동 통지의 서버 문구. links는 (받는 사람의 작업, 안전 규칙 표시 이름)."""
    return (
        f"담당자가 작업 {moved}을(를) 직접 옮겨 계획 R{plan_revision}이 확정되었습니다: "
        f"시작 {_clock(pack, before.start)} → {_clock(pack, after.start)}. "
        f"이 작업과 엮인 작업: {_linked(links)}."
    )


def resource_notice_text(
    plan_revision: int,
    moved: str,
    before: str | None,
    after: str,
    links: list[tuple[str, str]],
) -> str:
    """카드의 자원 바꾸기 통지의 서버 문구."""
    return (
        f"담당자가 작업 {moved}의 자원을 바꿔 계획 R{plan_revision}이 확정되었습니다: "
        f"자원 {before} → {after}. 이 작업과 엮인 작업: {_linked(links)}."
    )


def remove_notice_text(
    pack: LoadedPack,
    plan_revision: int,
    removed: str,
    before: Assignment,
    links: list[tuple[str, str]],
) -> str:
    """작업 없애기 통지의 서버 문구."""
    return (
        f"담당자가 작업 {removed}을(를) 없애 계획 R{plan_revision}이 확정되었습니다"
        f"(있던 자리: {_clock(pack, before.start)}–{_clock(pack, before.end)}). "
        f"이 작업과 엮여 있던 작업: {_linked(links)}."
    )


def _notify_linked(
    tx: sqlite3.Connection,
    ctx: CommandContext,
    facts: SnapshotContent,
    candidate: Candidate,
    task_id: str,
    text: Callable[[list[tuple[str, str]]], str],
) -> list[str]:
    """그 작업과 안전 규칙(SEPARATION)으로 엮인 Plan 작업의 담당자에게 통지한다. 판단이 없는 통지라
    서버가 보낸다(Run 없음). 옮기거나 없앤 사람 자신에게는 보내지 않는다."""
    tasks = facts.task_map()
    names = {r.rule_id: r.display_name for r in ctx.pack.rules}
    links: dict[str, list[tuple[str, str]]] = {}
    for a in candidate.assignments:
        other = tasks[a.task_id]
        if other.task_id == task_id or other.owner_actor_id == ctx.actor_id:
            continue
        for rule_id in separation_links(ctx.pack, tasks[task_id], other):
            links.setdefault(other.owner_actor_id, []).append((other.task_id, names[rule_id]))
    for owner in sorted(links):
        insert_message(
            tx,
            ctx.site_id,
            new_id("msg"),
            run_id=None,
            step_no=None,
            to_actor_id=owner,
            type_="NOTICE",
            proposal_id=None,
            body=text(links[owner]),
            agent_text=None,
            context_version=ctx.site.context_version,
            candidate_id=candidate.candidate_id,
        )
    return sorted(links)


def _commit(
    tx: sqlite3.Connection,
    ctx: CommandContext,
    candidate: Candidate,
    validation: Validation,
    event: str,
    task_id: str,
    context_version: int,
) -> int:
    """사람이 만든 후보를 바로 확정한다: 후보·검증·Plan 기록, 열린 Run 깨우기, 사건, 재확인. 새 Plan revision."""
    site_id = ctx.site_id
    insert_candidate(tx, site_id, candidate)
    insert_validation(tx, site_id, validation)
    plan_revision = bump_plan_revision(tx, site_id)
    insert_plan(
        tx,
        site_id,
        Plan(
            plan_revision=plan_revision,
            assignments=candidate.assignments,
            candidate_id=candidate.candidate_id,
            committed_context_version=context_version,
        ),
    )
    # 고정·해제와 같은 방식: 열린 재계획·협의 Run은 다시 관찰하게 깨우고, 열린 메인에만 사건을 전한다
    for run in list_active_runs(tx, site_id):
        if run.agent_type in ("REPLANNING", "COORDINATION"):
            wake_run(tx, site_id, run.run_id)
    ref = {
        "task_id": task_id,
        "actor_id": ctx.actor_id,
        "candidate_id": candidate.candidate_id,
        "plan_revision": plan_revision,
    }
    deliver_to_open_main(tx, ctx.pack, event, f"{event}:{candidate.candidate_id}", ref)
    register_recheck(tx, site_id, {"kind": candidate.kind, "plan_revision": plan_revision})
    return plan_revision


def _move(tx: sqlite3.Connection, ctx: CommandContext, body: MoveRequest) -> Result:
    r = Result()
    snapshot = create_snapshot(tx, ctx.site_id, ctx.pack)
    facts = snapshot.facts()
    codes, candidate, validation = _verdict(
        snapshot, facts, ctx.pack, body.task_id, body.start, ctx.actor_id
    )
    for code in codes:
        r.reject(code)
    if r.reason_codes or candidate is None or validation is None:
        return r

    plan_revision = _commit(
        tx, ctx, candidate, validation, "TASK_MOVED", body.task_id, ctx.site.context_version
    )
    before = next(a for a in facts.plan.assignments if a.task_id == body.task_id)
    after = next(a for a in candidate.assignments if a.task_id == body.task_id)
    notified = _notify_linked(
        tx,
        ctx,
        facts,
        candidate,
        body.task_id,
        lambda links: move_notice_text(ctx.pack, plan_revision, body.task_id, before, after, links),
    )
    r.refs = {
        "task_id": body.task_id,
        "plan_revision": plan_revision,
        "candidate_id": candidate.candidate_id,
        "validation_id": validation.validation_id,
        "notified": notified,
    }
    return r


def move_task(
    pack: LoadedPack, actor_id: str, idempotency_key: str, body: MoveRequest
) -> CommandOutcome:
    return run_command(pack, "MOVE_TASK", actor_id, idempotency_key, body, _move)


# ── 카드에서 자원 바꾸기 ───────────────────────────────────────


def _resource_verdict(
    snapshot: Snapshot,
    facts: SnapshotContent,
    pack: LoadedPack,
    task_id: str,
    resource_id: str,
    actor_id: str,
) -> tuple[list[str], Candidate | None, Validation | None]:
    """그 자원으로 바꿀 수 있는지. 지금 시각 그대로 자원만 바꾼 배치를 직접 이동과 같은 함수로 본다."""
    refusal = _refusal(facts, task_id, actor_id)
    if refusal is not None:
        return [refusal], None, None
    current = next(a for a in facts.plan.assignments if a.task_id == task_id)
    if resource_id == current.resource_id:
        return ["NO_CHANGE"], None, None
    if resource_id not in facts.resource_map():
        return ["RESOURCE_NOT_FOUND"], None, None
    candidate, validation = _judge(
        snapshot, facts, pack, task_id, current.start, actor_id, resource_id
    )
    if validation.status != "PASS":
        failed = [c.reason_code for c in validation.checks if c.reason_code is not None]
        return ["MOVE_NOT_VALID", *dict.fromkeys(failed)], candidate, validation
    return [], candidate, validation


def resource_check(
    conn: sqlite3.Connection, pack: LoadedPack, actor_id: str, task_id: str, resource_id: str
) -> dict[str, Any]:
    """작업 카드의 자원 바꾸기 확인 (읽기 전용). Plan에 있는 작업은 확정과 같은 판정이다.

    Plan에 없는 요청 작업(READY·QUEUED)은 요청 자원을 고친다(path EDIT): 판정은 값 고치기 명령이 한다.
    """
    snapshot, facts = _probe(conn, pack)
    queue = list_review_queue(conn, pack.site_id)
    task = next(
        (t for t in list_current_tasks(conn, pack.site_id, pack) if t.task_id == task_id), None
    )
    in_plan = any(a.task_id == task_id for a in facts.plan.assignments)
    base = {"task_id": task_id, "resource_id": resource_id}
    if task is not None and not in_plan and task.lifecycle in ("READY", "QUEUED"):
        invalidates = queue if task.lifecycle == "READY" else []
        return {**base, "path": "EDIT", "ok": True, "reason_codes": [], "invalidates": invalidates}
    codes, _, _ = _resource_verdict(snapshot, facts, pack, task_id, resource_id, actor_id)
    return {**base, "path": "MOVE", "ok": not codes, "reason_codes": codes, "invalidates": queue}


def _change_resource(tx: sqlite3.Connection, ctx: CommandContext, body: ResourceRequest) -> Result:
    r = Result()
    site_id = ctx.site_id
    snapshot = create_snapshot(tx, site_id, ctx.pack)
    facts = snapshot.facts()
    codes, candidate, validation = _resource_verdict(
        snapshot, facts, ctx.pack, body.task_id, body.resource_id, ctx.actor_id
    )
    for code in codes:
        r.reject(code)
    if r.reason_codes or candidate is None or validation is None:
        return r

    # 요청 자원도 새 자원으로 바꾼다. 담당자가 직접 고른 값이라 출처는 말함이다 (AG-33)
    task = facts.task_map()[body.task_id]
    revision = task.revision + 1
    data = {**task.model_dump(), "requested_resource_id": body.resource_id}
    source = f"card:{new_id('edit')}"
    updated = task.model_copy(
        update={
            "revision": revision,
            "requested_resource_id": body.resource_id,
            "fields": edited_fields(task, data, {"requested_resource_id"}, source),
        }
    )
    insert_task_revision(tx, site_id, updated)
    context_version = bump_context_version(tx, site_id)
    plan_revision = _commit(
        tx, ctx, candidate, validation, "TASK_MOVED", body.task_id, context_version
    )
    before = next(a for a in facts.plan.assignments if a.task_id == body.task_id)
    notified = _notify_linked(
        tx,
        ctx,
        facts,
        candidate,
        body.task_id,
        lambda links: resource_notice_text(
            plan_revision, body.task_id, before.resource_id, body.resource_id, links
        ),
    )
    r.refs = {
        "task_id": body.task_id,
        "revision": revision,
        "plan_revision": plan_revision,
        "candidate_id": candidate.candidate_id,
        "validation_id": validation.validation_id,
        "notified": notified,
    }
    return r


def change_resource(
    pack: LoadedPack, actor_id: str, idempotency_key: str, body: ResourceRequest
) -> CommandOutcome:
    return run_command(
        pack, "CHANGE_TASK_RESOURCE", actor_id, idempotency_key, body, _change_resource
    )


# ── 작업 없애기 ────────────────────────────────────────────────


def _remove_verdict(
    conn: sqlite3.Connection,
    snapshot: Snapshot,
    facts: SnapshotContent,
    pack: LoadedPack,
    task_id: str,
    actor_id: str,
) -> tuple[list[str], Candidate | None, Validation | None]:
    """그 작업을 없앨 수 있는지. 현재 Plan에서 그 작업만 뺀 배치를 Validator로 본다. 확인과 확정이
    같이 쓴다. 사유가 비어 있으면 없앨 수 있다."""
    refusal = _refusal(facts, task_id, actor_id)
    if refusal is not None:
        return [refusal], None, None
    # 뒤에 이어지는 작업(이 작업을 선행으로 가진 READY·QUEUED 작업)이 있으면 그쪽을 먼저 없앤다 (CV-09)
    if any(
        p.task_id == task_id
        for t in list_current_tasks(conn, pack.site_id, pack)
        if t.lifecycle in ("READY", "QUEUED")
        for p in t.predecessors
    ):
        return ["TASK_HAS_SUCCESSORS"], None, None
    tasks = facts.task_map()
    assignments = tuple(
        a for a in facts.plan.assignments if a.task_id in tasks and a.task_id != task_id
    )
    candidate = Candidate(
        candidate_id=new_id("cand"),
        snapshot_id=snapshot.snapshot_id,
        search_spec_id=None,
        search_spec_hash=None,
        solver_result_id=None,
        base_plan_revision=facts.plan_revision,
        context_version=facts.context_version,
        pack_hash=facts.pack_hash,
        assignments=assignments,
        candidate_hash=candidate_hash(
            assignments,
            facts.plan_revision,
            facts.context_version,
            snapshot.snapshot_hash,
            None,
            facts.pack_hash,
        ),
        kind="REMOVE",
        made_by=actor_id,
    )
    validation = validate(snapshot, candidate, None, pack)
    if validation.status != "PASS":
        failed = [c.reason_code for c in validation.checks if c.reason_code is not None]
        return ["REMOVE_NOT_VALID", *dict.fromkeys(failed)], candidate, validation
    return [], candidate, validation


def remove_check(
    conn: sqlite3.Connection, pack: LoadedPack, actor_id: str, task_id: str
) -> dict[str, Any]:
    """[작업 없애기]의 확인 (읽기 전용). Plan에 있는 작업은 확정과 같은 판정이다.

    Plan에 없는 요청 작업(READY·QUEUED)은 요청 철회로 없앤다(path WITHDRAW): 판정은 철회 명령이 한다.
    invalidates는 확정하면 무효가 될 검토 중인 안이다(대기열 작업의 철회는 무효로 만들지 않는다).
    """
    snapshot, facts = _probe(conn, pack)
    queue = list_review_queue(conn, pack.site_id)
    task = next(
        (t for t in list_current_tasks(conn, pack.site_id, pack) if t.task_id == task_id), None
    )
    in_plan = any(a.task_id == task_id for a in facts.plan.assignments)
    if task is not None and not in_plan and task.lifecycle in ("READY", "QUEUED"):
        return {
            "task_id": task_id,
            "path": "WITHDRAW",
            "ok": True,
            "reason_codes": [],
            "invalidates": queue if task.lifecycle == "READY" else [],
        }
    codes, _, _ = _remove_verdict(conn, snapshot, facts, pack, task_id, actor_id)
    return {
        "task_id": task_id,
        "path": "REMOVE",
        "ok": not codes,
        "reason_codes": codes,
        "invalidates": queue,
    }


def _remove(tx: sqlite3.Connection, ctx: CommandContext, body: RemoveRequest) -> Result:
    r = Result()
    site_id = ctx.site_id
    snapshot = create_snapshot(tx, site_id, ctx.pack)
    facts = snapshot.facts()
    codes, candidate, validation = _remove_verdict(
        tx, snapshot, facts, ctx.pack, body.task_id, ctx.actor_id
    )
    for code in codes:
        r.reject(code)
    if r.reason_codes or candidate is None or validation is None:
        return r

    # 작업은 지우지 않고 새 revision에 CANCELLED로 남긴다. 사실이 바뀌므로 context +1
    task = facts.task_map()[body.task_id]
    revision = task.revision + 1
    insert_task_revision(
        tx, site_id, task.model_copy(update={"revision": revision, "lifecycle": "CANCELLED"})
    )
    context_version = bump_context_version(tx, site_id)
    plan_revision = _commit(
        tx, ctx, candidate, validation, "TASK_REMOVED", body.task_id, context_version
    )
    before = next(a for a in facts.plan.assignments if a.task_id == body.task_id)
    notified = _notify_linked(
        tx,
        ctx,
        facts,
        candidate,
        body.task_id,
        lambda links: remove_notice_text(ctx.pack, plan_revision, body.task_id, before, links),
    )
    r.refs = {
        "task_id": body.task_id,
        "revision": revision,
        "plan_revision": plan_revision,
        "candidate_id": candidate.candidate_id,
        "validation_id": validation.validation_id,
        "notified": notified,
    }
    return r


def remove_task(
    pack: LoadedPack, actor_id: str, idempotency_key: str, body: RemoveRequest
) -> CommandOutcome:
    return run_command(pack, "REMOVE_TASK", actor_id, idempotency_key, body, _remove)
