"""Replanning Action 실행기.

ToolGateway.execute 안에서만 불린다(도구 실행 경로는 하나). 공통 판정(MALFORMED·LLM 오류·
STALE_OBSERVATION·연속 2회·step 완료 기록)은 ToolGateway에 있고, 이 클래스는 그 도우미를 받아 쓴다.
SOLVE_WITH_SCOPE·SOLVE_WITH_CONDITIONS는 예약 tx → tx 밖 Solver → 등록 tx이고 목적 순서는 접근에서
서버가 채운다(CV-27),
나머지는 tx 하나다. 조건은 좁히기만 하고 서버는 유효성만 본다(CV-24). 해가 살아 있는 기존 후보와 같은
배치면 새 후보를 만들지 않는다(CV-25). 같은 사실에서 이미 한 탐색(같은 범위·조건·목적 순서)이면 Solver를
부르지 않고 그때의 결과를 돌려준다(CV-13).
사람에게 묻지 않는다. 막힌 결과에는 서버가 계산한 열 수 있는 것(openers)을 붙인다 (AG-23).
관찰 계산은 binding.observer로 쓴다(observers를 import하지 않는다).
승인·확정·Hold 해제·Proposal 확인·Validation 등록 함수는 없다.
"""

import sqlite3
from typing import Any

from app.agents.observe import Observation
from app.agents.specs import replanning as spec
from app.agents.tool_gateway import ACCEPTED, REJECTED, ToolGateway, _Parsed
from app.agents.types import GatewayResult, StepMeta
from app.domain.calendar import parse_site_time
from app.domain.canonical import canonical_hash
from app.domain.models import Assignment, Candidate, Condition, SearchSpec, Snapshot
from app.packs.loader import LoadedPack
from app.rules.engine import detect_conflicts
from app.solver import cpsat
from app.solver.candidate import build_candidate
from app.solver.search_spec import SearchSpecError, build_search_spec
from app.store import db
from app.store.repos.consultations import candidate_state
from app.store.repos.decisions import rejected_candidate_ids
from app.store.repos.records import (
    StaleError,
    get_candidate,
    insert_search_spec,
    register_solver_outcome,
)
from app.store.repos.runs import (
    charge,
    finish_solver_job,
    insert_solver_job,
    list_attempts,
    tried_search_keys,
)
from app.store.repos.snapshots import create_snapshot
from app.store.repos.tasks import list_current_tasks


