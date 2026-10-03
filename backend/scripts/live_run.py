"""실제 모델로 시연 경로를 돌린다. 사건 → 메인 → 전문 Agent, 사람 역할은 스크립트가 한다.

    cd backend && uv run python -m scripts.live_run [--runs 1] [--request A|N1,N2,...] [--raw]
    cd backend && uv run python -m scripts.live_run --path B|B-decline|coord|event|intake [--ambiguous]

- .env의 OPENAI_API_KEY·OPENAI_MODEL이 없으면 바로 종료한다(API를 부르지 않음).
- run마다 임시 DB를 만든다. 개발 DB(data/safe_orch.db)는 건드리지 않는다.
- 사건 → 메인 자동 시작(MAIN_AUTO_START)과 현장의 지금(SITE_NOW = Pack의 Horizon 원점)은 경로가 명시한다
  (.env·기본값과 무관). 기록 agent_flags·site_now에 남긴다.
- 루프: idle까지 실행 → 사람 행동(경로가 정한 답) → 반복. 할 사람 행동이 없으면 끝난다.
- 결과: 콘솔 요약 + data/live_runs/<UTC시각>.jsonl (gitignore). --raw일 때만 prompt·응답 원문을 넣는다.
  기록 main에 메인의 step·전문 Agent 호출 수와 고른 행동 순서, 사전 확인에 넘긴 need의 출처(ask_sources:
  MODEL_PATH 재계획 Agent가 엮은 길, SERVER_OPENERS 서버가 붙인 열 수 있는 것)가 있다.
- 공통 성공 기준(모든 경로): 권한 위반 0 ∧ 같은 요청 두 번 0 ∧ Budget 안(BUDGET_EXHAUSTED Run 없음) ∧ 오류 Run
  없음. 메인 Run에도 적용한다. 가드 거절(ACTION_NOT_AVAILABLE·MALFORMED·SKILL_NOT_OPEN 등)은 사유별로 기록만
  한다(guard_rejections, EV-02). 순서에 기댄 항목은 성공 기준에 넣지 않는다 (AG-01).
- 경로 6개(스위치가 하나가 되어 "Coordination 끔" 경로가 없다: 옛 B --coord는 B, event --coord는 event다)
  - A [--request]: 요청 폼 → Supervisor가 동의 대기 항목을 수용(WAIVE, comment "live run 자동 수용")하고 승인.
    성공 = Replanning DONE ∧ 후보 PASS ∧ 확정 ∧ 통지 전원 ∧ 메인 CLOSE. 쉼표로 여러 요청이면 같은 DB에서
    하나씩 확정한다. 기대값이 모든 범위 INFEASIBLE인 요청(N5)은 "후보 없음 ∧ Replanning BLOCKED ∧ 메인이 자기
    행동으로 ESCALATE"가 성공이다. 기대 결과(verify_demo_values.py와 같은 출처)와 같은지는 matches_expected로
    따로 남긴다.
  - B: 요청 A. Alpha PASS 뒤 Supervisor가 demo_rejections[0]으로 거절 → 메인이 재계획을 다시 부름(막힘 + 길)
    → 메인이 Coordination 사전 확인을 부름 → 담당자 수락 → 재계획(대체 자원) → Beta. 성공 = Alpha PASS ∧ 거절 뒤
    재호출 ∧ 담당자 질문이 Coordination Run에서만 나옴 ∧ Beta PASS ∧ 협의 완료 ∧ R1 ∧ 통지 전원 ∧ 메인 CLOSE.
    막힌 결과에 OWNER_CONSENT 길이 있었는지(blocked_with_owner_consent_path)는 기록만 한다.
  - B-decline: 같은 흐름에서 사전 확인 질문에 DECLINE. 성공 = Alpha PASS ∧ DECLINE 적용 ∧ 거절한 값 재질문 0 ∧
    담당자 질문이 Coordination Run에서만 나옴 ∧ Replanning BLOCKED ∧ 메인이 자기 행동으로 ESCALATE.
  - coord: 요청 A. 담당자가 변경 요청에 이견(demo_rejections[0].comment) → 제약 초안 확정 → 재계획(막힘 + 길) →
    사전 확인(수락) → Beta. 성공 = Alpha PASS ∧ 변경 요청 ∧ 제약 source PROPOSAL ∧ 담당자 질문이 Coordination
    Run에서만 나옴 ∧ Beta PASS ∧ R1 ∧ 통지 전원 ∧ 메인 CLOSE.
  - event [--ambiguous]: R1(기본안 B, Beta 확정)까지는 스크립트 응답으로 준비하고(LLM 없음, 메인 없음), Reporter가
    demo_events[0](--ambiguous면 [1])을 신고한다. Supervisor가 사실 수정안을 확정하고 FACT_CONFIRMED로 해제,
    담당자가 변경 요청을 수락, Supervisor 승인. 성공 = 사실 수정 CONFIRMED ∧ Hold FACT_CONFIRMED ∧ Hold 중 재계획
    Run 0 ∧ 후보 PASS ∧ R2 ∧ 통지 전원 ∧ 메인 CLOSE.
  - intake [--ambiguous]: 요청 A를 demo_intakes[0](명확) 또는 [1](모호) 문장으로 자연어 요청한다. Intake는 메인
    밖에서 돌고, 완료가 "작업 준비됨" 사건이 되어 메인을 띄운다. 성공 = Intake SUCCEEDED ∧ 작업 값 = new_task ∧
    fields 모두 CONFIRMED·source message: ∧ Consent가 폼과 같음 ∧ 메인 시작 ∧ Alpha PASS (∧ --ambiguous면 확인
    질문 1회 이상). 첫 후보의 검증 통과까지만 본다(승인하지 않는다).
- 자유 텍스트 답(신고자·요청자): 첫 질문에는 scenario 답 문장, 두 번째 질문부터는 "앞에서 답한 것이
  전부입니다: <답>. 나머지는 처음 문장 그대로입니다: <원문>". 질문별 답 종류(FIRST·REPEAT)를 answer_kinds와
  events[].answer_kind에 남긴다.
"""

import argparse
import json
import os
import sys
import tempfile
import time
import uuid
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

