"""시나리오 실행기.

    cd backend && uv run python -m evals.run --scenario S1|S2|S3|all [--hidden] [--runs 10]

- 회차마다 임시 DB를 쓴다. 개발 DB는 건드리지 않는다.
- 설정은 .env와 무관하게 명시 고정한다: 사건 → 메인 자동 시작 켬, SITE_NOW = 시나리오 값.
  모델·온도·seed·reasoning_effort는 .env 그대로 쓰고 결과 첫 줄에 남긴다.
- 루프: idle까지 실행 → 사람 규칙 평가 → 사람 행동 하나 → 반복.
- 종료: DONE(할 일 없음) / END_POINT(단계별 종료 지점) / STALLED(답할 규칙이 없는 대기) /
  HARNESS_LIMIT(시간·사람 행동 수 상한). Run ERROR·준비 실패는 무효(다시 돌린다).
- 결과: data/evals/<UTC>.jsonl (gitignore). 첫 줄은 설정 기록이다.
"""

import argparse
import json
import sys
import tempfile
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.agents.llm import ModelFactory, model_settings, openai_model
from app.agents.registry import BINDINGS
from app.commands.intake import IntakeRequest, submit_intake
from app.commands.task_request import submit_task_request
from app.config import REPO_ROOT, Settings, get_settings
from app.coordinator.dispatcher import run_until_idle
from app.packs.loader import LoadedPack, load_pack, pack_dir
from app.store import db
from app.store.repos._rows import rows
from app.store.repos.seed import seed_pack
from evals import EVAL_STAGE, HARNESS_VERSION, judge, prep
from evals.humans import Humans, answerable, classify
from evals.scenario import Scenario, ScenarioError, load, scenario_names, scenario_path
from scripts.live_run import (
    _form_and_requester,
    _Recorder,
    _RecordingModel,
    _restore_env,
    _set_env,
)

OUT_DIR = REPO_ROOT / "data" / "evals"
TIME_LIMIT_S = 300
MAX_HUMAN_ACTIONS = 30
MAX_INVALID_IN_A_ROW = 2
AGENT_FLAGS = {"MAIN_AUTO_START": "true"}


def _start(pack: LoadedPack, scn: Scenario, humans: Humans) -> str | None:
    """시작 상태를 만든다. 무효 사유를 돌려준다(없으면 None)."""
    start = scn.data["start"]
    if start["kind"] == "INTAKE":
        body = IntakeRequest(task_id=start["task_id"], text=start["text"])
        out = submit_intake(pack, start["requester"], humans.key(), body)
        humans.record(start["requester"], "SUBMIT_INTAKE", out, text=start["text"])
        return None if out.status == "APPLIED" else f"START_REJECTED:{out.reason_codes}"
    form, requester = _form_and_requester(pack, pack.new_task.task_id)
    out = submit_task_request(pack, requester, humans.key(), form)
    humans.record(requester, "SUBMIT_FORM", out, task=form.task_id)
    if out.status != "APPLIED":
        return f"START_REJECTED:{out.reason_codes}"
    if start["kind"] != "CONSULTING":
        return None
    expect = start["expect"]
    factory = prep.consulting_factory(expect["task"])
    if factory is None:
        return "PREP_SCRIPT_MISSING"
    run_until_idle(pack, model_factory=factory)
    with db.read() as conn:
        open_requests = [
            classify(conn, pack, m) for m in answerable(conn, pack.site_id) if m["status"] == "OPEN"
        ]
    reached = len(open_requests) == 1 and (
        open_requests[0]["kind"],
        open_requests[0]["to"],
        open_requests[0]["task"],
        (open_requests[0].get("after") or {}).get("start"),
    ) == ("CHANGE_REQUEST", expect["to"], expect["task"], expect["start"])
    return None if reached else "PREP_STATE_NOT_REACHED"


def _end_point(conn: Any, scn: Scenario, stage: int) -> bool:
    for point in scn.data.get("end_points", []):
        released = point["when"] == "HOLD_RELEASED_FACT_CONFIRMED" and judge.hold_released(conn)
        if stage < point.get("until_stage", 0) and released:
            return True
    return False


