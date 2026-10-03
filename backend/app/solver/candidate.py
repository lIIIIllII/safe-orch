"""Solver 결과 → Candidate."""

from app.domain.hashes import candidate_hash
from app.domain.ids import new_id
from app.domain.models import Assignment, Candidate, SearchSpec, Snapshot, SolverResult


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
