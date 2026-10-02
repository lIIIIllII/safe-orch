"""시간창 넓히기 선택지 (부록 A.29 5). Replanning ASK_WINDOW_CHANGE가 담당자에게 묻는 값.

대상 작업 하나의 시간창을 Horizon 끝까지 풀고, 나머지 작업은 기준 배정에 고정한 채 시작 시각을
earliest_start부터 1분씩 옮기며 Rule Engine 검사(기본 제약·Rule)와 자원 겹침을 본다. 첫 가능 시작 s*에서
해가 열리는 가장 좁은 창 하나를 낸다. Solver를 부르지 않는다(app.solver를 import하지 않는다).
한계: 대상 작업 하나만 옮기므로, 다른 작업을 같이 움직이면 더 좁은 창으로 풀리는 경우는 찾지 못한다.
"""

from typing import Any

from app.domain.calendar import start_domain
from app.domain.models import Assignment, SnapshotContent
from app.packs.loader import LoadedPack
from app.rules.engine import conflicts_in


def _resource_overlap(a: Assignment, others: list[Assignment]) -> bool:
    """Solver는 Pack의 CAPACITY Rule 선언과 상관없이 자원 겹침을 막는다(§18.1). 같은 기준으로 본다."""
    return a.resource_id is not None and any(
        o.resource_id == a.resource_id and a.start < o.end and o.start < a.end for o in others
    )


def widen_option(facts: SnapshotContent, pack: LoadedPack, task_id: str) -> dict[str, Any] | None:
    """해가 열리는 가장 좁은 창. 넓힐 필요가 없거나(지금 창 안에 자리가 있음) 자리가 없으면 None.

    나머지 작업끼리 이미 충돌하면 이 작업만 옮겨서는 풀 수 없으므로 None이다. 자원은 기준 배정의
    자원 그대로다. 반환: {task_id, fit_start, resource_id, current{latest_start, latest_end},
    proposed{latest_start, latest_end}}.
    """
    task = facts.task_map()[task_id]
    base = facts.base_assignments()
    others = [a for tid, a in base.items() if tid != task_id]
    if conflicts_in(facts, others, pack):
        return None
    horizon = facts.horizon_minutes
    relaxed = facts.model_copy(
        update={
            "tasks": tuple(
                t.model_copy(update={"latest_start": horizon - t.duration, "latest_end": horizon})
                if t.task_id == task_id
                else t
                for t in facts.tasks
            )
        }
    )
    resource = base[task_id].resource_id
    for lo, hi in start_domain(task.duration, facts.work_intervals):
        for start in range(max(lo, task.earliest_start), hi + 1):
            a = Assignment(
                task_id=task_id, start=start, end=start + task.duration, resource_id=resource
            )
            if _resource_overlap(a, others) or conflicts_in(relaxed, [*others, a], pack):
                continue
            if start <= task.latest_start and a.end <= task.latest_end:
                return None
            return {
                "task_id": task_id,
                "fit_start": start,
                "resource_id": resource,
                "current": {"latest_start": task.latest_start, "latest_end": task.latest_end},
                "proposed": {
                    "latest_start": max(task.latest_start, start),
                    "latest_end": max(task.latest_end, a.end),
                },
            }
    return None
