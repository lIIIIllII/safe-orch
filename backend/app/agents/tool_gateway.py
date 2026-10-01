"""Tool Gateway (설계서 §11.1·§11.2·§11.4, 부록 A.16). Agent의 유일한 도구 실행 경로.

tool_call 개수·이름·스키마를 검사하고(MALFORMED), 실행 직전 Available Actions를 최신 DB로 다시
계산한다. SOLVE_WITH_SCOPE·TRY_ALTERNATIVE_RESOURCE는 예약 tx → tx 밖 Solver → 등록 tx, 나머지는
tx 하나다. ASK_TASK_OWNER는 MOVABILITY 제안과 질문 메시지를 만들고 대기한다(A.21 5). step 완료,
Budget, CommandResult(키 run_id:step_no)를 도구 효과와 같은 tx에 쓴다.
승인·확정·Hold 해제·Proposal 확인·Validation 등록 함수는 없다(I-01).
"""

import sqlite3
from typing import Any

from langchain_core.messages import AIMessage
from pydantic import ValidationError

from app.agents.observe import (
    Observation,
    assignable_resources,
    budget_remaining,
    build_observation,
    current_snapshot,
)
from app.agents.specs import replanning as spec
from app.agents.types import GatewayResult, StepMeta
from app.domain.canonical import canonical_hash
from app.domain.ids import new_id
from app.domain.models import Snapshot, Task
from app.packs.loader import LoadedPack
from app.solver import cpsat
from app.solver.candidate import build_candidate
from app.solver.search_spec import SearchSpecError, build_search_spec
from app.store import db
from app.store.repos.commands import insert_command_result
from app.store.repos.decisions import rejected_candidate_ids
from app.store.repos.messages import insert_message, insert_proposal
from app.store.repos.records import (
    StaleError,
    get_candidate,
    insert_search_spec,
    register_solver_outcome,
)
from app.store.repos.runs import (
    abort_step,
    charge,
    complete_step,
    enter_wait,
    finish_solver_job,
    get_run,
    get_step,
    insert_solver_job,
    list_steps,
)
from app.store.repos.site import get_site
from app.store.repos.snapshots import create_snapshot
from app.store.repos.tasks import list_current_tasks

ACCEPTED = "ACCEPTED"
REJECTED = "REJECTED"
# 연속 2회면 이관하는 실패: 형식 오류와 전송 실패 (§11.2, Test Case API 오류, A.17)
RETRY_ONCE = {"MALFORMED", "LLM_ERROR"}


class _Parsed:
    def __init__(
        self, name: str | None, action: spec.Action | None, raw: dict[str, Any], error: str | None
    ):
        self.name = name
        self.action = action
        self.raw = raw
        self.error = error

    @property
    def summary(self) -> str | None:
        if self.action is None:
            return None
        return self.action.decision_summary[: spec.SUMMARY_MAX]

    @property
    def record(self) -> dict[str, Any]:
        if self.action is None:
            return {"name": self.name, "raw": self.raw}
        return {"name": self.name, "args": self.action.model_dump(exclude={"decision_summary"})}


def _parse(message: AIMessage) -> _Parsed:
    """tool_call 1개, 아는 이름, 스키마 일치가 아니면 MALFORMED (§11.2)."""
    calls = list(message.tool_calls)
    raw = {"tool_calls": calls, "invalid_tool_calls": list(message.invalid_tool_calls)}
    if message.invalid_tool_calls or len(calls) != 1:
        return _Parsed(None, None, raw, f"tool_calls={len(calls)}")
    name = calls[0]["name"]
    model = spec.ACTIONS.get(name)
    if model is None:
        return _Parsed(name, None, raw, "unknown action")
    try:
        return _Parsed(name, model.model_validate(calls[0]["args"]), raw, None)
    except ValidationError as e:
        return _Parsed(name, None, raw, f"schema: {e.error_count()} errors")


