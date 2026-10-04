"""사실 변경: 두 Snapshot 사이에 사람이 바꾼 사실. DB를 읽지 않는 순수 함수다.

후보의 기준 계획을 확정할 때의 사실과 그 후보가 계산된 사실을 비교해, 무엇 때문에 다시 계획하거나
다시 확정하는지 보인다. 배치가 그대로인 재확정 후보에도 나온다. 값은 분·ID 그대로다(화면이 풀어 쓴다).
"""

from typing import Any

from app.domain.models import PreferredWindow, SnapshotContent

# 작업 카드에서 고칠 수 있는 값 (사실 수정 확정·카드의 자원 바꾸기로 바뀐 값도 같은 모양으로 나온다)
VALUES = (
    "zone_id",
    "duration",
    "earliest_start",
    "latest_start",
    "latest_end",
    "required_resource_type",
    "requested_resource_id",
)


def _hope(window: PreferredWindow | None) -> dict[str, Any] | None:
    return None if window is None else window.model_dump(include={"start", "end", "origin"})


def fact_changes(before: SnapshotContent, after: SnapshotContent) -> list[dict[str, Any]]:
    """before → after 사이의 사실 변경 (작업 ID순).

    - TASK_ADDED 새로 들어온 작업, TASK_REMOVED 없앴거나 철회한 작업
    - VALUE_CHANGED 작업 값이 바뀜(field, before, after)
    - PINNED·UNPINNED 고정·고정 해제
    - PREFERRED_WINDOW_CHANGED 희망 영역을 그리거나 지우거나 확인함(before·after: 구간과 출처, 없으면 None)
    """
    out: list[dict[str, Any]] = []
    old, new = before.task_map(), after.task_map()
    for tid in sorted(old.keys() | new.keys()):
        if tid not in new:
            out.append({"kind": "TASK_REMOVED", "task_id": tid})
        elif tid not in old:
            out.append({"kind": "TASK_ADDED", "task_id": tid})
        else:
            for name in VALUES:
                a, b = getattr(old[tid], name), getattr(new[tid], name)
                if a != b:
                    out.append(
                        {
                            "kind": "VALUE_CHANGED",
                            "task_id": tid,
                            "field": name,
                            "before": a,
                            "after": b,
                        }
                    )
    kept = old.keys() & new.keys()
    pinned_before = {p.task_id: p for p in before.pins}
    pinned_after = {p.task_id: p for p in after.pins}
    for tid in sorted(kept):
        if tid in pinned_after and tid not in pinned_before:
            pin = pinned_after[tid]
            out.append(
                {
                    "kind": "PINNED",
                    "task_id": tid,
                    "pinned_by": pin.pinned_by,
                    "by_role": pin.by_role,
                }
            )
        elif tid in pinned_before and tid not in pinned_after:
            out.append({"kind": "UNPINNED", "task_id": tid})
    wanted_before, wanted_after = before.preferred_map(), after.preferred_map()
    for tid in sorted(kept):
        a, b = _hope(wanted_before.get(tid)), _hope(wanted_after.get(tid))
        if a != b:
            out.append(
                {"kind": "PREFERRED_WINDOW_CHANGED", "task_id": tid, "before": a, "after": b}
            )
    return out
