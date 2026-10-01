"""Coordination Observation과 Available Actions 계산 (설계서 §18.2.2, 부록 A.24).

읽기 전용이다. GET_CHANGE_IMPACT는 Action이 아니라 이 관찰에 서버가 넣는다(협의 항목·통지 대상).
사람이 쓴 이견 문장은 quoted_comment(인용 데이터)로만 들어간다. 이 모듈을 import하는 곳은
registry(와 테스트)뿐이고, 실행기는 binding을 거쳐 쓴다 (A.23).
"""

import sqlite3
from typing import Any

from app.agents.observe import Observation, budget_remaining, last_guard, recent_steps
from app.agents.specs import coordination as spec
from app.domain.models import Assignment
from app.packs.loader import LoadedPack
from app.rules.engine import separation_links
from app.store.repos.consultations import candidate_state, consultation_view
from app.store.repos.messages import list_change_requests, list_run_messages
from app.store.repos.plans import get_plan
from app.store.repos.records import get_candidate
from app.store.repos.runs import get_run, list_steps
from app.store.repos.site import get_site
from app.store.repos.tasks import list_current_tasks


def changed_axes(before: Assignment, after: Assignment) -> list[str]:
    """협의 항목에서 바뀐 축 (A.24 7: 제약 초안의 축은 이 중 하나 이상을 포함해야 한다)."""
    axes = []
    if before.start != after.start:
        axes.append("TIME")
    if before.resource_id != after.resource_id:
        axes.append("RESOURCE")
    return axes


def _items(conn: sqlite3.Connection, pack: LoadedPack, run_id: str, candidate_id: str) -> list:
    view = consultation_view(conn, pack.site_id, candidate_id)
    if view is None:
        return []
    mine: dict[str, list[dict[str, Any]]] = {}
    for r in list_change_requests(conn, pack.site_id, candidate_id):
        if r["run_id"] != run_id:
            continue
        draft = r["draft"]
        reply = r["reply"] or {}
        mine.setdefault(r["change_hash"], []).append(
            {
                "message_id": r["message_id"],
                "status": r["status"],
                "decision": reply.get("decision"),
                "quoted_comment": reply.get("comment"),
                "draft": None
                if draft is None
                else {
                    "proposal_id": draft["proposal_id"],
                    "status": draft["status"],
                    "reason_code": draft["payload"].get("reason_code"),
                    "axes": draft["payload"].get("axes"),
                },
            }
        )
    return [
        {
            "task_id": i.task_id,
            "owner_actor_id": i.owner_actor_id,
            "before": i.before.model_dump(),
            "after": i.after.model_dump(),
            "changed_axes": changed_axes(i.before, i.after),
            "status": view.item_status[i.task_id],
            "requests": mine.get(i.change_hash, []),
        }
        for i in view.items
    ]


def notice_targets(
    conn: sqlite3.Connection, pack: LoadedPack, plan_revision: int
) -> list[dict[str, Any]]:
    """확정 Plan과 직전 Plan을 비교한 통지 대상 (A.24 8).

    바뀐(새로 들어간) 작업의 담당자와, 새 Plan에서 SEPARATION Rule로 그 작업과 엮인 작업의 담당자.
    자원 공유는 넣지 않는다.
    """
    plan, prev = get_plan(conn, pack.site_id, plan_revision), None
    if plan_revision > 0:
        prev = get_plan(conn, pack.site_id, plan_revision - 1)
    if plan is None:
        return []
    before = {a.task_id: a for a in (prev.assignments if prev else ())}
    tasks = {t.task_id: t for t in list_current_tasks(conn, pack.site_id, pack)}
    placed = [a for a in plan.assignments if a.task_id in tasks]
    changed = [a.task_id for a in placed if before.get(a.task_id) != a]
    targets: dict[str, dict[str, Any]] = {}

    def add(task_id: str, reason: dict[str, Any]) -> None:
        owner = tasks[task_id].owner_actor_id
        t = targets.setdefault(owner, {"actor_id": owner, "task_ids": [], "reasons": []})
        if task_id not in t["task_ids"]:
            t["task_ids"].append(task_id)
        t["reasons"].append(reason)

    for tid in changed:
        add(tid, {"task_id": tid, "kind": "CHANGED"})
    for tid in changed:
        for other in placed:
            if other.task_id == tid or other.task_id in changed:
                continue
            for rule_id in separation_links(pack, tasks[tid], tasks[other.task_id]):
                add(
                    other.task_id,
                    {
                        "task_id": other.task_id,
                        "kind": "SAFETY_LINK",
                        "rule_id": rule_id,
                        "with_task_id": tid,
                    },
                )
    return [targets[k] for k in sorted(targets)]


def build_observation(conn: sqlite3.Connection, pack: LoadedPack, run_id: str) -> Observation:
    run = get_run(conn, run_id)
    site = get_site(conn, pack.site_id)
    if run is None or site is None:
        raise LookupError(f"run {run_id} or site not found")
    phase = run.input_ref.get("phase")
    candidate_id = run.input_ref.get("candidate_id")
    candidate = get_candidate(conn, pack.site_id, candidate_id) if candidate_id else None
    live, consultation = False, None
    if candidate is not None:
        state = candidate_state(conn, pack.site_id, candidate)
        live = not (state.stale or state.rejected or state.committed)
        view = consultation_view(conn, pack.site_id, candidate.candidate_id)
        consultation = None if view is None else view.status
    items = _items(conn, pack, run_id, candidate_id) if phase == "CONSULT" and candidate_id else []
    targets: list[dict[str, Any]] = []
    if phase == "NOTICE":
        sent = {m["to_actor_id"] for m in list_run_messages(conn, run_id) if m["type"] == "NOTICE"}
        targets = [
            {**t, "sent": t["actor_id"] in sent}
            for t in notice_targets(conn, pack, run.input_ref.get("plan_revision", 0))
        ]
    steps = [s for s in list_steps(conn, run_id) if s["status"] == "COMPLETED"]
    data = {
        "run": {
            "run_id": run.run_id,
            "agent_type": run.agent_type,
            "goal": spec.GOAL,
        },
        "versions": {
            "context_version": site.context_version,
            "plan_revision": site.plan_revision,
            "wake_seq": run.wake_seq,
        },
        "phase": phase,
        "candidate": {
            "candidate_id": candidate_id,
            "live": live,
            "consultation_status": consultation,
        },
        "items": items,
        "notice_targets": targets,
        "last_guard": last_guard(steps),
        "recent_steps": recent_steps(steps),
        "budget_remaining": budget_remaining(run, spec.SPEC),
    }
    return Observation(
        run=run,
        versions=(site.context_version, site.plan_revision, run.wake_seq),
        data=data,
        available=spec.available_actions(data),
        spec=spec.SPEC,
    )
