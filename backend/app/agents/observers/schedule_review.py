"""Schedule Review Observation 계산.

읽기 전용이다. 모두 서버가 계산한 사실이다: 충돌 목록, 최소 묶음(작업을 공유하는 충돌), 최소 묶음 사이의
관계(같은 자원, 같은 구역·관계 구역, 시간 간격), 사람만 풀 수 있는 최소 묶음, 일정에서 온 작업과 기존 작업의
구분, 이 Case의 거절·이견 사유(인용). 묶음을 어떻게 합칠지는 계산하지 않는다 (AG-36).
이 모듈을 import하는 곳은 registry(와 테스트)뿐이고, 실행기는 binding을 거쳐 쓴다.
"""

import sqlite3
from typing import Any

from app.agents import casefacts
from app.agents.observe import Observation, budget_remaining, last_guard, recent_steps
from app.agents.specs import schedule_review as spec
from app.domain.bundles import group_relations, group_span, human_only_reason
from app.domain.calendar import site_time
from app.domain.groups import ConflictGroup
from app.domain.models import SnapshotContent
from app.packs.loader import LoadedPack
from app.store.repos.bundles import list_bundle_plans
from app.store.repos.case_events import list_case_events
from app.store.repos.consultations import case_objections
from app.store.repos.decisions import case_rejection_reasons
from app.store.repos.runs import get_run, list_steps
from app.store.repos.site import get_site


def group_views(
    pack: LoadedPack, facts: SnapshotContent, groups: list[ConflictGroup]
) -> list[dict[str, Any]]:
    """최소 묶음마다: 작업, 걸린 규칙, 충돌 시간, 사람만 풀 수 있는지와 그 이유 (서버 계산)."""

    def clock(minute: int) -> str:
        return site_time(pack.horizon_start_utc, pack.timezone, minute)

    out = []
    for g in groups:
        lo, hi = group_span(g)
        reason = human_only_reason(facts, g)
        out.append(
            {
                "group_id": g.group_id,
                "task_ids": list(g.task_ids),
                "rule_ids": sorted({c.rule_id for c in g.conflicts}),
                "interval": [lo, hi],
                "clock": [clock(lo), clock(hi)],
                # 사람만 풀 수 있다: 걸린 작업이 모두 고정(ALL_PINNED)이거나 사실이 바뀌어야 한다(FACT_REQUIRED)
                "human_only": reason is not None,
                "human_only_reason": reason,
            }
        )
    return out


def schedule_events(conn: sqlite3.Connection, site_id: str, case_id: str) -> list[dict[str, Any]]:
    """이 Case의 일정 넣기 사건의 참조: 일정 ID, 새로 들어온 작업, 값이 바뀐 기존 작업."""
    return [
        {
            "schedule_id": e["ref"].get("schedule_id"),
            "task_ids": list(e["ref"].get("task_ids", ())),
            "changed_task_ids": list(e["ref"].get("changed_task_ids", ())),
        }
        for e in list_case_events(conn, site_id)
        if e["case_id"] == case_id and e["kind"] == "SCHEDULE_IMPORTED"
    ]


def plan_summary(plan: dict[str, Any], context_version: int, plan_revision: int) -> dict[str, Any]:
    """묶음안 요약(서버 값만): 묶음과 사람만 풀 수 있는 묶음. 메모와 의견(모델 문장)은 넣지 않는다."""
    return {
        "bundle_plan_id": plan["bundle_plan_id"],
        # 그 묶음안을 낸 뒤 현장 사실이 바뀌지 않았는가
        "current": (plan["context_version"], plan["plan_revision"])
        == (context_version, plan_revision),
        "bundles": [
            {k: b[k] for k in ("bundle_id", "group_ids", "task_ids", "human_only")}
            for b in plan["bundles"]
        ],
    }


def build_observation(conn: sqlite3.Connection, pack: LoadedPack, run_id: str) -> Observation:
    run = get_run(conn, run_id)
    site = get_site(conn, pack.site_id)
    if run is None or site is None:
        raise LookupError(f"run {run_id} or site not found")
    _, facts, groups = casefacts.current_groups(conn, pack)
    events = schedule_events(conn, pack.site_id, run.case_id)
    imported = {tid for e in events for tid in e["task_ids"]}
    in_plan = {a.task_id for a in facts.plan.assignments}
    base, pins = facts.base_assignments(), facts.pinned_task_ids()
    involved = sorted({tid for g in groups for tid in g.task_ids})
    tasks = facts.task_map()

    def clock(minute: int) -> str:
        return site_time(pack.horizon_start_utc, pack.timezone, minute)

    steps = [s for s in list_steps(conn, run_id) if s["status"] == "COMPLETED"]
    data = {
        "run": {"run_id": run.run_id, "agent_type": run.agent_type, "goal": spec.GOAL},
        "versions": {
            "context_version": site.context_version,
            "plan_revision": site.plan_revision,
            "wake_seq": run.wake_seq,
        },
        # 이 Case의 일정 넣기: 어느 일정에서 어떤 작업이 들어왔는가
        "schedule": events,
        "conflicts": [c.model_dump(mode="json") for g in groups for c in g.conflicts],
        # 최소 묶음: 작업을 공유하는 충돌끼리 서버가 묶은 것. 쪼갤 수 없다
        "groups": group_views(pack, facts, groups),
        # 최소 묶음 사이의 관계: 같은 자원, 같은 구역·관계 구역, 충돌 시간 사이의 간격
        "relations": group_relations(facts, groups),
        # 충돌에 걸린 작업. from_schedule이면 이 Case의 일정으로 들어온 작업이고 아니면 기존 작업이다
        "tasks": [
            {
                "task_id": tid,
                "unit_id": tasks[tid].unit_id,
                "zone_id": tasks[tid].zone_id,
                "from_schedule": tid in imported,
                "in_plan": tid in in_plan,
                "pinned": tid in pins,
                "base": base[tid].model_dump(),
                "base_clock": [clock(base[tid].start), clock(base[tid].end)],
            }
            for tid in involved
            if tid in tasks
        ],
        # 이 Case에서 사람이 남긴 사유(인용): Supervisor 거절과 담당자 이견
        "rejections": case_rejection_reasons(conn, run.case_id),
        "objections": case_objections(conn, pack.site_id, run.case_id),
        # 이 Run이 낸 묶음안
        "bundle_plans": [
            plan_summary(p, site.context_version, site.plan_revision)
            for p in list_bundle_plans(conn, pack.site_id, run_id=run_id)
        ],
        "last_guard": last_guard(steps),
        "recent_steps": recent_steps(steps),
        "budget_remaining": budget_remaining(run, spec.SPEC),
    }
    data["open_skills"] = spec.open_skills(data)
    return Observation(
        run=run,
        versions=(site.context_version, site.plan_revision, run.wake_seq),
        data=data,
        available=spec.available_actions(data),
        spec=spec.SPEC,
    )
