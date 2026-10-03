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
from app.domain.canonical import canonical_hash
from app.domain.eligibility import exclusion_reasons
from app.domain.groups import conflict_groups
from app.domain.models import AgentRun, Conflict, Snapshot, SnapshotContent, Task
from app.packs.loader import LoadedPack
from app.rules.engine import detect_conflicts
from app.solver.search_spec import SearchSpecError, build_search_spec
from app.store.repos.consultations import candidate_state
from app.store.repos.decisions import list_case_rejections
from app.store.repos.messages import list_case_replies
from app.store.repos.records import get_candidate, list_validations
from app.store.repos.runs import get_run, list_attempts, list_steps, tried_search_keys
from app.store.repos.site import get_site
from app.store.repos.snapshots import build_snapshot_content

RECENT_STEPS = 5


@dataclass(frozen=True)
class Observation(common.Observation):
    """Replanning 관찰. 공통 Observation에 이 Run이 맡은 주 충돌을 더한다."""

    primary: Conflict | None = None


def primary_conflict(
    facts: SnapshotContent, conflicts: list[Conflict], run: AgentRun
) -> Conflict | None:
    """input_ref.conflict와 같은 충돌, 없으면 acting_unit 작업을 포함한 첫 충돌."""
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


def level_keys(snapshot: Snapshot, primary: Conflict, acting_unit_id: str) -> dict[str, str]:
    """level별 실효 탐색 키. 만들 수 없는 level(NO_ACTING_TASKS 등)은 뺀다."""
    out = {}
    for level in spec.LEVELS:
        try:
            out[level] = build_search_spec(snapshot, primary, acting_unit_id, level).search_key
        except SearchSpecError:
            continue
    return out


def resources_hash(facts: SnapshotContent) -> str:
    """자원 사실의 hash. 자원 조회 결과는 이 값이 같은 동안 유효하다."""
    return canonical_hash([r.model_dump(mode="json") for r in facts.resources])


def assignable_resources(facts: SnapshotContent, task: Task, acting_unit_id: str) -> dict[str, Any]:
    """LIST_ASSIGNABLE_RESOURCES 결과. TRY 필터·실행 검사와 같은 적격성 함수로 판정한다 (CV-20).

    유형이 다른 자원은 대상이 아니므로 목록에 넣지 않는다(excluded는 같은 유형만).
    excluded의 reasons는 제외 사유 전부다. 요구 조건 사유에는 어느 속성인지(attribute)가 붙는다.
    """
    current = facts.base_assignments()[task.task_id].resource_id
    assignable, excluded = [], []
    for r in sorted(facts.resources, key=lambda r: r.resource_id):
        reasons = exclusion_reasons(task, r, acting_unit_id)
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


def try_search_key(
    snapshot: Snapshot, primary: Conflict, acting_unit_id: str, task_id: str, resource_id: str
) -> str | None:
    """TRY의 실효 탐색 키(주 충돌 L0 + 대체 자원 1개). 만들 수 없으면 None."""
    try:
        spec_ = build_search_spec(snapshot, primary, acting_unit_id, "L0", {task_id: [resource_id]})
    except SearchSpecError:
        return None
    return spec_.search_key


