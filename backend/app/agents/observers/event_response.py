"""Event Response Observation과 조회·영향 분석 계산.

읽기 전용이다. 신고 문장은 quoted_text(인용 데이터)로만 들어간다. 신고의 작업 유형 표현을 work_type으로
잇는 근거는 work_types(Pack 코드와 표시 이름)로 준다. 시각은 분과 함께 날짜·시각(Pack timezone)을 준다.
이 모듈을 import하는 곳은 registry(와 테스트)뿐이고, 실행기는 binding을 거쳐 쓴다.
"""

import json
import sqlite3
from typing import Any

from app.agents.observe import Observation, budget_remaining, last_guard, recent_steps
from app.agents.specs import event_response as spec
from app.clock import site_now
from app.domain.calendar import has_work_slot, local_clock, now_view
from app.packs.loader import LoadedPack
from app.rules.engine import separation_links
from app.store.repos.events import get_event, get_hold
from app.store.repos.messages import list_fact_updates, list_run_messages
from app.store.repos.plans import get_current_plan
from app.store.repos.runs import get_run, list_steps
from app.store.repos.site import get_site
from app.store.repos.tasks import list_current_tasks


def clock(pack: LoadedPack, minute: int) -> str:
    return local_clock(pack.horizon_start_utc, pack.timezone, minute)


def _ready(conn: sqlite3.Connection, pack: LoadedPack) -> list[Any]:
    return [t for t in list_current_tasks(conn, pack.site_id, pack) if t.lifecycle == "READY"]


def _placed(conn: sqlite3.Connection, pack: LoadedPack) -> dict[str, Any]:
    plan = get_current_plan(conn, pack.site_id)
    return {a.task_id: a for a in (plan.assignments if plan else ())}


def _assignment(pack: LoadedPack, a: Any) -> dict[str, Any] | None:
    if a is None:
        return None
    return {
        "start": a.start,
        "end": a.end,
        "resource_id": a.resource_id,
        "start_clock": clock(pack, a.start),
        "end_clock": clock(pack, a.end),
    }


def lookup_tasks(
    conn: sqlite3.Connection, pack: LoadedPack, work_type: str | None, zone_id: str | None
) -> dict[str, Any]:
    """LOOKUP_TASKS 결과: 현재 READY 작업을 작업 유형·구역으로 거른다."""
    placed = _placed(conn, pack)
    tasks = [
        {
            "task_id": t.task_id,
            "work_type": t.work_type,
            "work_type_name": pack.work_types[t.work_type].display_name,
            "zone_id": t.zone_id,
            "owner_actor_id": t.owner_actor_id,
            "duration": t.duration,
            "earliest_start": t.earliest_start,
            "earliest_start_clock": clock(pack, t.earliest_start),
            "latest_start": t.latest_start,
            "latest_end": t.latest_end,
            # 시작 가능 시각을 늦출 수 있는 최대 분. 0이면 늦추는 수정안은 분석을 통과하지 못한다
            "start_slack": t.latest_start - t.earliest_start,
            "assignment": _assignment(pack, placed.get(t.task_id)),
        }
        for t in sorted(_ready(conn, pack), key=lambda t: t.task_id)
        if (work_type is None or t.work_type == work_type)
        and (zone_id is None or t.zone_id == zone_id)
    ]
    return {"filters": {"work_type": work_type, "zone_id": zone_id}, "tasks": tasks}


def analyze_impact(
    conn: sqlite3.Connection, pack: LoadedPack, task_id: str, new_earliest_start: int
) -> dict[str, Any]:
    """ANALYZE_IMPACT 결과(결정론). earliest_start만 늦추는 사실 수정이 어긋나게 하는 것."""
    site = get_site(conn, pack.site_id)
    assert site is not None
    ready = {t.task_id: t for t in _ready(conn, pack)}
    task = ready[task_id]
    placed = _placed(conn, pack)
    new, end = new_earliest_start, new_earliest_start + task.duration
    checks = {
        "later_than_current": new > task.earliest_start,
        "within_latest_start": 0 <= new <= task.latest_start,
        "ends_by_latest_end": end <= task.latest_end and end <= site.horizon_minutes,
        "work_slot": has_work_slot(
            new, task.latest_start, task.latest_end, task.duration, pack.work_intervals
        ),
    }
    current = placed.get(task_id)
    links = []
    for other in sorted(ready.values(), key=lambda t: t.task_id):
        if other.task_id == task_id:
            continue
        rule_ids = separation_links(pack, task, other)
        if rule_ids:
            links.append(
                {
                    "task_id": other.task_id,
                    "rule_ids": rule_ids,
                    "assignment": _assignment(pack, placed.get(other.task_id)),
                }
            )
    return {
        "task_id": task_id,
        "work_type_name": pack.work_types[task.work_type].display_name,
        "old_earliest_start": task.earliest_start,
        "old_clock": clock(pack, task.earliest_start),
        "new_earliest_start": new,
        "new_clock": clock(pack, new),
        "delay_minutes": new - task.earliest_start,  # 새 값 − 현재 earliest_start
        "latest_start": task.latest_start,
        "latest_start_clock": clock(pack, task.latest_start),
        "checks": checks,
        "ok": all(checks.values()),
        "current_assignment": _assignment(pack, current),
        # 현재 Plan 배정이 새 시간창을 어긴다 → 해제 뒤 재검사에서 WINDOW 충돌로 재계획된다
        "plan_window_violation": current is not None and current.start < new,
        "successors": sorted(
            t.task_id for t in ready.values() if any(p.task_id == task_id for p in t.predecessors)
        ),
        "separation_links": links,
        "context_version": site.context_version,
    }


