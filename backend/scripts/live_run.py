"""실제 모델로 시연 요청을 돌린다 (설계서 §16 Agent 평가, 우선순위 문서 시연 안정성, 부록 A.17).

    cd backend && uv run python -m scripts.live_run [--runs 1] [--request A|N1,N2,...] [--raw]
    cd backend && uv run python -m scripts.live_run --path B|B-decline [--runs 1] [--raw]

- .env의 OPENAI_API_KEY·OPENAI_MODEL이 없으면 바로 종료한다(API를 부르지 않음).
- run마다 임시 DB를 만든다. 개발 DB(data/safe_orch.db)는 건드리지 않는다.
- --request: scenario.yaml의 시연 요청 이름(기본 new_task A). 쉼표로 여러 개면 같은 DB에서 순서대로
  하나씩 처리한다(앞 요청을 승인으로 확정한 뒤 다음). 기대값은 verify_demo_values.py와 같은 출처다.
- 흐름: 폼 → 워커(START_RUN → 실제 모델 Replanning → 후보 → VALIDATE → Consultation)
  → 스크립트가 Supervisor로 PENDING item을 WAIVE(comment "live run 자동 수용") → 승인.
- 결과: 콘솔 요약 + data/live_runs/<UTC시각>.jsonl (gitignore). --raw일 때만 prompt·응답 원문을 넣는다.
- 성공 = PASS 후보 도달 ∧ 금지 Action(ACTION_NOT_AVAILABLE) 0 ∧ Budget 안. 기대값이 모든 범위 INFEASIBLE인
  요청(N5)은 "후보 없음 ∧ ESCALATE_NO_SOLUTION으로 종료"가 성공이다. 기대 결과와 같은지는 matches_expected로 따로 남긴다.
- --path B(기본안 B, 부록 A.21 9): 요청 A만. 스크립트가 사람 역할을 한다: Alpha PASS 뒤 Supervisor로
  demo_rejections[0] 거절 → OPEN 메시지가 생기면 그 수신자로 ACCEPT(comment "live run 자동 수락")
  → Beta PASS ∧ 협의 완료면 승인(WAIVE 없음). 성공 = Beta PASS ∧ Consultation COMPLETE ∧ 확정 R1 ∧ Run SUCCEEDED
  ∧ 금지 Action 0 ∧ Budget 안(사람 라운드 ≤ 2) ∧ ASK가 LIST 결과의 대체 자원을 담음 ∧ 수락 전 TRY 없음.
- --path B --coord(A.28): Coordination을 켠 기본안 B(운영 기본값의 기본안 B 흐름). Alpha PASS 뒤 협의 Run이
  C 담당자에게 변경 요청을 보내지만 스크립트는 답하지 않고 Supervisor가 거절한다. 성공 = 기본안 B 성공 기준 ∧ 협의 Run
  STALE(REJECTED:) ∧ 확정 뒤 통지 대상 전원 통지.
- Agent 자동 시작 설정(COORDINATION_ENABLED·EVENT_RESPONSE_ENABLED)은 경로가 명시한다(.env·기본값과 무관, A.28):
  A·B·B-decline·intake = 둘 다 끔, coord = Coordination만, B --coord = Coordination만, event = Event Response
  (--coord면 Coordination도). 기록 agent_flags에 남긴다.
- --path B-decline: 같은 흐름에서 ACCEPT 대신 DECLINE(comment "live run 자동 거절"). 성공 = Alpha PASS ∧ ASK
  1회 ∧ DECLINE 적용 ∧ 거절 뒤 ASK·TRY 없음 ∧ Run ESCALATED ∧ 금지 Action 0 ∧ Budget 안.
- --path coord(기본안 A, 부록 A.24): COORDINATION_ENABLED를 켠 임시 DB에서 요청 A만. 스크립트가 사람 역할을
  한다: 변경 요청에는 이견(demo_rejections[0].comment), 제약 초안에는 확정, 담당자 질문에는 수락, 동의가 끝난
  PASS 후보는 승인. 성공 = Alpha PASS ∧ C 담당자에게 C 변경 요청 ∧ DRAFT_CONSTRAINT(TASK_IMMOVABLE, C, 바뀐 축
  포함) ∧ 제약 source PROPOSAL ∧ Beta PASS ∧ R1 ∧ Replanning SUCCEEDED ∧ 통지 대상 전원에게 NOTICE ∧ 통지 Run
  SUCCEEDED ∧ 모든 Run에서 금지 Action·MALFORMED 0 ∧ Budget 안.
- --path event(Scene 4 최소 경로, 부록 A.25) [--coord]: R1(기본안 B, Beta 확정)까지는 스크립트 응답으로 준비하고
  (LLM 없음), EVENT_RESPONSE_ENABLED를 켠 뒤 Reporter가 demo_events[0]을 신고한다. 사람 역할: Supervisor가 사실
  수정안을 확정하고 FACT_CONFIRMED로 해제, (--coord면 COORDINATION_ENABLED를 켜고 변경 요청에 담당자 수락, 아니면
  Supervisor WAIVE) → 승인. 성공 = PROPOSE(E, 60) ∧ 제안 CONFIRMED ∧ Hold FACT_CONFIRMED ∧ Gamma PASS(E 60,
  변경 1, 지연 15) ∧ R2 ∧ ER Run SUCCEEDED ∧ (--coord면 통지 대상 전원 통지) ∧ 신고 뒤 Run에서 금지 Action·
  MALFORMED 0 ∧ Budget 안. --ambiguous면 demo_events[1]을 신고하고, 신고자 질문에 ANSWER로 답한다.
- --path intake [--ambiguous] (Work Intake, 부록 A.26): 요청 A를 demo_intakes[0](명확) 또는 [1](모호) 문장으로
  자연어 요청한다. 사람 역할(요청자): 확인 질문에는 scenario의 answer 문장으로 ANSWER, 값 확인 요청에는 확인.
  완료 뒤 Replanning을 실제 모델로 Alpha 후보까지. 성공 = Intake SUCCEEDED ∧ 작업 값 = new_task ∧ fields 모두
  CONFIRMED·source message: ∧ Consent(TIME 시작 범위, RESOURCE 요청 자원, source message:) ∧ Alpha PASS·기대값(L1)
  일치 ∧ 금지 Action·MALFORMED 0 ∧ Budget 안 (∧ --ambiguous면 확인 질문 1회 이상). 명확한 요청의 질문 횟수는
  기록만 한다(성공 기준 아님).
- 자유 텍스트 답(신고자·요청자, A.27): 첫 질문에는 scenario 답 문장, 두 번째 질문부터는 "앞에서 답한 것이
  전부입니다: <답>. 나머지는 처음 문장 그대로입니다: <원문>". 질문별 답 종류(FIRST·REPEAT)를 answer_kinds와
  events[].answer_kind에 남긴다. 성공 기준은 바꾸지 않는다.
"""

import argparse
import json
import os
import sys
import tempfile
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

from app.agents.llm import model_settings, openai_model
from app.agents.prompts.coordination import PROMPT_VERSION as COORDINATION_PROMPT_VERSION
from app.agents.prompts.event_response import PROMPT_VERSION as EVENT_RESPONSE_PROMPT_VERSION
from app.agents.prompts.intake import PROMPT_VERSION as INTAKE_PROMPT_VERSION
from app.agents.prompts.replanning import PROMPT_VERSION
from app.agents.specs.replanning import MAX_HUMAN_ROUNDS
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
from app.domain.calendar import work_delay
from app.packs.loader import load_pack, pack_dir
from app.solver import cpsat
from app.store import db
from app.store.repos.consultations import consultation_view
from app.store.repos.decisions import list_constraints
from app.store.repos.messages import get_message
from app.store.repos.records import get_candidate, get_snapshot, list_validations
from app.store.repos.runs import get_run, list_steps
from app.store.repos.seed import seed_pack
from app.store.repos.site import get_site
from app.store.repos.tasks import list_current_tasks
from scripts import verify_demo_values as verify