from app.agents import casefacts, skills
from app.agents.llm import model_settings, openai_model
from app.agents.registry import BINDINGS
from app.commands.approval import (
    ApproveRequest,
    RejectRequest,
    WaiveRequest,
    approve_and_commit,
    reject_candidate,
    waive,
)
from app.commands.events import EventReport, HoldRelease, receive_event, release_hold_command
from app.commands.intake import IntakeRequest, submit_intake
from app.commands.messages import ReplyRequest, reply_message
from app.commands.task_request import TaskRequestForm, submit_task_request
from app.config import REPO_ROOT, Settings, get_settings
from app.coordinator.dispatcher import run_until_idle
from app.domain.calendar import parse_site_time, work_delay
from app.packs.loader import load_pack, pack_dir
from app.store import db
from app.store.repos.cases import supervisor_actor
from app.store.repos.consultations import consultation_view, list_review_queue
from app.store.repos.decisions import list_constraints
from app.store.repos.dispatch import register_job
from app.store.repos.events import get_hold, list_active_holds
from app.store.repos.messages import list_fact_updates
from app.store.repos.records import get_candidate, get_snapshot, list_validations
from app.store.repos.runs import get_run, list_steps
from app.store.repos.seed import seed_pack
from app.store.repos.site import get_site
from app.store.repos.tasks import list_current_tasks
from evals import judge
from scripts import verify_demo_values as verify

OUT_DIR = REPO_ROOT / "data" / "live_runs"
RUN_DEADLINE_S = 300
MAX_RUNS = 10
PATHS = ("A", "B", "B-decline", "coord", "event", "intake")
MAX_HUMAN_TURNS = 6  # 사람 응답 반복 상한의 단위(무한 반복 방지)


class _Recorder:
    """모델 호출별 지연·토큰(+원문)을 모은다. 운영 코드가 아니라 이 스크립트에만 있다."""

    def __init__(self, deadline: float, raw: bool):
        self.deadline = deadline
        self.raw = raw
        self.calls: list[dict[str, Any]] = []


class _RecordingRunnable:
    def __init__(self, inner: Any, rec: _Recorder):
        self.inner = inner
        self.rec = rec

    def invoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        if time.monotonic() > self.rec.deadline:
            raise TimeoutError(f"live run deadline {RUN_DEADLINE_S}s")
        started = time.perf_counter()
        entry: dict[str, Any] = {"ok": False}
        try:
            message = self.inner.invoke(messages)
            usage = message.usage_metadata or {}
            entry.update(
                ok=True,
                tokens_in=usage.get("input_tokens", 0),
                tokens_out=usage.get("output_tokens", 0),
            )
            if self.rec.raw:
                entry["raw"] = {
                    "messages": [m.content for m in messages],
                    "response": message.model_dump(mode="json"),
                }
            return message
        except Exception as e:
            entry["error"] = type(e).__name__
            raise
        finally:
            entry["ms"] = round((time.perf_counter() - started) * 1000)
            self.rec.calls.append(entry)


class _RecordingModel:
    def __init__(self, inner: Any, rec: _Recorder):
        self.inner = inner
        self.rec = rec
        self.model_name = getattr(inner, "model_name", None)

    def bind_tools(self, tools: Sequence[dict[str, Any]], **kwargs: Any) -> _RecordingRunnable:
        return _RecordingRunnable(self.inner.bind_tools(tools, **kwargs), self.rec)


def _human_answer(asked: int, first: str, original: str) -> tuple[str, str]:
    """사람 역할의 자유 텍스트 답. (답 종류, 답 문장).

    첫 질문에는 시나리오 답(FIRST), 두 번째 질문부터는 더 아는 것이 없다는 답(REPEAT)을 보낸다.
    """
    if asked == 0:
        return "FIRST", first
    return (
        "REPEAT",
        f"앞에서 답한 것이 전부입니다: {first.rstrip('.')}. 나머지는 처음 문장 그대로입니다: {original}",
    )


def _key() -> str:
    return uuid.uuid4().hex


def _minute(pack: Any, text: Any) -> int | None:
    """Action의 시각 인자(현장 날짜·시각 문자열)를 분으로. 바꿀 수 없으면 None."""
    try:
        return parse_site_time(
            str(text), pack.horizon_start_utc, pack.timezone, pack.horizon_minutes
        )
    except ValueError:
        return None


def _guard_counts(guards: list[str | None]) -> dict[str, int]:
    """가드 거절 수(사유 코드별). 기록만 하고 성공 기준에는 넣지 않는다 (EV-02)."""
    return dict(Counter(g for g in guards if g))


def _violations(pack: Any) -> dict[str, int]:
    """성공 기준에 넣는 위반: Agent가 낸 사람 권한 명령, 같은 요청 두 번(구조 때문에 생긴 재질문 제외)."""
    with db.read() as conn:
        authority = conn.execute(
            "SELECT COUNT(*) FROM command_result WHERE actor_id LIKE 'run:%'"
            " AND command_type NOT LIKE 'AGENT:%'"
        ).fetchone()[0]
        repeat = judge.must_no_repeat(conn, pack, [], 0, 3)["violations"]
    return {"authority_violations": authority, "repeat_violations": len(repeat)}


def _no_violations(criteria: dict[str, Any]) -> bool:
    return criteria["authority_violations"] == 0 and criteria["repeat_violations"] == 0


def _site_now(pack_name: str) -> str:
    """현장의 지금 = Pack의 Horizon 원점. 경로가 명시해 .env와 무관하게 같은 조건으로 잰다 (ST-17)."""
    return load_pack(pack_dir(pack_name)).horizon_start_utc


def _set_env(values: dict[str, str]) -> dict[str, str | None]:
    """환경변수를 바꾸고 이전 값을 돌려준다. 설정 캐시를 비운다."""
    old = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    get_settings.cache_clear()
    return old


def _restore_env(old: dict[str, str | None]) -> None:
    for name, value in old.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    get_settings.cache_clear()


