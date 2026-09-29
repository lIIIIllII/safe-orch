"""Solver 결과 → Candidate (설계서 §5.2, 부록 A.11)."""

from app.domain.canonical import canonical_hash
from app.domain.ids import new_id
from app.domain.models import Assignment, Candidate, SearchSpec, Snapshot, SolverResult


def candidate_hash(
    assignments: tuple[Assignment, ...],
    base_plan_revision: int,
    context_version: int,
    snapshot_hash: str,
    search_spec_hash: str,
    pack_hash: str,
) -> str:
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


def build_candidate(snapshot: Snapshot, spec: SearchSpec, result: SolverResult) -> Candidate | None:
    """해가 없으면 None. assignments는 모든 READY 작업(task_id순)."""
    if result.solution is None:
        return None
    facts = snapshot.facts()
    assignments = tuple(sorted((Assignment(**a) for a in result.solution), key=lambda a: a.task_id))
    return Candidate(
        candidate_id=new_id("cand"),
        snapshot_id=snapshot.snapshot_id,
        search_spec_id=spec.search_spec_id,
        search_spec_hash=spec.hash,
        solver_result_id=result.solver_result_id,
        base_plan_revision=facts.plan_revision,
        context_version=facts.context_version,
        pack_hash=facts.pack_hash,
        assignments=assignments,
        candidate_hash=candidate_hash(
            assignments,
            facts.plan_revision,
            facts.context_version,
            snapshot.snapshot_hash,
            spec.hash,
            facts.pack_hash,
        ),
        kind="REPLAN",
    )