OUT_DIR = REPO_ROOT / "data" / "live_runs"
RUN_DEADLINE_S = 300
MAX_RUNS = 10
PATHS = ("A", "B", "B-decline", "coord", "event", "intake")
MAX_HUMAN_TURNS = 6  # 기본안 B에서 사람 응답 반복 상한(무한 반복 방지)


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
    """사람 역할의 자유 텍스트 답 (A.27). (답 종류, 답 문장).

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


def _flag_env(coordination: bool, event_response: bool) -> dict[str, str]:
    """Agent 자동 시작 설정을 경로가 명시한다. .env·기본값과 무관하게 같은 조건으로 잰다 (A.28)."""
    return {
        "COORDINATION_ENABLED": "true" if coordination else "false",
        "EVENT_RESPONSE_ENABLED": "true" if event_response else "false",
    }


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


def run_once(
    index: int,
    settings: Settings,
    pack_name: str,
    raw: bool,
    requests: Sequence[str] = ("A",),
) -> list[dict[str, Any]]:
    """임시 DB 하나에서 요청을 순서대로 하나씩 처리한다(앞 요청을 승인으로 확정한 뒤 다음). 요청마다 기록 1개."""
    tmp = tempfile.TemporaryDirectory(prefix="live_run_")
    old_env = _set_env({"DB_PATH": str(Path(tmp.name) / "live.db"), **_flag_env(False, False)})
    get_settings.cache_clear()
    db.close()
    real_solve = cpsat.solve
    records: list[dict[str, Any]] = []
    try:
        db.init_db()
        pack = load_pack(pack_dir(pack_name))
        with db.write() as tx:
            seed_pack(tx, pack)
        # 기대값은 verify_demo_values.py와 같은 출처(Pack YAML + 독립 CP-SAT)에서 얻는다
        world_model, world, task_a, demos = verify.load(pack_dir(pack_name))
        req_model = {task_a.id: task_a, **{d.id: d for d in demos}}
        for position, name in enumerate(requests, start=1):
            expected = verify.expected(world_model, world, req_model[name])
            escalate = bool(expected) and all(e["status"] != "OPTIMAL" for e in expected.values())
            record = _run_request(index, position, name, settings, pack, raw, real_solve)
            record["agent_flags"] = {"coordination": False, "event_response": False}
            record["expected"] = expected
            record["expected_outcome"] = "ESCALATE" if escalate else "CANDIDATE"
            if escalate:
                c = record.get("success_criteria") or {}
                c["no_candidate"] = record.get("actual") is None
                c["escalated"] = record.get("run_status") == "ESCALATED" and str(
                    record.get("end_reason") or ""
                ).startswith("ESCALATE_NO_SOLUTION")
                # 해가 없는 요청은 "후보 없음 + ESCALATE_NO_SOLUTION 종료"가 성공이다
                record["success"] = (
                    c["no_candidate"]
                    and c["escalated"]
                    and c.get("forbidden_actions") == 0
                    and bool(c.get("within_budget"))
                )
            record["matches_expected"] = _matches(expected, record.get("actual"), escalate)
            records.append(record)
            committed = (record.get("committed") or {}).get("status") == "APPLIED"
            level = (record.get("actual") or {}).get("level")
            world = verify.advance(
                world, req_model[name], expected.get(level or "") if committed else None
            )
    except Exception as e:  # noqa: BLE001 — 준비 단계 실패도 기록한다
        records.append({"index": index, "success": False, "error": f"{type(e).__name__}: {e}"})
    finally:
        cpsat.solve = real_solve
        db.close()
        _restore_env(old_env)
        tmp.cleanup()
    return records


# ── 기본안 B (부록 A.21 9) ─────────────────────────────────────


def _pass_validation(conn: Any, site_id: str, cid: str | None) -> Any:
    passes = [v for v in list_validations(conn, site_id, cid) if v.status == "PASS"] if cid else []
    return passes[-1] if passes else None


def _waiting(conn: Any, run_id: str) -> tuple[Any, str | None]:
    run = get_run(conn, run_id)
    ref = run.wait_ref if run.status == "WAITING_HUMAN" else None
    return run, ref


def run_path_b(
    index: int, settings: Settings, pack_name: str, raw: bool, decline: bool, coord: bool = False
) -> dict:
    """임시 DB에서 요청 A로 기본안 B(또는 B-decline)를 끝까지 돌린다. 기록 1개.

    coord면 Coordination을 켠다(운영 기본값의 기본안 B, A.28).
    """
    tmp = tempfile.TemporaryDirectory(prefix="live_run_")
    old_env = _set_env({"DB_PATH": str(Path(tmp.name) / "live.db"), **_flag_env(coord, False)})
    get_settings.cache_clear()
    db.close()
    real_solve = cpsat.solve
    path = "B-decline" if decline else ("B-coord" if coord else "B")
    record: dict[str, Any] = {
        "index": index,
        "request": "A",
        "path": path,
        "agent_flags": {"coordination": coord, "event_response": False},
    }
    t0 = time.perf_counter()
    try:
        db.init_db()
        pack = load_pack(pack_dir(pack_name))
        with db.write() as tx:
            seed_pack(tx, pack)
        _path_b(record, settings, pack, pack_name, raw, decline, real_solve, coord)
        record["l0_first"] = record.get("first_solve_level") == "L0"
    except Exception as e:  # noqa: BLE001 — 실패도 기록한다
        record.update(success=False, error=f"{type(e).__name__}: {e}")
    finally:
        cpsat.solve = real_solve
        db.close()
        _restore_env(old_env)
        tmp.cleanup()
    record["total_seconds"] = round(time.perf_counter() - t0, 2)
    return record


def _path_b(
    record: dict[str, Any],
    settings: Settings,
    pack: Any,
    pack_name: str,
    raw: bool,
    decline: bool,
    real_solve: Any,
    coord: bool = False,
) -> None:
    rec = _Recorder(time.monotonic() + RUN_DEADLINE_S * 2, raw)
    site_id = pack.site_id
    factory = lambda: _RecordingModel(openai_model(settings), rec)
    form, requester = _form_and_requester(pack, pack.new_task.task_id)
    record["submitted"] = submit_task_request(pack, requester, _key(), form).status
    run_until_idle(pack, model_factory=factory)
    with db.read() as conn:
        # Coordination을 켜면 Alpha의 협의 Run도 생긴다. 기본안 B는 Replanning Run을 따라간다
        [run_id] = [
            r[0]
            for r in conn.execute("SELECT run_id FROM agent_run WHERE agent_type = 'REPLANNING'")
        ]
        run, alpha = _waiting(conn, run_id)
        alpha_v = _pass_validation(conn, site_id, alpha)
        alpha_step = next(
            (s for s in list_steps(conn, run_id) if (s["tool_result"] or {}).get("candidate_id")),
            None,
        )
        alpha_actual = _actual(conn, site_id, alpha, alpha_step) if alpha and alpha_step else None
    events: list[dict[str, Any]] = []
    beta = beta_v = view = committed = None
    reply_status = None
    if alpha_v is not None:
        x = pack.demo_rejections[0]
        out = reject_candidate(
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
        events.append({"rejected": alpha, "status": out.status})
        for _ in range(MAX_HUMAN_TURNS):
            run_until_idle(pack, model_factory=factory)
            with db.read() as conn:
                run, ref = _waiting(conn, run_id)
                message = (
                    get_message(conn, site_id, ref) if run.wait_kind == "MESSAGE" and ref else None
                )
                cand_v = _pass_validation(conn, site_id, ref) if message is None else None
            if message is not None and message["status"] == "OPEN":
                decision = "DECLINE" if decline else "ACCEPT"
                comment = "live run 자동 거절" if decline else "live run 자동 수락"
                out = reply_message(
                    pack,
                    message["to_actor_id"],
                    _key(),
                    ReplyRequest(message_id=ref, decision=decision, comment=comment),
                )
                reply_status = out.status
                events.append({"reply": decision, "message_id": ref, "status": out.status})
                continue
            if cand_v is not None:
                beta, beta_v = ref, cand_v
            break
        if beta is not None and not decline:
            with db.read() as conn:
                view = consultation_view(conn, site_id, beta)
                ctx = get_site(conn, site_id).context_version
            if view is not None and view.items_status == "COMPLETE":
                out = approve_and_commit(
                    pack,
                    "supervisor",
                    _key(),
                    ApproveRequest(
                        candidate_id=beta,
                        validation_id=beta_v.validation_id,
                        expected_context_version=ctx,
                    ),
                )
                committed = {"status": out.status, "reason_codes": list(out.reason_codes)}
                if coord:
                    run_until_idle(pack, model_factory=factory)  # 통지 Run (A.28)

    with db.read() as conn:
        run = get_run(conn, run_id)
        runs = [
            get_run(conn, r[0]) for r in conn.execute("SELECT run_id FROM agent_run ORDER BY rowid")
        ]
        order = [
            (r[0], r[1])
            for r in conn.execute("SELECT run_id, step_no FROM agent_step ORDER BY rowid")
        ]
        by_run = {r.run_id: list_steps(conn, r.run_id) for r in runs}
        # 모든 Run의 step(예약 순서, 모델 호출 기록과 맞춤). Coordination을 끄면 Replanning뿐이다
        all_steps = [next(x for x in by_run[rid] if x["step_no"] == no) for rid, no in order]
        steps = by_run[run_id]
        cur = conn.execute("SELECT to_actor_id, type FROM message ORDER BY rowid")
        messages = [{"to": r[0], "type": r[1]} for r in cur.fetchall()]
        plan_revision = get_site(conn, site_id).plan_revision
        beta_step = next(
            (s for s in steps if beta and (s["tool_result"] or {}).get("candidate_id") == beta),
            None,
        )
        beta_actual = _actual(conn, site_id, beta, beta_step) if beta and beta_step else None
        n_messages = conn.execute("SELECT COUNT(*) FROM message").fetchone()[0]
    rows = _step_rows(all_steps, rec.calls)
    for row, s in zip(rows, all_steps, strict=True):
        row["run_id"] = s["run_id"]
        row["agent"] = next(r.agent_type for r in runs if r.run_id == s["run_id"])
    names = [(s["action"] or {}).get("name") for s in steps]
    guards = [(s["guard"] or {}).get("reason_code") for s in all_steps]
    ask_steps = [s for s in steps if (s["action"] or {}).get("name") == "ASK_TASK_OWNER"
                 and s["guard"]["verdict"] == "ACCEPTED"]  # fmt: skip
    list_alts = {
        a["resource_id"]
        for s in steps
        if (s["action"] or {}).get("name") == "LIST_ASSIGNABLE_RESOURCES"
        and s["guard"]["verdict"] == "ACCEPTED"
        for a in s["tool_result"]["assignable"]
        if a["resource_id"] != s["tool_result"]["current"]
    }
    asked = [v for s in ask_steps for v in s["action"]["args"]["allowed_values"]]
    tries = [s for s in steps if (s["action"] or {}).get("name") == "TRY_ALTERNATIVE_RESOURCE"]
    accepted = lambda s: any(h["decision"] == "ACCEPT" for h in s["observation"]["human_replies"])
    reject_step = next((s for s in steps if s["observation"]["rejections"]), None)
    criteria: dict[str, Any] = {
        "alpha_pass": alpha_v is not None,
        "forbidden_actions": guards.count("ACTION_NOT_AVAILABLE"),
        "malformed": guards.count("MALFORMED"),
        "llm_errors": guards.count("LLM_ERROR"),
        "within_budget": run.status != "BUDGET_EXHAUSTED"
        and run.human_rounds_used <= MAX_HUMAN_ROUNDS,
        "ask_count": len(ask_steps),
        "ask_uses_listed_alternative": bool(asked) and set(asked) <= list_alts,
        "reply_applied": reply_status == "APPLIED",
    }
    if decline:
        after = [
            s for s in steps if s["step_no"] > max((a["step_no"] for a in ask_steps), default=0)
        ]
        criteria.update(
            no_ask_or_try_after_decline=not any(
                (s["action"] or {}).get("name") in ("ASK_TASK_OWNER", "TRY_ALTERNATIVE_RESOURCE")
                and s["guard"]["verdict"] == "ACCEPTED"
                for s in after
            ),
            escalated=run.status == "ESCALATED",
        )
        keys = ("alpha_pass", "reply_applied", "no_ask_or_try_after_decline", "escalated",
                "within_budget")  # fmt: skip
        success = all(criteria[k] for k in keys) and criteria["ask_count"] == 1
    else:
        criteria.update(
            beta_pass=beta_v is not None,
            consultation_complete=view is not None and view.items_status == "COMPLETE",
            committed_r1=(committed or {}).get("status") == "APPLIED" and plan_revision == 1,
            run_succeeded=run.status == "SUCCEEDED",
            no_try_before_accept=all(accepted(s) for s in tries),
        )
        keys = ("beta_pass", "consultation_complete", "committed_r1", "run_succeeded",
                "within_budget", "ask_uses_listed_alternative", "no_try_before_accept")  # fmt: skip
        success = all(criteria[k] for k in keys)
    if coord:
        # Coordination을 켠 기본안 B (A.28): Alpha 협의 Run은 거절로 STALE, 확정 뒤 통지 대상 전원 통지
        coords = [r for r in runs if r.agent_type == "COORDINATION"]
        consults = [r for r in coords if r.input_ref.get("phase") == "CONSULT"]
        notice_run = next((r for r in coords if r.input_ref.get("phase") == "NOTICE"), None)
        targets: list[str] = []
        if notice_run is not None:
            first = next((x for x in all_steps if x["run_id"] == notice_run.run_id), None)
            targets = sorted(
                t["actor_id"]
                for t in (first or {}).get("observation", {}).get("notice_targets", [])
            )
        noticed = sorted({m["to"] for m in messages if m["type"] == "NOTICE"})
        criteria.update(
            consult_stale_rejected=bool(consults)
            and all(
                r.status == "STALE" and str(r.end_reason or "").startswith("REJECTED:")
                for r in consults
            ),
            notice_targets=targets,
            noticed=noticed,
            notices_complete=bool(targets) and noticed == targets,
            all_runs_within_budget=all(r.status != "BUDGET_EXHAUSTED" for r in runs),
        )
        success = (
            success
            and criteria["consult_stale_rejected"]
            and criteria["notices_complete"]
            and criteria["all_runs_within_budget"]
        )
    success = success and criteria["forbidden_actions"] == 0

    # matches_expected: Alpha = verify의 L1, Beta = 거절 고정 + L0 + try (A.21 9)
    world_model, world, task_a, _ = verify.load(pack_dir(pack_name))
    exp_alpha = verify.expected(world_model, world, task_a).get("L1")
    try_res = {task_a.id: sorted(set(asked))} if asked else {}
    frozen = set(pack.demo_rejections[0].target_task_ids)
    exp_beta = verify.expected_try(world_model, world, task_a, try_res, frozen) if try_res else None
    record.update(
        model_settings=model_settings(settings),
        model=next((r["model_id"] for r in rows if r["model_id"]), None),
        prompt_version=PROMPT_VERSION,
        success=success,
        success_criteria=criteria,
        alpha_matches_expected=alpha_actual is not None
        and _matches({"L1": exp_alpha}, alpha_actual, False),
        # B-decline은 Beta가 없는 경로라 None(해당 없음)
        beta_matches_expected=None
        if decline
        else beta_actual is not None
        and exp_beta is not None
        and _matches({"L0": exp_beta}, beta_actual, False),
        # 첫 SOLVE의 level (--path A와 같은 키로 요약에 집계한다)
        first_solve_level=next(
            (
                (s["action"].get("args") or {}).get("level")
                for s in steps
                if (s["action"] or {}).get("name") == "SOLVE_WITH_SCOPE"
            ),
            None,
        ),
        first_action_after_reject=None
        if reject_step is None
        else (reject_step["action"] or {}).get("name"),
        step_count=len(steps),
        actions=names,
        # 모든 Run의 Action(에이전트 첫 글자 접두어). Coordination을 켠 경로에서 협의·통지를 본다
        actions_all=[f"{row['agent'][0]}:{row['action'] or '-'}" for row in rows],
        runs=[
            {
                "run_id": r.run_id,
                "agent_type": r.agent_type,
                "phase": r.input_ref.get("phase"),
                "status": r.status,
                "end_reason": r.end_reason,
            }
            for r in runs
        ],
        events=events,
        messages=n_messages,
        steps=rows,
        run_status=run.status,
        end_reason=run.end_reason,
        human_rounds_used=run.human_rounds_used,
        solver_calls_used=run.solver_calls_used,
        committed=committed,
        alpha=alpha_actual,
        beta=beta_actual,
        tokens_in=sum(r["tokens_in"] for r in rows),
        tokens_out=sum(r["tokens_out"] for r in rows),
        llm_seconds=round(sum(c["ms"] for c in rec.calls) / 1000, 2),
    )


# ── 기본안 A: Coordination (부록 A.24) ─────────────────────────


def run_path_coord(index: int, settings: Settings, pack_name: str, raw: bool) -> dict:
    """COORDINATION_ENABLED를 켠 임시 DB에서 요청 A로 기본안 A를 끝까지 돌린다. 기록 1개."""
    tmp = tempfile.TemporaryDirectory(prefix="live_run_")
    old_env = _set_env({"DB_PATH": str(Path(tmp.name) / "live.db"), **_flag_env(True, False)})
    db.close()
    record: dict[str, Any] = {
        "index": index,
        "request": "A",
        "path": "coord",
        "agent_flags": {"coordination": True, "event_response": False},
    }
    t0 = time.perf_counter()
    try:
        db.init_db()
        pack = load_pack(pack_dir(pack_name))
        with db.write() as tx:
            seed_pack(tx, pack)
        _path_coord(record, settings, pack, raw)
    except Exception as e:  # noqa: BLE001 — 실패도 기록한다
        record.update(success=False, error=f"{type(e).__name__}: {e}")
    finally:
        db.close()
        _restore_env(old_env)
        tmp.cleanup()
    record["total_seconds"] = round(time.perf_counter() - t0, 2)
    return record


ANSWERABLE = ("QUESTION", "CHANGE_REQUEST", "CONFIRMATION")


def _open_requests(conn: Any) -> list[dict[str, Any]]:
    cur = conn.execute(
        "SELECT message_id, to_actor_id, type FROM message WHERE status = 'OPEN' ORDER BY rowid"
    )
    return [
        {"message_id": r[0], "to_actor_id": r[1], "type": r[2]}
        for r in cur.fetchall()
        if r[2] in ANSWERABLE
    ]


def _path_coord(record: dict[str, Any], settings: Settings, pack: Any, raw: bool) -> None:
    rec = _Recorder(time.monotonic() + RUN_DEADLINE_S * 2, raw)
    site_id = pack.site_id
    factory = lambda: _RecordingModel(openai_model(settings), rec)
    objection = pack.demo_rejections[0].comment
    form, requester = _form_and_requester(pack, pack.new_task.task_id)
    record["submitted"] = submit_task_request(pack, requester, _key(), form).status
    events: list[dict[str, Any]] = []
    alpha = committed = None
    for _ in range(MAX_HUMAN_TURNS * 2):
        run_until_idle(pack, model_factory=factory)
        with db.read() as conn:
            [rp_id] = [
                r[0]
                for r in conn.execute(
                    "SELECT run_id FROM agent_run WHERE agent_type = 'REPLANNING'"
                )
            ]
            rp, ref = _waiting(conn, rp_id)
            alpha = alpha or ref
            opened = _open_requests(conn)
            cand_v = (
                _pass_validation(conn, site_id, ref)
                if rp.wait_kind == "CANDIDATE_OUTCOME"
                else None
            )
            view = consultation_view(conn, site_id, ref) if cand_v else None
            ctx = get_site(conn, site_id).context_version
        if opened:
            m = opened[0]
            # 사람 역할: 변경 요청에는 이견, 제약 초안에는 확정, 담당자 질문에는 수락
            decision, comment = {
                "CHANGE_REQUEST": ("DECLINE", objection),
                "CONFIRMATION": ("ACCEPT", "live run 자동 확정"),
                "QUESTION": ("ACCEPT", "live run 자동 수락"),
            }[m["type"]]
            out = reply_message(
                pack,
                m["to_actor_id"],
                _key(),
                ReplyRequest(message_id=m["message_id"], decision=decision, comment=comment),
            )
            events.append({"reply": decision, "type": m["type"], "status": out.status})
            continue
        if cand_v is not None and view is not None and view.items_status == "COMPLETE":
            out = approve_and_commit(
                pack,
                "supervisor",
                _key(),
                ApproveRequest(
                    candidate_id=ref,
                    validation_id=cand_v.validation_id,
                    expected_context_version=ctx,
                ),
            )
            committed = {
                "candidate_id": ref,
                "status": out.status,
                "reasons": list(out.reason_codes),
            }
            events.append({"approve": ref, "status": out.status})
            run_until_idle(pack, model_factory=factory)  # 통지 Run
        break

    with db.read() as conn:
        runs = [
            get_run(conn, r[0]) for r in conn.execute("SELECT run_id FROM agent_run ORDER BY rowid")
        ]
        order = [
            (r[0], r[1])
            for r in conn.execute("SELECT run_id, step_no FROM agent_step ORDER BY rowid")
        ]
        by_run = {r.run_id: list_steps(conn, r.run_id) for r in runs}
        steps = [next(s for s in by_run[rid] if s["step_no"] == no) for rid, no in order]
        constraints = list_constraints(conn, site_id)
        cur = conn.execute("SELECT to_actor_id, type, body FROM message ORDER BY rowid")
        messages = [{"to": r[0], "type": r[1], "body": r[2]} for r in cur.fetchall()]
        plan_revision = get_site(conn, site_id).plan_revision
        alpha_v = _pass_validation(conn, site_id, alpha)
        beta_v = _pass_validation(conn, site_id, (committed or {}).get("candidate_id"))
    rows = _step_rows(steps, rec.calls)
    for row, s in zip(rows, steps, strict=True):
        row["run_id"], row["agent"] = (
            s["run_id"],
            next(r.agent_type for r in runs if r.run_id == s["run_id"]),
        )
    rp = next(r for r in runs if r.agent_type == "REPLANNING")
    coords = [r for r in runs if r.agent_type == "COORDINATION"]
    notice_run = next((r for r in coords if r.input_ref.get("phase") == "NOTICE"), None)
    c_task = next(t for t in pack.tasks if t.task_id in pack.demo_rejections[0].target_task_ids)
    drafts = [
        s for s in steps
        if (s["action"] or {}).get("name") == "DRAFT_CONSTRAINT" and s["guard"]["verdict"] == "ACCEPTED"
    ]  # fmt: skip
    targets = []
    if notice_run is not None:
        first = next((s for s in steps if s["run_id"] == notice_run.run_id), None)
        targets = sorted(
            t["actor_id"] for t in (first or {}).get("observation", {}).get("notice_targets", [])
        )
    noticed = sorted({m["to"] for m in messages if m["type"] == "NOTICE"})
    guards = [(s["guard"] or {}).get("reason_code") for s in steps]
    criteria: dict[str, Any] = {
        "alpha_pass": alpha_v is not None,
        "change_request_to_owner": any(
            m["type"] == "CHANGE_REQUEST" and m["to"] == c_task.owner_actor_id for m in messages
        ),
        "draft_ok": any(
            s["action"]["args"]["reason_code"] == "TASK_IMMOVABLE"
            and s["action"]["args"]["task_id"] == c_task.task_id
            and "TIME" in s["action"]["args"]["axes"]
            for s in drafts
        ),
        "constraint_from_proposal": any(
            c.task_id == c_task.task_id and c.source_type == "PROPOSAL" for c in constraints
        ),
        "beta_pass": beta_v is not None and (committed or {}).get("candidate_id") != alpha,
        "committed_r1": (committed or {}).get("status") == "APPLIED" and plan_revision == 1,
        "replanning_succeeded": rp.status == "SUCCEEDED",
        "notice_targets": targets,
        "noticed": noticed,
        "notices_complete": bool(targets) and noticed == targets,
        "notice_run_succeeded": notice_run is not None and notice_run.status == "SUCCEEDED",
        "forbidden_actions": guards.count("ACTION_NOT_AVAILABLE"),
        "malformed": guards.count("MALFORMED"),
        "llm_errors": guards.count("LLM_ERROR"),
        "within_budget": all(r.status != "BUDGET_EXHAUSTED" for r in runs),
    }
    keys = ("alpha_pass", "change_request_to_owner", "draft_ok", "constraint_from_proposal",
            "beta_pass", "committed_r1", "replanning_succeeded", "notices_complete",
            "notice_run_succeeded", "within_budget")  # fmt: skip
    success = (
        all(criteria[k] for k in keys)
        and criteria["forbidden_actions"] == 0
        and criteria["malformed"] == 0
    )
    record.update(
        model_settings=model_settings(settings),
        model=next((r["model_id"] for r in rows if r["model_id"]), None),
        prompt_version=PROMPT_VERSION,
        coordination_prompt_version=COORDINATION_PROMPT_VERSION,
        success=success,
        success_criteria=criteria,
        step_count=len(steps),
        draft_args=[s["action"]["args"] for s in drafts],
        actions=[f"{row['agent'][0]}:{row['action'] or '-'}" for row in rows],
        events=events,
        steps=rows,
        runs=[
            {
                "run_id": r.run_id,
                "agent_type": r.agent_type,
                "phase": r.input_ref.get("phase"),
                "status": r.status,
                "end_reason": r.end_reason,
                "steps_used": r.steps_used,
            }
            for r in runs
        ],
        run_status=rp.status,
        end_reason=rp.end_reason,
        committed=committed,
        tokens_in=sum(r["tokens_in"] for r in rows),
        tokens_out=sum(r["tokens_out"] for r in rows),
        llm_seconds=round(sum(c["ms"] for c in rec.calls) / 1000, 2),
    )


# ── Scene 4 최소 경로: Event Response (부록 A.25) ───────────────


class _Script:
    """R1 준비용 스크립트 응답(LLM 없음). 테스트의 ScriptedChatModel과 같은 프로토콜이다."""

    model_name = "script-setup"

    def __init__(self, replies: list[AIMessage]):
        self.replies = list(replies)

    def bind_tools(self, tools: Sequence[dict[str, Any]], **kwargs: Any) -> "_Script":
        return self

    def invoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        return self.replies.pop(0)


def _call(name: str, **args: Any) -> AIMessage:
    args = {"decision_summary": "R1 준비(스크립트)", **args}
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": _key()}])


def _setup_r1(pack: Any) -> None:
    """기본안 B로 R1(Beta 확정)까지 스크립트로 준비한다: 거절(C 고정) → LIST → ASK → 수락 → TRY → 승인."""
    site_id = pack.site_id
    form, requester = _form_and_requester(pack, pack.new_task.task_id)
    submit_task_request(pack, requester, _key(), form)
    solve = [_call("SOLVE_WITH_SCOPE", level="L0"), _call("SOLVE_WITH_SCOPE", level="L1")]
    run_until_idle(pack, model_factory=lambda: _Script(solve))
    with db.read() as conn:
        [run_id] = [r[0] for r in conn.execute("SELECT run_id FROM agent_run")]
        _, alpha = _waiting(conn, run_id)
        alpha_v = _pass_validation(conn, site_id, alpha)
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
    ask = _call(
        "ASK_TASK_OWNER",
        task_id=pack.new_task.task_id,
        axis="RESOURCE",
        allowed_values=["SITE-CR-01"],
        question="대체 자원 확인",
    )
    listing = _call("LIST_ASSIGNABLE_RESOURCES", task_id=pack.new_task.task_id)
    run_until_idle(pack, model_factory=lambda: _Script([listing, ask]))
    with db.read() as conn:
        _, mid = _waiting(conn, run_id)
        to = get_message(conn, site_id, mid)["to_actor_id"]
    reply_message(pack, to, _key(), ReplyRequest(message_id=mid, decision="ACCEPT"))
    try_ = _call(
        "TRY_ALTERNATIVE_RESOURCE", task_id=pack.new_task.task_id, resource_id="SITE-CR-01"
    )
    run_until_idle(pack, model_factory=lambda: _Script([try_]))
    with db.read() as conn:
        _, beta = _waiting(conn, run_id)
        beta_v = _pass_validation(conn, site_id, beta)
        ctx = get_site(conn, site_id).context_version
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


def run_path_event(
    index: int, settings: Settings, pack_name: str, raw: bool, coord: bool, ambiguous: bool = False
) -> dict:
    """임시 DB에서 R1을 스크립트로 준비하고, 지연 신고부터 실제 모델로 Scene 4 최소 경로를 돌린다."""
    tmp = tempfile.TemporaryDirectory(prefix="live_run_")
    names = ("DB_PATH", "EVENT_RESPONSE_ENABLED", "COORDINATION_ENABLED")
    old = {n: os.environ.get(n) for n in names}
    os.environ["DB_PATH"] = str(Path(tmp.name) / "live.db")
    os.environ["EVENT_RESPONSE_ENABLED"] = "false"
    os.environ["COORDINATION_ENABLED"] = "false"
    get_settings.cache_clear()
    db.close()
    record: dict[str, Any] = {
        "index": index,
        "request": "event",
        "path": "event-ambiguous" if ambiguous else "event",
        "coord": coord,
        "ambiguous": ambiguous,
        # 신고부터 잰 구간의 설정. R1 준비(스크립트)는 둘 다 끈다 (A.28)
        "agent_flags": {"coordination": coord, "event_response": True},
    }
    t0 = time.perf_counter()
    try:
        db.init_db()
        pack = load_pack(pack_dir(pack_name))
        with db.write() as tx:
            seed_pack(tx, pack)
        _setup_r1(pack)
        # 신고부터 설정을 켠다 (R1 준비 중 Coordination이 시작하지 않게)
        os.environ["EVENT_RESPONSE_ENABLED"] = "true"
        os.environ["COORDINATION_ENABLED"] = "true" if coord else "false"
        get_settings.cache_clear()
        _path_event(record, settings, pack, raw, coord, ambiguous)
    except Exception as e:  # noqa: BLE001 — 실패도 기록한다
        record.update(success=False, error=f"{type(e).__name__}: {e}")
    finally:
        db.close()
        for n, v in old.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v
        get_settings.cache_clear()
        tmp.cleanup()
    record["total_seconds"] = round(time.perf_counter() - t0, 2)
    return record


def _path_event(
    record: dict[str, Any],
    settings: Settings,
    pack: Any,
    raw: bool,
    coord: bool,
    ambiguous: bool = False,
) -> None:
    rec = _Recorder(time.monotonic() + RUN_DEADLINE_S * 2, raw)
    site_id = pack.site_id
    factory = lambda: _RecordingModel(openai_model(settings), rec)
    with db.read() as conn:
        before = {r[0] for r in conn.execute("SELECT run_id FROM agent_run")}
        last_step = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM agent_step").fetchone()[0]
    # 모호 신고(시각 없음)는 demo_events[1], 되물으면 신고자가 answer로 답한다 (A.25 S4)
    report = pack.demo_events[1 if ambiguous else 0]
    expected_new = 75 if ambiguous else 60  # 10:15 / 10:00 (verify_demo_values Delta·Gamma)
    expected_delay = 30 if ambiguous else 15
    out = receive_event(
        pack,
        "reporter",
        _key(),
        EventReport(source_event_id=_key(), event_type=report.event_type, text=report.text),
    )
    hold_id = out.result_refs["hold_id"]
    events: list[dict[str, Any]] = [{"report": report.text, "status": out.status}]
    answer_kinds: list[str] = []  # 질문별로 보낸 답 종류 (A.27)
    committed = gamma = None
    released = None
    for _ in range(MAX_HUMAN_TURNS * 2):
        run_until_idle(pack, model_factory=factory)
        with db.read() as conn:
            opened = _open_requests(conn)
            hold = conn.execute("SELECT status FROM hold WHERE hold_id = ?", (hold_id,)).fetchone()[
                0
            ]
            confirmed = conn.execute(
                "SELECT COUNT(*) FROM proposal WHERE type = 'FACT_UPDATE' AND status = 'CONFIRMED'"
            ).fetchone()[0]
            rp = conn.execute(
                "SELECT run_id FROM agent_run WHERE agent_type = 'REPLANNING' ORDER BY rowid DESC"
            ).fetchone()[0]
            run, ref = _waiting(conn, rp)
            cand_v = (
                _pass_validation(conn, site_id, ref)
                if run.run_id not in before and run.wait_kind == "CANDIDATE_OUTCOME"
                else None
            )
            view = consultation_view(conn, site_id, ref) if cand_v else None
            ctx = get_site(conn, site_id).context_version
        if opened:
            m = opened[0]
            # 신고자 되묻기(제안 없는 질문)에는 ANSWER: 첫 질문은 scenario 답 문장, 두 번째부터는
            # "앞에서 답한 것이 전부" (A.27). 나머지는 수락
            kind = None
            if m["type"] == "QUESTION":
                first = report.answer or f"신고 문장 그대로입니다: {report.text}"
                kind, comment = _human_answer(len(answer_kinds), first, report.text)
                answer_kinds.append(kind)
                decision = "ANSWER"
            else:
                decision, comment = "ACCEPT", "live run 자동 수락"
            out = reply_message(
                pack,
                m["to_actor_id"],
                _key(),
                ReplyRequest(message_id=m["message_id"], decision=decision, comment=comment),
            )
            events.append(
                {"reply": decision, "type": m["type"], "answer_kind": kind, "status": out.status}
            )
            continue
        if hold == "ACTIVE" and confirmed:
            out = release_hold_command(
                pack,
                "supervisor",
                _key(),
                HoldRelease(
                    hold_id=hold_id,
                    resolution="FACT_CONFIRMED",
                    expected_context_version=ctx,
                    comment="live run 사실 확인 해제",
                ),
            )
            released = out.status
            events.append({"release": "FACT_CONFIRMED", "status": out.status})
            continue
        if cand_v is not None and view is not None:
            gamma = ref
            if view.items_status != "COMPLETE" and not coord:
                pending = tuple(t for t, st in view.item_status.items() if st == "PENDING")
                waive(
                    pack,
                    "supervisor",
                    _key(),
                    WaiveRequest(candidate_id=ref, task_ids=pending, comment="live run 자동 수용"),
                )
                with db.read() as conn:
                    view = consultation_view(conn, site_id, ref)
            if view.items_status == "COMPLETE":
                out = approve_and_commit(
                    pack,
                    "supervisor",
                    _key(),
                    ApproveRequest(
                        candidate_id=ref,
                        validation_id=cand_v.validation_id,
                        expected_context_version=ctx,
                    ),
                )
                committed = {
                    "candidate_id": ref,
                    "status": out.status,
                    "reasons": list(out.reason_codes),
                }
                events.append({"approve": ref, "status": out.status})
                run_until_idle(pack, model_factory=factory)  # 통지 Run(--coord)
        break

    with db.read() as conn:
        runs = [
            get_run(conn, r[0])
            for r in conn.execute("SELECT run_id FROM agent_run ORDER BY rowid")
            if r[0] not in before
        ]
        order = [
            (r[0], r[1])
            for r in conn.execute(
                "SELECT run_id, step_no FROM agent_step WHERE rowid > ? ORDER BY rowid",
                (last_step,),
            )
        ]
        by_run = {r.run_id: list_steps(conn, r.run_id) for r in runs}
        steps = [next(s for s in by_run[rid] if s["step_no"] == no) for rid, no in order]
        facts = conn.execute(
            "SELECT target_task_id, payload, status FROM proposal WHERE type = 'FACT_UPDATE'"
        ).fetchall()
        hold_row = conn.execute(
            "SELECT status, resolution FROM hold WHERE hold_id = ?", (hold_id,)
        ).fetchone()
        cur = conn.execute("SELECT to_actor_id, type FROM message ORDER BY rowid")
        messages = [{"to": r[0], "type": r[1]} for r in cur.fetchall()]
        plan_revision = get_site(conn, site_id).plan_revision
        cand = get_candidate(conn, site_id, gamma) if gamma else None
        gamma_step = next(
            (s for s in steps if gamma and (s["tool_result"] or {}).get("candidate_id") == gamma),
            None,
        )
    rows = _step_rows(steps, rec.calls)
    for row, s in zip(rows, steps, strict=True):
        row["run_id"] = s["run_id"]
        row["agent"] = next(r.agent_type for r in runs if r.run_id == s["run_id"])
    er = next((r for r in runs if r.agent_type == "EVENT_RESPONSE"), None)
    proposes = [
        s["action"]["args"]
        for s in steps
        if (s["action"] or {}).get("name") == "PROPOSE_FACT_UPDATE"
        and s["guard"]["verdict"] == "ACCEPTED"
    ]
    notice_run = next(
        (
            r
            for r in runs
            if r.agent_type == "COORDINATION" and r.input_ref.get("phase") == "NOTICE"
        ),
        None,
    )
    targets = []
    if notice_run is not None:
        first = next((s for s in steps if s["run_id"] == notice_run.run_id), None)
        targets = sorted(
            t["actor_id"] for t in (first or {}).get("observation", {}).get("notice_targets", [])
        )
    noticed = sorted({m["to"] for m in messages if m["type"] == "NOTICE"} & set(targets))
    placed = {a.task_id: a.start for a in cand.assignments} if cand else {}
    guards = [(s["guard"] or {}).get("reason_code") for s in steps]
    criteria: dict[str, Any] = {
        "asks": sum(
            1
            for s in steps
            if (s["action"] or {}).get("name") == "ASK_REPORTER"
            and s["guard"]["verdict"] == "ACCEPTED"
        ),
        "proposed_e_60": any(
            p["task_id"] == "E" and p["new_earliest_start"] == expected_new for p in proposes
        ),
        "fact_confirmed": any(r[0] == "E" and r[2] == "CONFIRMED" for r in facts),
        "hold_fact_confirmed": tuple(hold_row) == ("RELEASED", "FACT_CONFIRMED"),
        "er_succeeded": er is not None and er.status == "SUCCEEDED",
        "gamma_e_60": placed.get("E") == expected_new,
        "gamma_changed_delay": None
        if gamma_step is None
        else [
            gamma_step["tool_result"]["stage1"]["changed"],
            (gamma_step["tool_result"]["stage2"] or {}).get("delay"),
        ],
        "committed_r2": (committed or {}).get("status") == "APPLIED" and plan_revision == 2,
        "notice_targets": targets,
        "noticed": noticed,
        "forbidden_actions": guards.count("ACTION_NOT_AVAILABLE"),
        "malformed": guards.count("MALFORMED"),
        "llm_errors": guards.count("LLM_ERROR"),
        "within_budget": all(r.status != "BUDGET_EXHAUSTED" for r in runs),
    }
    keys = ["proposed_e_60", "fact_confirmed", "hold_fact_confirmed", "er_succeeded",
            "gamma_e_60", "committed_r2", "within_budget"]  # fmt: skip
    success = (
        all(criteria[k] for k in keys)
        and criteria["gamma_changed_delay"] == [1, expected_delay]
        and (not ambiguous or criteria["asks"] >= 1)
        and criteria["forbidden_actions"] == 0
        and criteria["malformed"] == 0
        and (not coord or (bool(targets) and noticed == targets))
    )
    record.update(
        model_settings=model_settings(settings),
        model=next((r["model_id"] for r in rows if r["model_id"]), None),
        prompt_version=PROMPT_VERSION,
        coordination_prompt_version=COORDINATION_PROMPT_VERSION,
        event_response_prompt_version=EVENT_RESPONSE_PROMPT_VERSION,
        success=success,
        success_criteria=criteria,
        step_count=len(steps),
        propose_args=proposes,
        # ER 조회 조건(모두)과 ER step 수 — 같은 조건 반복을 센다 (A.25 S2 뒤)
        lookup_args=[
            s["action"]["args"] for s in steps if (s["action"] or {}).get("name") == "LOOKUP_TASKS"
        ],
        er_steps=sum(1 for row in rows if row["agent"] == "EVENT_RESPONSE"),
        answer_kinds=answer_kinds,
        released=released,
        actions=[f"{row['agent'][0]}:{row['action'] or '-'}" for row in rows],
        events=events,
        steps=rows,
        runs=[
            {
                "run_id": r.run_id,
                "agent_type": r.agent_type,
                "phase": r.input_ref.get("phase"),
                "status": r.status,
                "end_reason": r.end_reason,
                "steps_used": r.steps_used,
            }
            for r in runs
        ],
        run_status=None if er is None else er.status,
        end_reason=None if er is None else er.end_reason,
        committed=committed,
        tokens_in=sum(r["tokens_in"] for r in rows),
        tokens_out=sum(r["tokens_out"] for r in rows),
        llm_seconds=round(sum(c["ms"] for c in rec.calls) / 1000, 2),
    )


# ── Work Intake (부록 A.26) ───────────────────────────────────


def run_path_intake(
    index: int, settings: Settings, pack_name: str, raw: bool, ambiguous: bool
) -> dict:
    """임시 DB에서 자연어 요청 A → Intake → Replanning Alpha까지 실제 모델로 돌린다."""
    tmp = tempfile.TemporaryDirectory(prefix="live_run_")
    old_env = _set_env({"DB_PATH": str(Path(tmp.name) / "live.db"), **_flag_env(False, False)})
    get_settings.cache_clear()
    db.close()
    path = "intake-ambiguous" if ambiguous else "intake"
    record: dict[str, Any] = {
        "index": index,
        "request": "A",
        "path": path,
        "ambiguous": ambiguous,
        "agent_flags": {"coordination": False, "event_response": False},
    }
    t0 = time.perf_counter()
    try:
        db.init_db()
        pack = load_pack(pack_dir(pack_name))
        with db.write() as tx:
            seed_pack(tx, pack)
        _path_intake(record, settings, pack, pack_name, raw, ambiguous)
    except Exception as e:  # noqa: BLE001 — 실패도 기록한다
        record.update(success=False, error=f"{type(e).__name__}: {e}")
    finally:
        db.close()
        _restore_env(old_env)
        tmp.cleanup()
    record["total_seconds"] = round(time.perf_counter() - t0, 2)
    return record


def _path_intake(
    record: dict[str, Any],
    settings: Settings,
    pack: Any,
    pack_name: str,
    raw: bool,
    ambiguous: bool,
) -> None:
    rec = _Recorder(time.monotonic() + RUN_DEADLINE_S * 2, raw)
    site_id = pack.site_id
    factory = lambda: _RecordingModel(openai_model(settings), rec)
    demo = pack.demo_intakes[1 if ambiguous else 0]
    # 명확한 요청에 질문이 오면 요청 문장 그대로 답한다(값은 문장에 다 있다)
    answer = demo.answer or f"요청 문장에 적은 대로입니다: {demo.text}"
    answer_kinds: list[str] = []  # 질문별로 보낸 답 종류 (A.27)
    out = submit_intake(
        pack, demo.requester, _key(), IntakeRequest(task_id=demo.task_id, text=demo.text)
    )
    events: list[dict[str, Any]] = [{"intake": demo.label, "status": out.status}]
    for _ in range(MAX_HUMAN_TURNS * 2):
        run_until_idle(pack, model_factory=factory)
        with db.read() as conn:
            opened = _open_requests(conn)
        if not opened:
            break
        m = opened[0]
        kind = None
        if m["type"] == "QUESTION":
            # 첫 질문은 scenario 답, 두 번째부터는 "앞에서 답한 것이 전부" (A.27)
            kind, comment = _human_answer(len(answer_kinds), answer, demo.text)
            answer_kinds.append(kind)
            decision = "ANSWER"
        else:
            decision, comment = "ACCEPT", ""
        out = reply_message(
            pack,
            m["to_actor_id"],
            _key(),
            ReplyRequest(message_id=m["message_id"], decision=decision, comment=comment),
        )
        events.append(
            {"reply": decision, "type": m["type"], "answer_kind": kind, "status": out.status}
        )

    with db.read() as conn:
        runs = [
            get_run(conn, r[0]) for r in conn.execute("SELECT run_id FROM agent_run ORDER BY rowid")
        ]
        order = [
            (r[0], r[1])
            for r in conn.execute("SELECT run_id, step_no FROM agent_step ORDER BY rowid")
        ]
        by_run = {r.run_id: list_steps(conn, r.run_id) for r in runs}
        steps = [next(s for s in by_run[rid] if s["step_no"] == no) for rid, no in order]
        task = next(
            (t for t in list_current_tasks(conn, site_id, pack) if t.task_id == demo.task_id),
            None,
        )
        consents = conn.execute(
            "SELECT axis, scope, source_ref FROM consent WHERE task_id = ? ORDER BY rowid",
            (demo.task_id,),
        ).fetchall()
        rp = next((r for r in runs if r.agent_type == "REPLANNING"), None)
        alpha = rp.wait_ref if rp is not None and rp.status == "WAITING_HUMAN" else None
        alpha_v = _pass_validation(conn, site_id, alpha)
        alpha_step = next(
            (s for s in steps if alpha and (s["tool_result"] or {}).get("candidate_id") == alpha),
            None,
        )
        alpha_actual = _actual(conn, site_id, alpha, alpha_step) if alpha and alpha_step else None
    rows = _step_rows(steps, rec.calls)
    for row, s in zip(rows, steps, strict=True):
        row["run_id"] = s["run_id"]
        row["agent"] = next(r.agent_type for r in runs if r.run_id == s["run_id"])
    intake = next((r for r in runs if r.agent_type == "INTAKE"), None)
    names = [
        (s["action"] or {}).get("name")
        for s in steps
        if next(r.agent_type for r in runs if r.run_id == s["run_id"]) == "INTAKE"
    ]
    asks = sum(
        1
        for s in steps
        if (s["action"] or {}).get("name") == "ASK_CLARIFICATION"
        and s["guard"]["verdict"] == "ACCEPTED"
    )
    nt = pack.new_task
    keys = ("work_type", "zone_id", "duration", "earliest_start", "latest_start", "latest_end",
            "required_resource_type", "requested_resource_id")  # fmt: skip
    sources = {f.source_ref for f in task.fields.values()} if task else set()
    expected_consents = [
        ("TIME", {"start_min": nt.earliest_start, "start_max": nt.latest_start}),
        ("RESOURCE", {"resource_ids": [nt.requested_resource_id]}),
    ]
    world_model, world, task_a, _ = verify.load(pack_dir(pack_name))
    exp_alpha = verify.expected(world_model, world, task_a).get("L1")
    guards = [(s["guard"] or {}).get("reason_code") for s in steps]
    criteria: dict[str, Any] = {
        "intake_succeeded": intake is not None and intake.status == "SUCCEEDED",
        "values_match_new_task": task is not None
        and all(getattr(task, k) == getattr(nt, k) for k in keys),
        "fields_confirmed_from_message": task is not None
        and {f.status for f in task.fields.values()} == {"CONFIRMED"}
        and len(sources) == 1
        and next(iter(sources)).startswith("message:"),
        "consents_like_form": [(c[0], json.loads(c[1])) for c in consents] == expected_consents
        and all(c[2].startswith("message:") for c in consents),
        "alpha_pass": alpha_v is not None,
        "alpha_matches_expected": alpha_actual is not None
        and _matches({"L1": exp_alpha}, alpha_actual, False),
        "asks": asks,
        "forbidden_actions": guards.count("ACTION_NOT_AVAILABLE"),
        "malformed": guards.count("MALFORMED"),
        "llm_errors": guards.count("LLM_ERROR"),
        "within_budget": all(r.status != "BUDGET_EXHAUSTED" for r in runs),
    }
    keys_ok = ("intake_succeeded", "values_match_new_task", "fields_confirmed_from_message",
               "consents_like_form", "alpha_pass", "alpha_matches_expected", "within_budget")  # fmt: skip
    success = (
        all(criteria[k] for k in keys_ok)
        and criteria["forbidden_actions"] == 0
        and criteria["malformed"] == 0
        and (not ambiguous or asks >= 1)
    )
    record.update(
        model_settings=model_settings(settings),
        model=next((r["model_id"] for r in rows if r["model_id"]), None),
        prompt_version=PROMPT_VERSION,
        intake_prompt_version=INTAKE_PROMPT_VERSION,
        success=success,
        success_criteria=criteria,
        step_count=len(steps),
        intake_steps=len(names),
        intake_actions=names,
        asks=asks,
        answer_kinds=answer_kinds,
        # 값 확인 요청이 막힌 사유(TASKSPEC_INVALID의 폼 사유 코드) (A.26 intake-p2)
        request_rejections=[
            (s["tool_result"] or {}).get("reason_codes")
            for s in steps
            if (s["guard"] or {}).get("reason_code") == "TASKSPEC_INVALID"
        ],
        actions=[f"{row['agent'][0]}:{row['action'] or '-'}" for row in rows],
        events=events,
        steps=rows,
        runs=[
            {
                "run_id": r.run_id,
                "agent_type": r.agent_type,
                "status": r.status,
                "end_reason": r.end_reason,
                "steps_used": r.steps_used,
                "human_rounds_used": r.human_rounds_used,
            }
            for r in runs
        ],
        run_status=None if intake is None else intake.status,
        end_reason=None if intake is None else intake.end_reason,
        alpha=alpha_actual,
        tokens_in=sum(r["tokens_in"] for r in rows),
        tokens_out=sum(r["tokens_out"] for r in rows),
        llm_seconds=round(sum(c["ms"] for c in rec.calls) / 1000, 2),
    )


def _run_request(
    index: int,
    position: int,
    name: str,
    settings: Settings,
    pack: Any,
    raw: bool,
    real_solve: Any,
) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    t0 = time.perf_counter()
    rec = _Recorder(time.monotonic() + RUN_DEADLINE_S, raw)
    solver_s = 0.0

    def timed_solve(*args: Any) -> Any:
        nonlocal solver_s
        s = time.perf_counter()
        try:
            return real_solve(*args)
        finally:
            solver_s += time.perf_counter() - s

    cpsat.solve = timed_solve
    record: dict[str, Any] = {
        "index": index,
        "request": name,
        "position": position,
        "started_at": started_at,
    }
    try:
        with db.read() as conn:
            before = {r[0] for r in conn.execute("SELECT run_id FROM agent_run")}
        form, requester = _form_and_requester(pack, name)
        submitted = submit_task_request(pack, requester, _key(), form)
        record["submitted"] = {
            "status": submitted.status,
            "reason_codes": list(submitted.reason_codes),
        }
        run_until_idle(pack, model_factory=lambda: _RecordingModel(openai_model(settings), rec))

        with db.read() as conn:
            new = [
                r[0]
                for r in conn.execute("SELECT run_id FROM agent_run ORDER BY rowid")
                if r[0] not in before
            ]
            run = get_run(conn, new[-1]) if new else None
            steps = list_steps(conn, run.run_id) if run else []
            cid = run.wait_ref if run and run.status == "WAITING_HUMAN" else None
            validations = list_validations(conn, pack.site_id, cid) if cid else []
            view = consultation_view(conn, pack.site_id, cid) if cid else None
            cand_step = next(
                (s for s in steps if (s["tool_result"] or {}).get("candidate_id")), None
            )
            actual = (
                _actual(conn, pack.site_id, cand_step["tool_result"]["candidate_id"], cand_step)
                if cand_step
                else None
            )
        passing = [v for v in validations if v.status == "PASS"]

        committed = None
        if cid and passing and view is not None:
            pending = tuple(t for t, st in view.item_status.items() if st == "PENDING")
            if pending:
                body = WaiveRequest(
                    candidate_id=cid, task_ids=pending, comment="live run 자동 수용"
                )
                waive(pack, "supervisor", _key(), body)
            with db.read() as conn:
                ctx = get_site(conn, pack.site_id).context_version
            out = approve_and_commit(
                pack,
                "supervisor",
                _key(),
                ApproveRequest(
                    candidate_id=cid,
                    validation_id=passing[-1].validation_id,
                    expected_context_version=ctx,
                ),
            )
            committed = {"status": out.status, "reason_codes": list(out.reason_codes)}
            with db.read() as conn:
                run = get_run(conn, run.run_id)

        rows = _step_rows(steps, rec.calls)
        solves = [r for r in rows if r["action"] == "SOLVE_WITH_SCOPE"]
        guards = [(r["guard"] or {}).get("reason_code") for r in rows]
        criteria = {
            "pass_reached": bool(passing),
            "forbidden_actions": guards.count("ACTION_NOT_AVAILABLE"),
            "malformed": guards.count("MALFORMED"),
            "llm_errors": guards.count("LLM_ERROR"),
            "within_budget": run is not None and run.status != "BUDGET_EXHAUSTED",
        }
        record.update(
            model_settings=model_settings(settings),
            model=next((r["model_id"] for r in rows if r["model_id"]), None),
            prompt_version=PROMPT_VERSION,
            success=criteria["pass_reached"]
            and criteria["forbidden_actions"] == 0
            and criteria["within_budget"],
            success_criteria=criteria,
            first_solve_level=solves[0]["level"] if solves else None,
            l0_first=bool(solves) and solves[0]["level"] == "L0",
            steps=rows,
            run_status=None if run is None else run.status,
            end_reason=None if run is None else run.end_reason,
            committed=committed,
            actual=actual,
            tokens_in=sum(r["tokens_in"] for r in rows),
            tokens_out=sum(r["tokens_out"] for r in rows),
            llm_seconds=round(sum(c["ms"] for c in rec.calls) / 1000, 2),
            solver_seconds=round(solver_s, 2),
        )
    except Exception as e:  # noqa: BLE001 — 한 요청의 실패를 기록하고 다음으로 간다
        record.update(success=False, error=f"{type(e).__name__}: {e}")
    finally:
        cpsat.solve = real_solve
    record["total_seconds"] = round(time.perf_counter() - t0, 2)
    return record


def _na(value: Any) -> Any:
    return "해당 없음" if value is None else value


def _line(r: dict[str, Any]) -> str:
    if r.get("path") in ("coord", "event", "event-ambiguous", "intake", "intake-ambiguous"):
        return (
            f"#{r['index']} path {r['path']}{' --coord' if r.get('coord') else ''} success={r.get('success')} "
            f"run={r.get('run_status')}/{r.get('end_reason')} steps={r.get('step_count')} "
            f"{r.get('total_seconds')}s  {' → '.join(r.get('actions', []))}  "
            f"criteria={r.get('success_criteria')}"
            + (f"  error={r['error']}" if r.get("error") else "")
        )
    if r.get("path"):
        c = r.get("success_criteria") or {}
        return (
            f"#{r['index']} path {r['path']} success={r.get('success')} "
            f"run={r.get('run_status')}/{r.get('end_reason')} steps={r.get('step_count')} "
            f"alpha_matches={r.get('alpha_matches_expected')} "
            f"beta_matches={_na(r.get('beta_matches_expected'))} "
            f"after_reject={r.get('first_action_after_reject')} {r.get('total_seconds')}s  "
            f"{' → '.join(a or '-' for a in r.get('actions', []))}  criteria={c}"
            + (f"  error={r['error']}" if r.get("error") else "")
        )
    steps = " → ".join(
        f"{s['action'] or '-'}{'(' + s['level'] + ')' if s['level'] else ''}:{s['result_kind']}"
        for s in r.get("steps", [])
    )
    return (
        f"#{r['index']} {r.get('request', '?')} success={r.get('success')} "
        f"expected={r.get('expected_outcome')} matches={r.get('matches_expected')} "
        f"l0_first={r.get('l0_first')} run={r.get('run_status')}/{r.get('end_reason')} "
        f"{r.get('total_seconds')}s  {steps}" + (f"  error={r['error']}" if r.get("error") else "")
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="실제 모델로 시연 요청 live run")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--pack", default=None)
    parser.add_argument(
        "--request",
        default=None,
        help="scenario.yaml의 시연 요청 이름(쉼표로 여러 개, 순서대로 하나씩 확정). 기본은 new_task(A)",
    )
    parser.add_argument("--raw", action="store_true", help="prompt·응답 원문을 기록에 넣는다")
    parser.add_argument(
        "--coord",
        action="store_true",
        help="--path event·B에서 Coordination도 켠다(A.25·A.28)",
    )
    parser.add_argument(
        "--ambiguous", action="store_true", help="--path intake에서 모호한 요청을 쓴다(A.26)"
    )
    parser.add_argument(
        "--path",
        choices=PATHS,
        default="A",
        help="B: 기본안 B(거절 → 담당자 확인 수락 → Beta 승인), B-decline: 확인을 거절 → 이관. 요청 A만",
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
    if args.coord and args.path not in ("event", "B"):
        parser.error("--coord는 --path event 또는 B에서만 쓴다")
    if args.path != "A" and requests != [known[0]]:
        parser.error(f"--path {args.path} runs only --request {known[0]}")

    if not settings.openai_api_key.get_secret_value() or not settings.openai_model:
        print("OPENAI_API_KEY와 OPENAI_MODEL을 .env에 넣은 뒤 실행한다.", file=sys.stderr)
        return 2
    os.environ["LANGSMITH_TRACING"] = "false"

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    print(
        f"model settings: {model_settings(settings)}  prompt: {PROMPT_VERSION}  requests: {requests}"
    )
    records = []
    for i in range(args.runs):
        if args.path == "A":
            batch = run_once(i + 1, settings, pack_name, args.raw, requests)
        elif args.path == "coord":
            batch = [run_path_coord(i + 1, settings, pack_name, args.raw)]
        elif args.path == "intake":
            batch = [run_path_intake(i + 1, settings, pack_name, args.raw, args.ambiguous)]
        elif args.path == "event":
            batch = [
                run_path_event(i + 1, settings, pack_name, args.raw, args.coord, args.ambiguous)
            ]
        else:
            decline = args.path == "B-decline"
            batch = [run_path_b(i + 1, settings, pack_name, args.raw, decline, args.coord)]
        for r in batch:
            records.append(r)
            with out.open("a", encoding="utf-8") as f:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            print(_line(r))

    ok = sum(1 for r in records if r.get("success"))
    l0 = sum(1 for r in records if r.get("l0_first"))
    match = sum(1 for r in records if r.get("matches_expected"))
    paths = [r for r in records if r.get("path")]
    tokens = sum(r.get("tokens_in", 0) + r.get("tokens_out", 0) for r in records)
    n = len(records)
    if paths and paths[0]["path"] == "coord":
        print(f"success {ok}/{n} (기본안 A), tokens {tokens}")
    elif paths and paths[0]["path"].startswith("intake"):
        asks = [r.get("asks") for r in paths]
        print(f"success {ok}/{n} ({paths[0]['path']}), asks {asks}, tokens {tokens}")
    elif paths and paths[0]["path"].startswith("event"):
        print(
            f"success {ok}/{n} (Scene 4 최소 경로, coord={paths[0].get('coord')}), tokens {tokens}"
        )
    elif paths:  # 기본안 B: Alpha·Beta 기대값 일치를 따로 센다 (B-decline에는 Beta가 없다)
        alpha = sum(1 for r in paths if r.get("alpha_matches_expected"))
        with_beta = [r for r in paths if r.get("beta_matches_expected") is not None]
        beta = sum(1 for r in with_beta if r["beta_matches_expected"])
        beta_text = f"{beta}/{len(with_beta)}" if with_beta else "해당 없음"
        print(
            f"success {ok}/{n}, L0 first {l0}/{n}, alpha matches {alpha}/{n}, "
            f"beta matches {beta_text}, tokens {tokens}"
        )
    else:
        print(f"success {ok}/{n}, L0 first {l0}/{n}, matches expected {match}/{n}, tokens {tokens}")
    print(f"saved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
