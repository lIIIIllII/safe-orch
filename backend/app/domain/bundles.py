"""일정 검토의 묶음 계산. 순수 계산이다.

서버가 계산하는 것은 최소 묶음(작업을 공유하는 충돌, domain/groups.py), 최소 묶음 사이의 관계, 사람만 풀 수
있는 최소 묶음뿐이다. 실제 묶음(최소 묶음을 어떻게 합칠지)은 일정 검토 Agent가 정하고, 서버는 그 묶음이
최소 묶음의 합인지만 검사한다 (AG-36). 서버가 묶음을 대신 정하지 않는다.
"""

from collections.abc import Sequence
from typing import Any

from app.domain.eligibility import exclusion_reasons
from app.domain.groups import ConflictGroup
from app.domain.models import SnapshotContent

# 작업을 옮기거나 자원을 바꿔도 풀리지 않는 충돌: 사실(풀, 선행 작업)이 바뀌어야 한다
FACT_ONLY_RULES = ("POOL_MISSING", "PREDECESSOR_MISSING")


def human_only_reason(facts: SnapshotContent, group: ConflictGroup) -> str | None:
    """그 최소 묶음을 사람만 풀 수 있는 이유. 재계획으로 풀 수 있으면 None.

    ALL_PINNED: 걸린 작업이 모두 고정되어 움직일 작업이 없다. FACT_REQUIRED: 작업을 옮겨도 풀리지 않는
    충돌(필수 풀 없음, 선행 작업 없음)이 들어 있다."""
    if set(group.task_ids) <= facts.pinned_task_ids():
        return "ALL_PINNED"
    if any(c.rule_id in FACT_ONLY_RULES for c in group.conflicts):
        return "FACT_REQUIRED"
    return None


def group_span(group: ConflictGroup) -> tuple[int, int]:
    """그 최소 묶음의 충돌이 덮는 시간 [가장 이른 시작, 가장 늦은 끝)."""
    return (
        min(c.interval[0] for c in group.conflicts),
        max(c.interval[1] for c in group.conflicts),
    )


def _usable_resources(facts: SnapshotContent, group: ConflictGroup) -> set[str]:
    """그 묶음의 작업이 쓸 수 있는 자원: 기준 자원과, 고정되지 않은 작업의 적격 대안."""
    base, tasks, pinned = facts.base_assignments(), facts.task_map(), facts.pinned_task_ids()
    out: set[str] = set()
    for tid in group.task_ids:
        task = tasks.get(tid)
        if task is None:
            continue
        if base[tid].resource_id is not None:
            out.add(base[tid].resource_id)
        if task.required_resource_type is None or tid in pinned:
            continue
        out |= {
            r.resource_id for r in facts.resources if not exclusion_reasons(task, r, task.unit_id)
        }
    return out


def group_relations(
    facts: SnapshotContent, groups: Sequence[ConflictGroup]
) -> list[dict[str, Any]]:
    """최소 묶음 사이의 관계 (묶음 쌍마다 하나). 따로 풀면 다시 부딪힐 수 있는 사이를 판단할 근거다.

    shared_resource_ids: 두 묶음의 작업이 함께 쓸 수 있는 자원(기준 자원과 적격 대안).
    zone_links: 두 묶음의 작업 구역이 같거나(SAME) 구역 관계가 선언된 쌍.
    gap_minutes: 두 묶음의 충돌 시간 사이의 간격(겹치면 0)."""
    tasks = facts.task_map()
    usable = {g.group_id: _usable_resources(facts, g) for g in groups}
    zones = {
        g.group_id: sorted({tasks[t].zone_id for t in g.task_ids if t in tasks}) for g in groups
    }
    out = []
    for i, a in enumerate(groups):
        for b in groups[i + 1 :]:
            links = []
            for za in zones[a.group_id]:
                for zb in zones[b.group_id]:
                    relation = facts.rel(za, zb) or facts.rel(zb, za)
                    if relation is not None:
                        links.append({"zone_a": za, "zone_b": zb, "relation": relation})
            (lo_a, hi_a), (lo_b, hi_b) = group_span(a), group_span(b)
            out.append(
                {
                    "group_a": a.group_id,
                    "group_b": b.group_id,
                    "shared_resource_ids": sorted(usable[a.group_id] & usable[b.group_id]),
                    "zone_links": links,
                    "gap_minutes": max(0, max(lo_a, lo_b) - min(hi_a, hi_b)),
                }
            )
    return out


def check_bundles(
    group_ids: Sequence[str], bundles: Sequence[Sequence[str]]
) -> list[dict[str, Any]]:
    """묶음이 최소 묶음의 합인가. 어긴 것의 목록 [{code, group_ids}]이고 비면 통과다.

    UNKNOWN_GROUP 없는 최소 묶음 ID, GROUP_REPEATED 한 최소 묶음이 두 번 들어감(둘로 쪼개 넣은 것: 같은
    작업을 따로 움직이게 된다), GROUP_MISSING 빠진 최소 묶음. 모든 최소 묶음이 정확히 한 번 들어가야 한다."""
    known = list(group_ids)
    seen: list[str] = [gid for bundle in bundles for gid in bundle]
    unknown = sorted({g for g in seen if g not in known})
    repeated = sorted({g for g in seen if g in known and seen.count(g) > 1})
    missing = [g for g in known if g not in seen]
    found = (("UNKNOWN_GROUP", unknown), ("GROUP_REPEATED", repeated), ("GROUP_MISSING", missing))
    return [{"code": code, "group_ids": ids} for code, ids in found if ids]