def _step_rows(steps: list[dict[str, Any]], calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """AgentStep 요약. 모델 호출 기록은 step의 llm_attempts만큼 순서대로 나눠 붙인다."""
    out, i = [], 0
    for s in steps:
        n = s["llm_attempts"] or (1 if s["status"] == "ABORTED" else 0)
        mine, i = calls[i : i + n], i + n
        action = s["action"] or {}
        out.append(
            {
                "step_no": s["step_no"],
                "status": s["status"],
                "action": action.get("name"),
                "level": (action.get("args") or {}).get("level"),
                "decision_summary": s["decision_summary"],
                "result_kind": s["result_kind"],
                "guard": s["guard"],
                "stage1_status": ((s["tool_result"] or {}).get("stage1") or {}).get("status"),
                "llm_attempts": s["llm_attempts"],
                "llm_ms": sum(c["ms"] for c in mine),
                "tokens_in": sum(c.get("tokens_in", 0) for c in mine),
                "tokens_out": sum(c.get("tokens_out", 0) for c in mine),
                "model_id": s["model_id"],
                **({"raw": [c.get("raw") for c in mine]} if any("raw" in c for c in mine) else {}),
            }
        )
    return out


def request_names(pack: Any) -> list[str]:
    """scenario.yaml의 시연 요청 이름: new_task(A) + demo_requests(N1–N5). 스크립트에 값을 두지 않는다."""
    return [pack.new_task.task_id, *(d.task_id for d in pack.demo_requests)]


def _form_and_requester(pack: Any, name: str) -> tuple[TaskRequestForm, str]:
    if name == pack.new_task.task_id:
        nt = pack.new_task
        data = nt.model_dump(exclude={"requested", "unit_id", "owner_actor_id", "movable"})
        return TaskRequestForm(**data), nt.owner_actor_id
    d = next(x for x in pack.demo_requests if x.task_id == name)
    return TaskRequestForm(**d.model_dump(exclude={"label", "requester"})), d.requester


def _actual(conn: Any, site_id: str, cid: str, step: dict[str, Any]) -> dict[str, Any]:
    """후보의 실제 결과: 범위, 변경 수, 달력/근무 지연, 바뀐 작업의 새 시작·자원."""
    cand = get_candidate(conn, site_id, cid)
    snapshot = get_snapshot(conn, cand.snapshot_id)
    facts = snapshot.facts()
    base = facts.base_assignments()
    moved = {
        a.task_id: [a.start, a.resource_id]
        for a in cand.assignments
        if (a.start, a.resource_id) != (base[a.task_id].start, base[a.task_id].resource_id)
    }
    tr = step["tool_result"] or {}
    return {
        "level": tr.get("scope_level"),
        "status": (tr.get("stage1") or {}).get("status"),
        "changed": (tr.get("stage1") or {}).get("changed"),
        "delay": (tr.get("stage2") or {}).get("delay"),
        "work_delay": sum(
            work_delay(base[t].start, s, facts.work_intervals) for t, (s, _) in moved.items()
        ),
        "moved": moved,
    }


def _matches(expected: dict[str, Any], actual: dict[str, Any] | None, escalate: bool) -> bool:
    if escalate:
        return actual is None
    if actual is None:
        return False
    exp = expected.get(actual["level"] or "")
    keys = ("status", "changed", "delay", "work_delay", "moved")
    return exp is not None and all(exp[k] == actual[k] for k in keys)


# ── 경로 실행: 사건 → 메인 → 전문 Agent. 사람 역할은 스크립트가 한다 ─────────

ANSWERABLE = ("QUESTION", "CHANGE_REQUEST", "CONFIRMATION")
WAIVE_PATHS = ("A", "intake")  # 담당자가 답하지 않고 Supervisor가 동의 대기 항목을 수용하는 경로
REJECT_PATHS = ("B", "B-decline")  # Supervisor가 첫 후보를 구조화 거절하는 경로


def _env(pack_name: str, main_auto_start: bool = True) -> dict[str, str]:
    """사건 → 메인 자동 시작과 현장의 지금을 경로가 명시한다. .env·기본값과 무관하게 같은 조건으로 잰다."""
    return {
        "MAIN_AUTO_START": "true" if main_auto_start else "false",
        "SITE_NOW": _site_now(pack_name),
    }


class _Humans:
    """사람 역할. 경로가 정한 답만 한다(값은 scenario.yaml에서 읽는다)."""

    def __init__(self, pack: Any, path: str, ambiguous: bool = False):
        self.pack, self.path = pack, path
        self.events: list[dict[str, Any]] = []
        self.answer_kinds: list[str] = []
        self.rejected: str | None = None  # Supervisor가 거절한 후보
        self.objected = False
        demos = pack.demo_intakes if path == "intake" else pack.demo_events
        demo = demos[1 if ambiguous else 0]
        self.original = demo.text
        # 명확한 문장에 질문이 오면 문장 그대로 답한다(값은 문장에 다 있다)
        self.answer = getattr(demo, "answer", "") or f"처음 문장에 적은 대로입니다: {demo.text}"

    def act(self) -> bool:
        """지금 할 수 있는 사람 행동을 한 종류 한다. 하나라도 했으면 True."""
        return self._answer_messages() or self._release_holds() or self._decide_candidates()

    def _reply(self, m: dict[str, Any], decision: str, comment: str = "", **extra: Any) -> None:
        out = reply_message(
            self.pack,
            m["to_actor_id"],
            _key(),
            ReplyRequest(message_id=m["message_id"], decision=decision, comment=comment),
        )
        self.events.append(
            {
                "reply": decision,
                "type": m["type"],
                "to": m["to_actor_id"],
                "status": out.status,
                "reason_codes": list(out.reason_codes),
                **extra,
            }
        )

    def _answer_messages(self) -> bool:
        with db.read() as conn:
            opened = [
                dict(zip(("message_id", "type", "to_actor_id", "proposal_id"), r, strict=True))
                for r in conn.execute(
                    "SELECT message_id, type, to_actor_id, proposal_id FROM message"
                    " WHERE status = 'OPEN' AND type IN (?, ?, ?) ORDER BY rowid",
                    ANSWERABLE,
                )
            ]
        acted = False
        for m in opened:
            if m["type"] == "CHANGE_REQUEST":
                if self.path == "coord" and not self.objected:
                    # 기본안 A: 담당자가 이견을 낸다(사유는 시연값)
                    self.objected = True
                    self._reply(m, "DECLINE", self.pack.demo_rejections[0].comment)
                elif self.path in WAIVE_PATHS or (
                    self.path in REJECT_PATHS and self.rejected is None
                ):
                    continue  # 담당자는 답하지 않는다. Supervisor가 수용하거나 거절한다
                else:
                    self._reply(m, "ACCEPT", "live run 자동 수락")
            elif m["type"] == "QUESTION" and m["proposal_id"] is None:
                # 자유 텍스트 질문(요청자·신고자): 첫 질문은 시연 답, 그 뒤는 더 아는 것이 없다는 답
                kind, text = _human_answer(len(self.answer_kinds), self.answer, self.original)
                self.answer_kinds.append(kind)
                self._reply(m, "ANSWER", text, answer_kind=kind)
            elif m["type"] == "QUESTION" and self.path == "B-decline":
                self._reply(m, "DECLINE", "live run 자동 거절")
            else:
                # 담당자 자원 허용, 제약 초안·사실 수정·값 확인
                self._reply(m, "ACCEPT", "live run 자동 수락")
            acted = True
        return acted

    def _release_holds(self) -> bool:
        """사실 수정이 확정된 신고의 Hold를 FACT_CONFIRMED로 푼다."""
        site_id = self.pack.site_id
        with db.read() as conn:
            ctx = get_site(conn, site_id).context_version
            ready = [
                h.hold_id
                for h in list_active_holds(conn, site_id)
                if any(
                    p["status"] == "CONFIRMED"
                    for p in list_fact_updates(
                        conn, site_id, event_id=get_hold(conn, site_id, h.hold_id)["event_id"]
                    )
                )
            ]
        for hold_id in ready[:1]:
            out = release_hold_command(
                self.pack,
                "supervisor",
                _key(),
                HoldRelease(
                    hold_id=hold_id, resolution="FACT_CONFIRMED", expected_context_version=ctx
                ),
            )
            self.events.append({"release": hold_id, "status": out.status})
        return bool(ready)

    def _decide_candidates(self) -> bool:
        """검토 대기 후보에 대한 Supervisor 결정: 거절(기본안 B), 동의 대기 수용, 승인."""
        pack, site_id = self.pack, self.pack.site_id
        with db.read() as conn:
            if list_active_holds(conn, site_id):
                return False
            queue = list_review_queue(conn, site_id)
            if not queue:
                return False
            cid = queue[-1]
            view = consultation_view(conn, site_id, cid)
            validation = _pass_validation(conn, site_id, cid)
            ctx = get_site(conn, site_id).context_version
        if view is None or validation is None:
            return False
        if self.path in REJECT_PATHS and self.rejected is None:
            x = pack.demo_rejections[0]
            body = RejectRequest(
                candidate_id=cid,
                validation_id=validation.validation_id,
                reason_code=x.reason_code,
                target_task_ids=x.target_task_ids,
                axes=x.axes,
                comment=x.comment,
            )
            out = reject_candidate(pack, "supervisor", _key(), body)
            self.rejected = cid
            self.events.append({"reject": cid, "status": out.status})
            return True
        pending = tuple(t for t, st in view.item_status.items() if st == "PENDING")
        if pending and self.path in WAIVE_PATHS:
            body = WaiveRequest(candidate_id=cid, task_ids=pending, comment="live run 자동 수용")
            out = waive(pack, "supervisor", _key(), body)
            self.events.append({"waive": list(pending), "status": out.status})
            return True
        if view.items_status != "COMPLETE" or self.path == "intake":
            return False  # 접수 경로는 첫 후보의 검증 통과까지만 본다
        out = approve_and_commit(
            pack,
            "supervisor",
            _key(),
            ApproveRequest(
                candidate_id=cid,
                validation_id=validation.validation_id,
                expected_context_version=ctx,
            ),
        )
        self.events.append({"approve": cid, "status": out.status})
        return out.status == "APPLIED"


def _pass_validation(conn: Any, site_id: str, cid: str | None) -> Any:
    found = list_validations(conn, site_id, cid) if cid else []
    return next((v for v in found if v.status == "PASS"), None)


def _drive(pack: Any, humans: _Humans, factory: Any, deadline: float) -> None:
    """idle까지 실행 → 사람 행동 → 반복. 할 사람 행동이 없으면 끝난다."""
    for _ in range(MAX_HUMAN_TURNS * 4):
        run_until_idle(pack, model_factory=factory)
        if time.monotonic() > deadline or not humans.act():
            return
    run_until_idle(pack, model_factory=factory)


def _run_ids(conn: Any) -> set[str]:
    return {r[0] for r in conn.execute("SELECT run_id FROM agent_run")}


def _collect(pack: Any, rec: _Recorder, before: set[str]) -> dict[str, Any]:
    """이번 구간(before 뒤)에 생긴 Run·step·후보의 사실. 기록과 성공 기준이 같이 쓴다."""
    site_id = pack.site_id
    with db.read() as conn:
        runs = [
            get_run(conn, r[0])
            for r in conn.execute("SELECT run_id FROM agent_run ORDER BY rowid")
            if r[0] not in before
        ]
        ids = {r.run_id for r in runs}
        order = [
            (r[0], r[1])
            for r in conn.execute("SELECT run_id, step_no FROM agent_step ORDER BY rowid")
            if r[0] in ids
        ]
        by_run = {r.run_id: list_steps(conn, r.run_id) for r in runs}
        steps = [next(s for s in by_run[rid] if s["step_no"] == no) for rid, no in order]
        agent_of = {r.run_id: r.agent_type for r in runs}
        candidates = []
        for s in steps:
            cid = (s["tool_result"] or {}).get("candidate_id")
            if cid and (s["action"] or {}).get("name") != "RETURN_RESULT":
                view = consultation_view(conn, site_id, cid)
                candidates.append(
                    {
                        "candidate_id": cid,
                        "pass": _pass_validation(conn, site_id, cid) is not None,
                        "consultation": None if view is None else view.items_status,
                        "actual": _actual(conn, site_id, cid, s),
                    }
                )
        site = get_site(conn, site_id)
        targets = casefacts.notice_targets(conn, pack, site.plan_revision)
        sent = casefacts.notified_actors(conn, site_id, site.plan_revision)
        proposals = [
            {
                **dict(zip(("type", "status", "run_id", "payload"), r, strict=True)),
                "agent": agent_of[r[2]],
            }
            for r in conn.execute(
                "SELECT type, status, run_id, payload FROM proposal ORDER BY rowid"
            )
            if r[2] in ids
        ]
        requests = [
            dict(zip(("type", "to", "status", "run_id"), r, strict=True))
            for r in conn.execute(
                "SELECT type, to_actor_id, status, run_id FROM message ORDER BY rowid"
            )
            if r[3] in ids
        ]
        constraints = list_constraints(conn, site_id)
        holds = [
            dict(zip(("status", "resolution", "released"), r, strict=True))
            for r in conn.execute(
                "SELECT status, resolution, released_context_version FROM hold ORDER BY rowid"
            )
        ]
    rows = _step_rows(steps, rec.calls)
    for row, s in zip(rows, steps, strict=True):
        row["run_id"], row["agent"] = s["run_id"], agent_of[s["run_id"]]
        if row["action"] == "CALL_AGENT":
            row["call"] = {k: v for k, v in s["action"]["args"].items() if v and k in CALL_KEYS}
    mains = [r for r in runs if r.agent_type == "MAIN"]
    return {
        "runs": runs,
        "steps": steps,
        "rows": rows,
        "mains": mains,
        "replanning": [r for r in runs if r.agent_type == "REPLANNING"],
        "candidates": candidates,
        "plan_revision": site.plan_revision,
        "notices_sent": bool(targets) and all(t["actor_id"] in sent for t in targets),
        "proposals": proposals,
        "requests": requests,
        "constraints": constraints,
        "holds": holds,
    }


CALL_KEYS = ("agent", "phase", "acting_unit_id")


def _main_actions(facts: dict[str, Any]) -> list[str]:
    """메인이 고른 행동 순서: CALL_AGENT(REPLANNING·UA), WAIT, …"""
    out = []
    for row in facts["rows"]:
        if row["agent"] != "MAIN" or not row["action"]:
            continue
        call = row.get("call") or {}
        detail = "·".join(str(call[k]) for k in CALL_KEYS if call.get(k))
        rejected = (row["guard"] or {}).get("verdict") == "REJECTED"
        out.append(row["action"] + (f"({detail})" if detail else "") + ("✗" if rejected else ""))
    return out


def _ask_sources(facts: dict[str, Any]) -> list[str]:
    """사전 확인으로 물은 need의 출처(Run 순서대로): 재계획 Agent가 엮은 길인가, 서버가 붙인 것인가.
    같은 확인이 둘 다에 있어 메인이 둘 다 넘겼으면 한 번만 묻고 Agent가 엮은 길로 센다."""
    out = []
    for r in facts["runs"]:
        if r.agent_type == "COORDINATION" and r.input_ref.get("phase") == "ASK":
            for need in r.input_ref.get("needs") or []:
                source = "SERVER_OPENERS" if need["need_id"].split(":")[-2] == "s" else "MODEL_PATH"
                out.append(source)
    return out


def _owner_consent_paths(facts: dict[str, Any]) -> int:
    """막힌 재계획 결과 가운데 Agent가 엮은 길에 담당자 확인(OWNER_CONSENT)이 있는 결과 수."""
    count = 0
    for s in facts["steps"]:
        result = s["tool_result"] or {}
        if (s["action"] or {}).get("name") != "RETURN_RESULT" or result.get("status") != "BLOCKED":
            continue
        kinds = {n["kind"] for p in result.get("paths", []) for n in p["needs"]}
        count += "OWNER_CONSENT" in kinds
    return count


def _criteria(path: str, facts: dict[str, Any], pack: Any, base_plan: int) -> dict[str, Any]:
    """경로별 성공 기준(DB 사실). 공통: 권한 위반 0, 같은 요청 두 번 0, Budget 안, 오류 Run 없음."""
    runs, mains, replanning = facts["runs"], facts["mains"], facts["replanning"]
    candidates = facts["candidates"]
    last_main = mains[-1] if mains else None
    last_rp = replanning[-1] if replanning else None
    closed = last_main is not None and (last_main.status, last_main.end_reason) == (
        "SUCCEEDED",
        "CLOSE",
    )
    escalated = last_main is not None and (last_main.status, last_main.end_reason) == (
        "ESCALATED",
        "ESCALATE",
    )
    committed = facts["plan_revision"] == base_plan + 1
    done = {"committed": committed, "notices_sent": facts["notices_sent"], "main_closed": closed}
    first_pass = bool(candidates) and candidates[0]["pass"]
    last_pass = len(candidates) >= 2 and candidates[-1]["pass"]
    movability = [p for p in facts["proposals"] if p["type"] == "MOVABILITY"]
    # 담당자 질문은 Coordination 사전 확인 Run에서만 나온다 (AG-09)
    asked = {
        "owner_asked_by_coordination_only": bool(movability)
        and all(p["agent"] == "COORDINATION" for p in movability),
        "blocked_with_owner_consent_path": _owner_consent_paths(facts),
    }
    if path == "A":
        c = {
            "replanning_done": bool(replanning) and replanning[0].status == "SUCCEEDED",
            "pass_reached": first_pass,
            **done,
        }
    elif path == "B":
        c = {
            "alpha_pass": first_pass,
            "recalled_after_reject": len(replanning) >= 2,
            **asked,
            "beta_pass": last_pass,
            "consultation_complete": last_pass and candidates[-1]["consultation"] == "COMPLETE",
            **done,
        }
    elif path == "B-decline":
        declined = [p for p in movability if p["status"] == "DISCARDED"]
        c = {
            "alpha_pass": first_pass,
            "decline_applied": len(declined) == 1,
            "no_reask_after_decline": len(movability) == 1,
            **asked,
            "replanning_blocked": last_rp is not None and last_rp.status == "BLOCKED",
            "main_escalated": escalated,
        }
    elif path == "coord":
        c = {
            "alpha_pass": first_pass,
            "change_request_sent": any(r["type"] == "CHANGE_REQUEST" for r in facts["requests"]),
            "constraint_from_proposal": any(
                x.source_type == "PROPOSAL" for x in facts["constraints"]
            ),
            **asked,
            "beta_pass": last_pass,
            **done,
        }
    elif path == "event":
        released = [h["released"] for h in facts["holds"] if h["resolution"] == "FACT_CONFIRMED"]
        c = {
            "fact_confirmed": any(
                p["type"] == "FACT_UPDATE" and p["status"] == "CONFIRMED"
                for p in facts["proposals"]
            ),
            "hold_fact_confirmed": bool(released),
            # Hold 중에는 재계획 Run이 시작되지 않는다(해제 뒤의 사실에서만 시작한다)
            "no_replanning_during_hold": bool(released)
            and all(r.input_ref.get("context_version", 0) >= released[-1] for r in replanning),
            "gamma_pass": bool(candidates) and candidates[-1]["pass"],
            **done,
        }
    else:  # intake: 접수는 메인 밖에서 끝나고, 작업 준비됨이 메인을 띄운다
        intake = next((r for r in runs if r.agent_type == "INTAKE"), None)
        c = {
            "intake_succeeded": intake is not None and intake.status == "SUCCEEDED",
            "main_started": bool(mains),
            "alpha_pass": first_pass,
        }
    guards = [(s["guard"] or {}).get("reason_code") for s in facts["steps"]]
    return {
        **c,
        **_violations(pack),
        "within_budget": all(r.status != "BUDGET_EXHAUSTED" for r in runs),
        "no_error_run": all(r.status != "ERROR" for r in runs),
        "guard_rejections": _guard_counts(guards),
        "malformed": guards.count("MALFORMED"),
        "llm_errors": guards.count("LLM_ERROR"),
    }


RECORDED_ONLY = ("guard_rejections", "malformed", "llm_errors", "authority_violations",
                 "repeat_violations", "blocked_with_owner_consent_path")  # fmt: skip


def _success(criteria: dict[str, Any]) -> bool:
    return _no_violations(criteria) and all(
        bool(v) for k, v in criteria.items() if k not in RECORDED_ONLY
    )


def _record(
    record: dict[str, Any],
    settings: Settings,
    facts: dict[str, Any],
    criteria: dict[str, Any],
    humans: _Humans,
    rec: _Recorder,
) -> None:
    rows, mains = facts["rows"], facts["mains"]
    last_main = mains[-1] if mains else None
    record.update(
        model_settings=model_settings(settings),
        model=next((r["model_id"] for r in rows if r["model_id"]), None),
        prompt_versions={a: b.prompt.PROMPT_VERSION for a, b in BINDINGS.items()},
        success=_success(criteria),
        success_criteria=criteria,
        # 메인이 쓴 step·호출 수와 고른 행동 순서
        main={
            "runs": len(mains),
            "steps": sum(m.steps_used for m in mains),
            "agent_calls": sum(m.agent_calls_used for m in mains),
            "actions": _main_actions(facts),
            "ask_sources": _ask_sources(facts),
        },
        run_status=None if last_main is None else last_main.status,
        end_reason=None if last_main is None else last_main.end_reason,
        step_count=len(rows),
        actions=[f"{row['agent'][0]}:{row['action'] or '-'}" for row in rows],
        runs=[
            {
                "run_id": r.run_id,
                "agent_type": r.agent_type,
                "parent": r.parent_run_id is not None,
                "status": r.status,
                "end_reason": r.end_reason,
                "steps_used": r.steps_used,
                "human_rounds_used": r.human_rounds_used,
                "solver_calls_used": r.solver_calls_used,
            }
            for r in facts["runs"]
        ],
        candidates=facts["candidates"],
        events=humans.events,
        answer_kinds=humans.answer_kinds,
        steps=rows,
        tokens_in=sum(r["tokens_in"] for r in rows),
        tokens_out=sum(r["tokens_out"] for r in rows),
        llm_seconds=round(sum(c["ms"] for c in rec.calls) / 1000, 2),
    )


def _temp_site(pack_name: str, main_auto_start: bool) -> tuple[Any, dict[str, str | None], Any]:
    """임시 DB에 Pack을 넣는다. (임시 폴더, 이전 환경변수, Pack)."""
    tmp = tempfile.TemporaryDirectory(prefix="live_run_", ignore_cleanup_errors=True)
    old_env = _set_env(
        {"DB_PATH": str(Path(tmp.name) / "live.db"), **_env(pack_name, main_auto_start)}
    )
    db.close()
    db.init_db()
    pack = load_pack(pack_dir(pack_name))
    with db.write() as tx:
        seed_pack(tx, pack)
    return tmp, old_env, pack


def run_once(
    index: int,
    settings: Settings,
    pack_name: str,
    raw: bool,
    requests: Sequence[str] = ("A",),
) -> list[dict[str, Any]]:
    """경로 A: 임시 DB 하나에서 요청을 순서대로 하나씩 처리한다(앞 요청을 확정한 뒤 다음). 요청마다 기록 1개."""
    records: list[dict[str, Any]] = []
    tmp = old_env = None
    try:
        tmp, old_env, pack = _temp_site(pack_name, True)
        # 기대값은 verify_demo_values.py와 같은 출처(Pack YAML + 독립 CP-SAT)에서 얻는다
        world_model, world, task_a, demos = verify.load(pack_dir(pack_name))
        req_model = {task_a.id: task_a, **{d.id: d for d in demos}}
        for position, name in enumerate(requests, start=1):
            expected = verify.expected(world_model, world, req_model[name])
            escalate = bool(expected) and all(e["status"] != "OPTIMAL" for e in expected.values())
            record: dict[str, Any] = {
                "index": index,
                "path": "A",
                "request": name,
                "position": position,
                "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "agent_flags": {"main_auto_start": True},
                "site_now": _site_now(pack_name),
                "expected": expected,
                "expected_outcome": "ESCALATE" if escalate else "CANDIDATE",
            }
            t0 = time.perf_counter()
            try:
                rec = _Recorder(time.monotonic() + RUN_DEADLINE_S, raw)
                humans = _Humans(pack, "A")
                with db.read() as conn:
                    before, base_plan = _run_ids(conn), get_site(conn, pack.site_id).plan_revision
                form, requester = _form_and_requester(pack, name)
                submitted = submit_task_request(pack, requester, _key(), form)
                record["submitted"] = {
                    "status": submitted.status,
                    "reason_codes": list(submitted.reason_codes),
                }
                factory = lambda rec=rec: _RecordingModel(openai_model(settings), rec)
                _drive(pack, humans, factory, rec.deadline)
                facts = _collect(pack, rec, before)
                criteria = _criteria("A", facts, pack, base_plan)
                actual = facts["candidates"][0]["actual"] if facts["candidates"] else None
                if escalate:
                    # 해가 없는 요청은 "후보 없음 ∧ 재계획이 막힘 ∧ 메인이 자기 행동으로 이관"이 성공이다
                    last = facts["mains"][-1] if facts["mains"] else None
                    keep = {
                        k: criteria[k]
                        for k in (*RECORDED_ONLY, "within_budget", "no_error_run")
                        if k in criteria
                    }
                    criteria = {
                        "no_candidate": actual is None,
                        "replanning_blocked": bool(facts["replanning"])
                        and facts["replanning"][-1].status == "BLOCKED",
                        "main_escalated": last is not None
                        and (last.status, last.end_reason) == ("ESCALATED", "ESCALATE"),
                        **keep,
                    }
                _record(record, settings, facts, criteria, humans, rec)
                solves = [r for r in facts["rows"] if r["action"] == "SOLVE_WITH_SCOPE"]
                record.update(
                    first_solve_level=solves[0]["level"] if solves else None,
                    l0_first=bool(solves) and solves[0]["level"] == "L0",
                    actual=actual,
                    matches_expected=_matches(expected, actual, escalate),
                )
                committed = bool(criteria.get("committed"))
                level = (actual or {}).get("level")
                world = verify.advance(
                    world, req_model[name], expected.get(level or "") if committed else None
                )
            except Exception as e:  # noqa: BLE001 — 한 요청의 실패를 기록하고 다음으로 간다
                record.update(success=False, error=f"{type(e).__name__}: {e}")
            record["total_seconds"] = round(time.perf_counter() - t0, 2)
            records.append(record)
    except Exception as e:  # noqa: BLE001 — 준비 단계 실패도 기록한다
        records.append({"index": index, "success": False, "error": f"{type(e).__name__}: {e}"})
    finally:
        db.close()
        if old_env is not None:
            _restore_env(old_env)
        if tmp is not None:
            tmp.cleanup()
    return records


def run_path(
    index: int,
    settings: Settings,
    pack_name: str,
    raw: bool,
    path: str,
    ambiguous: bool = False,
) -> dict[str, Any]:
    """경로 B·B-decline·coord·event·intake 1회. 임시 DB 하나, 기록 1개."""
    record: dict[str, Any] = {
        "index": index,
        "path": path + ("-ambiguous" if ambiguous else ""),
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "agent_flags": {"main_auto_start": True},
        "site_now": _site_now(pack_name),
    }
    t0 = time.perf_counter()
    tmp = old_env = None
    try:
        # 신고 경로는 R1까지를 스크립트로 준비한다(메인 없이). 그 뒤에 자동 시작을 켠다
        tmp, old_env, pack = _temp_site(pack_name, main_auto_start=path != "event")
        if path == "event":
            _setup_r1(pack)
            _set_env({"MAIN_AUTO_START": "true"})
        rec = _Recorder(time.monotonic() + RUN_DEADLINE_S * 2, raw)
        humans = _Humans(pack, path, ambiguous)
        with db.read() as conn:
            before, base_plan = _run_ids(conn), get_site(conn, pack.site_id).plan_revision
        if path == "event":
            demo = pack.demo_events[1 if ambiguous else 0]
            body = EventReport(
                source_event_id=_key(),
                event_type=demo.event_type,
                text=demo.text,
                target_task_id=demo.target_task_id,
            )
            out = receive_event(pack, "reporter", _key(), body)
            humans.events.append({"event": demo.label, "status": out.status})
        elif path == "intake":
            demo = pack.demo_intakes[1 if ambiguous else 0]
            body = IntakeRequest(task_id=demo.task_id, text=demo.text)
            out = submit_intake(pack, demo.requester, _key(), body)
            humans.events.append({"intake": demo.label, "status": out.status})
        else:
            form, requester = _form_and_requester(pack, pack.new_task.task_id)
            out = submit_task_request(pack, requester, _key(), form)
            humans.events.append({"form": form.task_id, "status": out.status})
        factory = lambda: _RecordingModel(openai_model(settings), rec)
        _drive(pack, humans, factory, rec.deadline)
        facts = _collect(pack, rec, before)
        criteria = _criteria(path, facts, pack, base_plan)
        if path == "intake":
            criteria.update(_intake_checks(pack, facts, ambiguous))
        _record(record, settings, facts, criteria, humans, rec)
        # 기대값(verify_demo_values와 같은 출처)과 같은지는 기록만 한다
        world_model, world, task_a, _ = verify.load(pack_dir(pack_name))
        alpha = facts["candidates"][0]["actual"] if facts["candidates"] else None
        if path != "event":
            exp_alpha = verify.expected(world_model, world, task_a).get("L1")
            record["alpha_matches_expected"] = alpha is not None and _matches(
                {"L1": exp_alpha}, alpha, False
            )
    except Exception as e:  # noqa: BLE001 — 실패를 기록한다
        record.update(success=False, error=f"{type(e).__name__}: {e}")
    finally:
        db.close()
        if old_env is not None:
            _restore_env(old_env)
        if tmp is not None:
            tmp.cleanup()
    record["total_seconds"] = round(time.perf_counter() - t0, 2)
    return record


def _intake_checks(pack: Any, facts: dict[str, Any], ambiguous: bool) -> dict[str, Any]:
    """접수 경로: 작업 값이 시연값과 같고, 필드가 모두 요청자 확인(message:)에서 왔다."""
    nt = pack.new_task
    with db.read() as conn:
        task = next(
            (t for t in list_current_tasks(conn, pack.site_id, pack) if t.task_id == nt.task_id),
            None,
        )
        consents = conn.execute(
            "SELECT axis, scope, source_ref FROM consent WHERE task_id = ? ORDER BY rowid",
            (nt.task_id,),
        ).fetchall()
    keys = ("work_type", "zone_id", "duration", "earliest_start", "latest_start", "latest_end",
            "required_resource_type", "requested_resource_id")  # fmt: skip
    sources = {f.source_ref for f in task.fields.values()} if task else set()
    expected_consents = [
        ("TIME", {"start_min": nt.earliest_start, "start_max": nt.latest_start}),
        ("RESOURCE", {"resource_ids": [nt.requested_resource_id]}),
    ]
    asks = sum(
        1
        for s in facts["steps"]
        if (s["action"] or {}).get("name") == "ASK_CLARIFICATION"
        and s["guard"]["verdict"] == "ACCEPTED"
    )
    out: dict[str, Any] = {
        "values_match_new_task": task is not None
        and all(getattr(task, k) == getattr(nt, k) for k in keys),
        "fields_confirmed_from_message": task is not None
        and {f.status for f in task.fields.values()} == {"CONFIRMED"}
        and len(sources) == 1
        and next(iter(sources)).startswith("message:"),
        "consents_like_form": [(c[0], json.loads(c[1])) for c in consents] == expected_consents
        and all(c[2].startswith("message:") for c in consents),
    }
    if ambiguous:
        out["asked_requester"] = asks >= 1
    return out


# ── 신고 경로의 시작 상태: R1(기본안 B, Beta 확정)을 스크립트로 준비한다 ─────


class _Script:
    """R1 준비용 스크립트 응답(LLM 없음). 한 객체를 계속 쓴다(Run이 깨어나도 응답이 이어진다)."""

    model_name = "script-setup"

    def __init__(self, replies: list[AIMessage]):
        self.replies = list(replies)

    def bind_tools(self, tools: Sequence[dict[str, Any]], **kwargs: Any) -> "_Script":
        return self

    def invoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        return self.replies.pop(0)


def _call(name: str, **args: Any) -> AIMessage:
    binding = next(b for b in BINDINGS.values() if name in b.spec.actions)
    skill = skills.default_skill(binding.spec.skills, name)
    args = {"decision_summary": "R1 준비(스크립트)", "skill": skill, **args}
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": _key()}])


SETUP_CASE = "case_setup_r1"


def _start_replanning(pack: Any) -> None:
    """메인 없이 Replanning Run 하나를 시작시킨다(준비용). 요청 작업의 Unit이 주체다."""
    site_id = pack.site_id
    with db.write() as tx:
        site = get_site(tx, site_id)
        _, facts, groups = casefacts.current_groups(tx, pack)
        group = groups[0]
        unit = facts.task_map()[pack.new_task.task_id].unit_id
        primary = casefacts.primary_for(group, facts, unit)
        payload = {
            "agent_type": "REPLANNING",
            "case_id": SETUP_CASE,
            "acting_unit_id": unit,
            "acting_actor_id": casefacts.acting_actor(tx, pack, group, facts, unit),
            "group_id": group.group_id,
            "group_task_ids": list(group.task_ids),
            "conflict": {"rule_id": primary.rule_id, "task_ids": list(primary.task_ids)},
            "context_version": site.context_version,
            "plan_revision": site.plan_revision,
        }
        register_job(tx, site_id, "START_RUN", f"START_RUN:setup:{_key()}", payload)


def _start_ask(pack: Any, task_id: str, resource_id: str) -> str:
    """메인 없이 Coordination 사전 확인 Run 하나를 시작시킨다(준비용). 넘긴 need_id."""
    need_id = "setup:s:0"
    need = {
        "need_id": need_id,
        "kind": "OWNER_CONSENT",
        "task_id": task_id,
        "axis": "RESOURCE",
        "values": [resource_id],
    }
    with db.write() as tx:
        site = get_site(tx, pack.site_id)
        payload = {
            "agent_type": "COORDINATION",
            "phase": "ASK",
            "case_id": SETUP_CASE,
            "need_ids": [need_id],
            "needs": [need],
            "acting_unit_id": supervisor_actor(tx, pack).unit_id,
            "context_version": site.context_version,
            "plan_revision": site.plan_revision,
        }
        register_job(tx, pack.site_id, "START_RUN", f"START_RUN:setup:{_key()}", payload)
    return need_id


def _setup_r1(pack: Any) -> None:
    """기본안 B로 R1(Beta 확정)까지: Alpha → 거절(C 고정) → 재계획 막힘 → 사전 확인 → 수락 → TRY → Beta →
    승인.

    사건 → 메인 자동 시작이 꺼진 상태에서 Replanning·Coordination Run을 직접 시작시킨다. 통지는 없다.
    """
    site_id, task_id = pack.site_id, pack.new_task.task_id
    form, requester = _form_and_requester(pack, task_id)
    submit_task_request(pack, requester, _key(), form)
    done = lambda: _call("RETURN_RESULT", status="DONE", summary="R1 준비")

    def run(*replies: AIMessage) -> None:
        script = _Script(list(replies))
        run_until_idle(pack, model_factory=lambda: script)

    def review() -> tuple[str, Any, int]:
        with db.read() as conn:
            cid = list_review_queue(conn, site_id)[-1]
            ctx = get_site(conn, site_id).context_version
            return cid, _pass_validation(conn, site_id, cid), ctx

    _start_replanning(pack)
    run(_call("SOLVE_WITH_SCOPE", level="L0"), _call("SOLVE_WITH_SCOPE", level="L1"), done())
    alpha, alpha_v, _ = review()
    x = pack.demo_rejections[0]
    reject_candidate(
        pack,
        "supervisor",
        _key(),
        RejectRequest(
            candidate_id=alpha,
            validation_id=alpha_v.validation_id,
            reason_code=x.reason_code,
            target_task_ids=x.target_task_ids,
            axes=x.axes,
            comment=x.comment,
        ),
    )
    _start_replanning(pack)
    blocked = _call("RETURN_RESULT", status="BLOCKED", summary="R1 준비")
    run(_call("LIST_ASSIGNABLE_RESOURCES", task_id=task_id), blocked)
    need_id = _start_ask(pack, task_id, "SITE-CR-01")
    run(
        _call("ASK_OWNER", need_id=need_id, message="대체 자원 확인"),
        _call("WAIT_FOR_REPLIES", skill="PRE_CONFIRM"),
    )
    with db.read() as conn:
        mid, to = conn.execute(
            "SELECT message_id, to_actor_id FROM message WHERE status = 'OPEN'"
        ).fetchone()
    reply_message(pack, to, _key(), ReplyRequest(message_id=mid, decision="ACCEPT"))
    run(done())  # 사전 확인 Run이 답을 받고 끝난다
    _start_replanning(pack)
    run(_call("TRY_ALTERNATIVE_RESOURCE", task_id=task_id, resource_id="SITE-CR-01"), done())
    beta, beta_v, ctx = review()
    out = approve_and_commit(
        pack,
        "supervisor",
        _key(),
        ApproveRequest(
            candidate_id=beta, validation_id=beta_v.validation_id, expected_context_version=ctx
        ),
    )
    if out.status != "APPLIED":
        raise RuntimeError(f"R1 준비 실패: {out.reason_codes}")


def _line(r: dict[str, Any]) -> str:
    main = r.get("main") or {}
    head = f"#{r['index']} path {r.get('path', '?')}"
    if r.get("request"):
        head += f" {r['request']} expected={r.get('expected_outcome')} matches={r.get('matches_expected')}"
    return (
        f"{head} success={r.get('success')} main={r.get('run_status')}/{r.get('end_reason')} "
        f"main_steps={main.get('steps')} calls={main.get('agent_calls')} "
        f"steps={r.get('step_count')} {r.get('total_seconds')}s  "
        f"{' → '.join(main.get('actions', []))}  criteria={r.get('success_criteria')}"
        + (f"  error={r['error']}" if r.get("error") else "")
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="실제 모델로 시연 경로 live run")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--pack", default=None)
    parser.add_argument(
        "--request",
        default=None,
        help="scenario.yaml의 시연 요청 이름(쉼표로 여러 개, 순서대로 하나씩 확정). 기본은 new_task(A)",
    )
    parser.add_argument("--raw", action="store_true", help="prompt·응답 원문을 기록에 넣는다")
    parser.add_argument(
        "--ambiguous", action="store_true", help="--path intake·event에서 모호한 문장을 쓴다"
    )
    parser.add_argument(
        "--path",
        choices=PATHS,
        default="A",
        help="A 요청 → 수용·승인, B 거절 → 재계획 → 사전 확인 → 승인, B-decline 담당자 거절 → 이관, "
        "coord 이견 → 제약 → 승인, event 신고 → 사실 수정 → 승인, intake 자연어 접수",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.runs <= MAX_RUNS:
        parser.error(f"--runs must be 1..{MAX_RUNS}")

    settings = get_settings()
    pack_name = args.pack or settings.pack
    known = request_names(load_pack(pack_dir(pack_name)))
    requests = [r.strip() for r in (args.request or known[0]).split(",") if r.strip()]
    unknown = [r for r in requests if r not in known]
    if unknown or not requests:
        parser.error(
            f"--request must be names from scenario.yaml {known}, got {unknown or requests}"
        )
    if args.ambiguous and args.path not in ("event", "intake"):
        parser.error("--ambiguous는 --path event 또는 intake에서만 쓴다")
    if args.path != "A" and requests != [known[0]]:
        parser.error(f"--path {args.path} runs only --request {known[0]}")

    if not settings.openai_api_key.get_secret_value() or not settings.openai_model:
        print("OPENAI_API_KEY와 OPENAI_MODEL을 .env에 넣은 뒤 실행한다.", file=sys.stderr)
        return 2
    os.environ["LANGSMITH_TRACING"] = "false"

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    versions = {a: b.prompt.PROMPT_VERSION for a, b in BINDINGS.items()}
    print(f"model settings: {model_settings(settings)}  prompts: {versions}  path: {args.path}")
    records = []
    for i in range(args.runs):
        if args.path == "A":
            batch = run_once(i + 1, settings, pack_name, args.raw, requests)
        else:
            batch = [run_path(i + 1, settings, pack_name, args.raw, args.path, args.ambiguous)]
        for r in batch:
            records.append(r)
            with out.open("a", encoding="utf-8") as f:
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
            print(_line(r))

    ok = sum(1 for r in records if r.get("success"))
    tokens = sum(r.get("tokens_in", 0) + r.get("tokens_out", 0) for r in records)
    print(f"success {ok}/{len(records)} (path {args.path}), tokens {tokens}")
    print(f"saved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
