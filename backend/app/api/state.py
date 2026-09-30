"""상태 조회 GET /sites/{id}/state와 Run 조회 (설계서 §9.5·§12·§13, 부록 A.18).

화면이 1초 폴링으로 쓴다. 단일 읽기 트랜잭션(db.read_tx)에서 계산하고 아무것도 쓰지 않는다.
"""

import sqlite3
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter

from app.api.deps import ActorDep, ApiError, PackDep, check_site
from app.domain.canonical import canonical_hash
from app.domain.models import Plan, Snapshot, Task
from app.packs.loader import LoadedPack
from app.rules.engine import detect_conflicts
from app.store import db
from app.store.repos._rows import loads, rows
from app.store.repos.consultations import (
    candidate_state,
    consultation_view,
    list_review_queue,
)
from app.store.repos.plans import get_current_plan
from app.store.repos.records import get_candidate, get_snapshot, list_validations
from app.store.repos.resources import list_resources
from app.store.repos.runs import get_run, list_steps, run_for_solver_result
from app.store.repos.site import get_site, list_actors, list_zone_relations
from app.store.repos.snapshots import build_snapshot_content
from app.store.repos.tasks import list_current_tasks

router = APIRouter()

RECENT_CANDIDATES = 5
RECENT_EVENTS = 10
RECENT_RUNS = 10


# ── Gate (§9.5) ────────────────────────────────────────────────


def gate(
    task: Task, plan: Plan, context_version: int, holds: list[dict[str, Any]]
) -> dict[str, Any]:
    """ALLOW = Plan에 있음 ∧ context = plan.committed_context_version ∧ 관련 ACTIVE Hold 없음.

    관련 Hold: SITE는 모든 작업, TASK는 그 작업. HOLD와 STALE이 겹치면 HOLD (reasons에는 둘 다).
    """
    reasons = [
        f"HOLD:{h['hold_id']}"
        for h in holds
        if h["scope"] == "SITE" or h["task_id"] == task.task_id
    ]
    if task.task_id not in {a.task_id for a in plan.assignments}:
        reasons.append("NOT_IN_PLAN")
    if context_version != plan.committed_context_version:
        reasons.append("CONTEXT_CHANGED")
    status = (
        "HOLD" if any(r.startswith("HOLD:") for r in reasons) else "STALE" if reasons else "ALLOW"
    )
    return {"gate": status, "reasons": reasons}


# ── 후보 ───────────────────────────────────────────────────────


def _solver(conn: sqlite3.Connection, solver_result_id: str | None) -> dict[str, Any] | None:
    if solver_result_id is None:
        return None
    found = rows(
        conn,
        "SELECT r.stage1, r.stage2, r.chosen_stage, s.scope_level FROM solver_result r"
        " JOIN search_spec s ON s.search_spec_id = r.search_spec_id"
        " WHERE r.solver_result_id = ?",
        (solver_result_id,),
    )
    if not found:
        return None
    r = found[0]
    s1, s2 = loads(r["stage1"]), loads(r["stage2"])
    solution = s1 if r["chosen_stage"] == 1 else s2
    return {
        "scope_level": r["scope_level"],
        "stage1": {"status": s1["status"], "changed": s1["changed"]},
        "stage2": None if s2 is None else {"status": s2["status"], "delay": s2["delay"]},
        "chosen_stage": r["chosen_stage"],
        "minimal_change": s1["status"] == "OPTIMAL",
        "delay_optimality_unconfirmed": solution is not None
        and (s2 is None or s2["status"] != "OPTIMAL"),
    }


def candidate_view(conn: sqlite3.Connection, site_id: str, candidate_id: str) -> dict[str, Any]:
    cand = get_candidate(conn, site_id, candidate_id)
    assert cand is not None
    state = candidate_state(conn, site_id, cand)
    display = (
        "COMMITTED"
        if state.committed
        else "REJECTED"
        if state.rejected
        else "STALE"
        if state.stale
        else "OPEN"
    )
    snapshot = get_snapshot(conn, cand.snapshot_id)
    base = snapshot.facts().base_assignments() if snapshot else {}
    changes = [
        {"task_id": a.task_id, "before": base[a.task_id].model_dump(), "after": a.model_dump()}
        for a in cand.assignments
        if a.task_id in base
        and (a.start, a.resource_id) != (base[a.task_id].start, base[a.task_id].resource_id)
    ]
    validations = list_validations(conn, site_id, candidate_id)
    validation = None
    if validations:
        v = validations[-1]
        validation = {
            "validation_id": v.validation_id,
            "status": v.status,
            # 대표 상태: STALE > INCOMPLETE > FAIL > PASS (§8). 확정된 후보는 STALE로 보지 않는다.
            "display_status": "STALE" if state.stale and not state.committed else v.status,
            "checks": [c.model_dump(mode="json") for c in v.checks],
        }
    view = consultation_view(conn, site_id, candidate_id)
    consultation = None
    if view is not None:
        consultation = {
            "status": view.status,
            "items": [
                {**i.model_dump(mode="json"), "item_status": view.item_status[i.task_id]}
                for i in view.items
            ],
        }
    return {
        "candidate_id": cand.candidate_id,
        "kind": cand.kind,
        "run_id": run_for_solver_result(conn, cand.solver_result_id)
        if cand.solver_result_id
        else None,
        "context_version": cand.context_version,
        "base_plan_revision": cand.base_plan_revision,
        "display_status": display,
        "assignments": [a.model_dump() for a in cand.assignments],
        "changes": changes,
        "solver": _solver(conn, cand.solver_result_id),
        "validation": validation,
        "consultation": consultation,
    }


# ── Run ────────────────────────────────────────────────────────