def build_observation(conn: sqlite3.Connection, pack: LoadedPack, run_id: str) -> Observation:
    run = get_run(conn, run_id)
    site = get_site(conn, pack.site_id)
    if run is None or site is None:
        raise LookupError(f"run {run_id} or site not found")
    snapshot = current_snapshot(conn, pack)
    facts = snapshot.facts()
    conflicts = detect_conflicts(snapshot, facts.check_assignments(), pack)
    primary = primary_conflict(facts, conflicts, run)
    # 미시도 판정은 실효 탐색 키(Solver 입력)로 한다. 무결성 hash가 아니다
    tried = tried_search_keys(conn, pack.site_id, run.case_id)
    keys = level_keys(snapshot, primary, run.acting_unit_id) if primary else {}
    untried = [lv for lv, k in keys.items() if k not in tried]

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
            "required_resource_type": t.required_resource_type,
            "demands": t.demands,
            "movable": t.movable.model_dump(),
            "base": base[t.task_id].model_dump(),
        }
        for t in sorted(facts.tasks, key=lambda t: t.task_id)
        if t.unit_id == run.acting_unit_id
    ]
    acting_ids = {t["task_id"] for t in acting_tasks}
    # 이전 계산과 마지막 검증은 Case 단위다: 메인이 다시 부른 Run도 앞 Run의 결과를 본다 (CV-13)
    attempts = list_attempts(conn, run_id)
    candidates = [a["candidate_id"] for a in attempts if a["candidate_id"]]
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
    groups = conflict_groups(conflicts, facts.task_map())
    group = next((g for g in groups if primary is not None and primary in g.conflicts), None)
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
    # 유효한 자원 조회 결과 + 아직 시도하지 않은 대체 자원. resources_hash는 모델에 보이지 않는다.
    listings = []
    for tid, r in sorted(valid_listings(steps, facts).items()):
        alternatives = [
            a["resource_id"] for a in r["assignable"] if a["resource_id"] != r["current"]
        ]
        untried_alt = [
            rid
            for rid in alternatives
            if primary is not None
            and (h := try_search_key(snapshot, primary, run.acting_unit_id, tid, rid)) is not None
            and h not in tried
        ]
        listings.append(
            {
                **{k: v for k, v in r.items() if k != "resources_hash"},
                "untried_alternatives": untried_alt,
            }
        )

    # 자원 적격성(유형·사용 권한·가용 구간·구역·요구 조건). 조회했는지와 무관하게 서버가 계산하고 모델에는 보이지 않는다 (CV-15)
    eligible = {}
    for t in facts.tasks:
        if t.unit_id != run.acting_unit_id or not t.required_resource_type:
            continue
        r = assignable_resources(facts, t, run.acting_unit_id)
        alternatives = [
            a["resource_id"] for a in r["assignable"] if a["resource_id"] != r["current"]
        ]
        eligible[t.task_id] = {
            "alternatives": alternatives,
            "untried": [
                rid
                for rid in alternatives
                if primary is not None
                and (h := try_search_key(snapshot, primary, run.acting_unit_id, t.task_id, rid))
                is not None
                and h not in tried
            ],
        }
    hidden = {"eligible": eligible, "done_ready": done_ready}

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
        # 맡은 충돌이 속한 충돌 그룹(공유 작업으로 묶은 것)과 그 그룹에 작업을 가진 Unit
        "group": None
        if group is None
        else {
            "group_id": group.group_id,
            "task_ids": list(group.task_ids),
            "unit_ids": sorted(group.units),
        },
        "acting_tasks": acting_tasks,
        "constraints": [c.model_dump(mode="json") for c in facts.constraints],
        "consents": [c.model_dump(mode="json") for c in facts.consents if c.task_id in acting_ids],
        "untried_levels": untried,
        # search_key는 내부 계산(시도 여부)에만 쓰고 모델에는 보이지 않는다
        "attempts": [{k: v for k, v in a.items() if k != "search_key"} for a in attempts],
        "latest_validation": latest_validation,
        # 이 Case 후보에 대한 Supervisor 거절. comment는 인용 데이터다
        "rejections": list_case_rejections(conn, run.case_id),
        # 거절 사실(서버 계산): 제약 있는·없는 거절 수, 마지막 거절, 미시도 범위가 남았는지
        "rejection_facts": {
            **casefacts.rejection_facts(conn, run.case_id),
            "untried_remaining": bool(untried),
        },
        "assignable_resources": listings,
        # 이 Case가 담당자에게 보낸 질문과 답. comment는 인용 데이터다
        "human_replies": list_case_replies(conn, run.case_id),
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
        primary=primary,
    )