class ReplanningExecutor:
    def __init__(self, gateway: ToolGateway):
        self.pack = gateway.pack
        self.observer = gateway.binding.observer
        # 공통 도우미 (ToolGateway)
        self._active = gateway._active
        self._reject = gateway._reject
        self._complete = gateway._complete
        self.begin_step = gateway.begin_step
        self.wait_or_continue = gateway.wait_or_continue
        self.return_result = gateway.return_result

    def run(self, run_id: str, step_no: int, meta: StepMeta, parsed: _Parsed) -> GatewayResult:
        if parsed.name in ("SOLVE_WITH_SCOPE", "SOLVE_WITH_CONDITIONS"):
            return self._solve(run_id, step_no, meta, parsed)
        if parsed.name == "LIST_ASSIGNABLE_RESOURCES":
            return self._single_tx(run_id, step_no, meta, parsed)
        return self._return(run_id, step_no, meta, parsed)

    def _permitted(self, obs: Observation, action: spec.Action) -> bool | str:
        """선택한 Action과 인자 조합이 최신 Available Actions 안에 있는가 (작업별 조합까지)."""
        available = obs.available
        if isinstance(action, spec.SolveWithScope):
            return action.level in available.get("SOLVE_WITH_SCOPE", {}).get("level", [])
        if isinstance(action, spec.SolveWithConditions):
            # 조건의 유효성은 예약 tx에서 본다(시각 형식, 시간창, 고정, 범위, 자원 적격성)
            return action.level in available.get("SOLVE_WITH_CONDITIONS", {}).get("level", [])
        if isinstance(action, spec.ListAssignableResources):
            c = spec.choices(obs.data, obs.hidden)
            return "LIST_ASSIGNABLE_RESOURCES" in available and action.task_id in c["LIST"]
        if isinstance(action, spec.ReturnResult):
            return action.status in available.get("RETURN_RESULT", {}).get("status", [])
        return False

    @staticmethod
    def _refusal(verdict: bool | str) -> str:
        return verdict if isinstance(verdict, str) else "ACTION_NOT_AVAILABLE"

    def _single_tx(
        self, run_id: str, step_no: int, meta: StepMeta, parsed: _Parsed
    ) -> GatewayResult:
        """LIST(CONTINUE). 재계산·효과·step 완료가 tx 하나다."""
        action = parsed.action
        assert action is not None
        with db.write() as tx:
            rejected, obs = self.begin_step(
                tx, run_id, step_no, meta, parsed, lambda o: self._permitted(o, action)
            )
            if rejected is not None:
                return rejected
            assert obs is not None
            tasks = {t.task_id: t for t in list_current_tasks(tx, self.pack.site_id, self.pack)}
            assert isinstance(action, spec.ListAssignableResources)
            facts = self.observer.current_snapshot(tx, self.pack).facts()
            result = self.observer.assignable_resources(facts, tasks[action.task_id])
            self._complete(
                tx,
                run_id,
                step_no,
                meta,
                parsed,
                verdict=ACCEPTED,
                reason=None,
                result_kind="CONTINUE",
                tool_result=result,
            )
            return GatewayResult("CONTINUE")

    def _return(self, run_id: str, step_no: int, meta: StepMeta, parsed: _Parsed) -> GatewayResult:
        action = parsed.action
        assert isinstance(action, spec.ReturnResult)
        with db.write() as tx:
            rejected, obs = self.begin_step(
                tx, run_id, step_no, meta, parsed, lambda o: self._permitted(o, action)
            )
            if rejected is not None:
                return rejected
            assert obs is not None
            # 서버가 채우는 내용: 이 Run이 등록한 후보와 마지막 검증
            produced = {
                "candidate_ids": list(
                    dict.fromkeys(
                        cid
                        for a in obs.data["attempts"]
                        if (cid := a["candidate_id"] or a["same_as_candidate_id"])
                    )
                ),
                "latest_validation": obs.data["latest_validation"],
                # 막힌 결과에 서버가 붙인다: 모델이 길을 비워도 메인이 볼 것이 남는다
                "openers": obs.data["openers"],
            }
            return self.return_result(tx, run_id, step_no, meta, parsed, produced)

    def _solve(self, run_id: str, step_no: int, meta: StepMeta, parsed: _Parsed) -> GatewayResult:
        """SOLVE_WITH_SCOPE(level), SOLVE_WITH_CONDITIONS(level + 작업별 조건)."""
        action = parsed.action
        asked: list[spec.TaskCondition] = []
        if isinstance(action, spec.SolveWithConditions):
            level, asked = action.level, action.conditions
        else:
            assert isinstance(action, spec.SolveWithScope)
            level = action.level
        site_id = self.pack.site_id
        # 범위·조건 계산은 허용 판정을 미룬다: 이미 한 탐색이면 허용 여부와 관계없이 그때의 결과를 돌려준다
        verdicts: list[bool | str] = []

        def permitted(o: Observation) -> bool | str:
            verdict = self._permitted(o, action)
            if verdict is True:
                verdict = bool(o.conflicts)
            verdicts.append(verdict)
            return bool(o.conflicts)

        # 1. 예약 tx: 관찰 버전 확인 → Available 재계산 → Snapshot·SearchSpec·SolverJob, Solver Budget
        with db.write() as tx:
            rejected, obs = self.begin_step(tx, run_id, step_no, meta, parsed, permitted)
            if rejected is not None:
                return rejected
            assert obs is not None
            verdict = verdicts[-1]
            conditions = _resolve_conditions(self.pack, obs, asked)
            if isinstance(conditions, str):
                return self._reject(tx, run_id, step_no, meta, parsed, conditions)
            if isinstance(action, spec.SolveWithConditions) and not conditions:
                # 조건 없이 푸는 것은 범위 계산(SOLVE_WITH_SCOPE)과 같다
                return self._reject(tx, run_id, step_no, meta, parsed, "CONDITION_INVALID")
            # 목적 순서는 메인이 준 접근에서 서버가 채운다. Agent는 고르지 않는다 (CV-27)
            objective = obs.data["approach"]["objective"]
            args = (obs.conflicts, level, conditions, objective)
            try:
                # 저장하지 않는 Snapshot으로 탐색 키부터 본다(이미 한 탐색이면 아무것도 남기지 않는다)
                probe = build_search_spec(self.observer.current_snapshot(tx, self.pack), *args)
            except SearchSpecError as e:
                reason = e.reason_code if verdict is True else self._refusal(verdict)
                return self._reject(tx, run_id, step_no, meta, parsed, reason)
            # 같은 사실에서 같은 범위·같은 조건·같은 목적 순서는 다시 풀지 않는다: Solver를 부르지 않고
            # Solver Budget도 쓰지 않으며, 그때의 결과를 돌려준다 (CV-13)
            if probe.search_key in tried_search_keys(tx, site_id, obs.run.case_id):
                previous = _previous_result(tx, run_id, probe.search_key)
                self._complete(
                    tx,
                    run_id,
                    step_no,
                    meta,
                    parsed,
                    verdict=REJECTED,
                    reason="ALREADY_TRIED",
                    result_kind="REJECTED",
                    tool_result=previous,
                )
                return GatewayResult("REJECTED", "ALREADY_TRIED")
            if verdict is not True:
                return self._reject(tx, run_id, step_no, meta, parsed, self._refusal(verdict))
            snapshot = create_snapshot(tx, site_id, self.pack)
            search_spec = build_search_spec(snapshot, *args)
            insert_search_spec(tx, site_id, search_spec)
            insert_solver_job(tx, site_id, run_id, step_no, search_spec.search_spec_id)
            charge(tx, run_id, solver_calls=1, solver_seconds=search_spec.time_limit_s)

        # 2. 트랜잭션 밖에서 계산
        result = cpsat.solve(snapshot, search_spec, self.pack)
        candidate = build_candidate(snapshot, search_spec, result)

        # 3. 등록 tx: Run·step·버전 재확인 → SolverResult·Candidate(+VALIDATE), step 완료
        with db.write() as tx:
            if not self._active(tx, run_id, step_no):
                finish_solver_job(tx, run_id, step_no, "ABORTED")
                return GatewayResult("INACTIVE")
            tool_result = _solver_summary(snapshot, level, search_spec.hash, result, candidate)
            if search_spec.objective != "CHANGE_FIRST":
                tool_result["objective"] = search_spec.objective
            if search_spec.conditions:
                tool_result["conditions"] = {
                    tid: c.model_dump() for tid, c in search_spec.conditions.items()
                }
                # 모두 지정한 배치인데 해가 없으면 그 배치가 어긴 규칙을 붙인다
                if candidate is None and result.stage1.get("status") == "INFEASIBLE":
                    violations = _placement_violations(snapshot, search_spec, self.pack)
                    if violations is not None:
                        tool_result["violations"] = violations
            duplicate = candidate is not None and _rejected_duplicate(
                tx, site_id, candidate.context_version, candidate.assignments
            )
            if duplicate:
                # 같은 Context에서 거절된 배정을 다시 제안하지 않는다(T33). 결과는 남기고 후보는 없다.
                candidate = None
                tool_result = {**tool_result, "candidate_id": None}
            # 살아 있는 기존 후보와 전체 배정이 같으면 새 후보를 만들지 않고 그 후보에 도달했다고 적는다
            same = None if candidate is None else _same_live_candidate(tx, site_id, candidate)
            if same is not None:
                candidate = None
                tool_result = {**tool_result, "candidate_id": None, "same_as_candidate_id": same}
            try:
                register_solver_outcome(tx, snapshot, result, candidate)
            except StaleError:
                finish_solver_job(tx, run_id, step_no, "STALE")
                self._complete(
                    tx,
                    run_id,
                    step_no,
                    meta,
                    parsed,
                    verdict=ACCEPTED,
                    reason="STALE_SNAPSHOT",
                    result_kind="CONTINUE",
                    tool_result={**tool_result, "candidate_id": None},
                )
                return GatewayResult("CONTINUE", "STALE_SNAPSHOT")
            finish_solver_job(tx, run_id, step_no, "REGISTERED", result.solver_result_id, same)
            if duplicate:
                self._complete(
                    tx,
                    run_id,
                    step_no,
                    meta,
                    parsed,
                    verdict=REJECTED,
                    reason="DUPLICATE_REJECTED",
                    result_kind="CONTINUE",
                    tool_result=tool_result,
                    state_changes={"solver_result_id": result.solver_result_id},
                )
                return GatewayResult("CONTINUE", "DUPLICATE_REJECTED")
            changes = {
                "snapshot_id": snapshot.snapshot_id,
                "search_spec_id": search_spec.search_spec_id,
                "solver_result_id": result.solver_result_id,
                "candidate_id": None if candidate is None else candidate.candidate_id,
            }
            if result.stage1.get("status") == "MODEL_INVALID":
                outcome = GatewayResult("DONE", "MODEL_INVALID", "ERROR", "MODEL_INVALID")
            elif candidate is not None:
                # 관찰 이후 새 변화(wake)가 왔으면 대기하지 않고 다시 관찰한다 (T36)
                outcome = self.wait_or_continue(
                    tx, run_id, step_no, "CANDIDATE_OUTCOME", candidate.candidate_id
                )
            elif same is not None:
                changes["same_as_candidate_id"] = same
                outcome = GatewayResult("CONTINUE", "SAME_AS_EXISTING")
            else:
                outcome = GatewayResult("CONTINUE")
            self._complete(
                tx,
                run_id,
                step_no,
                meta,
                parsed,
                verdict=ACCEPTED,
                reason=outcome.reason if outcome.kind == "CONTINUE" else None,
                result_kind=outcome.kind,
                tool_result=tool_result,
                state_changes=changes,
                end=outcome,
            )
            return outcome


