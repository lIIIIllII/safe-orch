"""불변 객체의 내용 hash. solver와 validator가 같이 쓴다."""

from collections.abc import Mapping, Sequence

from app.domain.canonical import canonical_hash
from app.domain.models import Assignment, Movable, SnapshotContent


def search_spec_hash(
    snapshot_hash: str,
    acting_unit_id: str,
    axes: Mapping[str, Movable],
    resource_alternatives: Mapping[str, Sequence[str]],
    time_limit_s: int,
) -> str:
    """실효 내용의 hash. 두 축이 모두 false인 작업, ID, scope_level은 넣지 않는다."""
    return canonical_hash(
        {
            "snapshot_hash": snapshot_hash,
            "acting_unit_id": acting_unit_id,
            "axes": {tid: ax.model_dump() for tid, ax in axes.items() if ax.time or ax.resource},
            "resource_alternatives": {k: list(v) for k, v in resource_alternatives.items()},
            "time_limit_s": time_limit_s,
        }
    )


def search_key(
    facts: SnapshotContent,
    acting_unit_id: str,
    axes: Mapping[str, Movable],
    resource_alternatives: Mapping[str, Sequence[str]],
    time_limit_s: int,
) -> str:
    """실효 탐색 키: "같은 실효 SearchSpec 미시도" 판정용. Solver 입력만 넣는다.

    무결성 hash(search_spec_hash)와 다르다. snapshot_hash 대신 Solver가 읽는 사실만 넣으므로
    context_version·plan_revision 번호, Consent, fields, revision 번호, Hold가 바뀌어도 같다.
    - 작업(READY): 구역, duration, 시간창, 필요 자원 유형, 자원 요구 조건, 기준 배정, 선후행, hazard_tags
    - 자원(유형·허용 Unit·사용 가능 구역·속성 값·가용 구간), 구역 관계, pack_hash(Rule), 근무 구간, Horizon
    - 풀(종류·허용 Unit·수량)과 작업의 수요·필수 직종. 누적 제약의 입력이다 (CV-04)
    - 자원·풀의 표시 이름·메모·비용은 Solver 입력이 아니므로 넣지 않는다 (CV-04)
    - 고정은 넣지 않는다. 고정은 axes를 통해서만 Solver 입력에 영향을 준다(범위 밖 작업의 고정은
      그 범위의 탐색을 바꾸지 않는다)
    - axes는 정규화: resource 축이 true여도 대체 자원이 없으면 Solver 입력이 같으므로 false
    """
    base = facts.base_assignments()
    effective = {}
    for tid, ax in axes.items():
        resource = ax.resource and bool(resource_alternatives.get(tid))
        if ax.time or resource:
            effective[tid] = {"time": ax.time, "resource": resource}
    return canonical_hash(
        {
            "tasks": [
                {
                    "task_id": t.task_id,
                    "zone_id": t.zone_id,
                    "duration": t.duration,
                    "window": [t.earliest_start, t.latest_start, t.latest_end],
                    "required_resource_type": t.required_resource_type,
                    "requirements": [r.model_dump() for r in t.requirements],
                    "demands": t.demands,
                    "required_kinds": list(t.required_kinds),
                    "base": base[t.task_id].model_dump(),
                    "predecessors": [p.model_dump() for p in t.predecessors],
                    "hazard_tags": sorted(t.hazard_tags),
                }
                for t in sorted(facts.tasks, key=lambda t: t.task_id)
            ],
            "resources": [
                {
                    "resource_id": r.resource_id,
                    "resource_type": r.resource_type,
                    "allowed_unit_ids": sorted(r.allowed_unit_ids),
                    "allowed_zone_ids": sorted(r.allowed_zone_ids),
                    "attributes": r.model_dump(mode="json")["attributes"],
                    "available_intervals": [list(iv) for iv in r.available_intervals],
                }
                for r in sorted(facts.resources, key=lambda r: r.resource_id)
            ],
            "pools": [
                {
                    "pool_id": p.pool_id,
                    "kind": p.kind,
                    "allowed_unit_ids": sorted(p.allowed_unit_ids),
                    "quantity": p.quantity,
                }
                for p in sorted(facts.pools, key=lambda p: p.pool_id)
            ],
            "zone_relations": sorted(
                [r.zone_a, r.zone_b, r.relation] for r in facts.zone_relations
            ),
            "pack_hash": facts.pack_hash,
            "work_intervals": [list(iv) for iv in facts.work_intervals],
            "horizon_minutes": facts.horizon_minutes,
            "acting_unit_id": acting_unit_id,
            "axes": effective,
            "resource_alternatives": {k: list(v) for k, v in resource_alternatives.items()},
            "time_limit_s": time_limit_s,
        }
    )


def candidate_hash(
    assignments: Sequence[Assignment],
    base_plan_revision: int,
    context_version: int,
    snapshot_hash: str,
    search_spec_hash: str | None,
    pack_hash: str,
) -> str:
    """search_spec_hash는 RECONFIRM 후보면 None."""
    return canonical_hash(
        {
            "assignments": [a.model_dump() for a in sorted(assignments, key=lambda a: a.task_id)],
            "base_plan_revision": base_plan_revision,
            "context_version": context_version,
            "snapshot_hash": snapshot_hash,
            "search_spec_hash": search_spec_hash,
            "pack_hash": pack_hash,
        }
    )
