"""Coordination Action 실행기.

ToolGateway.execute 안에서만 불린다(도구 실행 경로는 하나). 모든 Action은 tx 하나다:
begin_step(활성·차감·STALE_OBSERVATION·재관찰·허용 판정) → 효과 → step 완료.
변경 요청·통지의 서버 문구(body)는 여기서 서버 값으로 만들고, 모델 문장은 agent_text다.
사전 확인(ASK_OWNER)은 MOVABILITY 제안과 질문을 만든다. 동의 효과는 담당자의 답 명령이 만든다.
승인·확정·Hold 해제·Proposal 확인·미응답 수용·고정·고정 해제 함수는 없다.
"""

import sqlite3
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.agents.observe import Observation
from app.agents.specs import coordination as spec
from app.agents.tool_gateway import ACCEPTED, ToolGateway, _Parsed
from app.agents.types import GatewayResult, StepMeta
from app.domain.ids import new_id
from app.domain.models import Task
from app.packs.loader import LoadedPack
from app.store import db
from app.store.repos.consultations import get_consultation_items
from app.store.repos.messages import insert_message, insert_proposal
from app.store.repos.plans import get_current_plan
from app.store.repos.site import get_site
from app.store.repos.tasks import list_current_tasks


class CoordinationExecutor:
    def __init__(self, gateway: ToolGateway):
        self.pack = gateway.pack
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
            if isinstance(action, spec.SendChangeRequest):
                return self._request(tx, run_id, step_no, meta, parsed, obs, action)
            if isinstance(action, spec.AskOwner):
                return self._ask(tx, run_id, step_no, meta, parsed, obs, action)
            if isinstance(action, spec.WaitForReplies):
                opened = [a["message_id"] for a in obs.data["asks"] if a["status"] == "OPEN"]
                kind, ref = (
                    ("MESSAGE", opened[0])
                    if obs.data["phase"] == "ASK"
                    else ("CONSULTATION", obs.data["candidate"]["candidate_id"])
                )
                outcome = self.wait_or_continue(tx, run_id, step_no, kind, ref)
                self._done(tx, run_id, step_no, meta, parsed, outcome, None)
                return outcome
            if isinstance(action, spec.SendNotice):
                return self._notice(tx, run_id, step_no, meta, parsed, obs, action)
            assert isinstance(action, spec.ReturnResult)
            return self.return_result(tx, run_id, step_no, meta, parsed, _produced(obs))

    def _permitted(self, obs: Observation, action: Any) -> bool | str:
        """선택한 Action과 인자 조합이 최신 Available Actions 안에 있는가 (조합까지)."""
        available = obs.available
        c = spec.choices(obs.data)
        if isinstance(action, spec.AskOwner):
            ask = next((a for a in obs.data["asks"] if a["need_id"] == action.need_id), None)
            if ask is not None and ask["status"] == "NOT_ASKABLE":
                return str(ask["reason"])  # 거절한 값(VALUE_DECLINED), 고정된 작업 등 사실 조건
            return "ASK_OWNER" in available and action.need_id in c["ASK"]
        if isinstance(action, spec.ReturnResult):
            return action.status in available["RETURN_RESULT"].get("status", ["DONE", "BLOCKED"])
        if isinstance(action, spec.SendChangeRequest):
            return "SEND_CHANGE_REQUEST" in available and action.task_id in c["REQUEST"]
        if isinstance(action, spec.WaitForReplies):
            return "WAIT_FOR_REPLIES" in available
        if isinstance(action, spec.SendNotice):
            allowed = c["NOTICE"].get(action.actor_id)
            return (
                "SEND_NOTICE" in available
                and allowed is not None
                and set(action.task_ids) <= set(allowed)
            )
        return False

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
    ) -> None:
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

    def _task(self, tx: sqlite3.Connection, task_id: str) -> Task:
        return next(
            t for t in list_current_tasks(tx, self.pack.site_id, self.pack) if t.task_id == task_id
        )

    def _request(
        self,
        tx: sqlite3.Connection,
        run_id: str,
        step_no: int,
        meta: StepMeta,
        parsed: _Parsed,
        obs: Observation,
        action: spec.SendChangeRequest,
    ) -> GatewayResult:
        """변경 요청(CHANGE_REQUEST): 후보·change_hash에 묶고 수신자는 항목의 담당자다."""
        candidate_id = obs.data["candidate"]["candidate_id"]
        items = get_consultation_items(tx, self.pack.site_id, candidate_id) or ()
        item = next(i for i in items if i.task_id == action.task_id)
        site = get_site(tx, self.pack.site_id)
        assert site is not None
        task = self._task(tx, action.task_id)
        body = change_request_text(self.pack, task, item.before, item.after)
        message_id = new_id("msg")
        insert_message(
            tx,
            self.pack.site_id,
            message_id,
            run_id=run_id,
            step_no=step_no,
            to_actor_id=item.owner_actor_id,
            type_="CHANGE_REQUEST",
            proposal_id=None,
            body=body,
            agent_text=action.message,
            context_version=site.context_version,
            candidate_id=candidate_id,
            change_hash=item.change_hash,
        )
        outcome = GatewayResult("CONTINUE")
        result = {"message_id": message_id, "to_actor_id": item.owner_actor_id, "body": body}
        self._done(tx, run_id, step_no, meta, parsed, outcome, result, {"message_id": message_id})
        return outcome

    def _ask(
        self,
        tx: sqlite3.Connection,
        run_id: str,
        step_no: int,
        meta: StepMeta,
        parsed: _Parsed,
        obs: Observation,
        action: spec.AskOwner,
    ) -> GatewayResult:
        """사전 확인: MOVABILITY 제안 + 질문(QUESTION). need 하나에 질문 하나, 수신자는 작업 담당자다.

        동의 효과는 구조화 값(axis·allowed_values)으로만 정해진다. 모델의 message는 agent_text로만 둔다.
        """
        ask = next(a for a in obs.data["asks"] if a["need_id"] == action.need_id)
        site = get_site(tx, self.pack.site_id)
        assert site is not None
        task = self._task(tx, ask["task_id"])
        values = list(dict.fromkeys(ask["values"]))
        proposal_id, message_id = new_id("prop"), new_id("msg")
        insert_proposal(
            tx,
            self.pack.site_id,
            proposal_id,
            type_="MOVABILITY",
            run_id=run_id,
            step_no=step_no,
            target_task_id=task.task_id,
            base_task_revision=task.revision,
            context_version=site.context_version,
            payload={"axis": ask["axis"], "allowed_values": values, "need_id": action.need_id},
            confirmer_actor_id=task.owner_actor_id,
        )
        body = movability_text(self.pack, task, values)
        insert_message(
            tx,
            self.pack.site_id,
            message_id,
            run_id=run_id,
            step_no=step_no,
            to_actor_id=task.owner_actor_id,
            type_="QUESTION",
            proposal_id=proposal_id,
            body=body,
            agent_text=action.message,
            context_version=site.context_version,
        )
        outcome = GatewayResult("CONTINUE")
        result = {
            "need_id": action.need_id,
            "proposal_id": proposal_id,
            "message_id": message_id,
            "to_actor_id": task.owner_actor_id,
            "body": body,
        }
        changes = {"proposal_id": proposal_id, "message_id": message_id}
        self._done(tx, run_id, step_no, meta, parsed, outcome, result, changes)
        return outcome

    def _notice(
        self,
        tx: sqlite3.Connection,
        run_id: str,
        step_no: int,
        meta: StepMeta,
        parsed: _Parsed,
        obs: Observation,
        action: spec.SendNotice,
    ) -> GatewayResult:
        """통지(NOTICE): 확정된 배정과 안전 규칙 연결을 서버 문구로. 답을 받지 않는다."""
        site = get_site(tx, self.pack.site_id)
        assert site is not None
        target = next(t for t in obs.data["notice_targets"] if t["actor_id"] == action.actor_id)
        task_ids = sorted(set(action.task_ids))
        body = notice_text(tx, self.pack, task_ids, target["reasons"])
        message_id = new_id("msg")
        insert_message(
            tx,
            self.pack.site_id,
            message_id,
            run_id=run_id,
            step_no=step_no,
            to_actor_id=action.actor_id,
            type_="NOTICE",
            proposal_id=None,
            body=body,
            agent_text=action.message,
            context_version=site.context_version,
        )
        outcome = GatewayResult("CONTINUE")
        result = {"message_id": message_id, "to_actor_id": action.actor_id, "body": body}
        self._done(tx, run_id, step_no, meta, parsed, outcome, result, {"message_id": message_id})
        return outcome


