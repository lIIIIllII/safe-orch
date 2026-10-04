"""Replanning Observation과 Available Actions 계산.

읽기 전용이다. Snapshot은 메모리에서만 만들고(hash만 계산) 저장은 Solver 예약 tx에서 한다.
observe 노드와 Gateway 예약 tx가 같은 함수로 계산한다(Gateway는 최신 tx 안에서 다시 계산).
agent_type 공통 부분(Observation, budget_remaining)은 app.agents.observe에 있다. 이 모듈을 import하는
곳은 registry(와 테스트)뿐이고, 실행기는 binding을 거쳐 쓴다.
"""

import sqlite3
from dataclasses import dataclass
from typing import Any

from app.agents import casefacts
from app.agents import observe as common
from app.agents.observe import budget_remaining
from app.agents.specs import replanning as spec
from app.domain.calendar import site_time
from app.domain.canonical import canonical_hash
from app.domain.eligibility import exclusion_reasons
from app.domain.models import Conflict, Snapshot, SnapshotContent, Task
from app.packs.loader import LoadedPack
from app.rules.engine import detect_conflicts
from app.solver.search_spec import SearchSpecError, build_search_spec
from app.store.repos.consultations import candidate_state, case_objections, contested_changes
from app.store.repos.decisions import case_rejection_reasons
from app.store.repos.records import get_candidate, list_validations
from app.store.repos.runs import (
    approach_attempts,
    get_run,
    list_attempts,
    list_steps,
    tried_search_keys,
)
from app.store.repos.site import get_site
from app.store.repos.snapshots import build_snapshot_content

RECENT_STEPS = 5


@dataclass(frozen=True)
class Observation(common.Observation):
    """Replanning 관찰. 공통 Observation에 지금 충돌 전체를 더한다(탐색 범위의 기준, AG-24)."""

    conflicts: tuple[Conflict, ...] = ()


def current_snapshot(conn: sqlite3.Connection, pack: LoadedPack) -> Snapshot:
    content = build_snapshot_content(conn, pack.site_id, pack)
    return Snapshot(snapshot_id="observe", snapshot_hash=canonical_hash(content), content=content)


def level_keys(snapshot: Snapshot, conflicts: list[Conflict]) -> dict[str, str]:
    """level별 실효 탐색 키. 만들 수 없는 level은 뺀다."""
    out = {}
    for level in spec.LEVELS:
        try:
            out[level] = build_search_spec(snapshot, conflicts, level).search_key
        except SearchSpecError:
            continue
    return out


def resources_hash(facts: SnapshotContent) -> str:
    """자원 사실의 hash. 자원 조회 결과는 이 값이 같은 동안 유효하다."""
    return canonical_hash([r.model_dump(mode="json") for r in facts.resources])


def assignable_resources(facts: SnapshotContent, task: Task) -> dict[str, Any]:
    """LIST_ASSIGNABLE_RESOURCES 결과. 탐색 범위·실행 검사와 같은 적격성 함수로, 그 작업의 Unit으로
    판정한다 (CV-20).

    유형이 다른 자원은 대상이 아니므로 목록에 넣지 않는다(excluded는 같은 유형만).
    excluded의 reasons는 제외 사유 전부다. 요구 조건 사유에는 어느 속성인지(attribute)가 붙는다.
    """
    current = facts.base_assignments()[task.task_id].resource_id
    assignable, excluded = [], []
    for r in sorted(facts.resources, key=lambda r: r.resource_id):
        reasons = exclusion_reasons(task, r, task.unit_id)
        if any(e.reason == "TYPE_MISMATCH" for e in reasons):
            continue
        if reasons:
            excluded.append(
                {
                    "resource_id": r.resource_id,
                    "reasons": [e.model_dump(exclude_none=True) for e in reasons],
                }
            )
        else:
            assignable.append({"resource_id": r.resource_id})
    return {
        "task_id": task.task_id,
        "required_type": task.required_resource_type,
        "current": current,
        "assignable": assignable,
        "excluded": excluded,
        "resources_hash": resources_hash(facts),
    }


def valid_listings(
    steps: list[dict[str, Any]], facts: SnapshotContent
) -> dict[str, dict[str, Any]]:
    """이 Run의 최근 자원 조회 결과 중 자원 사실이 같은 것 (작업별 마지막 1개)."""
    current = resources_hash(facts)
    out = {}
    for s in steps:
        result = s["tool_result"] or {}
        if (
            (s["action"] or {}).get("name") == "LIST_ASSIGNABLE_RESOURCES"
            and s["guard"]["verdict"] == "ACCEPTED"
            and result.get("resources_hash") == current
        ):
            out[result["task_id"]] = result
    return out


