"""SearchSpec 생성. 서버가 만들고 불변이다.

app.rules·app.validator를 import하지 않는다.
"""

from collections.abc import Mapping

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

    # 고정되지 않은 작업은 시각도 자원도 움직인다 (AG-34)
    pinned = facts.pinned_task_ids()
    axes = {
        t.task_id: Movable(time=t.task_id not in pinned, resource=t.task_id not in pinned)
        for t in scope
    }

    # 자원 대안은 서버가 채운다: 범위 안 고정되지 않은 작업마다 기준 자원 말고 쓸 수 있는 적격 자원 전부 (CV-20)
    tasks = facts.task_map()
    resources = facts.resource_map()
    alternatives: dict[str, tuple[str, ...]] = {}
    for t in scope:
        if t.required_resource_type is None or not axes[t.task_id].resource:
            continue
        ok = tuple(
            r.resource_id
            for r in sorted(facts.resources, key=lambda r: r.resource_id)
            if r.resource_id != base[t.task_id].resource_id
            and not exclusion_reasons(t, r, acting_unit_id)
        )
        if ok:
            alternatives[t.task_id] = ok

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
            # 기준 자원이 아니면 그 작업이 쓸 수 있는 적격 자원이어야 한다 (CV-20)
            r = resources.get(c.resource_id)
            if r is None or exclusion_reasons(task, r, acting_unit_id):
                raise SearchSpecError("RESOURCE_NOT_ELIGIBLE", f"{tid}: {c.resource_id}")
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
