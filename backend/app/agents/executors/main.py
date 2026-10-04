"""Main Action 실행기.

ToolGateway.execute 안에서만 불린다(도구 실행 경로는 하나). 모든 Action은 tx 하나다:
begin_step(활성·차감·STALE_OBSERVATION·재관찰·스킬) → 유효성(사실 조건) → 효과 → step 완료.
CALL_AGENT는 하위 Run의 START_RUN을 등록하고 대기한다(Run은 Coordinator가 만든다, AG-05). 넘기는 것은
Agent 종류와 참조뿐이다. 재계획은 접근만 받고 현장의 충돌 전체를 푼다 (AG-24).
메인 Run의 acting_unit_id는 어떤 판정에도 쓰지 않는다.
승인·확정·Hold 해제·Proposal 확인 함수는 없다.
"""

import sqlite3
from typing import Any

from app.agents import casefacts
from app.agents.needs import NEED_INVALID, invalid_needs
from app.agents.observe import Observation
from app.agents.specs import main as spec
from app.agents.tool_gateway import ACCEPTED, REJECTED, ToolGateway, _Parsed
from app.agents.types import GatewayResult, StepMeta
from app.domain.ids import new_id
from app.domain.needs import Path
from app.store import db
from app.store.repos.calls import call_key
from app.store.repos.cases import supervisor_actor
from app.store.repos.dispatch import register_job
from app.store.repos.messages import insert_message
from app.store.repos.plans import get_plan_by_candidate
from app.store.repos.runs import charge, get_run, get_step
from app.store.repos.site import get_site


