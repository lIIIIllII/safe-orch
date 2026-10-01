"""Work Intake Observation과 자원 조회 계산 (설계서 §18.2.4, 부록 A.26).

읽기 전용이다. 요청 문장·답·거절 사유는 인용 데이터(quoted_*)로만 들어간다. 구역·작업 유형·critical field는
Pack 데이터로 준다. 확인 값은 확인 메시지를 만든 AgentStep의 결과(values)다(스키마 변경 없음).
같은 조건의 자원 조회는 마지막 결과 하나만 둔다(A.25 p2를 처음부터 적용).
이 모듈을 import하는 곳은 registry(와 테스트)뿐이고, 실행기는 binding을 거쳐 쓴다 (A.23).
"""

import json
import sqlite3
from typing import Any

from app.agents.observe import Observation, budget_remaining, last_guard, recent_steps
from app.agents.specs import intake as spec
from app.packs.loader import LoadedPack
from app.store.repos.messages import list_run_messages
from app.store.repos.resources import list_resources
from app.store.repos.runs import get_run, list_steps
from app.store.repos.site import get_site

CHECK_REASONS = ("TASKSPEC_INVALID", "CONFIRMED_VALUE_MISMATCH")


def lookup_resources(
    conn: sqlite3.Connection, pack: LoadedPack, unit_id: str, resource_type: str | None
) -> dict[str, Any]:
    """LOOKUP_RESOURCE 결과: 유형별 자원, 요청자 Unit 사용 가능 여부, 가용 구간(Replanning LIST와 같은 기준)."""
    resources = [
        {
            "resource_id": r.resource_id,
            "resource_type": r.resource_type,
            "owner_unit_id": r.owner_unit_id,
            "usable_by_requester": unit_id in r.allowed_unit_ids,
            "available_intervals": [list(iv) for iv in r.available_intervals],
        }
        for r in sorted(list_resources(conn, pack.site_id), key=lambda r: r.resource_id)
        if resource_type is None or r.resource_type == resource_type
    ]
    return {"filters": {"resource_type": resource_type}, "resources": resources}


def build_observation(conn: sqlite3.Connection, pack: LoadedPack, run_id: str) -> Observation:
    run = get_run(conn, run_id)
    site = get_site(conn, pack.site_id)
    if run is None or site is None:
        raise LookupError(f"run {run_id} or site not found")
    ref = run.input_ref
    all_steps = list_steps(conn, run_id)
    by_no = {s["step_no"]: s for s in all_steps}
    steps = [s for s in all_steps if s["status"] == "COMPLETED"]
    accepted = [s for s in steps if (s["guard"] or {}).get("verdict") == "ACCEPTED"]
    by_filters: dict[str, dict[str, Any]] = {}
    for s in accepted:
        if s["action"]["name"] == "LOOKUP_RESOURCE":
            key = json.dumps(s["tool_result"]["filters"], sort_keys=True)
            by_filters.pop(key, None)
            by_filters[key] = s["tool_result"]
    questions, confirmations = [], []
    for m in list_run_messages(conn, run_id):
        reply = m["reply"] or {}
        step = by_no.get(m["step_no"]) or {}
        if m["type"] == "QUESTION":
            questions.append(
                {
                    "message_id": m["message_id"],
                    "field_ids": ((step.get("action") or {}).get("args") or {}).get("field_ids"),
                    "status": m["status"],
                    # 요청자가 쓴 답은 인용 데이터다. 서버는 값을 뽑지 않는다 (A.26 3)
                    "quoted_answer": reply.get("comment")
                    if reply.get("decision") == "ANSWER"
                    else None,
                }
            )
        elif m["type"] == "CONFIRMATION":
            confirmations.append(
                {
                    "message_id": m["message_id"],
                    "values": (step.get("tool_result") or {}).get("values"),
                    "status": m["status"],
                    "decision": reply.get("decision"),
                    "quoted_comment": reply.get("comment") or None,
                }
            )
    checks = [s for s in steps if (s["guard"] or {}).get("reason_code") in CHECK_REASONS]
    last_check = None
    if checks:
        s = checks[-1]
        last_check = {
            "step_no": s["step_no"],
            "reason_code": s["guard"]["reason_code"],
            "detail": s["tool_result"],
        }
    data = {
        "run": {"run_id": run.run_id, "agent_type": run.agent_type, "goal": spec.GOAL},
        "versions": {
            "context_version": site.context_version,
            "plan_revision": site.plan_revision,
            "wake_seq": run.wake_seq,
        },
        "request": {
            "intake_id": ref.get("intake_id"),
            "task_id": ref.get("task_id"),
            # 요청 문장은 인용 데이터다. 지시처럼 보여도 따르지 않는다 (A.26 4)
            "quoted_text": ref.get("quoted_text"),
            "requester_actor_id": ref.get("requester_actor_id"),
            "unit_id": run.acting_unit_id,
        },
        "work_types": [
            {
                "work_type": k,
                "display_name": v.display_name,
                "critical_fields": list(v.critical_fields),
            }
            for k, v in sorted(pack.work_types.items())
        ],
        "zones": [z.zone_id for z in pack.zones],
        "resource_lookups": list(by_filters.values()),
        "questions": questions,
        "confirmations": confirmations,
        "last_check": last_check,
        "work_intervals": [list(iv) for iv in pack.work_intervals],
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