FACT_BY_EXCLUSION = {"NOT_ALLOWED": "PERMISSION", "NO_AVAILABILITY": "AVAILABILITY"}


# 정한 값 → 요청자가 고치면 바뀌는 사실(FACT_CHANGE의 필드)
DECIDED_FACTS = {
    "DURATION": {"duration"},
}


def openers(
    facts: SnapshotContent, conflicts: list[Conflict], all_infeasible: bool
) -> list[dict[str, Any]]:
    """열 수 있는 것 (서버가 계산한 사실, need 모양). 길은 모델이 엮는다 (AG-23).

    사실(FACT_CHANGE)뿐이고, 대상은 충돌에 걸린 작업 전체다: 풀 초과 충돌의 풀(QUANTITY), 충돌 작업의
    자원 제외 사유(권한 없음 → PERMISSION, 가용 없음 → AVAILABILITY), 모든 범위가 INFEASIBLE인 요청
    작업의 시간창(WINDOW), 접수 Agent가 정한 작업 시간(decided 표시: 요청자가 작업 카드에서 고치면 열림,
    AG-32). 정한 기준 위치는 Hard가 아니라 해를 막지 않으므로 넣지 않는다 (AG-35).
    """
    tasks = facts.task_map()
    pinned = facts.pinned_task_ids()
    out: list[dict[str, Any]] = []
    involved = sorted({tid for c in conflicts for tid in c.task_ids if tid in tasks})
    changes: list[dict[str, Any]] = []
    for c in conflicts:
        if c.pool is not None:
            changes.append({"kind": "FACT_CHANGE", "field": "QUANTITY", "pool_id": c.pool.pool_id})
    in_plan = {a.task_id for a in facts.plan.assignments}
    for tid in involved:
        t = tasks[tid]
        if t.required_resource_type and tid not in pinned:
            for r in assignable_resources(facts, t)["excluded"]:
                for e in r["reasons"]:
                    field = FACT_BY_EXCLUSION.get(e["reason"])
                    if field is not None:
                        changes.append(
                            {"kind": "FACT_CHANGE", "field": field, "resource_id": r["resource_id"]}
                        )
        if all_infeasible and tid not in in_plan:
            changes.append({"kind": "FACT_CHANGE", "field": "WINDOW", "task_id": tid})
    # 접수 Agent가 정한 값: 요청자가 작업 카드에서 고치면 열릴 수 있다. 재계획은 바꾸지 못한다 (CV-24)
    decided: list[dict[str, Any]] = []
    for tid in involved:
        names = set(tasks[tid].decided_values)
        for field, values in DECIDED_FACTS.items():
            if names & values:
                decided.append({"kind": "FACT_CHANGE", "field": field, "task_id": tid})
    for change in changes:
        if change not in out and change not in decided:
            out.append(change)
    return out + [{**d, "decided": True} for d in decided]


def condition_args(pack: LoadedPack, conditions: dict[str, Any]) -> list[dict[str, Any]]:
    """저장된 조건(분)을 조건 도구의 인자 모양(현장 날짜·시각 문자열)으로. 같은 조건을 다시 쓰지 않게
    이전 계산에 붙인다 (AG-21)."""

    def clock(minute: int) -> str:
        return site_time(pack.horizon_start_utc, pack.timezone, minute)

    out = []
    for tid, c in sorted(conditions.items()):
        arg: dict[str, Any] = {"task_id": tid}
        lo, hi = c.get("start_min"), c.get("start_max")
        if lo is not None and lo == hi:
            arg["start_at"] = clock(lo)
        else:
            if lo is not None:
                arg["start_from"] = clock(lo)
            if hi is not None:
                arg["start_until"] = clock(hi)
        if c.get("resource_id") is not None:
            arg["resource_id"] = c["resource_id"]
        out.append(arg)
    return out


def _live_candidates(
    conn: sqlite3.Connection, pack: LoadedPack, case_id: str
) -> list[dict[str, Any]]:
    out = []
    for cid in casefacts.case_candidate_ids(conn, pack, case_id):
        candidate = get_candidate(conn, pack.site_id, cid)
        assert candidate is not None
        state = candidate_state(conn, pack.site_id, candidate)
        if state.stale or state.rejected or state.committed:
            continue
        out.append(
            {
                "candidate_id": cid,
                "contested": contested_changes(conn, pack.site_id, candidate),
            }
        )
    return out


