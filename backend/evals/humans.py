"""사람 역할. 요청을 DB 모양으로 분류하고(질문 문장은 해석하지 않는다) 시나리오 규칙대로 명령을 낸다.

모든 사람 행동은 commands/의 명령 함수를 하네스 멱등 키로 부른다. Supervisor 공통 규칙(WAIVE 없음,
PASS ∧ 협의 COMPLETE만 승인, 진실과 같은 사실 수정만 확인하고 FACT_CONFIRMED 해제, 막힌 후보에 제약 없는
거절 1회)은 시나리오가 아니라 여기에 있다.
"""

import sqlite3
import uuid
from typing import Any

from app.commands.approval import (
    ApproveRequest,
    RejectRequest,
    approve_and_commit,
    reject_candidate,
)
from app.commands.events import EventReport, HoldRelease, receive_event, release_hold_command
from app.commands.messages import ReplyRequest, reply_message
from app.commands.service import CommandOutcome
from app.packs.loader import LoadedPack
from app.store import db
from app.store.repos._rows import rows
from app.store.repos.cases import supervisor_actor
from app.store.repos.consultations import (
    consultation_view,
    get_consultation_items,
    list_review_queue,
)
from app.store.repos.messages import get_proposal, list_fact_updates
from app.store.repos.records import list_validations
from app.store.repos.runs import get_step
from app.store.repos.site import get_site

NEUTRAL = "앞에서 말씀드린 게 전부예요."
REJECT_COMMENT = "담당자 이견"
# 값 확인에서 필드 이름 → 작업 값 키
FIELD_KEYS = {
    "work_type": ("work_type",),
    "zone_id": ("zone_id",),
    "duration": ("duration",),
    "resource": ("required_resource_type", "requested_resource_id"),
    "window": ("earliest_start", "latest_start", "latest_end"),
}
PROPOSAL_KINDS = {
    "FEEDBACK_CONSTRAINT": "CONSTRAINT_DRAFT",
    "MOVABILITY": "MOVABILITY",
    "FACT_UPDATE": "FACT_UPDATE",
}


# ── 순수 함수 ──────────────────────────────────────────────────


def nth_answer(answers: list[str], n: int) -> str | None:
    """이 사람에게 온 n번째(0부터) 질문의 답. 바닥나면 None."""
    return answers[n] if n < len(answers) else None


def field_answers(fields: dict[str, list[str]], field_ids: list[str], asked: dict[str, int]) -> str:
    """필드별 n번째 답을 물은 필드 순서대로 이어 붙인다. 줄 답이 없으면 중립 답."""
    parts = [a for f in field_ids if (a := nth_answer(fields.get(f, []), asked.get(f, 0)))]
    return " ".join(parts) if parts else NEUTRAL


def wrong_fields(values: dict[str, Any], truth: dict[str, Any]) -> list[str]:
    """진실 범위와 다른 필드. 진실 값이 [lo, hi]면 범위, 아니면 같은 값."""
    out = []
    for name, keys in FIELD_KEYS.items():
        for key in keys:
            want, got = truth[key], values.get(key)
            if isinstance(want, list):
                ok = isinstance(got, int) and want[0] <= got <= want[1]
            else:
                ok = got == want
            if not ok and name not in out:
                out.append(name)
    return out


def values_decision(
    values: dict[str, Any], truth: dict[str, Any], lines: dict[str, str]
) -> tuple[str, str]:
    """값 확인: 진실 범위면 ACCEPT, 아니면 DECLINE과 틀린 필드마다 한 줄."""
    wrong = wrong_fields(values, truth)
    if not wrong:
        return "ACCEPT", ""
    return "DECLINE", " ".join(lines.get(f, f"{f} 값이 달라요.") for f in wrong)


def matches(when: dict[str, Any], req: dict[str, Any]) -> bool:
    if when.get("status", "OPEN") != req["status"]:
        return False
    for key in ("kind", "to", "task"):
        if key in when and when[key] != req.get(key):
            return False
    if "values" in when and not set(when["values"]) <= set(req.get("values") or []):
        return False
    return "axes_only" not in when or set(req.get("axes") or []) <= set(when["axes_only"])


