"""충돌 그룹: 현재 충돌을 공유 작업으로 묶은 것. 순수 계산이다.

그룹 ID는 그룹의 작업 집합에서 만든다(같은 작업 집합이면 같은 ID). 묶음 등록용 엮임 계산은 여기 없다.
"""

from collections.abc import Mapping, Sequence

from app.domain.canonical import canonical_hash
from app.domain.models import Conflict, Frozen, Pin, Task


class ConflictGroup(Frozen):
    group_id: str
    task_ids: tuple[str, ...]  # 정렬
    conflicts: tuple[Conflict, ...]  # 탐지 순서
    # Unit → 그 Unit이 이 그룹에 가진 작업 (정렬)
    units: dict[str, tuple[str, ...]]


def group_id(task_ids: Sequence[str]) -> str:
    return "grp_" + canonical_hash(sorted(task_ids))[:12]


def conflict_groups(
    conflicts: Sequence[Conflict], tasks: Mapping[str, Task]
) -> list[ConflictGroup]:
    """작업을 하나라도 공유하는 충돌끼리 묶는다. 그룹 순서는 첫 충돌의 탐지 순서다."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for c in conflicts:
        for tid in c.task_ids[1:]:
            parent[find(tid)] = find(c.task_ids[0])
    grouped: dict[str, list[Conflict]] = {}
    for c in conflicts:
        grouped.setdefault(find(c.task_ids[0]), []).append(c)
    out = []
    for members in grouped.values():
        ids = tuple(sorted({t for c in members for t in c.task_ids}))
        units: dict[str, list[str]] = {}
        for tid in ids:
            if tid in tasks:
                units.setdefault(tasks[tid].unit_id, []).append(tid)
        out.append(
            ConflictGroup(
                group_id=group_id(ids),
                task_ids=ids,
                conflicts=tuple(members),
                units={u: tuple(t) for u, t in sorted(units.items())},
            )
        )
    return out


def movable_task_ids(
    group: ConflictGroup,
    unit_id: str,
    pins: Sequence[Pin],
) -> list[str]:
    """그 Unit이 이 그룹에 가진 작업 가운데 고정되지 않은 것. 고정되지 않은 작업은 움직일 수 있다 (AG-27)."""
    pinned = {p.task_id for p in pins}
    return [tid for tid in group.units.get(unit_id, ()) if tid not in pinned]
