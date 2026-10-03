"""Work Intake Action 실행기.

ToolGateway.execute 안에서만 불린다(도구 실행 경로는 하나). 모든 Action은 tx 하나다:
begin_step(활성·차감·STALE_OBSERVATION·재관찰·허용 판정) → 효과 → step 완료.
값 검증과 작업 생성은 폼과 같은 함수(validate_task_request·create_requested_task)를 쓴다.
확인 값은 확인 메시지를 만든 이 step의 결과(values)에 묶인다. 완료는 그 값과 같을 때만 된다.
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
from app.domain.calendar import local_clock, parse_site_time
from app.domain.ids import new_id
from app.packs.loader import LoadedPack
from app.store import db
from app.store.repos.messages import insert_message
from app.store.repos.runs import charge
from app.store.repos.site import get_site, list_actors

FIELD_NAMES = {
    "work_type": "작업 유형",
    "zone_id": "구역",
    "duration": "작업 시간",
    "window": "시작 범위·종료 한도",
    "resource": "자원",
}


class IntakeExecutor:
    def __init__(self, gateway: ToolGateway):
        self.pack = gateway.pack
        self.observer = gateway.binding.observer
        # 공통 도우미 (ToolGateway)
        self._complete = gateway._complete
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
            if isinstance(action, spec.AskClarification):
                return self._ask(tx, run_id, step_no, meta, parsed, obs, action)
            if isinstance(action, spec.RequestConfirmation):
                return self._request_values_check(tx, run_id, step_no, meta, parsed, obs, action)
            if isinstance(action, spec.CompleteTaskspec):
                return self._complete_spec(tx, run_id, step_no, meta, parsed, obs, action)
            assert isinstance(action, spec.ReturnResult)
            return self._blocked(tx, run_id, step_no, meta, parsed, obs, action)

    def _permitted(self, obs: Observation, action: Any) -> bool:
        available = obs.available
        names = {
            spec.LookupResource: "LOOKUP_RESOURCE",
            spec.AskClarification: "ASK_CLARIFICATION",
            spec.RequestConfirmation: "REQUEST_CONFIRMATION",
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
        return TaskRequestForm(task_id=obs.data["request"]["task_id"], **values)

    def _ask(
        self,
        tx: sqlite3.Connection,
        run_id: str,
        step_no: int,
        meta: StepMeta,
        parsed: _Parsed,
        obs: Observation,
        action: spec.AskClarification,
    ) -> GatewayResult:
        """확인 질문(제안 없는 QUESTION) → 요청자. 답은 자유 텍스트(ANSWER)로 온다.

        물을 필드는 필드별 판단에서 도출한다(모호·빠짐 전부). 물을 것이 없으면 자기 인자끼리 모순이라
        거절한다. 앞 질문에서 받음으로 적은 필드를 모호·빠짐으로 바꾼 것(상태 후퇴)은 막지 않고 기록한다 (AG-22).
        """
        judgments = action.fields.model_dump()
        field_ids = spec.open_fields(judgments)
        if not field_ids:
            outcome = GatewayResult("REJECTED", "NOTHING_TO_ASK")
            detail = {"fields": judgments, "field_ids": []}
            return self._done(tx, run_id, step_no, meta, parsed, outcome, detail, verdict=REJECTED)
        asked = obs.data["questions"]
        before = (asked[-1].get("fields") if asked else None) or {}
        regressed = [f for f in field_ids if (before.get(f) or {}).get("status") == "RECEIVED"]
        site = get_site(tx, self.pack.site_id)
        assert site is not None
        request = obs.data["request"]
        fields = ", ".join(FIELD_NAMES[f] for f in field_ids)
        body = f"작업 요청 {request['task_id']} 확인 질문: {fields}을(를) 알려 주세요. 답은 문장으로 적습니다."
        message_id = new_id("msg")
        insert_message(
            tx,
            self.pack.site_id,
            message_id,
            run_id=run_id,
            step_no=step_no,
            to_actor_id=request["requester_actor_id"],
            type_="QUESTION",
            proposal_id=None,
            body=body,
            agent_text=action.question,
            context_version=site.context_version,
        )
        charge(tx, run_id, human_rounds=1)
        outcome = self.wait_or_continue(tx, run_id, step_no, "MESSAGE", message_id)
        result = {
            "message_id": message_id,
            "field_ids": field_ids,
            "fields": judgments,
            "regressed_field_ids": regressed,
            "body": body,
        }
        return self._done(
            tx, run_id, step_no, meta, parsed, outcome, result, {"message_id": message_id}
        )

    def _request_values_check(
        self,
        tx: sqlite3.Connection,
        run_id: str,
        step_no: int,
        meta: StepMeta,
        parsed: _Parsed,
        obs: Observation,
        action: spec.RequestConfirmation,
    ) -> GatewayResult:
        """값 확인 요청(CONFIRMATION) → 요청자. 폼 검증을 통과해야 나간다. 확인 값 = 이 step의 결과 values."""
        site = get_site(tx, self.pack.site_id)
        assert site is not None
        values = self._minutes(action.values)
        if values is None:
            return self._time_invalid(tx, run_id, step_no, meta, parsed, action.values)
        form = self._form(obs, values)
        codes = validate_task_request(tx, self.pack, site, self._requester(tx, obs), form)
        if codes:
            outcome = GatewayResult("REJECTED", "TASKSPEC_INVALID")
            detail = {"reason_codes": codes, "values": values}
            return self._done(tx, run_id, step_no, meta, parsed, outcome, detail, verdict=REJECTED)
        request = obs.data["request"]
        body = values_check_text(self.pack, request["task_id"], values)
        message_id = new_id("msg")
        insert_message(
            tx,
            self.pack.site_id,
            message_id,
            run_id=run_id,
            step_no=step_no,
            to_actor_id=request["requester_actor_id"],
            type_="CONFIRMATION",
            proposal_id=None,
            body=body,
            agent_text=action.message,
            context_version=site.context_version,
        )
        charge(tx, run_id, human_rounds=1)
        outcome = self.wait_or_continue(tx, run_id, step_no, "MESSAGE", message_id)
        result = {"message_id": message_id, "values": values, "body": body}
        return self._done(
            tx, run_id, step_no, meta, parsed, outcome, result, {"message_id": message_id}
        )

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
        """확인 값과 같으면 폼과 같은 함수로 작업을 만든다(source_ref message:<mid>)."""
        last = obs.data["confirmations"][-1]
        submitted = self._minutes(action.values)
        if submitted is None:
            return self._time_invalid(tx, run_id, step_no, meta, parsed, action.values)
        if submitted != last["values"]:
            outcome = GatewayResult("REJECTED", "CONFIRMED_VALUE_MISMATCH")
            detail = {"confirmed": last["values"], "submitted": submitted}
            return self._done(tx, run_id, step_no, meta, parsed, outcome, detail, verdict=REJECTED)
        site = get_site(tx, self.pack.site_id)
        assert site is not None
        actor = self._requester(tx, obs)
        form = self._form(obs, submitted)
        codes = validate_task_request(tx, self.pack, site, actor, form)
        if codes:
            outcome = GatewayResult("REJECTED", "TASKSPEC_INVALID")
            detail = {"reason_codes": codes, "values": submitted}
            return self._done(tx, run_id, step_no, meta, parsed, outcome, detail, verdict=REJECTED)
        refs = create_requested_task(
            tx, self.pack, site, actor, form, f"message:{last['message_id']}", "INTAKE"
        )
        outcome = GatewayResult("DONE", None, "SUCCEEDED", f"TASKSPEC_COMPLETE:{form.task_id}")
        return self._done(
            tx, run_id, step_no, meta, parsed, outcome, refs, {"task_id": form.task_id}
        )


def blocked_reason_codes(data: dict[str, Any]) -> list[str]:
    """접수 미완의 사유 코드(서버가 관찰 사실에서 만든다): 마지막 검증 실패 사유, 요청자의 값 확인 거절,
    사람 확인 라운드 소진. 해당하는 것이 없으면 NOT_COMPLETED."""
    codes: list[str] = []
    check = data["last_check"] or {}
    codes += (check.get("detail") or {}).get("reason_codes") or (
        [check["reason_code"]] if check else []
    )
    confirmations = data["confirmations"]
    if confirmations and confirmations[-1]["decision"] == "DECLINE":
        codes.append("REQUESTER_DECLINED")
    if data["human_rounds"]["remaining"] <= 0 and not data["can_complete"]:
        codes.append("HUMAN_ROUNDS_EXHAUSTED")
    return list(dict.fromkeys(codes)) or ["NOT_COMPLETED"]


def requirement_text(pack: LoadedPack, req: dict[str, Any]) -> str:
    """요구 조건 하나의 사람용 문구. 속성 표시 이름과 단위는 Pack 선언에서 읽는다."""
    decl = pack.resource_attributes.get(req["attribute"])
    name = decl.display_name if decl else req["attribute"]
    if req["op"] == "CONTAINS":
        return f"{name} {req['value']} 포함"
    unit = f" {decl.unit}" if decl and decl.unit else ""
    return f"{name} {'≥' if req['op'] == 'GTE' else '≤'} {req['value']}{unit}"


def demand_text(pack: LoadedPack, kind: str, quantity: int) -> str:
    """수요 하나의 사람용 문구. 종류 표시 이름과 단위는 Pack 선언에서 읽는다."""
    decl = pack.pool_kinds.get(kind)
    return f"{decl.display_name if decl else kind} {quantity}{decl.unit if decl else ''}"


def values_check_text(pack: LoadedPack, task_id: str, v: dict[str, Any]) -> str:
    """값 확인 요청의 서버 문구: 값을 날짜·시각과 분으로, 확인의 효과(동의)를 함께."""

    def at(m: int) -> str:
        return f"{local_clock(pack.horizon_start_utc, pack.timezone, m)}({m}분)"

    wt = pack.work_types.get(v["work_type"])
    name = wt.display_name if wt else v["work_type"]
    resource = (
        f"{v['required_resource_type']} {v['requested_resource_id']}"
        if v.get("requested_resource_id")
        else "없음"
    )
    # 자원이 맞춰야 하는 조건 = 작업 유형 기본값 + 요청 값 (CV-11)
    needs = [
        *(r.model_dump() for r in pack.default_requirements(v["work_type"])),
        *(v.get("resource_requirements") or ()),
    ]
    if v.get("requested_resource_id") and needs:
        resource += f"(요구 조건: {', '.join(requirement_text(pack, r) for r in needs)})"
    # 수요 = 작업 유형 기본값과 요청 값 중 큰 쪽 (CV-11)
    demands: dict[str, int] = {}
    for d in (
        *(x.model_dump() for x in pack.default_demands(v["work_type"])),
        *(v.get("pool_demands") or ()),
    ):
        demands[d["kind"]] = max(demands.get(d["kind"], 0), d["quantity"])
    if demands:
        resource += f", 수요 {', '.join(demand_text(pack, k, q) for k, q in demands.items())}"
    return (
        f"작업 요청 {task_id} 값 확인: {name}, {v['zone_id']} 구역, {v['duration']}분, "
        f"시작 {at(v['earliest_start'])}–{at(v['latest_start'])}, 종료 한도 {at(v['latest_end'])}, "
        f"자원 {resource}. 확인하면 이 값이 작업 사실(확인됨)이 되고, 시작 범위와 요청 자원에 "
        "동의한 것으로 기록됩니다."
    )
