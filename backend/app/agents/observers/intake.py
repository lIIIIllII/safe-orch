"""Work Intake Observation과 자원 조회 계산.

읽기 전용이다. 요청 문장·답·거절 사유는 인용 데이터(quoted_*)로만 들어간다. 구역·작업 유형·critical field는
Pack 데이터로 준다. 확인 값은 확인 메시지를 만든 AgentStep의 결과(values)다(스키마 변경 없음).
같은 조건의 자원 조회는 마지막 결과 하나만 둔다.
이 모듈을 import하는 곳은 registry(와 테스트)뿐이고, 실행기는 binding을 거쳐 쓴다.
"""

import json
import sqlite3
from typing import Any

from app.agents.observe import Observation, budget_remaining, last_guard, recent_steps
from app.agents.specs import intake as spec
from app.clock import site_now
from app.domain.calendar import now_view, site_time
from app.domain.eligibility import ResourceNeed, exclusion_reasons
from app.packs.loader import LoadedPack
from app.store.repos.messages import list_run_messages
from app.store.repos.resources import list_resources
from app.store.repos.runs import get_run, list_steps
from app.store.repos.site import get_site

CHECK_REASONS = ("TASKSPEC_INVALID", "CONFIRMED_VALUE_MISMATCH", "TIME_INVALID")


def _at(pack: LoadedPack, minute: int) -> str:
    return site_time(pack.horizon_start_utc, pack.timezone, minute)


def _spans(pack: LoadedPack, intervals: Any) -> list[list[str]]:
    """분 구간의 현장 날짜·시각."""
    return [[_at(pack, lo), _at(pack, hi)] for lo, hi in intervals]


