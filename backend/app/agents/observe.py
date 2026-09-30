"""Replanning Observation과 Available Actions 계산 (설계서 §11.2 observe·§11.7, 부록 A.16).

읽기 전용이다. Snapshot은 메모리에서만 만들고(hash만 계산) 저장은 Solver 예약 tx에서 한다.
observe 노드와 Gateway 예약 tx가 같은 함수로 계산한다(Gateway는 최신 tx 안에서 다시 계산).
"""

import sqlite3
from dataclasses import dataclass
from typing import Any

from app.agents.specs import replanning as spec
from app.domain.canonical import canonical_hash
from app.domain.models import AgentRun, Conflict, Snapshot, SnapshotContent
from app.packs.loader import LoadedPack
from app.rules.engine import detect_conflicts
from app.solver.search_spec import SearchSpecError, build_search_spec
from app.store.repos.records import list_validations
from app.store.repos.runs import get_run, list_attempts, list_steps, tried_spec_hashes
from app.store.repos.site import get_site
from app.store.repos.snapshots import build_snapshot_content

RECENT_STEPS = 5


@dataclass(frozen=True)
class Observation:
    run: AgentRun
    versions: tuple[int, int, int]  # (context_version, plan_revision, wake_seq)
    data: dict[str, Any]
    available: dict[str, dict[str, Any]]
    primary: Conflict | None

    @property
    def active(self) -> bool:
        return self.run.status == "RUNNING"

    @property
    def budget_exhausted(self) -> bool:
        return (
            self.run.steps_used >= spec.MAX_STEPS
            or self.run.llm_attempts_used >= spec.MAX_LLM_ATTEMPTS
        )


def budget_remaining(run: AgentRun) -> dict[str, float]:
    return {
        "steps": spec.MAX_STEPS - run.steps_used,
        "llm_attempts": spec.MAX_LLM_ATTEMPTS - run.llm_attempts_used,
        "human_rounds": spec.MAX_HUMAN_ROUNDS - run.human_rounds_used,
        "solver_calls": spec.MAX_SOLVER_CALLS - run.solver_calls_used,
    }


def primary_conflict(
    facts: SnapshotContent, conflicts: list[Conflict], run: AgentRun
) -> Conflict | None:
    """input_ref.conflict와 같은 충돌, 없으면 acting_unit 작업을 포함한 첫 충돌 (A.15·A.16)."""
    ref = run.input_ref.get("conflict") or {}
    for c in conflicts:
        if c.rule_id == ref.get("rule_id") and list(c.task_ids) == list(ref.get("task_ids", [])):
            return c
    tasks = facts.task_map()
    for c in conflicts:
        if any(tasks[t].unit_id == run.acting_unit_id for t in c.task_ids if t in tasks):
            return c
    return None


def current_snapshot(conn: sqlite3.Connection, pack: LoadedPack) -> Snapshot:
    content = build_snapshot_content(conn, pack.site_id, pack)
    return Snapshot(snapshot_id="observe", snapshot_hash=canonical_hash(content), content=content)


def level_hashes(snapshot: Snapshot, primary: Conflict, acting_unit_id: str) -> dict[str, str]:
    """level별 실효 SearchSpec hash. 만들 수 없는 level(NO_ACTING_TASKS 등)은 뺀다."""
    out = {}
    for level in spec.LEVELS:
        try:
            out[level] = build_search_spec(snapshot, primary, acting_unit_id, level).hash
        except SearchSpecError:
            continue
    return out


def build_observation(conn: sqlite3.Connection, pack: LoadedPack, run_id: str) -> Observation:
    run = get_run(conn, run_id)
    site = get_site(conn, pack.site_id)
    if run is None or site is None:
        raise LookupError(f"run {run_id} or site not found")
    snapshot = current_snapshot(conn, pack)
    facts = snapshot.facts()
    conflicts = detect_conflicts(snapshot, facts.check_assignments(), pack)
    primary = primary_conflict(facts, conflicts, run)
    tried = tried_spec_hashes(conn, pack.site_id)
    hashes = level_hashes(snapshot, primary, run.acting_unit_id) if primary else {}
    untried = [lv for lv, h in hashes.items() if h not in tried]

    base = facts.base_assignments()
    acting_tasks = [
        {
            "task_id": t.task_id,
            "zone_id": t.zone_id,
            "duration": t.duration,
            "window": {
                "earliest_start": t.earliest_start,
                "latest_start": t.latest_start,
                "latest_end": t.latest_end,
            },
            "movable": t.movable.model_dump(),
            "base": base[t.task_id].model_dump(),
        }
        for t in sorted(facts.tasks, key=lambda t: t.task_id)
        if t.unit_id == run.acting_unit_id
    ]
    acting_ids = {t["task_id"] for t in acting_tasks}
    attempts = list_attempts(conn, run_id)
    candidates = [a["candidate_id"] for a in attempts if a["candidate_id"]]
    latest_validation = None
    if candidates:
        validations = list_validations(conn, pack.site_id, candidates[-1])
        if validations:
            v = validations[-1]
            latest_validation = {
                "candidate_id": candidates[-1],
                "status": v.status,
                "failed_checks": [
                    c.model_dump(mode="json") for c in v.checks if c.status != "PASS"
                ],
            }
    steps = [s for s in list_steps(conn, run_id) if s["status"] == "COMPLETED"]
    recent = [
        {
            "step_no": s["step_no"],
            "action": (s["action"] or {}).get("name"),
            "result_kind": s["result_kind"],
            "guard": s["guard"],
        }
        for s in steps[-RECENT_STEPS:]
    ]
    last_guard = (
        steps[-1]["guard"] if steps and steps[-1]["guard"]["verdict"] == "REJECTED" else None
    )

    data = {
        "run": {
            "run_id": run.run_id,
            "agent_type": run.agent_type,
            "goal": spec.GOAL,
            "acting_unit_id": run.acting_unit_id,
        },
        "versions": {
            "context_version": site.context_version,
            "plan_revision": site.plan_revision,
            "wake_seq": run.wake_seq,
        },
        "conflicts": [c.model_dump(mode="json") for c in conflicts],
        "primary_conflict": None if primary is None else primary.model_dump(mode="json"),
        "acting_tasks": acting_tasks,
        "constraints": [c.model_dump(mode="json") for c in facts.constraints],
        "consents": [c.model_dump(mode="json") for c in facts.consents if c.task_id in acting_ids],
        "untried_levels": untried,
        "attempts": attempts,
        "latest_validation": latest_validation,
        "last_guard": last_guard,
        "recent_steps": recent,
        "budget_remaining": budget_remaining(run),
    }
    return Observation(
        run=run,
        versions=(site.context_version, site.plan_revision, run.wake_seq),
        data=data,
        available=spec.available_actions(data),
        primary=primary,
    )
