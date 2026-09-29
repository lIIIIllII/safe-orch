"""불변 객체의 내용 hash (설계서 §5.2, 부록 A.11·A.13). solver와 validator가 같이 쓴다."""

from collections.abc import Mapping, Sequence

from app.domain.canonical import canonical_hash
from app.domain.models import Assignment, Movable


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
