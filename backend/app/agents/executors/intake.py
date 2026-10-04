"""Work Intake Action 실행기.

ToolGateway.execute 안에서만 불린다(도구 실행 경로는 하나). 모든 Action은 tx 하나다:
begin_step(활성·차감·STALE_OBSERVATION·재관찰·허용 판정) → 효과 → step 완료.
값 검증과 작업 생성은 폼과 같은 함수(validate_task_request·create_requested_task)를 쓴다.
요청자에게 묻지 않는다: 완료는 값과 값마다의 출처(말함·정함)를 받고, 서버는 출처를 검사하지 않는다 (AG-32).
완료가 낸 시각은 가능 범위(Hard)가 아니라 기준 위치(요청한 시작 범위)가 된다. 시간창은 서버가 Horizon
전체로 채운다 (AG-35, CV-29).
승인·확정·Hold 해제·Proposal 확인 함수는 없다.
"""

import sqlite3
from typing import Any

from app.agents.observe import Observation
from app.agents.specs import intake as spec
from app.agents.tool_gateway import ACCEPTED, REJECTED, ToolGateway, _Parsed
from app.agents.types import GatewayResult, StepMeta
from app.commands.task_request import (
    TaskRequestForm,
    create_requested_task,
    validate_task_request,
)
from app.domain.calendar import parse_site_time
from app.domain.models import TaskBase
from app.store import db
from app.store.repos.site import get_site, list_actors


