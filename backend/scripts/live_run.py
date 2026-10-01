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
- --path B-decline: 같은 흐름에서 ACCEPT 대신 DECLINE(comment "live run 자동 거절"). 성공 = Alpha PASS ∧ ASK
  1회 ∧ DECLINE 적용 ∧ 거절 뒤 ASK·TRY 없음 ∧ Run ESCALATED ∧ 금지 Action 0 ∧ Budget 안.
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
from app.commands.messages import ReplyRequest, reply_message
from app.commands.task_request import TaskRequestForm, submit_task_request
from app.config import REPO_ROOT, Settings, get_settings
from app.coordinator.dispatcher import run_until_idle
from app.domain.calendar import work_delay
from app.packs.loader import load_pack, pack_dir
from app.solver import cpsat
from app.store import db
from app.store.repos.consultations import consultation_view
from app.store.repos.messages import get_message
from app.store.repos.records import get_candidate, get_snapshot, list_validations
from app.store.repos.runs import get_run, list_steps
from app.store.repos.seed import seed_pack
from app.store.repos.site import get_site
from scripts import verify_demo_values as verify

OUT_DIR = REPO_ROOT / "data" / "live_runs"
RUN_DEADLINE_S = 300
MAX_RUNS = 10
PATHS = ("A", "B", "B-decline")
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


def _key() -> str:
    return uuid.uuid4().hex


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
    old_db = os.environ.get("DB_PATH")
    os.environ["DB_PATH"] = str(Path(tmp.name) / "live.db")
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
        if old_db is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = old_db
        get_settings.cache_clear()
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


def run_path_b(index: int, settings: Settings, pack_name: str, raw: bool, decline: bool) -> dict:
    """임시 DB에서 요청 A로 기본안 B(또는 B-decline)를 끝까지 돌린다. 기록 1개."""
    tmp = tempfile.TemporaryDirectory(prefix="live_run_")
    old_db = os.environ.get("DB_PATH")
    os.environ["DB_PATH"] = str(Path(tmp.name) / "live.db")
    get_settings.cache_clear()
    db.close()
    real_solve = cpsat.solve
    path = "B-decline" if decline else "B"
    record: dict[str, Any] = {"index": index, "request": "A", "path": path}
    t0 = time.perf_counter()
    try:
        db.init_db()
        pack = load_pack(pack_dir(pack_name))
        with db.write() as tx:
            seed_pack(tx, pack)
        _path_b(record, settings, pack, pack_name, raw, decline, real_solve)
    except Exception as e:  # noqa: BLE001 — 실패도 기록한다
        record.update(success=False, error=f"{type(e).__name__}: {e}")
    finally:
        cpsat.solve = real_solve
        db.close()
        if old_db is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = old_db
        get_settings.cache_clear()
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
) -> None:
    rec = _Recorder(time.monotonic() + RUN_DEADLINE_S * 2, raw)
    site_id = pack.site_id
    factory = lambda: _RecordingModel(openai_model(settings), rec)
    form, requester = _form_and_requester(pack, pack.new_task.task_id)
    record["submitted"] = submit_task_request(pack, requester, _key(), form).status
    run_until_idle(pack, model_factory=factory)
    with db.read() as conn:
        [run_id] = [r[0] for r in conn.execute("SELECT run_id FROM agent_run")]
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

    with db.read() as conn:
        run = get_run(conn, run_id)
        steps = list_steps(conn, run_id)
        plan_revision = get_site(conn, site_id).plan_revision
        beta_step = next(
            (s for s in steps if beta and (s["tool_result"] or {}).get("candidate_id") == beta),
            None,
        )
        beta_actual = _actual(conn, site_id, beta, beta_step) if beta and beta_step else None
        n_messages = conn.execute("SELECT COUNT(*) FROM message").fetchone()[0]
    rows = _step_rows(steps, rec.calls)
    names = [(s["action"] or {}).get("name") for s in steps]
    guards = [(s["guard"] or {}).get("reason_code") for s in steps]
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
        beta_matches_expected=beta_actual is not None
        and exp_beta is not None
        and _matches({"L0": exp_beta}, beta_actual, False),
        first_action_after_reject=None
        if reject_step is None
        else (reject_step["action"] or {}).get("name"),
        step_count=len(steps),
        actions=names,
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


def _line(r: dict[str, Any]) -> str:
    if r.get("path"):
        c = r.get("success_criteria") or {}
        return (
            f"#{r['index']} path {r['path']} success={r.get('success')} "
            f"run={r.get('run_status')}/{r.get('end_reason')} steps={r.get('step_count')} "
            f"alpha_matches={r.get('alpha_matches_expected')} "
            f"beta_matches={r.get('beta_matches_expected')} "
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
        batch = (
            run_once(i + 1, settings, pack_name, args.raw, requests)
            if args.path == "A"
            else [run_path_b(i + 1, settings, pack_name, args.raw, args.path == "B-decline")]
        )
        for r in batch:
            records.append(r)
            with out.open("a", encoding="utf-8") as f:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            print(_line(r))

    ok = sum(1 for r in records if r.get("success"))
    l0 = sum(1 for r in records if r.get("l0_first"))
    match = sum(1 for r in records if r.get("matches_expected"))
    tokens = sum(r.get("tokens_in", 0) + r.get("tokens_out", 0) for r in records)
    n = len(records)
    print(f"success {ok}/{n}, L0 first {l0}/{n}, matches expected {match}/{n}, tokens {tokens}")
    print(f"saved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