def run_once(
    scn: Scenario,
    pack_name: str,
    model_factory: ModelFactory,
    index: int = 1,
    stage: int = EVAL_STAGE,
    time_limit_s: float = TIME_LIMIT_S,
    max_actions: int = MAX_HUMAN_ACTIONS,
) -> dict[str, Any]:
    """시나리오 1회. 무효면 valid False와 사유, 아니면 등급·판정·기록."""
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    env = {"DB_PATH": str(Path(tmp.name) / "eval.db"), "SITE_NOW": scn.data["site_now"]}
    old_env = _set_env({**env, **AGENT_FLAGS})
    db.close()
    started = time.monotonic()
    record: dict[str, Any] = {
        "scenario": scn.scenario_id,
        "hidden": scn.hidden,
        "run": index,
        "stage": stage,
        "valid": True,
    }
    try:
        db.init_db()
        pack = load_pack(pack_dir(pack_name))
        with db.write() as tx:
            seed_pack(tx, pack)
        humans = Humans(pack, scn.data, stage)
        invalid = _start(pack, scn, humans)
        overlaps: list[dict[str, Any]] = []
        end = None
        while invalid is None and end is None:
            run_until_idle(pack, model_factory=model_factory)
            with db.read() as conn:
                overlaps += judge.open_overlaps(conn, pack)
                invalid = judge.has_error(conn)
                if invalid is None and _end_point(conn, scn, stage):
                    end = "END_POINT"
            if invalid is not None or end is not None:
                break
            if time.monotonic() - started > time_limit_s or len(humans.log) >= max_actions:
                end = "HARNESS_LIMIT"
            elif not humans.act():
                with db.read() as conn:
                    end = "DONE" if judge.quiet(conn) else "STALLED"
            else:
                # 종료 지점은 사람 행동 직후에도 본다(그 뒤의 Run을 돌리지 않고 거기까지로 판정한다)
                with db.read() as conn:
                    if _end_point(conn, scn, stage):
                        end = "END_POINT"
        with db.read() as conn:
            record["model_ids"] = sorted(
                {
                    r["model_id"]
                    for r in rows(conn, "SELECT DISTINCT model_id FROM agent_step")
                    if r["model_id"] and r["model_id"] != prep.PREP_MODEL
                }
            )
            if invalid is not None:
                record.update(valid=False, invalid_reason=invalid)
            else:
                assert end is not None
                must = judge.must_checks(
                    conn, pack, scn.data, stage, humans.keys, overlaps, humans.log
                )
                result = judge.outcome(conn, pack, scn.data, stage)
                record.update(
                    end=end,
                    grade=judge.grade(must, result["allowed"], end),
                    must=must,
                    outcome=result,
                    metrics={
                        **judge.metrics(conn, pack),
                        "human_actions": len(humans.log),
                        "unscripted_requests": list(humans.unscripted.values()),
                        "in_text_reasks": humans.reasks["in_text"],
                        "received_reasks": humans.reasks["received"],
                    },
                    texts=judge.texts(conn, pack),
                )
            record["human_actions"] = humans.log
    finally:
        db.close()
        _restore_env(old_env)
        db.close()
        tmp.cleanup()
    record["seconds"] = round(time.monotonic() - started, 1)
    return record


def header(
    settings: Settings, pack: LoadedPack, scenarios: list[Scenario], records: list[dict]
) -> dict[str, Any]:
    """설정 기록. 비교 도구가 이 값으로 같은 조건인지 본다 (EV-04)."""
    return {
        "kind": "config",
        "harness_version": HARNESS_VERSION,
        "eval_stage": EVAL_STAGE,
        "model_settings": {k: v for k, v in model_settings(settings).items() if k != "api_key"},
        "model_ids": sorted({m for r in records for m in r.get("model_ids", [])}),
        "prompt_versions": {a: b.prompt.PROMPT_VERSION for a, b in BINDINGS.items()},
        "exec_contract_versions": {a: b.exec_contract_version for a, b in BINDINGS.items()},
        "pack": pack.name,
        "pack_hash": pack.pack_hash,
        "scenario_hashes": {s.scenario_id: s.file_hash for s in scenarios},
        "hidden": any(s.hidden for s in scenarios),
        "agent_flags": AGENT_FLAGS,
        "site_now": {s.scenario_id: s.data["site_now"] for s in scenarios},
    }