def _produced(obs: Observation) -> dict[str, Any]:
    """결과에 서버가 채우는 내용: 협의는 항목별 상태와 남은 이견, 통지는 대상 수와 보낸 수, 사전 확인은
    need별 답(수락한 값·거절·미응답·묻지 못함)."""
    data = obs.data
    if data["phase"] == "ASK":
        results = {"OPEN": "NO_REPLY", "UNASKED": "NOT_ASKED", "NOT_ASKABLE": "NOT_ASKED"}
        return {
            "phase": "ASK",
            "asks": [
                {
                    "need_id": a["need_id"],
                    "task_id": a["task_id"],
                    "owner_actor_id": a["owner_actor_id"],
                    "result": results.get(a["status"], a["status"]),
                    "accepted_values": a.get("accepted_values", []),
                }
                for a in data["asks"]
            ],
        }
    if data["phase"] == "NOTICE":
        targets = data["notice_targets"]
        return {
            "phase": "NOTICE",
            "notice_targets": len(targets),
            "sent": sum(1 for t in targets if t["sent"]),
        }
    items = {i["task_id"]: i["status"] for i in data["items"]}
    return {
        "phase": data["phase"],
        "candidate_id": data["candidate"]["candidate_id"],
        "consultation_status": data["candidate"]["consultation_status"],
        "items": items,
        "open_items": sorted(t for t, s in items.items() if s in spec.OPEN_ITEM),
    }


