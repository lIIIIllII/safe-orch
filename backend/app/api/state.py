"""상태 조회 GET /sites/{id}/state와 Run 조회.

화면이 1초 폴링으로 쓴다. 단일 읽기 트랜잭션(db.read_tx)에서 계산하고 아무것도 쓰지 않는다.
"""

import sqlite3
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter

from app.agents import casefacts
from app.api.deps import ActorDep, ApiError, PackDep, check_site
from app.commands.moves import move_check, move_range, remove_check, resource_check
from app.domain.calendar import work_delay, work_minutes
from app.domain.canonical import canonical_hash
from app.domain.factdiff import fact_changes
from app.domain.models import AgentRun, Plan, Snapshot, SnapshotContent, Task
from app.packs.loader import LoadedPack
from app.rules.engine import detect_conflicts
from app.store import db
from app.store.repos._rows import loads, rows
from app.store.repos.cases import queued_task_ids
from app.store.repos.consultations import (
    candidate_state,
    consultation_view,
    contested_changes,
    list_review_queue,
)
from app.store.repos.decisions import is_chosen, list_decisions
from app.store.repos.messages import list_change_requests, list_fact_updates, list_inbox
from app.store.repos.pins import active_pin_views, preferred_windows
from app.store.repos.plans import get_current_plan
from app.store.repos.records import (
    get_candidate,
    get_search_spec,
    get_snapshot,
    list_validations,
)
from app.store.repos.resources import list_resources
from app.store.repos.runs import approach_attempts, get_run, list_steps, run_for_solver_result
from app.store.repos.site import get_site, list_actors, list_zone_relations
from app.store.repos.snapshots import build_snapshot_content, plan_facts
from app.store.repos.tasks import list_current_tasks

router = APIRouter()

RECENT_CANDIDATES = 5
RECENT_EVENTS = 10
RECENT_RUNS = 10


# ── Gate ────────────────────────────────────────────────


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


def work_deviation(facts: SnapshotContent, task_id: str, start: int) -> int:
    """희망에서 벗어난 정도의 근무 분. 달력 분(facts.deviation)과 같은 정의다: 희망 영역이 있으면 희망
    시작 범위 밖으로 벗어난 구간(앞뒤 모두)의 근무 분, 없으면 기준 시작보다 늦어진 근무 분 (ST-22)."""
    task = facts.task_map()[task_id]
    wanted = facts.preferred_map().get(task_id)
    if wanted is None:
        return work_delay(facts.base_assignments()[task_id].start, start, facts.work_intervals)
    lo, hi = wanted.start_range(task.duration)
    if start > hi:
        return work_minutes(hi, start, facts.work_intervals)
    if start < lo:
        return work_minutes(start, lo, facts.work_intervals)
    return 0


def _work_delay_sum(
    solution: list[dict[str, Any]] | None, facts: SnapshotContent | None
) -> int | None:
    """해의 근무 분 지연(희망에서 벗어난 정도) 합. 저장하지 않고 조회 시 계산한다."""
    if solution is None or facts is None:
        return None
    tasks = facts.task_map()
    return sum(
        work_deviation(facts, a["task_id"], a["start"]) for a in solution if a["task_id"] in tasks
    )


def plan_changes(facts: SnapshotContent | None, assignments: Any) -> list[dict[str, Any]]:
    """이 안이 기준 계획에서 바꾸는 것 (서버 계산, 작업 ID순). 화면은 그리기만 한다.

    - CHANGED: 계획에 있던 작업의 시각이나 자원이 바뀐다. before는 지금 배치다.
    - NEW: 계획에 없던 작업을 새로 배치한다(기준 자리 그대로여도 낸다). before는 없다.
    - time_changed·resource_changed: CHANGED는 지금 배치와 비교, NEW는 자원만 요청 자원과 비교한다.
    - off_request: 배치된 자원이 그 작업의 요청 자원과 다르다(요청 자원이 있을 때만).
    """
    if facts is None:
        return []
    tasks, base = facts.task_map(), facts.base_assignments()
    in_plan = {a.task_id for a in facts.plan.assignments}
    out = []
    for a in sorted(assignments, key=lambda a: a.task_id):
        task = tasks.get(a.task_id)
        if task is None:
            continue
        requested = task.requested_resource_id
        off_request = requested is not None and a.resource_id != requested
        new = a.task_id not in in_plan
        before = None if new else base[a.task_id]
        time_changed = before is not None and a.start != before.start
        resource_changed = off_request if before is None else a.resource_id != before.resource_id
        if not (new or time_changed or resource_changed):
            continue
        out.append(
            {
                "task_id": a.task_id,
                "kind": "NEW" if new else "CHANGED",
                "before": None if before is None else before.model_dump(),
                "after": a.model_dump(),
                "time_changed": time_changed,
                "resource_changed": resource_changed,
                "off_request": off_request,
                "requested_resource_id": requested,
                "resource_type": task.required_resource_type,
            }
        )
    return out