def build_observation(conn: sqlite3.Connection, pack: LoadedPack, run_id: str) -> Observation:
    run = get_run(conn, run_id)
    site = get_site(conn, pack.site_id)
    if run is None or site is None:
        raise LookupError(f"run {run_id} or site not found")
    snapshot = current_snapshot(conn, pack)
    facts = snapshot.facts()
    conflicts = detect_conflicts(snapshot, facts.check_assignments(), pack)
    # 미시도 판정은 실효 탐색 키(Solver 입력)로 한다. 무결성 hash가 아니다
    tried = tried_search_keys(conn, pack.site_id, run.case_id)
    keys = level_keys(snapshot, conflicts) if conflicts else {}
    untried = [lv for lv, k in keys.items() if k not in tried]

    base = facts.base_assignments()

    def clock(minute: int) -> str:
        return site_time(pack.horizon_start_utc, pack.timezone, minute)

    pins = {p.task_id: p for p in facts.pins}
    info = {t.task_id: facts.base_info(t.task_id) for t in facts.tasks}
    involved = {tid for c in conflicts for tid in c.task_ids}
    tasks = [
        {
            "task_id": t.task_id,
            "unit_id": t.unit_id,
            # 지금 충돌에 걸린 작업인가
            "in_conflict": t.task_id in involved,
            "zone_id": t.zone_id,
            "duration": t.duration,
            "window": {
                "earliest_start": t.earliest_start,
                "latest_start": t.latest_start,
                "latest_end": t.latest_end,
            },
            "required_resource_type": t.required_resource_type,
            "demands": t.demands,
            # 접수 Agent가 정한 값(요청자가 아직 고치지 않았다, AG-32)
            "decided_values": list(t.decided_values),
            # 사람이 건 고정(누가). 고정된 작업은 시각·자원 모두 움직이지 않는다 (AG-27)
            "pinned": None
            if t.task_id not in pins
            else {
                "pinned_by": pins[t.task_id].pinned_by,
                "by_role": pins[t.task_id].by_role,
            },
            # 기준 배정과 그 출처(계획 / 요청한 자리·범위 / 없음), 기준 시작 범위, 정함 여부. 변경과
            # 지연을 이 범위에서 잰다. 기준에서 바뀐 작업은 고른 안의 협의에서 담당자에게 간다 (CV-29)
            "base": {
                **base[t.task_id].model_dump(),
                "source": info[t.task_id]["source"],
                "start_range": None
                if info[t.task_id]["source"] == "NONE"
                else [info[t.task_id]["start"], info[t.task_id]["start_max"]],
                "decided": info[t.task_id]["origin"] == "DECIDED",
            },
            # 같은 값의 현장 날짜·시각. 조건 도구의 시각 인자가 이 형식이다 (AG-21)
            "clock": {
                "earliest_start": clock(t.earliest_start),
                "latest_start": clock(t.latest_start),
                "base_start": clock(base[t.task_id].start),
                "base_start_max": None
                if info[t.task_id]["source"] == "NONE"
                else clock(info[t.task_id]["start_max"]),
            },
        }
        for t in sorted(facts.tasks, key=lambda t: t.task_id)
    ]
    # 이전 계산과 마지막 검증은 Case 단위다: 메인이 다시 부른 Run도 앞 Run의 결과를 본다 (CV-13)
    attempts = list_attempts(conn, run_id)
    # 기존 후보와 같은 배치에 도달한 시도는 그 후보를 가리킨다 (CV-25)
    candidates = list(
        dict.fromkeys(
            cid for a in attempts if (cid := a["candidate_id"] or a["same_as_candidate_id"])
        )
    )
    latest_validation = None
    done_ready = False
    for cid in candidates:
        candidate = get_candidate(conn, pack.site_id, cid)
        assert candidate is not None
        state = candidate_state(conn, pack.site_id, candidate)
        live = not (state.stale or state.rejected or state.committed)
        validations = list_validations(conn, pack.site_id, cid)
        if validations:
            v = validations[-1]
            # 검증을 통과한 살아 있는 후보가 있으면 DONE으로 돌려줄 수 있다 (AG-25)
            done_ready = done_ready or (v.status == "PASS" and live)
            if cid == candidates[-1]:
                latest_validation = {
                    "candidate_id": cid,
                    "status": v.status,
                    # 무효가 되었거나(현장 정보 변경) 거절·확정된 후보는 live가 false다
                    "live": live,
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
    if last_guard is not None and last_guard["reason_code"] == "ALREADY_TRIED":
        # 이미 한 탐색: 그때의 결과를 함께 보인다(조건은 도구 인자 모양으로도)
        previous = dict(steps[-1]["tool_result"] or {})
        if previous.get("conditions"):
            previous["conditions_as_args"] = condition_args(pack, previous["conditions"])
        last_guard = {**last_guard, "previous": previous}
    # 유효한 자원 조회 결과 + 아직 시도하지 않은 대체 자원. resources_hash는 모델에 보이지 않는다.
    listings = [
        {k: v for k, v in r.items() if k != "resources_hash"}
        for _, r in sorted(valid_listings(steps, facts).items())
    ]
    hidden = {"done_ready": done_ready}
    # 모든 범위를 계산했고 전부 해가 없다 (요청 작업의 시간창이 바뀌어야 열린다)
    by_key = {a["search_key"]: a for a in attempts if not a["conditions"]}
    all_infeasible = bool(keys) and all(
        ((by_key.get(k) or {}).get("stage1") or {}).get("status") == "INFEASIBLE"
        for k in keys.values()
    )

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
        # 지금 충돌 전체. 이 Run은 이것을 한 번에 푼다 (AG-24)
        "conflicts": [c.model_dump(mode="json") for c in conflicts],
        # 메인이 이번 호출에 준 접근(무엇을 우선할지)과 짧은 문장(인용). 방식은 이 Agent가 고른다 (AG-28)
        "approach": {
            "approach": run.input_ref.get("approach"),
            "quoted_note": run.input_ref.get("approach_note"),
        },
        # 이 Case에서 접근별로 나온 후보(앞 Run 포함). same이면 기존 후보와 같은 배치에 도달한 것이다
        "approach_candidates": [
            {k: a[k] for k in ("no", "approach", "candidate_id", "same")}
            for a in approach_attempts(conn, case_id=run.case_id)
        ],
        # 작업 전체(Unit을 가리지 않는다). 고정되지 않은 작업은 범위에 들어가면 움직인다
        "tasks": tasks,
        "untried_levels": untried,
        # search_key는 내부 계산(시도 여부)에만 쓰고 모델에는 보이지 않는다
        "attempts": [
            {
                **{k: v for k, v in a.items() if k != "search_key"},
                # 건 조건을 도구 인자와 같은 모양으로(현장 날짜·시각 문자열)
                "conditions_as_args": condition_args(pack, a["conditions"]),
            }
            for a in attempts
        ],
        "latest_validation": latest_validation,
        # 이 Case 후보에 대한 Supervisor 거절. 사유 하나가 한 줄이다([모두 거절]은 한 줄에 안 여럿).
        # comment는 인용 데이터다 (CV-26)
        "rejections": case_rejection_reasons(conn, run.case_id),
        # 이 Case의 협의에서 담당자가 낸 이견 전부. quoted_comment는 인용 데이터다 (CV-26)
        "objections": case_objections(conn, pack.site_id, run.case_id),
        # 이 Case의 살아 있는 후보와 그 후보가 담은 거절·이견된 변경 (서버 계산)
        "live_candidates": _live_candidates(conn, pack, run.case_id),
        # 거절 사실(서버 계산): 거절 수, 마지막 거절, 미시도 범위가 남았는지
        "rejection_facts": {
            **casefacts.rejection_facts(conn, run.case_id),
            "untried_remaining": bool(untried),
        },
        "assignable_resources": listings,
        # 열 수 있는 것: 지금 계산으로는 열 수 없지만 충족되면 해가 열릴 수 있는 것 (서버 계산)
        "openers": openers(facts, conflicts, all_infeasible),
        "last_guard": last_guard,
        "recent_steps": recent,
        "budget_remaining": budget_remaining(run, spec.SPEC),
        "work_intervals": [list(iv) for iv in facts.work_intervals],  # 근무 달력
    }
    data["open_skills"] = spec.open_skills(data)
    return Observation(
        run=run,
        versions=(site.context_version, site.plan_revision, run.wake_seq),
        data=data,
        available=spec.available_actions(data, hidden),
        spec=spec.SPEC,
        hidden=hidden,
        conflicts=tuple(conflicts),
    )