def find_rule(rules: list[dict[str, Any]], req: dict[str, Any], stage: int) -> dict | None:
    """위에서부터 첫 일치. from_stage가 지금 단계보다 크면 아직 쓰지 않는다."""
    for rule in rules:
        if stage >= rule.get("from_stage", 0) and matches(rule["when"], req):
            return rule
    return None


# ── 요청 분류 (DB 모양) ────────────────────────────────────────


def classify(conn: sqlite3.Connection, pack: LoadedPack, m: dict[str, Any]) -> dict[str, Any]:
    """메시지 유형·제안 유형·수신자·작업·값으로 분류한다."""
    req: dict[str, Any] = {
        "message_id": m["message_id"],
        "to": m["to_actor_id"],
        "status": m["status"],
        "run_id": m["run_id"],
        "step_no": m["step_no"],
        "task": None,
    }
    proposal = get_proposal(conn, pack.site_id, m["proposal_id"]) if m["proposal_id"] else None
    step = get_step(conn, m["run_id"], m["step_no"]) or {}
    if proposal is not None:
        payload = proposal["payload"]
        req.update(
            kind=PROPOSAL_KINDS[proposal["type"]],
            task=proposal["target_task_id"],
            values=payload.get("allowed_values"),
            axes=payload.get("axes"),
            fact={"field": payload.get("field"), "new_value": payload.get("new_value")},
        )
    elif m["type"] == "CHANGE_REQUEST":
        items = get_consultation_items(conn, pack.site_id, m["candidate_id"]) or ()
        item = next((i for i in items if i.change_hash == m["change_hash"]), None)
        req.update(
            kind="CHANGE_REQUEST",
            task=item.task_id if item else None,
            after=item.after.model_dump() if item else None,
            change_hash=m["change_hash"],
        )
    elif m["type"] == "QUESTION":
        args = (step.get("action") or {}).get("args") or {}
        req.update(kind="FREE_TEXT", field_ids=args.get("field_ids"))
    elif m["type"] == "CONFIRMATION":
        req.update(kind="VALUES_CHECK", values=(step.get("tool_result") or {}).get("values"))
    else:
        req.update(kind=m["type"])
    return req


def answerable(conn: sqlite3.Connection, site_id: str) -> list[dict[str, Any]]:
    """답을 받는 메시지(통지 제외), 만든 순서."""
    return rows(
        conn,
        "SELECT rowid AS seq, * FROM message WHERE site_id = ? AND type <> 'NOTICE' ORDER BY rowid",
        (site_id,),
    )


def marks(conn: sqlite3.Connection) -> dict[str, int]:
    """지금까지 만든 행의 끝. 이 뒤에 생긴 것을 판정기가 가린다."""
    return {
        t: conn.execute(f"SELECT COALESCE(MAX(rowid), 0) FROM {t}").fetchone()[0]
        for t in ("message", "proposal", "candidate")
    }


# ── 실행 ───────────────────────────────────────────────────────


