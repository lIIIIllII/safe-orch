"""Tool Gateway. Agent의 유일한 도구 실행 경로.

agent_type 공통 판정을 한다: tool_call 개수·이름·스키마(MALFORMED), LLM 오류(LLM_ERROR·LLM_CONFIG),
MALFORMED·LLM_ERROR 연속 2회 이관, step 완료·Budget·CommandResult(키 run_id:step_no) 기록.
Action 효과는 runtime이 넘긴 binding의 실행기(binding.executor)가 맡고, 실행기는 execute 안에서만
부른다. 실행기는 STALE_OBSERVATION·ACTION_NOT_AVAILABLE 판정에 이 클래스의 도우미를 쓴다.
registry·observers·executors를 import하지 않는다.
승인·확정·Hold 해제·Proposal 확인·Validation 등록 함수는 없다.
"""

import sqlite3
from collections.abc import Callable
from typing import Any

from langchain_core.messages import AIMessage
from pydantic import ValidationError

from app.agents import skills
from app.agents.observe import Observation, budget_remaining
from app.agents.types import AgentBinding, AgentSpec, GatewayResult, StepMeta
from app.domain.canonical import canonical_hash
from app.packs.loader import LoadedPack
from app.store import db
from app.store.repos.commands import insert_command_result
from app.store.repos.runs import (
    abort_step,
    charge,
    complete_step,
    enter_wait,
    get_run,
    get_step,
    list_steps,
)
from app.store.repos.site import get_site

ACCEPTED = "ACCEPTED"
REJECTED = "REJECTED"
# 연속 2회면 이관하는 실패: 형식 오류와 전송 실패 (Test Case API 오류)
RETRY_ONCE = {"MALFORMED", "LLM_ERROR"}


class _Parsed:
    def __init__(
        self,
        name: str | None,
        action: Any | None,
        raw: dict[str, Any],
        error: str | None,
        summary_max: int = 0,
    ):
        self.name = name
        self.action = action
        self.raw = raw
        self.error = error
        self.summary_max = summary_max

    @property
    def summary(self) -> str | None:
        if self.action is None:
            return None
        return self.action.decision_summary[: self.summary_max]

    @property
    def record(self) -> dict[str, Any]:
        if self.action is None:
            return {"name": self.name, "raw": self.raw}
        return {"name": self.name, "args": self.action.model_dump(exclude={"decision_summary"})}


def _parse(message: AIMessage, spec: AgentSpec) -> _Parsed:
    """tool_call 1개, 아는 이름, 스키마 일치가 아니면 MALFORMED."""
    calls = list(message.tool_calls)
    raw = {"tool_calls": calls, "invalid_tool_calls": list(message.invalid_tool_calls)}
    if message.invalid_tool_calls or len(calls) != 1:
        return _Parsed(None, None, raw, f"tool_calls={len(calls)}")
    name = calls[0]["name"]
    model = spec.actions.get(name)
    if model is None:
        return _Parsed(name, None, raw, "unknown action")
    try:
        action = model.model_validate(calls[0]["args"])
        return _Parsed(name, action, raw, None, spec.summary_max)
    except ValidationError as e:
        return _Parsed(name, None, raw, f"schema: {e.error_count()} errors")


class ToolGateway:
    def __init__(self, pack: LoadedPack, binding: AgentBinding):
        self.pack = pack
        self.binding = binding
        self.executor = binding.executor(self)

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
            budget_remaining=budget_remaining(run, self.binding.spec),
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
        """site의 (context, plan)이 step의 관찰 버전과 다르면 True. 모든 Action에 같은 규칙."""
        step = get_step(tx, run_id, step_no)
        site = get_site(tx, self.pack.site_id)
        assert step is not None and site is not None
        return (site.context_version, site.plan_revision) != (
            step["observed_context_version"],
            step["observed_plan_revision"],
        )

    # ── 실행기 공통 틀 ────────────────────────────

    def begin_step(
        self,
        tx: sqlite3.Connection,
        run_id: str,
        step_no: int,
        meta: StepMeta,
        parsed: _Parsed,
        permitted: Callable[[Observation], bool | str] | None = None,
    ) -> tuple[GatewayResult | None, Observation | None]:
        """Action tx의 첫 부분: Run 활성 확인 → LLM 시도 차감 → STALE_OBSERVATION → 이 tx에서 다시
        관찰 → 스킬 검사(SKILL_NOT_OPEN·TOOL_NOT_IN_SKILL) → (permitted가 있으면) 허용 판정.

        permitted는 True, False(ACTION_NOT_AVAILABLE), 또는 거절 사유 문자열을 돌려준다.
        결과가 있으면 실행기는 그대로 돌려준다. 없으면 효과를 쓴다.
        """
        if not self._active(tx, run_id, step_no):
            return GatewayResult("INACTIVE"), None
        charge(tx, run_id, llm_attempts=meta.llm_attempts - 1)
        if self._stale_observation(tx, run_id, step_no):
            return self._reject(tx, run_id, step_no, meta, parsed, "STALE_OBSERVATION"), None
        obs = self.binding.observer.build_observation(tx, self.pack, run_id)
        # 고른 스킬이 열려 있고 그 도구를 가졌는가. 형식 오류 연속에는 세지 않는다 (AG-19)
        assert parsed.action is not None and parsed.name is not None
        reason = skills.check(parsed.action.skill, parsed.name, obs.data["open_skills"])
        if reason is not None:
            return self._reject(tx, run_id, step_no, meta, parsed, reason), None
        if permitted is None:
            return None, obs
        verdict = permitted(obs)
        if verdict is not True:
            reason = verdict if isinstance(verdict, str) else "ACTION_NOT_AVAILABLE"
            return self._reject(tx, run_id, step_no, meta, parsed, reason), None
        return None, obs

    def wait_or_continue(
        self, tx: sqlite3.Connection, run_id: str, step_no: int, wait_kind: str, wait_ref: str
    ) -> GatewayResult:
        """대기 진입 재확인: 관찰 이후 wake가 없으면 WAIT, 있으면 다시 관찰한다."""
        step = get_step(tx, run_id, step_no)
        assert step is not None
        if enter_wait(tx, run_id, wait_kind, wait_ref, step["observed_wake_seq"]):
            return GatewayResult("WAIT")
        return GatewayResult("CONTINUE", "NEW_CHANGE_BEFORE_WAIT")

    # ── 실행 ────────────────────────────────────────────────

    def execute(
        self, run_id: str, step_no: int, message: AIMessage | None, meta: StepMeta
    ) -> GatewayResult:
        if message is None:
            return self._llm_failure(run_id, step_no, meta)
        parsed = _parse(message, self.binding.spec)
        if parsed.action is None or parsed.name is None:
            with db.write() as tx:
                if not self._active(tx, run_id, step_no):
                    return GatewayResult("INACTIVE")
                charge(tx, run_id, llm_attempts=meta.llm_attempts - 1)
                return self._reject(tx, run_id, step_no, meta, parsed, "MALFORMED")
        return self.executor.run(run_id, step_no, meta, parsed)

    def _llm_failure(self, run_id: str, step_no: int, meta: StepMeta) -> GatewayResult:
        """모델 응답 없음. LLM_ERROR(전송 2회 실패)는 다시 관찰, LLM_CONFIG는 Run ERROR."""
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
