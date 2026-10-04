"""SearchSpec 생성. 서버가 만들고 불변이다.

app.rules·app.validator를 import하지 않는다.
"""

from collections.abc import Mapping, Sequence

from app.domain.eligibility import exclusion_reasons
from app.domain.hashes import search_key, search_spec_hash
from app.domain.ids import new_id
from app.domain.models import (
    Condition,
    Conflict,
    Movable,
    Objective,
    ScopeLevel,
    SearchSpec,
    Snapshot,
)

TIME_LIMIT_S = 10


class SearchSpecError(Exception):
    """SearchSpec을 만들 수 없다. Solver를 호출하지 않는다."""

    def __init__(self, reason_code: str, detail: str = ""):
        self.reason_code = reason_code
        super().__init__(f"{reason_code}: {detail}" if detail else reason_code)


def build_search_spec(
    snapshot: Snapshot,
    conflict: Conflict,
    acting_unit_id: str,
    scope_level: ScopeLevel,
    try_resources: Mapping[str, Sequence[str]] | None = None,
    conditions: Mapping[str, Condition] | None = None,
    objective: Objective = "CHANGE_FIRST",
) -> SearchSpec:
    facts = snapshot.facts()
    base = facts.base_assignments()
    acting = [
        t for t in sorted(facts.tasks, key=lambda t: t.task_id) if t.unit_id == acting_unit_id
    ]

    l0 = [t for t in acting if t.task_id in conflict.task_ids]
    if not l0:
        raise SearchSpecError("NO_ACTING_TASKS", f"{acting_unit_id} has no task in conflict")
    if scope_level == "L0":
        scope = l0
    elif scope_level == "L1":
        zones = {t.zone_id for t in l0}
        res = {base[t.task_id].resource_id for t in l0} - {None}
        scope = [t for t in acting if t.zone_id in zones or base[t.task_id].resource_id in res]
    elif scope_level == "L2":
        scope = acting
    else:
        raise ValueError(f"unknown scope_level {scope_level!r}")

    # 고정되지 않은 작업은 시각이 움직인다. 자원 축은 담당자 확인으로 열린 것만 (AG-27)
    pinned = facts.pinned_task_ids()
    axes = {
        t.task_id: Movable(
            time=t.task_id not in pinned,
            resource=t.movable.resource and t.task_id not in pinned,
        )
        for t in scope
    }

    # 자원 대안은 try_resources로만 추가한다. 적격성 필터(CV-20) 후 비면 Solver를 부르지 않는다.
    tasks = facts.task_map()
    resources = facts.resource_map()
    alternatives: dict[str, tuple[str, ...]] = {}
    for tid, rids in sorted((try_resources or {}).items()):
        if tid not in axes or not axes[tid].resource:
            raise SearchSpecError("RESOURCE_AXIS_NOT_ALLOWED", tid)
        task = tasks[tid]
        ok = []
        for rid in rids:
            r = resources.get(rid)
            if r is not None and not exclusion_reasons(task, r, acting_unit_id) and rid not in ok:
                ok.append(rid)
        if not ok:
            raise SearchSpecError("RESOURCE_NOT_AUTHORIZED", f"{tid}: {list(rids)}")
        alternatives[tid] = tuple(ok)

    # Agent가 건 조건은 좁히기만 한다. 서버는 유효성만 본다 (CV-24)
    conds = dict(sorted((conditions or {}).items()))
    for tid, c in conds.items():
        if tid in pinned and tid in tasks:
            raise SearchSpecError("TASK_PINNED", tid)
        if tid not in axes:
            raise SearchSpecError("CONDITION_TASK_NOT_IN_SCOPE", tid)
        task = tasks[tid]
        lo, hi = c.start_min, c.start_max
        if (lo, hi, c.resource_id) == (None, None, None) or (
            lo is not None and hi is not None and lo > hi
        ):
            raise SearchSpecError("CONDITION_INVALID", tid)
        if any(
            v is not None and not task.earliest_start <= v <= task.latest_start for v in (lo, hi)
        ):
            raise SearchSpecError("CONDITION_OUTSIDE_WINDOW", tid)
        if c.resource_id is not None and c.resource_id != base[tid].resource_id:
            # 기준 자원이 아니면 자원 축이 열려 있고 적격인 자원이어야 한다 (CV-20)
            if not axes[tid].resource:
                raise SearchSpecError("RESOURCE_AXIS_NOT_ALLOWED", tid)
            r = resources.get(c.resource_id)
            if r is None or exclusion_reasons(task, r, acting_unit_id):
                raise SearchSpecError("RESOURCE_NOT_ELIGIBLE", f"{tid}: {c.resource_id}")
            alternatives[tid] = tuple(dict.fromkeys((*alternatives.get(tid, ()), c.resource_id)))
    alternatives = dict(sorted(alternatives.items()))

    return SearchSpec(
        search_spec_id=new_id("ss"),
        hash=search_spec_hash(
            snapshot.snapshot_hash,
            acting_unit_id,
            axes,
            alternatives,
            TIME_LIMIT_S,
            conds,
            objective,
        ),
        snapshot_id=snapshot.snapshot_id,
        acting_unit_id=acting_unit_id,
        scope_level=scope_level,
        axes=axes,
        resource_alternatives=alternatives,
        conditions=conds,
        objective=objective,
        time_limit_s=TIME_LIMIT_S,
        search_key=search_key(
            facts, acting_unit_id, axes, alternatives, TIME_LIMIT_S, conds, objective
        ),
    )