def plan_numbers(conn: sqlite3.Connection, site_id: str, case_id: str) -> dict[str, int]:
    """이 Case의 살아 있는 재계획 후보에 만들어진 순서로 매긴 안 번호(1부터). 화면의 "n안"이다.
    한 호출이 후보를 여럿 내도 후보마다 번호가 다르다. 무효·거절·확정된 후보에는 번호가 없다."""
    ids = [
        r["candidate_id"]
        for r in rows(
            conn,
            "SELECT c.candidate_id FROM candidate c"
            " JOIN solver_job j ON j.solver_result_id = c.solver_result_id"
            " JOIN agent_run r ON r.run_id = j.run_id WHERE r.case_id = ? ORDER BY c.rowid",
            (case_id,),
        )
    ]
    out: dict[str, int] = {}
    for cid in ids:
        candidate = get_candidate(conn, site_id, cid)
        assert candidate is not None
        state = candidate_state(conn, site_id, candidate)
        if not (state.stale or state.rejected or state.committed):
            out[cid] = len(out) + 1
    return out


def off_hope(facts: SnapshotContent | None, assignments: Any) -> list[dict[str, Any]]:
    """그 배치에서 희망 영역 밖에 놓인 작업과 정도 (서버 계산). 바뀐 작업만이 아니라 희망 영역이 있는
    작업 전부를 본다. direction은 희망보다 이른지(EARLY) 늦은지(LATE)다."""
    if facts is None:
        return []
    tasks, wanted = facts.task_map(), facts.preferred_map()
    out = []
    for a in sorted(assignments, key=lambda a: a.task_id):
        if a.task_id not in wanted or a.task_id not in tasks:
            continue
        delay = facts.deviation(a.task_id, a.start)
        if delay:
            out.append(
                {
                    "task_id": a.task_id,
                    "delay": delay,
                    "work_delay": work_deviation(facts, a.task_id, a.start),
                    "direction": "EARLY" if a.start < wanted[a.task_id].start else "LATE",
                }
            )
    return out