def run_summary(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    run = get_run(conn, run_id)
    if run is None:
        raise ApiError(404, "RUN_NOT_FOUND")
    last = conn.execute(
        "SELECT status FROM agent_step WHERE run_id = ? ORDER BY step_no DESC LIMIT 1", (run_id,)
    ).fetchone()
    return {
        "run_id": run.run_id,
        "agent_type": run.agent_type,
        "case_id": run.case_id,
        "acting_unit_id": run.acting_unit_id,
        "status": run.status,
        "wait_kind": run.wait_kind,
        "wait_ref": run.wait_ref,
        "wait_generation": run.wait_generation,
        "last_step_no": run.last_step_no,
        "current_step_status": None if last is None else last[0],
        "end_reason": run.end_reason,
        "budget_used": run.budget_used,
    }


# ── 상태 ───────────────────────────────────────────────────────


def build_state(conn: sqlite3.Connection, pack: LoadedPack) -> dict[str, Any]:
    site_id = pack.site_id
    site = get_site(conn, site_id)
    plan = get_current_plan(conn, site_id)
    if site is None or plan is None:
        raise ApiError(404, "SITE_NOT_FOUND")

    holds = rows(
        conn,
        "SELECT h.hold_id, h.scope, h.task_id, h.created_context_version, h.event_id,"
        " e.event_type, e.text, e.reporter_actor_id, e.target_task_id FROM hold h"
        " JOIN event e ON e.event_id = h.event_id"
        " WHERE h.site_id = ? AND h.status = 'ACTIVE' ORDER BY h.rowid",
        (site_id,),
    )
    events = rows(
        conn,
        "SELECT e.event_id, e.source_event_id, e.event_type, e.text, e.reporter_actor_id,"
        " e.target_task_id, e.context_version, h.hold_id, h.status AS hold_status"
        " FROM event e JOIN hold h ON h.event_id = e.event_id"
        f" WHERE e.site_id = ? ORDER BY e.rowid DESC LIMIT {RECENT_EVENTS}",
        (site_id,),
    )
    tasks = list_current_tasks(conn, site_id, pack)
    content = build_snapshot_content(conn, site_id, pack)
    snapshot = Snapshot(snapshot_id="state", snapshot_hash=canonical_hash(content), content=content)
    conflicts = detect_conflicts(snapshot, snapshot.facts().check_assignments(), pack)

    live = rows(
        conn,
        "SELECT candidate_id FROM candidate WHERE site_id = ? AND context_version = ?"
        " AND base_plan_revision = ?",
        (site_id, site.context_version, site.plan_revision),
    )
    recent = rows(
        conn,
        f"SELECT candidate_id FROM candidate WHERE site_id = ? ORDER BY rowid DESC"
        f" LIMIT {RECENT_CANDIDATES}",
        (site_id,),
    )
    queue = list_review_queue(conn, site_id)
    ids = list(dict.fromkeys([r["candidate_id"] for r in recent + live] + queue))
    run_ids = [
        r["run_id"]
        for r in rows(
            conn,
            f"SELECT run_id FROM agent_run WHERE site_id = ? ORDER BY rowid DESC"
            f" LIMIT {RECENT_RUNS}",
            (site_id,),
        )
    ]
    jobs = {
        r["status"]: r["n"]
        for r in rows(
            conn,
            "SELECT status, COUNT(*) AS n FROM dispatch_job WHERE site_id = ? GROUP BY status",
            (site_id,),
        )
    }
    return {
        "server_time": datetime.now(UTC).isoformat(timespec="seconds"),
        "site": {
            **site.model_dump(),
            "pack": pack.name,
        },
        "actors": [a.model_dump() for a in list_actors(conn, site_id)],
        "units": rows(
            conn, "SELECT unit_id, name, unit_type FROM work_unit WHERE site_id = ?", (site_id,)
        ),
        "zones": [z.zone_id for z in pack.zones],
        "zone_relations": [r.model_dump() for r in list_zone_relations(conn, site_id)],
        "resources": [r.model_dump() for r in list_resources(conn, site_id)],
        "tasks": [
            {**t.model_dump(mode="json"), **gate(t, plan, site.context_version, holds)}
            for t in tasks
        ],
        "plan": plan.model_dump(mode="json"),
        "conflicts": [c.model_dump(mode="json") for c in conflicts],
        "candidates": [candidate_view(conn, site_id, cid) for cid in ids],
        "review_queue": queue,
        "holds": holds,
        "events": events,
        "runs": [run_summary(conn, rid) for rid in run_ids],
        "dispatch": {"pending": jobs.get("PENDING", 0), "failed": jobs.get("FAILED", 0)},
    }


@router.get("/sites/{site_id}/state")
def get_state(site_id: str, pack: PackDep, actor: ActorDep) -> dict[str, Any]:
    check_site(site_id, pack)
    with db.read_tx() as conn:
        return build_state(conn, pack)


@router.get("/runs/{run_id}")
def get_run_detail(run_id: str, actor: ActorDep) -> dict[str, Any]:
    with db.read_tx() as conn:
        summary = run_summary(conn, run_id)
        run = get_run(conn, run_id)
        steps = list_steps(conn, run_id)
    assert run is not None
    return {
        **summary,
        "acting_actor_id": run.acting_actor_id,
        "input_ref": run.input_ref,
        "exec_contract_version": run.exec_contract_version,
        "restart_count": run.restart_count,
        "last_step": steps[-1] if steps else None,
    }


@router.get("/runs/{run_id}/steps")
def get_run_steps(run_id: str, actor: ActorDep) -> list[dict[str, Any]]:
    with db.read_tx() as conn:
        if get_run(conn, run_id) is None:
            raise ApiError(404, "RUN_NOT_FOUND")
        return list_steps(conn, run_id)
