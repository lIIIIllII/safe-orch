"""Consultation 계산. DB를 읽지 않는 순수 함수다.

item은 후보 snapshot의 사실로 만들고, 상태는 저장하지 않고 조회할 때 계산한다.
"""

from collections.abc import Iterable, Mapping
from typing import Literal

from app.domain.canonical import canonical_hash
from app.domain.models import (
    Assignment,
    Candidate,
    ConsultationItem,
    SnapshotContent,
)

ItemStatus = Literal["PENDING", "ACCEPTED", "OBJECTED", "WAIVED"]
ConsultationStatus = Literal["COMPLETE", "BLOCKED", "OPEN", "CANCELLED"]

DONE = {"ACCEPTED", "WAIVED"}
BLOCKING = {"OBJECTED"}


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
    """기준에서 바뀐 작업마다 item 1개 (AG-33). Solver의 변경 수와 같은 기준이다 (CV-29).

    계획 작업은 승인된 자리에서 시작이나 자원이 바뀌면, 새 작업은 기준 시작 범위 밖으로 놓였거나 기준
    자원이 아닐 때만 항목이 된다. 기준 위치가 없는 새 작업(폼 요청)은 자원이 바뀔 때만이다. 묻지 않는
    범위는 따로 없다: 항목은 모두 담당자 확인을 기다린다.
    """
    base = facts.base_assignments()
    tasks = facts.task_map()
    items = []
    for after in sorted(candidate.assignments, key=lambda a: a.task_id):
        task = tasks.get(after.task_id)
        before = base.get(after.task_id)
        if task is None or before is None or not facts.is_changed(after):
            continue
        items.append(
            ConsultationItem(
                task_id=task.task_id,
                task_revision=task.revision,
                owner_actor_id=task.owner_actor_id,
                before=before,
                after=after,
                change_hash=change_hash(task.task_id, task.revision, before, after),
            )
        )
    return tuple(items)


def item_statuses(
    items: Iterable[ConsultationItem],
    waived_task_ids: Iterable[str],
    answers: Mapping[str, ItemStatus] | None = None,
) -> dict[str, ItemStatus]:
    """item의 실효 상태. WAIVE decision이 덮으면 WAIVED, 그다음 담당자 답, 아니면 확인 대기(PENDING).

    answers: change_hash → ACCEPTED·OBJECTED (변경 요청의 답).
    """
    waived = set(waived_task_ids)
    answered = answers or {}
    return {
        i.task_id: "WAIVED" if i.task_id in waived else answered.get(i.change_hash, "PENDING")
        for i in items
    }


def items_status(statuses: Iterable[str]) -> ConsultationStatus:
    """item만 본 상태. 승인 8단계는 이 값으로 판정한다."""
    found = set(statuses)
    if found & BLOCKING:
        return "BLOCKED"
    if found <= DONE:
        return "COMPLETE"
    return "OPEN"


def consultation_status(
    statuses: Iterable[str], *, committed: bool, stale: bool, rejected: bool
) -> ConsultationStatus:
    """COMMITTED → COMPLETE, STALE·REJECTED → CANCELLED, 그다음 item 상태."""
    if committed:
        return "COMPLETE"
    if stale or rejected:
        return "CANCELLED"
    return items_status(statuses)