def _previous_result(tx: sqlite3.Connection, run_id: str, key: str) -> dict[str, Any]:
    """이미 한 탐색의 결과: 그때의 Solver 상태, 변경 수·지연, 후보(또는 같은 배치였던 기존 후보),
    몇 번째 step·어느 Run이었는지. 같은 Case의 앞 Run이 한 계산도 찾는다."""
    found = [a for a in list_attempts(tx, run_id) if a["search_key"] == key]
    if not found:
        return {"already_tried": True}
    a = found[-1]
    return {
        "already_tried": True,
        "first": {k: a[k] for k in ("run_id", "step_no", "this_run")},
        **{
            k: a[k]
            for k in (
                "scope_level",
                "conditions",
                "objective",
                "job_status",
                "stage1",
                "stage2",
                "candidate_id",
                "same_as_candidate_id",
            )
        },
    }


def _resolve_conditions(
    pack: LoadedPack, obs: Observation, asked: list[spec.TaskCondition]
) -> dict[str, Condition] | str:
    """도구가 받은 조건을 분 단위 Condition으로 바꾼다. 바꿀 수 없으면 거절 사유 (AG-21).

    여기서는 모양만 본다: 작업마다 하나, 시작 지정은 범위와 같이 쓰지 않음, 시각 형식.
    시간창·고정·범위·자원 적격성은 SearchSpec이 본다.
    """
    tasks = {t["task_id"]: t for t in obs.data["tasks"]}
    out: dict[str, Condition] = {}
    for c in asked:
        task = tasks.get(c.task_id)
        if task is None:
            return "CONDITION_TASK_NOT_IN_SCOPE"
        ranged = c.start_from is not None or c.start_until is not None
        if c.task_id in out or (c.start_at is not None and ranged):
            return "CONDITION_INVALID"
        try:
            lo, hi, at = (
                None
                if text is None
                else parse_site_time(
                    text, pack.horizon_start_utc, pack.timezone, pack.horizon_minutes
                )
                for text in (c.start_from, c.start_until, c.start_at)
            )
        except ValueError:
            return "TIME_INVALID"
        if at is not None:
            lo = hi = at
        out[c.task_id] = Condition(start_min=lo, start_max=hi, resource_id=c.resource_id)
    return out