class ToolGateway:
    def __init__(self, pack: LoadedPack):
        self.pack = pack

    # ── 공통 ────────────────────────────────────────────────

    def _active(self, tx: sqlite3.Connection, run_id: str, step_no: int) -> bool:
        run = get_run(tx, run_id)
        step = get_step(tx, run_id, step_no)
        if run is None or step is None or step["status"] != "RESERVED":
            return False
        if run.status != "RUNNING":
            abort_step(tx, run_id, step_no, "RUN_INACTIVE")
            return False
        return True

    def _complete(
        self,
        tx: sqlite3.Connection,
        run_id: str,
        step_no: int,
        meta: StepMeta,
        parsed: _Parsed,
        *,
        verdict: str,
        reason: str | None,
        result_kind: str,
        tool_result: dict[str, Any] | None = None,
        state_changes: dict[str, Any] | None = None,
    ) -> None:
        run = get_run(tx, run_id)
        assert run is not None
        guard = {"verdict": verdict, "reason_code": reason}
        changes = state_changes or {}
        complete_step(
            tx,
            run_id,
            step_no,
            action=parsed.record,
            decision_summary=parsed.summary,
            tool_result=tool_result,
            guard=guard,
            state_changes=changes,
            result_kind=result_kind,
            budget_remaining=budget_remaining(run),
            model_id=meta.model_id,
            prompt_version=meta.prompt_version,
            llm_attempts=meta.llm_attempts,
        )
        insert_command_result(
            tx,
            self.pack.site_id,
            f"{run_id}:{step_no}",
            f"AGENT:{parsed.name or reason or 'MALFORMED'}",
            f"run:{run_id}",
            canonical_hash({"action": parsed.record}),
            "APPLIED" if verdict == ACCEPTED else "REJECTED",
            [] if reason is None or verdict == ACCEPTED else [reason],
            changes,
            {"result_kind": result_kind, "guard": guard},
        )

    def _reject(
        self,
        tx: sqlite3.Connection,
        run_id: str,
        step_no: int,
        meta: StepMeta,
        parsed: _Parsed,
        reason: str,
    ) -> GatewayResult:
        """REJECTED 결과. MALFORMED·LLM_ERROR가 연속 2회면 이관(DONE → ESCALATED <사유>_TWICE)."""
        if reason in RETRY_ONCE:
            done = [s for s in list_steps(tx, run_id) if s["status"] == "COMPLETED"]
            if done and (done[-1]["guard"] or {}).get("reason_code") in RETRY_ONCE:
                self._complete(
                    tx,
                    run_id,
                    step_no,
                    meta,
                    parsed,
                    verdict=REJECTED,
                    reason=reason,
                    result_kind="DONE",
                    tool_result={"error": parsed.error},
                )
                return GatewayResult("DONE", reason, "ESCALATED", f"{reason}_TWICE")
        self._complete(
            tx,
            run_id,
            step_no,
            meta,
            parsed,
            verdict=REJECTED,
            reason=reason,
            result_kind="REJECTED",
            tool_result={"error": parsed.error} if parsed.error else None,
        )
        return GatewayResult("REJECTED", reason)

    def _stale_observation(self, tx: sqlite3.Connection, run_id: str, step_no: int) -> bool:
        """site의 (context, plan)이 step의 관찰 버전과 다르면 True. 모든 Action에 같은 규칙 (A.16)."""
        step = get_step(tx, run_id, step_no)
        site = get_site(tx, self.pack.site_id)
        assert step is not None and site is not None
        return (site.context_version, site.plan_revision) != (
            step["observed_context_version"],
            step["observed_plan_revision"],
        )

    # ── 실행 ────────────────────────────────────────────────

    def execute(
        self, run_id: str, step_no: int, message: AIMessage | None, meta: StepMeta
    ) -> GatewayResult:
        if message is None:
            return self._llm_failure(run_id, step_no, meta)
        parsed = _parse(message)
        if parsed.action is None or parsed.name is None:
            with db.write() as tx:
                if not self._active(tx, run_id, step_no):
                    return GatewayResult("INACTIVE")
                charge(tx, run_id, llm_attempts=meta.llm_attempts - 1)
                return self._reject(tx, run_id, step_no, meta, parsed, "MALFORMED")
        if parsed.name in ("SOLVE_WITH_SCOPE", "TRY_ALTERNATIVE_RESOURCE"):
            return self._solve(run_id, step_no, meta, parsed)
        if parsed.name in ("LIST_ASSIGNABLE_RESOURCES", "ASK_TASK_OWNER"):
            return self._single_tx(run_id, step_no, meta, parsed)
        return self._escalate(run_id, step_no, meta, parsed)

    def _permitted(self, obs: Observation, action: spec.Action) -> bool:
        """선택한 Action과 인자 조합이 최신 Available Actions 안에 있는가 (작업별 조합까지)."""
        available = obs.available
        if isinstance(action, spec.SolveWithScope):
            return action.level in available.get("SOLVE_WITH_SCOPE", {}).get("level", [])
        c = spec.choices(obs.data)
        if isinstance(action, spec.ListAssignableResources):
            return "LIST_ASSIGNABLE_RESOURCES" in available and action.task_id in c["LIST"]
        if isinstance(action, spec.TryAlternativeResource):
            tries = c["TRY"].get(action.task_id, [])
            return "TRY_ALTERNATIVE_RESOURCE" in available and action.resource_id in tries
        if isinstance(action, spec.AskTaskOwner):
            asks = set(c["ASK"].get(action.task_id, []))
            return "ASK_TASK_OWNER" in available and set(action.allowed_values) <= asks
        return False

    def _single_tx(
        self, run_id: str, step_no: int, meta: StepMeta, parsed: _Parsed
    ) -> GatewayResult:
        """LIST(CONTINUE)와 ASK(WAIT). 재계산·효과·step 완료가 tx 하나다."""
        with db.write() as tx:
            if not self._active(tx, run_id, step_no):
                return GatewayResult("INACTIVE")
            charge(tx, run_id, llm_attempts=meta.llm_attempts - 1)
            if self._stale_observation(tx, run_id, step_no):
                return self._reject(tx, run_id, step_no, meta, parsed, "STALE_OBSERVATION")
            obs = build_observation(tx, self.pack, run_id)
            action = parsed.action
            assert action is not None
            if not self._permitted(obs, action):
                return self._reject(tx, run_id, step_no, meta, parsed, "ACTION_NOT_AVAILABLE")
            tasks = {t.task_id: t for t in list_current_tasks(tx, self.pack.site_id, self.pack)}
            if isinstance(action, spec.AskTaskOwner):
                return self._ask(tx, run_id, step_no, meta, parsed, action, tasks[action.task_id])
            assert isinstance(action, spec.ListAssignableResources)
            facts = current_snapshot(tx, self.pack).facts()
            result = assignable_resources(facts, tasks[action.task_id], obs.run.acting_unit_id)
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

    def _ask(
        self,
        tx: sqlite3.Connection,
        run_id: str,
        step_no: int,
        meta: StepMeta,
        parsed: _Parsed,
        action: spec.AskTaskOwner,
        task: Task,
    ) -> GatewayResult:
        """MOVABILITY 제안 → 질문 메시지(수신자 = 작업 담당자) → 사람 라운드 차감 → 대기 (A.21 5).

        동의 효과는 구조화 값(axis·allowed_values)으로만 정해진다. 모델의 question은 agent_text로만 둔다.
        """
        site_id = self.pack.site_id
        site = get_site(tx, site_id)
        assert site is not None
        values = list(dict.fromkeys(action.allowed_values))
        proposal_id, message_id = new_id("prop"), new_id("msg")
        insert_proposal(
            tx,
            site_id,
            proposal_id,
            type_="MOVABILITY",
            run_id=run_id,
            step_no=step_no,
            target_task_id=task.task_id,
            base_task_revision=task.revision,
            context_version=site.context_version,
            payload={"axis": action.axis, "allowed_values": values},
            confirmer_actor_id=task.owner_actor_id,
        )
        body = movability_text(self.pack, task, values)
        insert_message(
            tx,
            site_id,
            message_id,
            run_id=run_id,
            step_no=step_no,
            to_actor_id=task.owner_actor_id,
            type_="QUESTION",
            proposal_id=proposal_id,
            body=body,
            agent_text=action.question,
            context_version=site.context_version,
        )
        charge(tx, run_id, human_rounds=1)
        step = get_step(tx, run_id, step_no)
        assert step is not None
        if enter_wait(tx, run_id, "MESSAGE", message_id, step["observed_wake_seq"]):
            outcome = GatewayResult("WAIT")
        else:
            # 관찰 이후 새 변화(wake)가 왔다: 질문은 열어 둔 채 다시 관찰한다 (I-19)
            outcome = GatewayResult("CONTINUE", "NEW_CHANGE_BEFORE_WAIT")
        self._complete(
            tx,
            run_id,
            step_no,
            meta,
            parsed,
            verdict=ACCEPTED,
            reason=outcome.reason,
            result_kind=outcome.kind,
            tool_result={
                "proposal_id": proposal_id,
                "message_id": message_id,
                "to_actor_id": task.owner_actor_id,
                "body": body,
            },
            state_changes={"proposal_id": proposal_id, "message_id": message_id},
        )
        return outcome

    def _llm_failure(self, run_id: str, step_no: int, meta: StepMeta) -> GatewayResult:
        """모델 응답 없음. LLM_ERROR(전송 2회 실패)는 다시 관찰, LLM_CONFIG는 Run ERROR (A.17)."""
        parsed = _Parsed(None, None, {"error": meta.error}, meta.error)
        reason = meta.error_kind or "LLM_ERROR"
        with db.write() as tx:
            if not self._active(tx, run_id, step_no):
                return GatewayResult("INACTIVE")
            charge(tx, run_id, llm_attempts=meta.llm_attempts - 1)
            if reason == "LLM_CONFIG":
                self._complete(
                    tx,
                    run_id,
                    step_no,
                    meta,
                    parsed,
                    verdict=REJECTED,
                    reason=reason,
                    result_kind="DONE",
                    tool_result={"error": meta.error},
                )
                return GatewayResult("DONE", reason, "ERROR", f"LLM_CONFIG: {meta.error}")
            return self._reject(tx, run_id, step_no, meta, parsed, reason)

    def _escalate(
        self, run_id: str, step_no: int, meta: StepMeta, parsed: _Parsed
    ) -> GatewayResult:
        with db.write() as tx:
            if not self._active(tx, run_id, step_no):
                return GatewayResult("INACTIVE")
            charge(tx, run_id, llm_attempts=meta.llm_attempts - 1)
            if self._stale_observation(tx, run_id, step_no):
                return self._reject(tx, run_id, step_no, meta, parsed, "STALE_OBSERVATION")
            assert isinstance(parsed.action, spec.EscalateNoSolution)
            self._complete(
                tx,
                run_id,
                step_no,
                meta,
                parsed,
                verdict=ACCEPTED,
                reason=None,
                result_kind="DONE",
                tool_result={"reason": parsed.action.reason},
            )
            return GatewayResult("DONE", None, "ESCALATED", "ESCALATE_NO_SOLUTION")

    def _solve(self, run_id: str, step_no: int, meta: StepMeta, parsed: _Parsed) -> GatewayResult:
        """SOLVE_WITH_SCOPE(level)와 TRY_ALTERNATIVE_RESOURCE(주 충돌 L0 + 대체 자원 1개, A.21 5)."""
        action = parsed.action
        try_resources: dict[str, list[str]] | None = None
        if isinstance(action, spec.TryAlternativeResource):
            level, try_resources = "L0", {action.task_id: [action.resource_id]}
        else:
            assert isinstance(action, spec.SolveWithScope)
            level = action.level
        site_id = self.pack.site_id
        # 1. 예약 tx: 관찰 버전 확인 → Available 재계산 → Snapshot·SearchSpec·SolverJob, Solver Budget
        with db.write() as tx:
            if not self._active(tx, run_id, step_no):
                return GatewayResult("INACTIVE")
            charge(tx, run_id, llm_attempts=meta.llm_attempts - 1)
            if self._stale_observation(tx, run_id, step_no):
                return self._reject(tx, run_id, step_no, meta, parsed, "STALE_OBSERVATION")
            obs = build_observation(tx, self.pack, run_id)
            if not self._permitted(obs, action) or obs.primary is None:
                return self._reject(tx, run_id, step_no, meta, parsed, "ACTION_NOT_AVAILABLE")
            snapshot = create_snapshot(tx, site_id, self.pack)
            try:
                search_spec = build_search_spec(
                    snapshot, obs.primary, obs.run.acting_unit_id, level, try_resources
                )
            except SearchSpecError as e:
                return self._reject(tx, run_id, step_no, meta, parsed, e.reason_code)
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
            if try_resources:
                tool_result["try_resources"] = try_resources
            duplicate = candidate is not None and _rejected_duplicate(
                tx, site_id, candidate.context_version, candidate.assignments
            )
            if duplicate:
                # 같은 Context에서 거절된 배정을 다시 제안하지 않는다(§9.2, T33). 결과는 남기고 후보는 없다.
                candidate = None
                tool_result = {**tool_result, "candidate_id": None}
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
            finish_solver_job(tx, run_id, step_no, "REGISTERED", result.solver_result_id)
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
            step = get_step(tx, run_id, step_no)
            assert step is not None
            if result.stage1.get("status") == "MODEL_INVALID":
                outcome = GatewayResult("DONE", "MODEL_INVALID", "ERROR", "MODEL_INVALID")
            elif candidate is not None and enter_wait(
                tx, run_id, "CANDIDATE_OUTCOME", candidate.candidate_id, step["observed_wake_seq"]
            ):
                outcome = GatewayResult("WAIT")
            elif candidate is not None:
                # 관찰 이후 새 변화(wake)가 왔다: 대기하지 않고 다시 관찰한다 (§11.3(1)·I-19, T36)
                outcome = GatewayResult("CONTINUE", "NEW_CHANGE_BEFORE_WAIT")
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
            )
            return outcome