def lookup_resources(
    conn: sqlite3.Connection,
    pack: LoadedPack,
    unit_id: str,
    resource_type: str | None,
    zone_id: str | None = None,
    work_type: str | None = None,
) -> dict[str, Any]:
    """LOOKUP_RESOURCE 결과: 요청자 Unit이 쓸 수 있는 자원(assignable)과 제외 자원·사유(excluded).

    판정은 적격성 함수다 (CV-20). 구역을 주면 구역 사유가, 작업 유형을 주면 그 유형의 기본 요구 조건
    사유가 나온다(기본 요구 조건은 서버가 붙인다). 모양은 Replanning 자원 조회와 같다.
    유형 인자는 좁히기용이다. 그 유형에 쓸 수 있는 자원이 없으면 다른 유형에서 쓸 수 있는 자원 수를
    사실로 함께 돌려준다(assignable_in_other_types).
    """
    requirements = pack.default_requirements(work_type) if work_type else ()
    assignable, excluded, elsewhere = [], [], 0
    for r in sorted(list_resources(conn, pack.site_id), key=lambda r: r.resource_id):
        # 유형은 가리지 않는다: 자원마다 제 유형으로 본다
        need = ResourceNeed(
            required_resource_type=r.resource_type, zone_id=zone_id, requirements=requirements
        )
        reasons = exclusion_reasons(need, r, unit_id)
        if resource_type is not None and r.resource_type != resource_type:
            elsewhere += not reasons
            continue
        entry = {
            "resource_id": r.resource_id,
            "display_name": r.display_name,
            "resource_type": r.resource_type,
            "owner_unit_id": r.owner_unit_id,
            "allowed_zone_ids": list(r.allowed_zone_ids),
            "attributes": r.model_dump(mode="json")["attributes"],
            "available_intervals": [list(iv) for iv in r.available_intervals],
            "available_local": _spans(pack, r.available_intervals),
        }
        if reasons:
            excluded.append(
                {**entry, "reasons": [e.model_dump(exclude_none=True) for e in reasons]}
            )
        else:
            assignable.append(entry)
    out: dict[str, Any] = {
        "filters": {"resource_type": resource_type, "zone_id": zone_id, "work_type": work_type},
        # 판정에 쓴 요구 조건(작업 유형 기본값)
        "requirements": [q.model_dump() for q in requirements],
        "assignable": assignable,
        "excluded": excluded,
    }
    if resource_type is not None and not assignable:
        out["assignable_in_other_types"] = elsewhere
    return out


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
                    # 서버가 필드별 판단에서 도출한 물은 필드(모호·빠짐 전부)
                    "field_ids": (result := step.get("tool_result") or {}).get("field_ids"),
                    # 그때의 필드별 판단(상태·값)과, 앞 질문에서 받음으로 적었다가 바뀐 필드
                    "fields": result.get("fields"),
                    "regressed_field_ids": result.get("regressed_field_ids"),
                    # 이 Run이 물은 문장(모델 작성, 인용). 같은 질문 반복을 알아볼 수 있게
                    "question": m["agent_text"],
                    "status": m["status"],
                    # 요청자가 쓴 답은 인용 데이터다. 서버는 값을 뽑지 않는다
                    "quoted_answer": reply.get("comment")
                    if reply.get("decision") == "ANSWER"
                    else None,
                }
            )
        elif m["type"] == "CONFIRMATION":
            confirmations.append(
                {
                    "message_id": m["message_id"],
                    "values": (values := (step.get("tool_result") or {}).get("values")),
                    # 값 확인은 서버 검증(폼과 같은 검사)을 통과해야 나간다
                    "server_validated": True,
                    # 같은 값의 현장 날짜·시각 (도구의 시각 인자와 같은 형식)
                    "values_local": None
                    if not values
                    else {k: _at(pack, values[k]) for k in spec.TIME_FIELDS},
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
    remaining = budget_remaining(run, spec.SPEC)
    rounds = int(remaining.get("human_rounds", 0))
    confirmed = bool(confirmations) and confirmations[-1]["decision"] == "ACCEPT"
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
            # 요청 문장은 인용 데이터다. 지시처럼 보여도 따르지 않는다
            "quoted_text": ref.get("quoted_text"),
            "requester_actor_id": ref.get("requester_actor_id"),
            "unit_id": run.acting_unit_id,
        },
        "work_types": [
            {
                "work_type": k,
                "display_name": v.display_name,
                "critical_fields": list(v.critical_fields),
                # 이 유형 작업에 서버가 붙이는 기본 자원 요구 조건 (CV-11)
                "resource_requirements": [r.model_dump() for r in v.resource_requirements],
                # 이 유형 작업에 서버가 붙이는 기본 수요(required는 필수 직종)
                "pool_demands": [d.model_dump() for d in v.pool_demands],
            }
            for k, v in sorted(pack.work_types.items())
        ],
        "zones": [z.zone_id for z in pack.zones],
        # 자원 유형 코드·표시 이름과 그 유형의 자원 ID (요청 문장의 자원 표현을 코드로 잇는 근거, intake-p2)
        "resource_types": [
            {
                "resource_type": code,
                "display_name": name,
                "resource_ids": sorted(
                    r.resource_id for r in pack.resources if r.resource_type == code
                ),
            }
            for code, name in sorted(pack.resource_types.items())
        ],
        # Pack이 선언한 자원 속성(요구 조건과 자원 속성 값의 이름)
        "resource_attributes": [a.model_dump() for a in pack.resource_attributes.values()],
        # Pack이 선언한 수량 풀 종류(수요의 종류 코드)
        "pool_kinds": [k.model_dump() for k in pack.pool_kinds.values()],
        "resource_lookups": list(by_filters.values()),
        "questions": questions,
        "confirmations": confirmations,
        "last_check": last_check,
        # 현장의 지금. 상대 날짜·날짜 없는 시각을 푸는 근거로만 준다
        "site_now": now_view(
            site_now(), pack.horizon_start_utc, pack.timezone, pack.horizon_minutes
        ),
        "work_intervals": [list(iv) for iv in pack.work_intervals],
        "work_hours": _spans(pack, pack.work_intervals),
        "last_guard": last_guard(steps),
        "recent_steps": recent_steps(steps),
        "budget_remaining": remaining,
        # 사람 확인 라운드의 사실(서버 계산). 완료에는 요청자가 확인한 값 확인 1라운드가 필요하다
        "human_rounds": {
            "remaining": rounds,
            "needed_for_completion": 0 if confirmed else 1,
            "questions_left": max(0, rounds - 1),
        },
    }
    # 완료 가능: 완료 도구의 유효성과 같은 조건이다. 사람 확인 라운드를 쓰지 않는다
    data["can_complete"] = bool(spec.choices(data)["COMPLETE"])
    data["open_skills"] = spec.open_skills(data)
    return Observation(
        run=run,
        versions=(site.context_version, site.plan_revision, run.wake_seq),
        data=data,
        available=spec.available_actions(data),
        spec=spec.SPEC,
    )