def distribution(records: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """시나리오별 등급 분포와 무효 수."""
    out: dict[str, dict[str, int]] = {}
    for r in records:
        counts = out.setdefault(r["scenario"], {"PASS": 0, "SHORT": 0, "FAIL": 0, "INVALID": 0})
        counts[r["grade"] if r["valid"] else "INVALID"] += 1
    return out


def _line(r: dict[str, Any]) -> str:
    if not r["valid"]:
        return f"  #{r['run']} 무효 {r['invalid_reason']} {r['seconds']}s"
    broken = [name for name, c in r["must"].items() if not c["ok"]]
    return (
        f"  #{r['run']} {judge.GRADES[r['grade']]} end={r['end']} steps={r['metrics']['steps_total']}"
        f" questions={r['metrics']['questions_by_actor']} {r['seconds']}s"
        + (f" 반드시 위반={broken}" if broken else "")
    )


def run_scenarios(
    scenarios: list[Scenario],
    pack_name: str,
    runs: int,
    model_factory: ModelFactory,
    quiet: bool = False,
) -> tuple[list[dict[str, Any]], str | None]:
    """시나리오마다 유효 회차를 runs만큼 채운다. 연속 2회 무효면 멈춘다(사유를 돌려준다)."""
    records: list[dict[str, Any]] = []
    for scn in scenarios:
        valid, attempt, invalid_streak = 0, 0, 0
        while valid < runs:
            attempt += 1
            r = run_once(scn, pack_name, model_factory, index=attempt)
            records.append(r)
            if not quiet:
                print(f"{scn.scenario_id}{_line(r)}", flush=True)
            if r["valid"]:
                valid, invalid_streak = valid + 1, 0
                continue
            invalid_streak += 1
            if invalid_streak >= MAX_INVALID_IN_A_ROW:
                return (
                    records,
                    f"{scn.scenario_id}: 연속 {invalid_streak}회 무효 ({r['invalid_reason']})",
                )
    return records, None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.run")
    parser.add_argument("--scenario", default="all")
    parser.add_argument("--hidden", action="store_true", help="숨긴 판. 콘솔에는 등급 분포만 낸다")
    parser.add_argument("--runs", type=int, default=10)  # EV-05
    args = parser.parse_args(argv)

    settings = get_settings()
    if not settings.openai_api_key.get_secret_value() or not settings.openai_model:
        print(
            "OPENAI_API_KEY·OPENAI_MODEL이 없습니다. 실제 모델을 부르지 않고 끝냅니다.",
            file=sys.stderr,
        )
        return 2
    pack = load_pack(pack_dir(settings.pack))
    names = (
        scenario_names(pack.name, args.hidden)
        if args.scenario == "all"
        else args.scenario.split(",")
    )
    try:
        scenarios = [
            load(scenario_path(pack.name, n, args.hidden), pack, args.hidden) for n in names
        ]
    except (ScenarioError, FileNotFoundError) as e:
        print(f"시나리오를 읽을 수 없습니다: {e}", file=sys.stderr)
        return 2

    rec = _Recorder(float("inf"), raw=False)

    def factory() -> Any:
        # 호출마다 시간 상한을 다시 잡는다(하네스 상한보다 조금 길게: 넘으면 그 회차는 무효)
        rec.deadline = time.monotonic() + TIME_LIMIT_S + 60
        return _RecordingModel(openai_model(settings), rec)

    records, stopped = run_scenarios(scenarios, pack.name, args.runs, factory, quiet=args.hidden)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}.jsonl"
    lines = [header(settings, pack, scenarios, records), *records]
    out.write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8"
    )
    print(f"model {lines[0]['model_ids']} prompts {lines[0]['prompt_versions']}")
    for sid, c in distribution(records).items():
        print(
            f"{sid}: 통과 {c['PASS']} / 미달 {c['SHORT']} / 실패 {c['FAIL']} (무효 {c['INVALID']})"
        )
    print(f"saved: {out}")
    if stopped:
        print(f"멈춤: {stopped}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
