"""Consultation 계산 (설계서 §9.3, 부록 A.14). DB를 읽지 않는 순수 함수다.

item은 후보 snapshot의 사실과 Consent로 만들고, 상태는 저장하지 않고 조회할 때 계산한다.
"""

from collections.abc import Iterable
from typing import Literal

from app.domain.canonical import canonical_hash
from app.domain.models import Assignment, Candidate, ConsultationItem, SnapshotContent

ItemStatus = Literal[
    "COVERED", "PENDING", "ACCEPTED", "OBJECTED", "OBJECTION_DRAFT_PENDING", "WAIVED"
]
ConsultationStatus = Literal["COMPLETE", "BLOCKED", "OPEN", "CANCELLED"]

DONE = {"COVERED", "ACCEPTED", "WAIVED"}
BLOCKING = {"OBJECTED", "OBJECTION_DRAFT_PENDING"}


def change_hash(task_id: str, task_revision: int, before: Assignment, after: Assignment) -> str:
    return canonical_hash(
        {
            "task_id": task_id,
            "task_revision": task_revision,
            "before": before.model_dump(),
            "after": after.model_dump(),
        }
    )


def build_items(facts: SnapshotContent, candidate: Candidate) -> tuple[ConsultationItem, ...]:
    """기준(snapshot.base_assignments()) 대비 시작이나 자원이 바뀐 작업마다 item 1개.

    바뀐 축마다 그 작업 현재 revision의 같은 축 Consent가 새 값을 덮어야 COVERED다.
    """
    base = facts.base_assignments()
    tasks = facts.task_map()
    items = []
    for after in sorted(candidate.assignments, key=lambda a: a.task_id):
        task = tasks.get(after.task_id)
        before = base.get(after.task_id)
        if task is None or before is None:
            continue
        needed = []
        if after.start != before.start:
            needed.append(("TIME", after.start))
        if after.resource_id != before.resource_id:
            needed.append(("RESOURCE", after.resource_id))
        if not needed:
            continue
        consents = [
            c
            for c in facts.consents
            if c.task_id == task.task_id and c.task_revision == task.revision
        ]
        covered = all(
            any(c.axis == axis and c.covers(value) for c in consents) for axis, value in needed
        )
        items.append(
            ConsultationItem(
                task_id=task.task_id,
                task_revision=task.revision,
                owner_actor_id=task.owner_actor_id,
                before=before,
                after=after,
                change_hash=change_hash(task.task_id, task.revision, before, after),
                base_status="COVERED" if covered else "PENDING",
            )
        )
    return tuple(items)


def item_statuses(
    items: Iterable[ConsultationItem], waived_task_ids: Iterable[str]
) -> dict[str, ItemStatus]:
    """item의 실효 상태. WAIVE decision이 덮으면 WAIVED, 아니면 base_status."""
    waived = set(waived_task_ids)
    return {i.task_id: "WAIVED" if i.task_id in waived else i.base_status for i in items}


def items_status(statuses: Iterable[str]) -> ConsultationStatus:
    """item만 본 상태 (§9.3 표). 승인 8단계는 이 값으로 판정한다."""
    found = set(statuses)
    if found & BLOCKING:
        return "BLOCKED"
    if found <= DONE:
        return "COMPLETE"
    return "OPEN"


def consultation_status(
    statuses: Iterable[str], *, committed: bool, stale: bool, rejected: bool
) -> ConsultationStatus:
    """COMMITTED → COMPLETE, STALE·REJECTED → CANCELLED, 그다음 item 상태 (부록 A.14)."""
    if committed:
        return "COMPLETE"
    if stale or rejected:
        return "CANCELLED"
    return items_status(statuses)