# ── 서버 문구 (Pack 표시 이름과 현장 시각으로 만든다) ───────────


def clock(pack: LoadedPack, minute: int) -> str:
    """Horizon 원점 기준 분 → 현장 시각 "MM/DD HH:MM"."""
    origin = datetime.fromisoformat(pack.horizon_start_utc).astimezone(ZoneInfo(pack.timezone))
    return (origin + timedelta(minutes=minute)).strftime("%m/%d %H:%M")


def _work(pack: LoadedPack, task: Task) -> str:
    return f"{task.task_id}({pack.work_types[task.work_type].display_name})"


def change_request_text(pack: LoadedPack, task: Task, before: Any, after: Any) -> str:
    parts = []
    if before.start != after.start:
        parts.append(f"시작 {clock(pack, before.start)} → {clock(pack, after.start)}")
    if before.resource_id != after.resource_id:
        parts.append(f"자원 {before.resource_id} → {after.resource_id}")
    return (
        f"재계획 후보가 {_work(pack, task)} 작업을 바꿉니다: {', '.join(parts)}. "
        "이 변경을 수락하시겠습니까? 받아들일 수 없으면 이견과 사유를 적어 주세요."
    )


def movability_text(pack: LoadedPack, task: Task, values: list[str]) -> str:
    """사전 확인 질문의 서버 문구(동의 내용의 기준). Pack 표시 이름으로 서버가 만든다."""
    return (
        f"{_work(pack, task)} 작업에 {', '.join(values)}도 쓸 수 있게 허용하시겠습니까? "
        f"현재 요청 자원 {task.requested_resource_id}. "
        "허용하면 재계획이 이 자원을 대안으로 검토합니다."
    )


def notice_text(
    tx: sqlite3.Connection, pack: LoadedPack, task_ids: list[str], reasons: list[dict[str, Any]]
) -> str:
    plan = get_current_plan(tx, pack.site_id)
    placed = {a.task_id: a for a in (plan.assignments if plan else ())}
    tasks = {t.task_id: t for t in list_current_tasks(tx, pack.site_id, pack)}
    rules = {r.rule_id: r for r in pack.rules}
    lines = [f"계획 R{plan.plan_revision if plan else 0}이 확정되었습니다."]
    for tid in task_ids:
        a, t = placed.get(tid), tasks[tid]
        if a is None:
            continue
        where = f"{t.zone_id} 구역" + (f", 자원 {a.resource_id}" if a.resource_id else "")
        line = f"{_work(pack, t)} {clock(pack, a.start)}–{clock(pack, a.end)[-5:]} {where}"
        for r in reasons:
            if r["task_id"] != tid:
                continue
            if r["kind"] == "CHANGED":
                line += " (변경됨)"
            elif r["kind"] == "SAFETY_LINK":
                other = placed.get(r["with_task_id"])
                rule = rules[r["rule_id"]]
                gap = f", 간격 {rule.min_gap}분 이상" if rule.min_gap else ""
                if other is not None:
                    line += (
                        f" — 안전 규칙 '{rule.display_name}'로 {_work(pack, tasks[other.task_id])}"
                        f" {clock(pack, other.start)}–{clock(pack, other.end)[-5:]}와 겹치지 않게"
                        f" 유지{gap}"
                    )
        lines.append(line)
    return " ".join(lines)