def _placement_violations(
    snapshot: Snapshot, search_spec: SearchSpec, pack: LoadedPack
) -> list[dict[str, Any]] | None:
    """범위 안에서 움직일 수 있는 작업을 모두 시작 지정했을 때 그 배치가 어긴 규칙. 아니면 None."""
    facts = snapshot.facts()
    base, tasks = facts.base_assignments(), facts.task_map()
    open_ids = [tid for tid, ax in search_spec.axes.items() if ax.time]
    conds = search_spec.conditions
    if not open_ids or any(
        tid not in conds
        or conds[tid].start_min is None
        or conds[tid].start_min != conds[tid].start_max
        for tid in open_ids
    ):
        return None
    placed = dict(base)
    for tid in open_ids:
        start = conds[tid].start_min
        assert start is not None
        placed[tid] = Assignment(
            task_id=tid,
            start=start,
            end=start + tasks[tid].duration,
            resource_id=conds[tid].resource_id or base[tid].resource_id,
        )
    found = detect_conflicts(snapshot, tuple(placed[tid] for tid in sorted(placed)), pack)
    return [{"rule_id": c.rule_id, "task_ids": list(c.task_ids)} for c in found]


def _same_live_candidate(tx: sqlite3.Connection, site_id: str, candidate: Candidate) -> str | None:
    """같은 사실 위의 살아 있는 재계획 후보 가운데 전체 배정이 같은 것 (CV-25). 없으면 None."""
    target = assignments_hash(candidate.assignments)
    ids = [
        r[0]
        for r in tx.execute(
            "SELECT candidate_id FROM candidate WHERE site_id = ? AND kind = 'REPLAN'"
            " AND context_version = ? AND base_plan_revision = ? ORDER BY rowid",
            (site_id, candidate.context_version, candidate.base_plan_revision),
        )
    ]
    for cid in ids:
        other = get_candidate(tx, site_id, cid)
        if other is None or assignments_hash(other.assignments) != target:
            continue
        state = candidate_state(tx, site_id, other)
        if not (state.stale or state.rejected or state.committed):
            return cid
    return None


