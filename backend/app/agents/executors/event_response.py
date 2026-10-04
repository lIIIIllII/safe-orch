"""Event Response Action 실행기.

ToolGateway.execute 안에서만 불린다(도구 실행 경로는 하나). 모든 Action은 tx 하나다:
begin_step(활성·차감·STALE_OBSERVATION·재관찰·허용 판정) → 효과 → step 완료.
사실 수정은 제안(FACT_UPDATE)까지만 만든다. 효력은 Supervisor 확인 명령이 만든다.
조회·영향 분석 계산은 binding.observer로 쓴다(observers를 import하지 않는다).
Hold 해제·승인·확정·Proposal 확인 함수는 없다.
"""

import sqlite3
from typing import Any

from app.agents.observe import Observation
from app.agents.specs import event_response as spec
from app.agents.tool_gateway import ACCEPTED, ToolGateway, _Parsed
from app.agents.types import GatewayResult, StepMeta
from app.domain.calendar import local_clock, parse_site_time
from app.domain.ids import new_id
from app.packs.loader import LoadedPack
from app.store import db
from app.store.repos.cases import supervisor_actor
from app.store.repos.messages import insert_message, insert_proposal
from app.store.repos.site import get_site
from app.store.repos.tasks import list_current_tasks


class EventResponseExecutor:
    def __init__(self, gateway: ToolGateway):
        self.pack = gateway.pack
        self.observer = gateway.binding.observer
        # 공통 도우미 (ToolGateway)
        self._complete = gateway._complete
        self._reject = gateway._reject
        self.begin_step = gateway.begin_step
        self.wait_or_continue = gateway.wait_or_continue
        self.return_result = gateway.return_result

    def run(self, run_id: str, step_no: int, meta: StepMeta, parsed: _Parsed) -> GatewayResult:
        action = parsed.action
        assert action is not None
        with db.write() as tx:
            rejected, obs = self.begin_step(
                tx, run_id, step_no, meta, parsed, lambda o: self._permitted(o, action)
            )
            if rejected is not None:
                return rejected
            assert obs is not None
            if isinstance(action, spec.LookupTasks):
                result = self.observer.lookup_tasks(tx, self.pack, action.work_type, action.zone_id)
                return self._done(
                    tx, run_id, step_no, meta, parsed, GatewayResult("CONTINUE"), result
                )
            if isinstance(action, spec.AnalyzeImpact):
                result = self.observer.analyze_impact(
                    tx, self.pack, action.task_id, self._minute(action)
                )
                return self._done(
                    tx, run_id, step_no, meta, parsed, GatewayResult("CONTINUE"), result
                )
            if isinstance(action, spec.ProposeFactUpdate):
                # 제출 때 영향 분석을 다시 돌려 통과해야 받는다. 분석을 했는지는 보지 않는다 (CV-16)
                new_value = self._minute(action)
                analysis = self.observer.analyze_impact(tx, self.pack, action.task_id, new_value)
                if not analysis["ok"]:
                    return self._reject(tx, run_id, step_no, meta, parsed, "ANALYSIS_NOT_PASSED")
                return self._propose(tx, run_id, step_no, meta, parsed, obs, action, new_value)
            assert isinstance(action, spec.ReturnResult)
            # 서버가 채우는 내용: 이 Run이 낸 수정안과 Hold
            produced = {
                "proposals": [
                    {k: p[k] for k in ("proposal_id", "status")} for p in obs.data["proposals"]
                ],
                "hold": (obs.data["event"] or {}).get("hold"),
            }
            return self.return_result(tx, run_id, step_no, meta, parsed, produced)

    def _minute(self, action: Any) -> int | None:
        """도구가 받은 새 시각(현장 날짜·시각 문자열)을 분으로 바꾼다. 바꿀 수 없으면 None (AG-21)."""
        pack = self.pack
        try:
            return parse_site_time(
                action.new_earliest_start,
                pack.horizon_start_utc,
                pack.timezone,
                pack.horizon_minutes,
            )
        except ValueError:
            return None

    def _permitted(self, obs: Observation, action: Any) -> bool | str:
        """선택한 Action과 인자 조합이 최신 Available Actions 안에 있는가."""
        if isinstance(action, spec.AnalyzeImpact | spec.ProposeFactUpdate) and (
            self._minute(action) is None
        ):
            return "TIME_INVALID"
        available = obs.available
        c = spec.choices(obs.data, obs.hidden)
        if isinstance(action, spec.LookupTasks):
            return "LOOKUP_TASKS" in available
        if isinstance(action, spec.AnalyzeImpact):
            return "ANALYZE_IMPACT" in available and action.task_id in c["ANALYZE"]
        if isinstance(action, spec.ProposeFactUpdate):
            return (
                "PROPOSE_FACT_UPDATE" in available
                and action.task_id in c["PROPOSE"]
                and (action.task_id, self._minute(action)) not in c["DISCARDED"]
            )
        return isinstance(action, spec.ReturnResult) and action.status in available.get(
            "RETURN_RESULT", {}
        ).get("status", [])

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

    def _propose(
        self,
        tx: sqlite3.Connection,
        run_id: str,
        step_no: int,
        meta: StepMeta,
        parsed: _Parsed,
        obs: Observation,
        action: spec.ProposeFactUpdate,
        new_value: int,
    ) -> GatewayResult:
        """FACT_UPDATE 제안 + Supervisor 확인 메시지 → 대기. 확인자는 actor_id가 가장 작은 SUPERVISOR."""
        site = get_site(tx, self.pack.site_id)
        supervisor = supervisor_actor(tx, self.pack)
        assert site is not None and supervisor is not None
        task = next(
            t
            for t in list_current_tasks(tx, self.pack.site_id, self.pack)
            if t.task_id == action.task_id
        )
        event = obs.data["event"]
        payload = {
            "event_id": event["event_id"],
            "hold_id": (event["hold"] or {}).get("hold_id"),
            "field": "earliest_start",
            "old_value": task.earliest_start,
            "new_value": new_value,
            "evidence": action.evidence,
        }
        proposal_id, message_id = new_id("prop"), new_id("msg")
        insert_proposal(
            tx,
            self.pack.site_id,
            proposal_id,
            type_="FACT_UPDATE",
            run_id=run_id,
            step_no=step_no,
            target_task_id=task.task_id,
            base_task_revision=task.revision,
            context_version=site.context_version,
            payload=payload,
            confirmer_actor_id=supervisor.actor_id,
        )
        body = fact_update_text(self.pack, task, payload, event["quoted_text"])
        insert_message(
            tx,
            self.pack.site_id,
            message_id,
            run_id=run_id,
            step_no=step_no,
            to_actor_id=supervisor.actor_id,
            type_="CONFIRMATION",
            proposal_id=proposal_id,
            body=body,
            agent_text=action.evidence,
            context_version=site.context_version,
        )
        outcome = self.wait_or_continue(tx, run_id, step_no, "MESSAGE", message_id)
        result = {
            "proposal_id": proposal_id,
            "message_id": message_id,
            "to_actor_id": supervisor.actor_id,
            "body": body,
        }
        changes = {"proposal_id": proposal_id, "message_id": message_id}
        return self._done(tx, run_id, step_no, meta, parsed, outcome, result, changes)


def fact_update_text(pack: LoadedPack, task: Any, payload: dict[str, Any], quoted: str) -> str:
    """사실 수정 확인의 서버 문구: 옛 값·새 값을 분과 함께 날짜·시각으로."""
    old, new = payload["old_value"], payload["new_value"]

    def at(m: int) -> str:
        return f"{local_clock(pack.horizon_start_utc, pack.timezone, m)}({m}분)"

    name = pack.work_types[task.work_type].display_name
    return (
        f"사실 수정 확인: {task.task_id}({name}) 작업의 시작 가능 시각 {at(old)} → {at(new)}. "
        f"신고: “{quoted}”. 확정하면 작업 사실이 바뀌고, Hold는 사실 확인 해제(FACT_CONFIRMED)로 "
        "따로 풀어야 재검사가 시작됩니다."
    )
