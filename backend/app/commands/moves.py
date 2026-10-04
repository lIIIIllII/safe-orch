"""담당자의 직접 이동 (AG-31). 사람만 한다. Agent·Tool Gateway에는 이 함수가 없다.

담당자가 타임라인에서 자기 작업의 시각을 옮긴다. 한 트랜잭션에서 사람이 만든 후보(MOVE) + 검증 + Plan을
남기고 바로 확정한다(Supervisor 승인 없음). 검토 대기열에 오르지 않는다.
- 옮길 수 없는 것: 남의 작업, 고정된 작업, Plan에 없는 작업, ACTIVE Hold 중(ST-13), 검증을 통과하지
  못하는 자리(시간창 밖, 근무 달력, 다른 작업과의 Rule 위반).
- 놓을 수 있는 시작 구간(move_range)과 놓은 자리의 판정(move_check), 확정(move_task)은 같은 함수
  (_judge)로 판정한다. 앞의 둘은 읽기 전용이다.
- 확정되면 Plan revision이 올라 살아 있는 후보는 기존 판정으로 무효가 된다. 열린 재계획·협의 Run은
  깨우고, 열린 메인이 있을 때만 사건(TASK_MOVED)을 전하고, 재확인을 등록한다. 옮긴 작업과 안전 규칙으로
  엮인 작업의 담당자에게 서버 문구로 통지한다. 옮긴 작업을 고정하지 않는다.
"""

import sqlite3
from typing import Any

from pydantic import Field

from app.commands.service import Body, CommandContext, CommandOutcome, Result, run_command
from app.domain.calendar import local_clock
from app.domain.canonical import canonical_hash
from app.domain.hashes import candidate_hash
from app.domain.ids import new_id
from app.domain.models import Assignment, Candidate, Plan, Snapshot, SnapshotContent, Validation
from app.packs.loader import LoadedPack
from app.rules.engine import separation_links
from app.store.repos.cases import deliver_to_open_main, register_recheck, wake_run
from app.store.repos.consultations import list_review_queue
from app.store.repos.messages import insert_message
from app.store.repos.plans import insert_plan
from app.store.repos.records import insert_candidate, insert_validation
from app.store.repos.runs import list_active_runs
from app.store.repos.site import bump_plan_revision
from app.store.repos.snapshots import build_snapshot_content, create_snapshot
from app.validator.validator import validate

SNAP_MIN = 5  # 놓을 수 있는 시작의 격자(분). 화면의 맞춤 단위와 같다


class MoveRequest(Body):
    task_id: str = Field(min_length=1)
    start: int


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
) -> tuple[Candidate, Validation]:
    """현재 Plan에서 그 작업의 시작만 바꾼 배치를 Validator로 본다. 구간 계산·놓기·확정이 같이 쓴다."""
    tasks = facts.task_map()
    assignments = tuple(
        a
        if a.task_id != task_id
        else Assignment(
            task_id=task_id,
            start=start,
            end=start + tasks[task_id].duration,
            resource_id=a.resource_id,
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
        moved_by=actor_id,
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


def move_notice_text(
    pack: LoadedPack,
    plan_revision: int,
    moved: str,
    before: Assignment,
    after: Assignment,
    links: list[tuple[str, str]],
) -> str:
    """직접 이동 통지의 서버 문구. links는 (받는 사람의 작업, 안전 규칙 표시 이름)."""
    rules = ", ".join(f"{task_id}(안전 규칙 '{rule}')" for task_id, rule in links)
    return (
        f"담당자가 작업 {moved}을(를) 직접 옮겨 계획 R{plan_revision}이 확정되었습니다: "
        f"시작 {_clock(pack, before.start)} → {_clock(pack, after.start)}. "
        f"이 작업과 엮인 작업: {rules}."
    )


def _notify_linked(
    tx: sqlite3.Connection,
    ctx: CommandContext,
    facts: SnapshotContent,
    candidate: Candidate,
    plan_revision: int,
    task_id: str,
) -> list[str]:
    """옮긴 작업과 안전 규칙(SEPARATION)으로 엮인 Plan 작업의 담당자에게 통지한다. 판단이 없는 통지라
    서버가 보낸다(Run 없음). 옮긴 사람 자신에게는 보내지 않는다."""
    tasks = facts.task_map()
    names = {r.rule_id: r.display_name for r in ctx.pack.rules}
    before = next(a for a in facts.plan.assignments if a.task_id == task_id)
    after = next(a for a in candidate.assignments if a.task_id == task_id)
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
            body=move_notice_text(ctx.pack, plan_revision, task_id, before, after, links[owner]),
            agent_text=None,
            context_version=ctx.site.context_version,
            candidate_id=candidate.candidate_id,
        )
    return sorted(links)


def _move(tx: sqlite3.Connection, ctx: CommandContext, body: MoveRequest) -> Result:
    r = Result()
    site_id = ctx.site_id
    snapshot = create_snapshot(tx, site_id, ctx.pack)
    facts = snapshot.facts()
    codes, candidate, validation = _verdict(
        snapshot, facts, ctx.pack, body.task_id, body.start, ctx.actor_id
    )
    for code in codes:
        r.reject(code)
    if r.reason_codes or candidate is None or validation is None:
        return r

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
            committed_context_version=ctx.site.context_version,
        ),
    )
    # 고정·해제와 같은 방식: 열린 재계획·협의 Run은 다시 관찰하게 깨우고, 열린 메인에만 사건을 전한다
    for run in list_active_runs(tx, site_id):
        if run.agent_type in ("REPLANNING", "COORDINATION"):
            wake_run(tx, site_id, run.run_id)
    ref = {
        "task_id": body.task_id,
        "actor_id": ctx.actor_id,
        "candidate_id": candidate.candidate_id,
        "plan_revision": plan_revision,
    }
    deliver_to_open_main(tx, ctx.pack, "TASK_MOVED", f"TASK_MOVED:{candidate.candidate_id}", ref)
    register_recheck(tx, site_id, {"kind": "MOVE", "plan_revision": plan_revision})
    notified = _notify_linked(tx, ctx, facts, candidate, plan_revision, body.task_id)
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