def _solver_summary(
    snapshot: Snapshot, level: str, spec_hash: str, result: Any, candidate: Any
) -> dict[str, Any]:
    """Tool 결과(서버 값). 해 전체는 SolverResult에 있고 여기에는 요약만 둔다."""
    return {
        "scope_level": level,
        "spec_hash": spec_hash,
        "snapshot_id": snapshot.snapshot_id,
        # 지연 먼저로 푼 결과는 단계마다 변경 수와 지연을 다 가진다 (CV-27)
        "stage1": {
            k: result.stage1.get(k) for k in ("status", "changed", "delay") if k in result.stage1
        },
        "stage2": None
        if result.stage2 is None
        else {
            k: result.stage2.get(k)
            for k in ("status", "delay", "changed", "resource_changed")
            if k in result.stage2
        },
        "chosen_stage": result.chosen_stage,
        "minimal_change": result.minimal_change,
        "delay_optimality_unconfirmed": result.delay_optimality_unconfirmed,
        # 이 해가 기준에서 바꾸는 것: 기존 작업 변경 수, 추가 작업 변경 수, 옮긴 거리 (서버 계산)
        "change_counts": None
        if candidate is None
        else snapshot.facts().change_counts(candidate.assignments),
        "candidate_id": None if candidate is None else candidate.candidate_id,
    }


def assignments_hash(assignments: Any) -> str:
    """task_id순 배정의 canonical hash."""
    return canonical_hash(
        [a.model_dump(mode="json") for a in sorted(assignments, key=lambda a: a.task_id)]
    )


def _rejected_duplicate(
    tx: sqlite3.Connection, site_id: str, context_version: int, assignments: Any
) -> bool:
    """같은 Context에서 거절된 후보와 배정이 같으면 True."""
    target = assignments_hash(assignments)
    for cid in rejected_candidate_ids(tx, site_id, context_version):
        cand = get_candidate(tx, site_id, cid)
        if cand is not None and assignments_hash(cand.assignments) == target:
            return True
    return False