def _solver_summary(
    snapshot: Snapshot, level: str, spec_hash: str, result: Any, candidate: Any
) -> dict[str, Any]:
    """Tool 결과(서버 값). 해 전체는 SolverResult에 있고 여기에는 요약만 둔다."""
    return {
        "scope_level": level,
        "spec_hash": spec_hash,
        "snapshot_id": snapshot.snapshot_id,
        "stage1": {k: result.stage1.get(k) for k in ("status", "changed")},
        "stage2": None
        if result.stage2 is None
        else {k: result.stage2.get(k) for k in ("status", "delay")},
        "chosen_stage": result.chosen_stage,
        "minimal_change": result.minimal_change,
        "delay_optimality_unconfirmed": result.delay_optimality_unconfirmed,
        "candidate_id": None if candidate is None else candidate.candidate_id,
    }


def movability_text(pack: LoadedPack, task: Task, values: list[str]) -> str:
    """질문의 서버 문구(동의 내용의 기준, A.21 5). Pack 표시 이름으로 서버가 만든다."""
    name = pack.work_types[task.work_type].display_name
    return (
        f"{task.task_id}({name}) 작업에 {', '.join(values)}도 쓸 수 있게 허용하시겠습니까? "
        f"현재 요청 자원 {task.requested_resource_id}. "
        "허용하면 재계획이 이 자원을 대안으로 검토합니다."
    )


def assignments_hash(assignments: Any) -> str:
    """task_id순 배정의 canonical hash (§9.2 assignments_hash)."""
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