class MainExecutor:
    def __init__(self, gateway: ToolGateway):
        self.pack = gateway.pack
        # 공통 도우미 (ToolGateway)
        self._complete = gateway._complete
        self._reject = gateway._reject
        self.begin_step = gateway.begin_step
        self.wait_or_continue = gateway.wait_or_continue

    def run(self, run_id: str, step_no: int, meta: StepMeta, parsed: _Parsed) -> GatewayResult:
        action = parsed.action
        assert action is not None
        with db.write() as tx:
            rejected, obs = self.begin_step(tx, run_id, step_no, meta, parsed)
            if rejected is not None:
                return rejected
            assert obs is not None
            reason = self._refusal(tx, obs, action)
            if reason is not None:
                return self._reject(tx, run_id, step_no, meta, parsed, reason)
            if isinstance(action, spec.CallAgent):
                return self._call(tx, run_id, step_no, meta, parsed, obs, action)
            if isinstance(action, spec.Wait):
                waiting = obs.data["waiting_for"]
                ref = ",".join([*waiting["candidates"], *waiting["holds"]])
                outcome = self.wait_or_continue(tx, run_id, step_no, "HUMAN_DECISION", ref)
                return self._done(
                    tx, run_id, step_no, meta, parsed, outcome, {"waiting_for": waiting}
                )
            if isinstance(action, spec.Escalate):
                return self._escalate(tx, run_id, step_no, meta, parsed, obs, action)
            assert isinstance(action, spec.Close)
            outcome = GatewayResult("DONE", None, "SUCCEEDED", "CLOSE")
            return self._done(
                tx, run_id, step_no, meta, parsed, outcome, {"summary": action.summary}
            )

    # ── 유효성 (사실 조건만) ───────────────────────────────

    def _refusal(self, tx: sqlite3.Connection, obs: Observation, action: Any) -> str | None:
        """거절 사유. 없으면 None. 관찰은 이 tx에서 다시 계산한 것이다."""
        data, available = obs.data, obs.available
        main_id = obs.run.run_id
        child_open = casefacts.open_child(tx, main_id) is not None or casefacts.pending_child(
            tx, self.pack.site_id, main_id
        )
        if isinstance(action, spec.Wait):
            return None if "WAIT" in available else "NOTHING_TO_WAIT_FOR"
        if child_open:
            # 하위 Run은 한 번에 하나다. 열려 있으면 부르지도 끝내지도 못한다
            return "CHILD_RUN_OPEN"
        if isinstance(action, spec.CallAgent):
            reason = self._call_refusal(tx, obs, spec.call_refs(action))
            if reason is None and "CALL_AGENT" not in available:
                return "ACTION_NOT_AVAILABLE"  # 호출 Budget이 없다
            return reason
        # 끝내기: 아직 보지 않은 사건이 있으면 다시 관찰한다
        if any(e["new"] for e in data["events"]):
            return "NEW_EVENT"
        if isinstance(action, spec.Escalate):
            needs = (
                invalid_needs(tx, self.pack, obs.run, [Path(needs=action.needs)])
                if action.needs
                else []
            )
            return NEED_INVALID if needs else None
        return None if "CLOSE" in available else "OPEN_WORK"

    def _call_refusal(
        self, tx: sqlite3.Connection, obs: Observation, refs: dict[str, Any]
    ) -> str | None:
        data = obs.data
        holds = bool(data["holds"])
        if refs in data["calls"]:
            return None
        if refs["agent"] == "REPLANNING":
            if not data["groups"]:
                return "NO_CONFLICT"
            if not data["replanning"]["movable_task_ids"]:
                return "NO_MOVABLE_TASK"  # 충돌에 걸린 작업이 모두 고정되어 있다
            if refs.get("approach") is None:
                return "APPROACH_REQUIRED"
            return "HOLD_ACTIVE" if holds else "SAME_FACTS"
        if refs["agent"] == "COORDINATION":
            candidate = next(
                (c for c in data["candidates"] if c["candidate_id"] == refs.get("candidate_id")),
                None,
            )
            if candidate is None or refs.get("phase") is None:
                return "ACTION_NOT_AVAILABLE"
            if refs["phase"] == "CONSULT" and candidate["live"] and candidate["open_items"]:
                if not candidate["chosen"]:
                    return "CANDIDATE_NOT_CHOSEN"  # 협의는 Supervisor가 고른 안만 한다 (AG-29)
                return "HOLD_ACTIVE" if holds else "SAME_FACTS"
            return "ACTION_NOT_AVAILABLE"
        hold = next((h for h in data["holds"] if h["event_id"] == refs.get("event_id")), None)
        return (
            "SAME_FACTS"
            if hold is not None and hold["event_type"] == "DELAY"
            else "ACTION_NOT_AVAILABLE"
        )

    # ── 효과 ───────────────────────────────────────────────

    def _done(
        self,
        tx: sqlite3.Connection,
        run_id: str,
        step_no: int,
        meta: StepMeta,
        parsed: _Parsed,
        outcome: GatewayResult,
        tool_result: dict[str, Any] | None,
        state_changes: dict[str, Any] | None = None,
    ) -> GatewayResult:
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
            state_changes=state_changes,
            end=outcome,
        )
        return outcome

    def _call(
        self,
        tx: sqlite3.Connection,
        run_id: str,
        step_no: int,
        meta: StepMeta,
        parsed: _Parsed,
        obs: Observation,
        action: spec.CallAgent,
    ) -> GatewayResult:
        """하위 Run 요청: START_RUN 등록 → 호출 수 차감 → 하위 Run 대기."""
        site_id = self.pack.site_id
        run, step = get_run(tx, run_id), get_step(tx, run_id, step_no)
        site = get_site(tx, site_id)
        assert run is not None and step is not None and site is not None
        refs = spec.call_refs(action)
        if run.wake_seq > step["observed_wake_seq"]:
            # 관찰 이후 새 변화가 왔다. 부르지 않고 다시 관찰한다
            outcome = GatewayResult("CONTINUE", "NEW_CHANGE_BEFORE_WAIT")
            return self._done(tx, run_id, step_no, meta, parsed, outcome, {"call": refs})
        payload = self._payload(tx, obs, action)
        if payload is None:
            self._complete(
                tx,
                run_id,
                step_no,
                meta,
                parsed,
                verdict=REJECTED,
                reason="ACTION_NOT_AVAILABLE",
                result_kind="REJECTED",
            )
            return GatewayResult("REJECTED", "ACTION_NOT_AVAILABLE")
        child_id = new_id("run")
        payload = {
            **payload,
            "run_id": child_id,
            "parent_run_id": run_id,
            "case_id": run.case_id,
            "context_version": site.context_version,
            "plan_revision": payload.get("plan_revision", site.plan_revision),
            "call_key": call_key(action.agent, payload),
        }
        register_job(tx, site_id, "START_RUN", f"START_RUN:{child_id}", payload)
        charge(tx, run_id, agent_calls=1)
        outcome = self.wait_or_continue(tx, run_id, step_no, "CHILD_RUN", child_id)
        result = {"run_id": child_id, "call": refs}
        return self._done(tx, run_id, step_no, meta, parsed, outcome, result, {"run_id": child_id})

    def _payload(
        self, tx: sqlite3.Connection, obs: Observation, action: spec.CallAgent
    ) -> dict[str, Any] | None:
        """START_RUN payload의 참조 부분."""
        if action.agent == "REPLANNING":
            # 재계획은 현장의 충돌 전체를 푼다. 주체 Unit과 대신 움직이는 Actor는 없다 (AG-24)
            return {
                "agent_type": "REPLANNING",
                "acting_unit_id": None,
                "acting_actor_id": None,
                # 접근은 호출 키에 들어가고, 문장은 재계획 관찰에 인용으로만 간다
                "approach": action.approach,
                "approach_note": action.approach_note,
            }
        supervisor = supervisor_actor(tx, self.pack)
        if supervisor is None:
            return None
        if action.agent == "COORDINATION":
            assert action.candidate_id is not None
            payload: dict[str, Any] = {
                "agent_type": "COORDINATION",
                "phase": action.phase,
                "candidate_id": action.candidate_id,
                "acting_unit_id": supervisor.unit_id,
            }
            if action.phase == "NOTICE":
                plan = get_plan_by_candidate(tx, self.pack.site_id, action.candidate_id)
                if plan is None:
                    return None
                payload["plan_revision"] = plan.plan_revision
            return payload
        hold = next(h for h in obs.data["holds"] if h["event_id"] == action.event_id)
        return {
            "agent_type": "EVENT_RESPONSE",
            "event_id": action.event_id,
            "hold_id": hold["hold_id"],
            "acting_unit_id": supervisor.unit_id,
        }

    def _escalate(
        self,
        tx: sqlite3.Connection,
        run_id: str,
        step_no: int,
        meta: StepMeta,
        parsed: _Parsed,
        obs: Observation,
        action: spec.Escalate,
    ) -> GatewayResult:
        """Supervisor 이관: 서버 문구 통지(이 Case의 남은 일과 필요한 것)를 남기고 Run을 끝낸다."""
        site = get_site(tx, self.pack.site_id)
        supervisor = supervisor_actor(tx, self.pack)
        assert site is not None
        needs = [n.model_dump(exclude_defaults=True) for n in action.needs]
        open_work = obs.data["open_work"]
        outcome = GatewayResult("DONE", None, "ESCALATED", "ESCALATE")
        changes: dict[str, Any] = {}
        if supervisor is not None:
            message_id = new_id("msg")
            insert_message(
                tx,
                self.pack.site_id,
                message_id,
                run_id=run_id,
                step_no=step_no,
                to_actor_id=supervisor.actor_id,
                type_="NOTICE",
                proposal_id=None,
                body=escalation_text(open_work, needs),
                agent_text=action.summary,
                context_version=site.context_version,
            )
            changes["message_id"] = message_id
        result = {"summary": action.summary, "needs": needs, "open_work": open_work}
        return self._done(tx, run_id, step_no, meta, parsed, outcome, result, changes)


def escalation_text(open_work: list[dict[str, Any]], needs: list[dict[str, Any]]) -> str:
    """이관 통지의 서버 문구: 이 Case에 남은 일과 필요한 것(종류와 참조)."""

    def item(d: dict[str, Any], head: str) -> str:
        rest = ", ".join(f"{k} {v}" for k, v in d.items() if k != head)
        return f"{d[head]}({rest})" if rest else str(d[head])

    left = "; ".join(item(w, "kind") for w in open_work) or "없음"
    need = "; ".join(item(n, "kind") for n in needs) or "없음"
    return f"메인 Agent가 이 Case를 이관했습니다. 남은 일: {left}. 필요한 것: {need}."