class Humans:
    def __init__(self, pack: LoadedPack, data: dict[str, Any], stage: int):
        self.pack = pack
        self.data = data
        self.stage = stage
        self.humans = data.get("humans", {})
        self.truth = data.get("truth", {})
        self.keys: set[str] = set()
        self.log: list[dict[str, Any]] = []
        self.unscripted: dict[str, dict[str, Any]] = {}
        self.asked: dict[str, int] = {}
        self.field_asked: dict[str, dict[str, int]] = {}
        self.reasks = {"in_text": 0, "received": 0}
        self.events = list(data.get("events", []))
        self.tried: set[str] = set()
        self.rejections = 0

    def key(self) -> str:
        key = f"eval:{uuid.uuid4().hex}"
        self.keys.add(key)
        return key

    def record(self, actor: str, action: str, out: CommandOutcome, **extra: Any) -> None:
        with db.read() as conn:
            end = marks(conn)
        self.log.append(
            {
                "n": len(self.log) + 1,
                "actor": actor,
                "action": action,
                "status": out.status,
                "reason_codes": list(out.reason_codes),
                "marks": end,
                **extra,
            }
        )

    def act(self) -> bool:
        """정해진 순서로 규칙을 평가해 사람 행동 하나를 한다. 할 것이 없으면 False."""
        steps = (self._event, self._late, self._request, self._release, self._approve, self._reject)
        return any(step() for step in steps)

    # ── 신고 ──

    def _event(self) -> bool:
        if not self.events:
            return False
        e = self.events.pop(0)
        body = EventReport(
            source_event_id=f"eval-{uuid.uuid4().hex[:8]}",
            event_type=e["event_type"],
            text=e["text"],
        )
        out = receive_event(self.pack, e["actor"], self.key(), body)
        self.record(e["actor"], "REPORT_EVENT", out, text=e["text"])
        return True

    # ── 요청에 답하기 ──

    def _reply(self, req: dict[str, Any], decision: str, comment: str, late: bool) -> None:
        body = ReplyRequest(message_id=req["message_id"], decision=decision, comment=comment)
        out = reply_message(self.pack, req["to"], self.key(), body)
        self.record(
            req["to"],
            "REPLY",
            out,
            kind=req["kind"],
            task=req["task"],
            message_id=req["message_id"],
            decision=decision,
            comment=comment,
            late=late or bool(out.result_refs.get("late")),
        )

    def _late(self) -> bool:
        """취소된 요청에 늦은 답을 보내는 규칙(when.status CANCELLED). 요청 하나에 한 번."""
        with db.read() as conn:
            found = [
                classify(conn, self.pack, m)
                for m in answerable(conn, self.pack.site_id)
                if m["status"] == "CANCELLED" and m["message_id"] not in self.tried
            ]
        for req in found:
            rule = find_rule(self.humans.get("rules", []), req, self.stage)
            if rule is not None:
                self.tried.add(req["message_id"])
                self._reply(req, rule["do"]["decision"], rule["do"].get("comment", ""), True)
                return True
        return False

    def _free_text(self, req: dict[str, Any]) -> str:
        actor = req["to"]
        cfg = self.humans.get("free_text", {}).get(actor, {})
        n = self.asked.get(actor, 0)
        self.asked[actor] = n + 1
        fields = cfg.get("fields")
        if fields and req.get("field_ids"):
            counts = self.field_asked.setdefault(actor, {})
            answer = field_answers(fields, req["field_ids"], counts)
            for f in req["field_ids"]:
                if f in self.truth.get("in_text", []):
                    self.reasks["in_text"] += 1
                if counts.get(f, 0) >= len(fields.get(f, [])):
                    self.reasks["received"] += 1
                counts[f] = counts.get(f, 0) + 1
            return answer
        return nth_answer(cfg.get("answers", []), n) or NEUTRAL

    def _decide(self, req: dict[str, Any]) -> tuple[str, str] | None:
        kind = req["kind"]
        if kind == "FREE_TEXT":
            return "ANSWER", self._free_text(req)
        if kind == "VALUES_CHECK":
            cfg = self.humans.get("values_check", {}).get(req["to"])
            if cfg is None or "task" not in self.truth:
                return None
            return values_decision(req["values"] or {}, self.truth["task"], cfg["decline_lines"])
        if kind == "FACT_UPDATE":
            # Supervisor 공통 규칙: 진실과 같을 때만 확인한다
            fact = self.truth.get("fact")
            same = fact is not None and (req["task"], req["fact"]["field"]) == (
                fact["task"],
                fact["field"],
            )
            if same and req["fact"]["new_value"] == fact["new_value"]:
                return "ACCEPT", ""
            return "DECLINE", "신고 내용과 다릅니다."
        rule = find_rule(self.humans.get("rules", []), req, self.stage)
        return None if rule is None else (rule["do"]["decision"], rule["do"].get("comment", ""))

    def _request(self) -> bool:
        with db.read() as conn:
            found = [
                classify(conn, self.pack, m)
                for m in answerable(conn, self.pack.site_id)
                if m["status"] == "OPEN"
            ]
        for req in found:
            if req["message_id"] in self.unscripted:
                continue
            plan = self._decide(req)
            if plan is None:
                # 스크립트에 없는 구조화 요청: 답하지 않고 기록만 한다
                self.unscripted[req["message_id"]] = {
                    k: req.get(k) for k in ("kind", "to", "task", "values", "axes")
                }
                continue
            self._reply(req, plan[0], plan[1], False)
            return True
        return False

    # ── Supervisor 공통 규칙 ──

    def _supervisor(self, conn: sqlite3.Connection) -> str:
        return supervisor_actor(conn, self.pack).actor_id

    def _release(self) -> bool:
        site_id = self.pack.site_id
        with db.read() as conn:
            holds = rows(
                conn, "SELECT hold_id, event_id FROM hold WHERE status = 'ACTIVE' ORDER BY rowid"
            )
            ready = [
                h["hold_id"]
                for h in holds
                if h["hold_id"] not in self.tried
                and any(
                    p["status"] == "CONFIRMED"
                    for p in list_fact_updates(conn, site_id, event_id=h["event_id"])
                )
            ]
            if not ready:
                return False
            site = get_site(conn, site_id)
            actor = self._supervisor(conn)
        assert site is not None
        self.tried.add(ready[0])
        body = HoldRelease(
            hold_id=ready[0],
            resolution="FACT_CONFIRMED",
            expected_context_version=site.context_version,
        )
        out = release_hold_command(self.pack, actor, self.key(), body)
        self.record(actor, "RELEASE_HOLD", out, hold_id=ready[0], resolution="FACT_CONFIRMED")
        return True

    def _queue(self, conn: sqlite3.Connection, items_status: str) -> list[tuple[str, str]]:
        """검토 대기 후보 중 PASS이고 협의 상태가 items_status인 것. (candidate_id, validation_id)."""
        out = []
        for cid in list_review_queue(conn, self.pack.site_id):
            passes = [
                v for v in list_validations(conn, self.pack.site_id, cid) if v.status == "PASS"
            ]
            view = consultation_view(conn, self.pack.site_id, cid)
            if not passes or view is None or view.items_status != items_status:
                continue
            if "WAIVED" not in view.item_status.values():
                out.append((cid, passes[-1].validation_id))
        return out

    def _approve(self) -> bool:
        site_id = self.pack.site_id
        with db.read() as conn:
            if conn.execute("SELECT 1 FROM hold WHERE status = 'ACTIVE'").fetchone():
                return False
            ready = [c for c in self._queue(conn, "COMPLETE") if c[0] not in self.tried]
            if not ready:
                return False
            site = get_site(conn, site_id)
            actor = self._supervisor(conn)
        assert site is not None
        cid, vid = ready[0]
        self.tried.add(cid)
        body = ApproveRequest(
            candidate_id=cid, validation_id=vid, expected_context_version=site.context_version
        )
        out = approve_and_commit(self.pack, actor, self.key(), body)
        self.record(actor, "APPROVE", out, candidate_id=cid)
        return True

    def _reject(self) -> bool:
        """후보가 막혔고(BLOCKED) 열린 Coordination Run이 없으면 제약 없는 거절 1회."""
        if self.rejections:
            return False
        with db.read() as conn:
            blocked = self._queue(conn, "BLOCKED")
            open_coord = conn.execute(
                "SELECT 1 FROM agent_run WHERE agent_type = 'COORDINATION'"
                " AND status IN ('RUNNING', 'WAITING_HUMAN')"
            ).fetchone()
            if not blocked or open_coord:
                return False
            actor = self._supervisor(conn)
        cid, vid = blocked[0]
        self.rejections += 1
        body = RejectRequest(
            candidate_id=cid, validation_id=vid, reason_code="OTHER", comment=REJECT_COMMENT
        )
        out = reject_candidate(self.pack, actor, self.key(), body)
        self.record(actor, "REJECT", out, candidate_id=cid, comment=REJECT_COMMENT)
        return True