class IntakeExecutor:
    def __init__(self, gateway: ToolGateway):
        self.pack = gateway.pack
        self.observer = gateway.binding.observer
        # 공통 도우미 (ToolGateway)
        self._complete = gateway._complete
        self.begin_step = gateway.begin_step
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
            if isinstance(action, spec.LookupResource):
                result = self.observer.lookup_resources(
                    tx,
                    self.pack,
                    obs.run.acting_unit_id,
                    action.resource_type,
                    action.zone_id,
                    action.work_type,
                )
                return self._done(
                    tx, run_id, step_no, meta, parsed, GatewayResult("CONTINUE"), result
                )
            if isinstance(action, spec.CompleteTaskspec):
                return self._complete_spec(tx, run_id, step_no, meta, parsed, obs, action)
            assert isinstance(action, spec.ReturnResult)
            return self._blocked(tx, run_id, step_no, meta, parsed, obs, action)

    def _permitted(self, obs: Observation, action: Any) -> bool:
        available = obs.available
        names = {
            spec.LookupResource: "LOOKUP_RESOURCE",
            spec.CompleteTaskspec: "COMPLETE_TASKSPEC",
            spec.ReturnResult: "RETURN_RESULT",
        }
        if isinstance(action, spec.ReturnResult):
            return action.status in available.get("RETURN_RESULT", {}).get("status", [])
        return names.get(type(action)) in available

    def _blocked(
        self,
        tx: sqlite3.Connection,
        run_id: str,
        step_no: int,
        meta: StepMeta,
        parsed: _Parsed,
        obs: Observation,
        action: spec.ReturnResult,
    ) -> GatewayResult:
        """접수 미완 (AG-06). Run을 BLOCKED로 끝낸다. 요청자 통지(서버 문구, 사유 코드)는 Run 종료
        처리가 남긴다. Supervisor 이관도 메인 연결도 없다. 사유 코드는 서버가 관찰 사실에서 만든다.
        """
        request = obs.data["request"]
        codes = blocked_reason_codes(obs.data)
        produced: dict[str, Any] = {"task_id": request["task_id"], "reason_codes": codes}
        return self.return_result(tx, run_id, step_no, meta, parsed, produced)

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
        verdict: str = ACCEPTED,
    ) -> GatewayResult:
        self._complete(
            tx,
            run_id,
            step_no,
            meta,
            parsed,
            verdict=verdict,
            reason=outcome.reason if outcome.kind in ("CONTINUE", "REJECTED") else None,
            result_kind=outcome.kind,
            tool_result=tool_result,
            state_changes=state_changes,
            end=outcome,
        )
        return outcome

    def _requester(self, tx: sqlite3.Connection, obs: Observation) -> Any:
        actor_id = obs.data["request"]["requester_actor_id"]
        return next(a for a in list_actors(tx, self.pack.site_id) if a.actor_id == actor_id)

    def _minutes(self, values: spec.TaskValues) -> dict[str, Any] | None:
        """도구가 받은 값의 시각(현장 날짜·시각 문자열)을 분으로 바꾼다. 바꿀 수 없으면 None (AG-21)."""
        out = values.model_dump()
        pack = self.pack
        try:
            for key in spec.TIME_FIELDS:
                out[key] = parse_site_time(
                    out[key], pack.horizon_start_utc, pack.timezone, pack.horizon_minutes
                )
        except ValueError:
            return None
        return out

    def _time_invalid(
        self,
        tx: sqlite3.Connection,
        run_id: str,
        step_no: int,
        meta: StepMeta,
        parsed: _Parsed,
        values: spec.TaskValues,
    ) -> GatewayResult:
        outcome = GatewayResult("REJECTED", "TIME_INVALID")
        detail = {"reason_codes": ["TIME_INVALID"], "values": values.model_dump()}
        return self._done(tx, run_id, step_no, meta, parsed, outcome, detail, verdict=REJECTED)

    def _form(self, obs: Observation, values: dict[str, Any]) -> TaskRequestForm:
        """작업 요청 값. 시간창은 Horizon 전체다: 자연어에서 읽은 시각은 기준 위치로만 간다 (AG-35)."""
        horizon = self.pack.horizon_minutes
        window = {
            "earliest_start": 0,
            "latest_start": max(0, horizon - values["duration"]),
            "latest_end": horizon,
        }
        return TaskRequestForm(task_id=obs.data["request"]["task_id"], **{**values, **window})

    def _base_range(self, values: dict[str, Any]) -> tuple[int, int] | None:
        """완료가 낸 시각(분)의 기준 시작 범위 [가장 이른 시작, 시작 한도]. 시작 한도는 가장 늦은 시작과
        (종료 한도 − 작업 시간) 가운데 이른 쪽이다: "몇 시까지 끝"은 시작 한도로 바뀐다 (CV-29).
        모양이 맞지 않으면 None."""
        first, last, end = (values[k] for k in spec.TIME_FIELDS)
        if not 0 <= first <= last or first + values["duration"] > end:
            return None
        if end > self.pack.horizon_minutes:
            return None
        return first, min(last, end - values["duration"])

    def _complete_spec(
        self,
        tx: sqlite3.Connection,
        run_id: str,
        step_no: int,
        meta: StepMeta,
        parsed: _Parsed,
        obs: Observation,
        action: spec.CompleteTaskspec,
    ) -> GatewayResult:
        """폼과 같은 검증을 통과하면 폼과 같은 함수로 작업을 만든다(source_ref intake:<intake_id>).

        출처는 Agent가 적은 그대로 작업 기록에 남긴다. 자원이 있는데 출처를 적지 않았으면 정함으로 본다.
        시각은 기준 위치(요청한 시작 범위)가 되고(세 시각 가운데 하나라도 정함이면 정한 범위다. 계산에서는
        말한 범위와 똑같이 쓴다), 작업의 시간창 기록에는 출처를 적지 않는다(서버가 Horizon 전체로 채운
        값이다).
        """
        submitted = self._minutes(action.values)
        if submitted is None:
            return self._time_invalid(tx, run_id, step_no, meta, parsed, action.values)
        site = get_site(tx, self.pack.site_id)
        assert site is not None
        actor = self._requester(tx, obs)
        form = self._form(obs, submitted)
        found = self._base_range(submitted)
        codes = validate_task_request(tx, self.pack, site, actor, form)
        if found is None and "INVALID_WINDOW" not in codes:
            codes = [*codes, "INVALID_WINDOW"]
        if codes or found is None:
            outcome = GatewayResult("REJECTED", "TASKSPEC_INVALID")
            detail = {"reason_codes": codes, "values": submitted}
            return self._done(tx, run_id, step_no, meta, parsed, outcome, detail, verdict=REJECTED)
        origins = {
            name: origin or "DECIDED"
            for name, origin in action.origins.model_dump().items()
            if submitted.get(name) is not None
        }
        base = TaskBase(
            task_id=form.task_id,
            start=found[0],
            start_max=found[1],
            origin="DECIDED"
            if any(origins[k] == "DECIDED" for k in spec.TIME_FIELDS)
            else "STATED",
        )
        task_origins = {k: v for k, v in origins.items() if k not in spec.TIME_FIELDS}
        source = f"intake:{obs.data['request']['intake_id']}"
        refs = create_requested_task(
            tx, self.pack, site, actor, form, source, "INTAKE", task_origins, base
        )
        outcome = GatewayResult("DONE", None, "SUCCEEDED", f"TASKSPEC_COMPLETE:{form.task_id}")
        result = {
            **refs,
            "values": submitted,
            "origins": origins,
            # 낸 시각은 기준 위치(요청한 시작 범위)가 되었다. 시간창은 Horizon 전체다
            "base": {"start": base.start, "start_max": base.upper, "origin": base.origin},
        }
        return self._done(
            tx, run_id, step_no, meta, parsed, outcome, result, {"task_id": form.task_id}
        )


def blocked_reason_codes(data: dict[str, Any]) -> list[str]:
    """접수 미완의 사유 코드(서버가 관찰 사실에서 만든다): 마지막 검증 실패 사유. 없으면 NOT_COMPLETED."""
    check = data["last_check"] or {}
    codes = (check.get("detail") or {}).get("reason_codes") or (
        [check["reason_code"]] if check else []
    )
    return list(dict.fromkeys(codes)) or ["NOT_COMPLETED"]