def _solver(
    conn: sqlite3.Connection, solver_result_id: str | None, facts: SnapshotContent | None
) -> dict[str, Any] | None:
    """stage2.delay는 목적함수 값(달력 분, 희망에서 벗어난 정도의 합), work_delay는 같은 해의 근무 분."""
    if solver_result_id is None:
        return None
    found = rows(
        conn,
        "SELECT r.stage1, r.stage2, r.chosen_stage, s.scope_level, s.objective FROM solver_result r"
        " JOIN search_spec s ON s.search_spec_id = r.search_spec_id"
        " WHERE r.solver_result_id = ?",
        (solver_result_id,),
    )
    if not found:
        return None
    r = found[0]
    s1, s2 = loads(r["stage1"]), loads(r["stage2"])
    solution = s1 if r["chosen_stage"] == 1 else s2
    delay_first = r["objective"] == "DELAY_FIRST"
    return {
        "scope_level": r["scope_level"],
        # 목적 순서. 지연 먼저면 1단계가 지연(희망에서 벗어난 정도), 2단계가 변경 작업 수다 (CV-27)
        "objective": r["objective"],
        "stage1": {"status": s1["status"], "changed": s1["changed"], "delay": s1.get("delay")},
        "stage2": None
        if s2 is None
        else {
            "status": s2["status"],
            "delay": s2["delay"],
            "changed": s2.get("changed"),
            "work_delay": _work_delay_sum(s2.get("solution"), facts),
        },
        "chosen_stage": r["chosen_stage"],
        "minimal_change": s1["status"] == "OPTIMAL" and not delay_first,
        "minimal_delay": s1["status"] == "OPTIMAL" and delay_first,
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
    facts = snapshot.facts() if snapshot else None
    base = facts.base_assignments() if facts else {}
    # delay = 희망에서 벗어난 정도(달력 분), work_delay = 같은 값의 근무 분. 조회 시 계산하고 저장하지 않는다.
    changes = [
        {
            "task_id": a.task_id,
            "before": base[a.task_id].model_dump(),
            "after": a.model_dump(),
            "delay": facts.deviation(a.task_id, a.start),
            "work_delay": work_deviation(facts, a.task_id, a.start),
        }
        for a in cand.assignments
        if facts is not None
        and a.task_id in base
        and (a.start, a.resource_id) != (base[a.task_id].start, base[a.task_id].resource_id)
    ]
    # 이 안의 기준 계획을 확정할 때의 사실과 이 안이 계산된 사실의 차이 (서버 계산)
    basis = plan_facts(conn, site_id, cand.base_plan_revision)
    validations = list_validations(conn, site_id, candidate_id)
    validation = None
    if validations:
        v = validations[-1]
        validation = {
            "validation_id": v.validation_id,
            "status": v.status,
            # 대표 상태: STALE > INCOMPLETE > FAIL > PASS. 확정된 후보는 STALE로 보지 않는다.
            "display_status": "STALE" if state.stale and not state.committed else v.status,
            "checks": [c.model_dump(mode="json") for c in v.checks],
        }
    view = consultation_view(conn, site_id, candidate_id)
    consultation = None
    if view is not None:
        # 항목별 마지막 변경 요청과 담당자 답(이견 문장은 인용으로만)
        requests = {r["change_hash"]: r for r in list_change_requests(conn, site_id, candidate_id)}
        consultation = {
            "status": view.status,
            "items": [
                {
                    **i.model_dump(mode="json"),
                    "item_status": view.item_status[i.task_id],
                    "request": _request_view(requests.get(i.change_hash)),
                    # 상태를 만든 담당자 답의 출처. prior면 다른 후보에서 한 답이 적용된 것이다
                    "answer_source": view.answer_from.get(i.task_id),
                }
                for i in view.items
            ],
        }
    run_id = run_for_solver_result(conn, cand.solver_result_id) if cand.solver_result_id else None
    maker = get_run(conn, run_id) if run_id else None
    return {
        "candidate_id": cand.candidate_id,
        "kind": cand.kind,
        "rejection": _rejection(conn, site_id, cand.candidate_id),
        "run_id": run_id,
        # 이 후보를 만든 Case, 이 후보에 도달한 접근들(같은 배치면 여럿), Supervisor가 골랐는가 (AG-28·AG-29)
        "case_id": None if maker is None else maker.case_id,
        # 안 번호: 이 Case의 살아 있는 후보에 만들어진 순서로 매긴다(후보마다 다르다). 없으면 None
        "plan_no": None
        if maker is None
        else plan_numbers(conn, site_id, maker.case_id).get(cand.candidate_id),
        "approaches": [
            {k: a[k] for k in ("no", "approach", "quoted_note", "run_id", "same", "quoted_reason")}
            for a in approach_attempts(conn, candidate_id=cand.candidate_id)
        ],
        "chosen": is_chosen(conn, site_id, cand.candidate_id),
        "context_version": cand.context_version,
        "base_plan_revision": cand.base_plan_revision,
        "display_status": display,
        "assignments": [a.model_dump() for a in cand.assignments],
        "changes": changes,
        # 이 안이 기준 계획에서 바꾸는 것: 시각·자원·새 배치·요청 자원과 다름 (서버 계산)
        "plan_changes": plan_changes(facts, cand.assignments),
        # 희망 영역 밖에 놓인 작업과 정도 (ST-22)
        "off_hope": off_hope(facts, cand.assignments),
        # 기준 계획을 확정한 뒤 사람이 바꾼 사실: 무엇 때문에 다시 계획·확정하는가
        "fact_changes": fact_changes(basis, facts) if basis and facts else [],
        "solver": _solver(conn, cand.solver_result_id, facts),
        # Agent가 건 조건(서버가 받은 값, 분)과 거절·이견된 변경(서버 계산) (CV-24·CV-26)
        "conditions": _conditions(conn, cand.search_spec_id),
        "contested": contested_changes(conn, site_id, cand) if display == "OPEN" else [],
        "validation": validation,
        "consultation": consultation,
    }


def _conditions(conn: sqlite3.Connection, search_spec_id: str | None) -> list[dict[str, Any]]:
    """그 후보의 탐색에 Agent가 건 조건. 조건이 없으면 빈 목록."""
    spec = get_search_spec(conn, search_spec_id) if search_spec_id else None
    if spec is None:
        return []
    return [{"task_id": tid, **c.model_dump()} for tid, c in sorted(spec.conditions.items())]


def _rejection(conn: sqlite3.Connection, site_id: str, candidate_id: str) -> dict[str, Any] | None:
    """거절된 후보의 거절 사유. 거절이 없으면 None."""
    found = list_decisions(conn, site_id, candidate_id, "REJECT")
    if not found:
        return None
    d = found[-1]
    return {
        "decision_id": d["decision_id"],
        "actor_id": d["actor_id"],
        "reason_code": d["reason_code"],
        "target_task_ids": d["target_task_ids"],
        "comment": d["comment"],
        "context_version": d["context_version"],
    }


# ── Run ────────────────────────────────────────────────────────


def step_work_delays(conn: sqlite3.Connection, run_id: str) -> dict[int, int | None]:
    """Solver step별 2단계 해의 근무 분 지연. 조회 시 계산하고 AgentStep에는 저장하지 않는다.

    solver_job → solver_result(2단계 해) + search_spec → snapshot(기준 배정·근무 구간). 검토 패널과 같은 계산.
    """
    found = rows(
        conn,
        "SELECT j.step_no, r.stage2, s.snapshot_id FROM solver_job j"
        " JOIN solver_result r ON r.solver_result_id = j.solver_result_id"
        " JOIN search_spec s ON s.search_spec_id = j.search_spec_id"
        " WHERE j.run_id = ?",
        (run_id,),
    )
    out: dict[int, int | None] = {}
    for r in found:
        stage2 = loads(r["stage2"])
        snapshot = get_snapshot(conn, r["snapshot_id"])
        if stage2 is None or snapshot is None:
            continue
        out[r["step_no"]] = _work_delay_sum(stage2.get("solution"), snapshot.facts())
    return out


def run_summary(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    run = get_run(conn, run_id)
    if run is None:
        raise ApiError(404, "RUN_NOT_FOUND")
    last = conn.execute(
        "SELECT status FROM agent_step WHERE run_id = ? ORDER BY step_no DESC LIMIT 1", (run_id,)
    ).fetchone()
    # 재개 횟수 = 대기(WAIT)에 들어간 step 중 뒤에 step이 이어진 것 (조회 시 계산)
    resumes = conn.execute(
        "SELECT COUNT(*) FROM agent_step WHERE run_id = ? AND result_kind = 'WAIT'"
        " AND step_no < (SELECT MAX(step_no) FROM agent_step WHERE run_id = ?)",
        (run_id, run_id),
    ).fetchone()[0]
    return {
        "run_id": run.run_id,
        "agent_type": run.agent_type,
        "case_id": run.case_id,
        # 부른 메인 Run(하위 Run일 때). 메인과 Intake는 없다
        "parent_run_id": run.parent_run_id,
        "acting_unit_id": run.acting_unit_id,
        "status": run.status,
        "wait_kind": run.wait_kind,
        "wait_ref": run.wait_ref,
        "wait_generation": run.wait_generation,
        "resume_count": resumes,
        "last_step_no": run.last_step_no,
        "current_step_status": None if last is None else last[0],
        "end_reason": run.end_reason,
        "budget_used": run.budget_used,
        # 메인의 한도는 사람이 만든 일만큼 늘어난다 (AG-30). 다른 Agent는 고정값이라 주지 않는다
        "budget_max": _main_limits(conn, run) if run.agent_type == "MAIN" else None,
    }


def _main_limits(conn: sqlite3.Connection, run: AgentRun) -> dict[str, int]:
    site_id = conn.execute(
        "SELECT site_id FROM agent_run WHERE run_id = ?", (run.run_id,)
    ).fetchone()[0]
    return casefacts.budget_limits(conn, site_id, run)


# ── 상태 ───────────────────────────────────────────────────────


def build_state(
    conn: sqlite3.Connection, pack: LoadedPack, actor_id: str | None = None
) -> dict[str, Any]:
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
    pins = active_pin_views(conn, site_id)
    windows = preferred_windows(conn, site_id)
    content = build_snapshot_content(conn, site_id, pack)
    snapshot = Snapshot(snapshot_id="state", snapshot_hash=canonical_hash(content), content=content)
    conflicts = detect_conflicts(snapshot, snapshot.facts().check_assignments(), pack)
    base = snapshot.facts().base_assignments()

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
            {
                **t.model_dump(mode="json"),
                **gate(t, plan, site.context_version, holds),
                # 사람이 건 고정(누가·언제)과 희망 영역(구간·출처·만든 주체) (AG-27, ST-22)
                "pin": pins.get(t.task_id),
                "preferred_window": windows.get(t.task_id),
                # 기준 시작: 계획에 있으면 지금 배치, 없으면 희망 시작(없으면 가장 이른 시작). READY만
                "base_start": base[t.task_id].start if t.task_id in base else None,
            }
            for t in tasks
        ],
        "plan": plan.model_dump(mode="json"),
        "conflicts": [c.model_dump(mode="json") for c in conflicts],
        "candidates": [candidate_view(conn, site_id, cid) for cid in ids],
        "review_queue": queue,
        # 대기열(QUEUED) 접수 순서
        "task_queue": queued_task_ids(conn, site_id),
        # Hold마다 그 Event의 사실 수정안 (FACT_CONFIRMED 해제 판단)
        "holds": [
            {
                **h,
                "fact_updates": [
                    {
                        "proposal_id": p["proposal_id"],
                        "task_id": p["target_task_id"],
                        "field": p["payload"]["field"],
                        "old_value": p["payload"]["old_value"],
                        "new_value": p["payload"]["new_value"],
                        "status": p["status"],
                    }
                    for p in list_fact_updates(conn, site_id, event_id=h["event_id"])
                ],
            }
            for h in holds
        ],
        "events": events,
        "runs": [run_summary(conn, rid) for rid in run_ids],
        "dispatch": {"pending": jobs.get("PENDING", 0), "failed": jobs.get("FAILED", 0)},
        # X-Actor 본인에게 온 질문. body = 서버 문구, agent_text = 모델 작성
        "inbox": [] if actor_id is None else list_inbox(conn, site_id, actor_id),
    }


