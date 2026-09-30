"""실제 모델로 게이트 경로를 돌린다 (설계서 §16 Agent 평가, 우선순위 문서 시연 안정성, 부록 A.17).

    cd backend && uv run python -m scripts.live_run [--runs 1] [--raw]

- .env의 OPENAI_API_KEY·OPENAI_MODEL이 없으면 바로 종료한다(API를 부르지 않음).
- run마다 임시 DB를 만든다. 개발 DB(data/safe_orch.db)는 건드리지 않는다.
- 흐름: 폼 A → 워커(START_RUN → 실제 모델 Replanning → 후보 → VALIDATE → Consultation)
  → 스크립트가 Supervisor로 PENDING item을 WAIVE(comment "live run 자동 수용") → 승인.
- 결과: 콘솔 요약 + data/live_runs/<UTC시각>.jsonl (gitignore). --raw일 때만 prompt·응답 원문을 넣는다.
- 성공 = PASS 후보 도달 ∧ 금지 Action(ACTION_NOT_AVAILABLE) 0 ∧ Budget 안.
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
from app.commands.approval import ApproveRequest, WaiveRequest, approve_and_commit, waive
from app.commands.task_request import TaskRequestForm, submit_task_request
from app.config import REPO_ROOT, Settings, get_settings
from app.coordinator.dispatcher import run_until_idle
from app.packs.loader import load_pack, pack_dir
from app.solver import cpsat
from app.store import db
from app.store.repos.consultations import consultation_view
from app.store.repos.records import list_validations
from app.store.repos.runs import get_run, list_steps
from app.store.repos.seed import seed_pack
from app.store.repos.site import get_site

OUT_DIR = REPO_ROOT / "data" / "live_runs"
RUN_DEADLINE_S = 300
MAX_RUNS = 10


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


def run_once(index: int, settings: Settings, pack_name: str, raw: bool) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    t0 = time.perf_counter()
    rec = _Recorder(time.monotonic() + RUN_DEADLINE_S, raw)
    solver_s = 0.0
    real_solve = cpsat.solve

    def timed_solve(*args: Any) -> Any:
        nonlocal solver_s
        s = time.perf_counter()
        try:
            return real_solve(*args)
        finally:
            solver_s += time.perf_counter() - s

    tmp = tempfile.TemporaryDirectory(prefix="live_run_")
    old_db = os.environ.get("DB_PATH")
    os.environ["DB_PATH"] = str(Path(tmp.name) / "live.db")
    get_settings.cache_clear()
    db.close()
    cpsat.solve = timed_solve
    record: dict[str, Any] = {"index": index, "started_at": started_at}
    try:
        db.init_db()
        pack = load_pack(pack_dir(pack_name))
        with db.write() as tx:
            seed_pack(tx, pack)
        data = pack.new_task.model_dump(
            exclude={"requested", "unit_id", "owner_actor_id", "movable"}
        )
        submit_task_request(pack, "planner_a", _key(), TaskRequestForm(**data))
        run_until_idle(pack, model_factory=lambda: _RecordingModel(openai_model(settings), rec))

        with db.read() as conn:
            run_id = conn.execute("SELECT run_id FROM agent_run ORDER BY rowid").fetchone()
            run = get_run(conn, run_id[0]) if run_id else None
            steps = list_steps(conn, run.run_id) if run else []
            cid = run.wait_ref if run and run.status == "WAITING_HUMAN" else None
            validations = list_validations(conn, pack.site_id, cid) if cid else []
            view = consultation_view(conn, pack.site_id, cid) if cid else None
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
            tokens_in=sum(r["tokens_in"] for r in rows),
            tokens_out=sum(r["tokens_out"] for r in rows),
            llm_seconds=round(sum(c["ms"] for c in rec.calls) / 1000, 2),
            solver_seconds=round(solver_s, 2),
        )
    except Exception as e:  # noqa: BLE001 — 한 run의 실패를 기록하고 다음 run으로 간다
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


def _line(r: dict[str, Any]) -> str:
    steps = " → ".join(
        f"{s['action'] or '-'}{'(' + s['level'] + ')' if s['level'] else ''}:{s['result_kind']}"
        for s in r.get("steps", [])
    )
    return (
        f"#{r['index']} success={r.get('success')} l0_first={r.get('l0_first')} "
        f"run={r.get('run_status')}/{r.get('end_reason')} {r['total_seconds']}s  {steps}"
        + (f"  error={r['error']}" if r.get("error") else "")
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="실제 모델로 게이트 경로 live run")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--pack", default=None)
    parser.add_argument("--raw", action="store_true", help="prompt·응답 원문을 기록에 넣는다")
    args = parser.parse_args(argv)
    if not 1 <= args.runs <= MAX_RUNS:
        parser.error(f"--runs must be 1..{MAX_RUNS}")

    settings = get_settings()
    if not settings.openai_api_key.get_secret_value() or not settings.openai_model:
        print("OPENAI_API_KEY와 OPENAI_MODEL을 .env에 넣은 뒤 실행한다.", file=sys.stderr)
        return 2
    os.environ["LANGSMITH_TRACING"] = "false"

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    print(f"model settings: {model_settings(settings)}  prompt: {PROMPT_VERSION}")
    records = []
    for i in range(args.runs):
        r = run_once(i + 1, settings, args.pack or settings.pack, args.raw)
        records.append(r)
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(_line(r))

    ok = sum(1 for r in records if r.get("success"))
    l0 = sum(1 for r in records if r.get("l0_first"))
    tokens = sum(r.get("tokens_in", 0) + r.get("tokens_out", 0) for r in records)
    print(f"success {ok}/{len(records)}, L0 first {l0}/{len(records)}, tokens {tokens}")
    print(f"saved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