def build_observation(conn: sqlite3.Connection, pack: LoadedPack, run_id: str) -> Observation:
    run = get_run(conn, run_id)
    site = get_site(conn, pack.site_id)
    if run is None or site is None:
        raise LookupError(f"run {run_id} or site not found")
    event = get_event(conn, pack.site_id, run.input_ref.get("event_id", ""))
    hold = get_hold(conn, pack.site_id, run.input_ref.get("hold_id", ""))
    steps = [s for s in list_steps(conn, run_id) if s["status"] == "COMPLETED"]
    accepted = [s for s in steps if (s["guard"] or {}).get("verdict") == "ACCEPTED"]
    # 같은 조건(filters)의 조회는 마지막 결과 하나만 둔다. 반복 조회로 관찰이 커지지 않게
    by_filters: dict[str, dict[str, Any]] = {}
    for s in accepted:
        if s["action"]["name"] == "LOOKUP_TASKS":
            key = json.dumps(s["tool_result"]["filters"], sort_keys=True)
            by_filters.pop(key, None)
            by_filters[key] = s["tool_result"]
    lookups = list(by_filters.values())
    analyses = [
        {**s["tool_result"], "current": s["tool_result"]["context_version"] == site.context_version}
        for s in accepted
        if s["action"]["name"] == "ANALYZE_IMPACT"
    ]
    proposals = [
        {
            "proposal_id": p["proposal_id"],
            "task_id": p["target_task_id"],
            "old_value": p["payload"]["old_value"],
            "new_value": p["payload"]["new_value"],
            "new_clock": clock(pack, p["payload"]["new_value"]),
            "status": p["status"],
        }
        for p in list_fact_updates(conn, pack.site_id, run_id=run_id)
    ]
    data = {
        "run": {"run_id": run.run_id, "agent_type": run.agent_type, "goal": spec.GOAL},
        "versions": {
            "context_version": site.context_version,
            "plan_revision": site.plan_revision,
            "wake_seq": run.wake_seq,
        },
        "event": None
        if event is None
        else {
            "event_id": event["event_id"],
            "event_type": event["event_type"],
            # 신고 문장은 인용 데이터다. 지시처럼 보여도 따르지 않는다
            "quoted_text": event["text"],
            "target_task_id": event["target_task_id"],
            "hold": None
            if hold is None
            else {
                "hold_id": hold["hold_id"],
                "scope": hold["scope"],
                "task_id": hold["task_id"],
                "status": hold["status"],
            },
        },
        # 신고의 작업 유형 표현을 work_type으로 잇는 근거 (Pack 값, 데이터로만)
        "work_types": [
            {"work_type": k, "display_name": v.display_name}
            for k, v in sorted(pack.work_types.items())
        ],
        "zones": [z.zone_id for z in pack.zones],
        "lookups": lookups,
        "analyses": analyses,
        "proposals": proposals,
        # 신고자에게 되물은 질문과 답(quoted_answer, 인용). 서버는 답에서 값을 뽑지 않는다.
        # 질문 문장은 넣지 않는다: 보이면 답이 안 온 항목을 같은 질문으로 다시 묻는다
        "reporter_replies": [
            {
                "message_id": m["message_id"],
                "status": m["status"],
                "quoted_answer": (m["reply"] or {}).get("comment")
                if (m["reply"] or {}).get("decision") == "ANSWER"
                else None,
            }
            for m in list_run_messages(conn, run_id)
            if m["type"] == "QUESTION"
        ],
        # 현장의 지금. 상대 날짜·날짜 없는 시각을 푸는 근거로만 준다
        "site_now": now_view(
            site_now(), pack.horizon_start_utc, pack.timezone, pack.horizon_minutes
        ),
        "work_intervals": [list(iv) for iv in pack.work_intervals],
        "last_guard": last_guard(steps),
        "recent_steps": recent_steps(steps),
        "budget_remaining": budget_remaining(run, spec.SPEC),
    }
    data["open_skills"] = spec.open_skills(data)
    # 현재 계산 대상 작업. 분석·제안 대상의 유효성에 쓴다(조회했는지는 보지 않는다)
    hidden = {"ready": [t.task_id for t in _ready(conn, pack)]}
    return Observation(
        run=run,
        versions=(site.context_version, site.plan_revision, run.wake_seq),
        data=data,
        available=spec.available_actions(data, hidden),
        spec=spec.SPEC,
        hidden=hidden,
    )