@router.get("/sites/{site_id}/state")
def get_state(site_id: str, pack: PackDep, actor: ActorDep) -> dict[str, Any]:
    check_site(site_id, pack)
    with db.read_tx() as conn:
        return build_state(conn, pack, actor.actor_id)


@router.get("/tasks/{task_id}/move-range")
def get_move_range(task_id: str, pack: PackDep, actor: ActorDep) -> dict[str, Any]:
    """그 작업을 놓을 수 있는 시작 구간. 화면은 이 구간만 칠한다 (CV-28)."""
    with db.read_tx() as conn:
        return move_range(conn, pack, actor.actor_id, task_id)


@router.get("/tasks/{task_id}/move-check")
def get_move_check(task_id: str, start: int, pack: PackDep, actor: ActorDep) -> dict[str, Any]:
    """놓은 자리의 판정. 확정과 같은 판정이다 (CV-28)."""
    with db.read_tx() as conn:
        return move_check(conn, pack, actor.actor_id, task_id, start)


@router.get("/tasks/{task_id}/resource-check")
def get_resource_check(
    task_id: str, resource_id: str, pack: PackDep, actor: ActorDep
) -> dict[str, Any]:
    """작업 카드의 자원 바꾸기 확인: 그 자원으로 바꿀 수 있는지와 무효가 될 검토 중인 안 (AG-31)."""
    with db.read_tx() as conn:
        return resource_check(conn, pack, actor.actor_id, task_id, resource_id)


@router.get("/tasks/{task_id}/remove-check")
def get_remove_check(task_id: str, pack: PackDep, actor: ActorDep) -> dict[str, Any]:
    """[작업 없애기]의 확인: 없앨 수 있는지와 무효가 될 검토 중인 안 (AG-31)."""
    with db.read_tx() as conn:
        return remove_check(conn, pack, actor.actor_id, task_id)


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
        steps = list_steps(conn, run_id)
        delays = step_work_delays(conn, run_id)
    # SOLVE step의 tool_result.stage2에 근무 분 지연을 붙인다(응답에만)
    for s in steps:
        stage2 = (s["tool_result"] or {}).get("stage2")
        if isinstance(stage2, dict):
            stage2["work_delay"] = delays.get(s["step_no"])
    return steps


def _request_view(r: dict[str, Any] | None) -> dict[str, Any] | None:
    """검토 패널용 변경 요청 요약. comment는 담당자가 쓴 인용이다."""
    if r is None:
        return None
    reply = r["reply"] or {}
    return {
        "message_id": r["message_id"],
        "to_actor_id": r["to_actor_id"],
        "status": r["status"],
        "decision": reply.get("decision"),
        "quoted_comment": reply.get("comment") or None,
    }
