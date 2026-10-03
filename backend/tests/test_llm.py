"""실제 모델 연결. 실제 API는 부르지 않는다."""

import json
import os
import re

import httpx
import openai
import pytest
import yaml
from conftest import add_run
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from scripted import Router, ScriptedChatModel, call, done, escalate, field_judgments, solve

from app.agents import llm, runtime
from app.agents.observers.replanning import build_observation
from app.agents.prompts import replanning as prompt
from app.config import Settings
from app.main import app
from app.packs.loader import load_pack
from app.store import db
from app.store.repos.runs import list_steps
from scripts import live_run

REQ = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
CONFLICT = {"conflict": {"rule_id": "SEP-LIFT-BELOW", "task_ids": ["A", "B"]}}


def _edit_yaml(path, mutate):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _conn_error():
    return openai.APIConnectionError(request=REQ)


def _timeout():
    return openai.APITimeoutError(request=REQ)


def _auth_error():
    return openai.AuthenticationError(
        "bad key", response=httpx.Response(401, request=REQ), body=None
    )


def _server_error():
    return openai.InternalServerError("oops", response=httpx.Response(500, request=REQ), body=None)


class FakeRunnable:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def _raise(exc_factory):
    def reply():
        raise exc_factory()

    return reply


# ── invoke_with_retry ──────────────────────────────────────────


def test_retry_once_then_success_counts_two_attempts():
    ok = AIMessage(content="", tool_calls=[])
    r = FakeRunnable([_conn_error(), ok])
    call = llm.invoke_with_retry(r, [])
    assert (call.message, call.attempts, call.error_kind, r.calls) == (ok, 2, None, 2)


@pytest.mark.parametrize("errors", [(_timeout, _server_error), (_conn_error, _conn_error)])
def test_two_transport_failures_are_llm_error(errors):
    r = FakeRunnable([f() for f in errors])
    call = llm.invoke_with_retry(r, [])
    assert (call.message, call.attempts, call.error_kind) == (None, 2, "LLM_ERROR")


def test_auth_error_is_llm_config_without_retry():
    r = FakeRunnable([_auth_error(), AIMessage(content="")])
    call = llm.invoke_with_retry(r, [])
    assert (call.attempts, call.error_kind, call.error, r.calls) == (
        1,
        "LLM_CONFIG",
        "AuthenticationError",
        1,
    )


def _rate_limit(code):
    body = {"message": "m", "type": code, "code": code}
    return openai.RateLimitError("429", response=httpx.Response(429, request=REQ), body=body)


def test_insufficient_quota_is_llm_config_without_retry():
    r = FakeRunnable([_rate_limit("insufficient_quota"), AIMessage(content="")])
    call = llm.invoke_with_retry(r, [])
    assert (call.attempts, call.error_kind, call.error, r.calls) == (
        1,
        "LLM_CONFIG",
        "insufficient_quota",
        1,
    )


def test_other_rate_limit_is_retried():
    ok = AIMessage(content="", tool_calls=[])
    r = FakeRunnable([_rate_limit("rate_limit_exceeded"), ok])
    assert llm.invoke_with_retry(r, []).attempts == 2


def test_insufficient_quota_ends_run_as_llm_config(with_a):
    run, _ = _invoke(with_a, [_raise(lambda: _rate_limit("insufficient_quota"))])
    assert (run.status, run.end_reason) == ("ERROR", "LLM_CONFIG: insufficient_quota")


def test_other_exceptions_propagate():
    with pytest.raises(ValueError):
        llm.invoke_with_retry(FakeRunnable([ValueError("bug")]), [])


# ── 그래프에서의 LLM 실패 ──────────────────────────────────────


def _invoke(pack, replies, run_id="run_llm"):
    add_run(pack, run_id, input_ref=CONFLICT)
    run = runtime.invoke(pack, {"run_id": run_id}, ScriptedChatModel(replies))
    with db.read() as conn:
        return run, list_steps(conn, run_id)


def test_transport_retry_within_step_counts_attempts(with_a):
    run, steps = _invoke(with_a, [_raise(_conn_error), solve("L1")])
    assert run.status == "WAITING_HUMAN"
    assert steps[0]["llm_attempts"] == 2 and run.llm_attempts_used == 2 and run.steps_used == 1


def test_llm_error_on_two_consecutive_steps_escalates(with_a):
    run, steps = _invoke(with_a, [_raise(_conn_error)] * 4)
    assert [(s["result_kind"], s["guard"]["reason_code"]) for s in steps] == [
        ("REJECTED", "LLM_ERROR"),
        ("DONE", "LLM_ERROR"),
    ]
    assert (run.status, run.end_reason) == ("BLOCKED", "LLM_ERROR_TWICE")
    assert [s["llm_attempts"] for s in steps] == [2, 2] and run.llm_attempts_used == 4
    assert run.solver_calls_used == 0


def test_llm_error_then_malformed_escalates(with_a):
    """LLM_ERROR와 MALFORMED는 합산한다. 끝난 사유는 마지막 사유."""
    run, steps = _invoke(with_a, [_raise(_conn_error)] * 2 + [AIMessage(content="L1")])
    assert [(s["result_kind"], s["guard"]["reason_code"]) for s in steps] == [
        ("REJECTED", "LLM_ERROR"),
        ("DONE", "MALFORMED"),
    ]
    assert (run.status, run.end_reason) == ("BLOCKED", "MALFORMED_TWICE")


def test_llm_config_error_ends_run_as_error(with_a):
    run, steps = _invoke(with_a, [_raise(_auth_error)])
    assert (run.status, run.end_reason) == ("ERROR", "LLM_CONFIG: AuthenticationError")
    assert [(s["result_kind"], s["guard"]["reason_code"], s["llm_attempts"]) for s in steps] == [
        ("DONE", "LLM_CONFIG", 1)
    ]


# ── 설정 ───────────────────────────────────────────────────────


def test_blank_optional_settings_are_not_passed():
    s = Settings(
        openai_model="m-2026-01-01",
        openai_temperature="",
        openai_seed="",
        openai_reasoning_effort="",
    )
    assert llm.model_settings(s) == {"model": "m-2026-01-01", "timeout": 30, "max_retries": 0}


def test_set_optional_settings_are_passed():
    s = Settings(
        openai_model="m", openai_temperature="0", openai_seed="0", openai_reasoning_effort="low"
    )
    assert llm.model_settings(s) == {
        "model": "m",
        "timeout": 30,
        "max_retries": 0,
        "temperature": 0.0,
        "seed": 0,
        "reasoning_effort": "low",
    }


def test_openai_model_requires_key_and_model():
    with pytest.raises(RuntimeError, match="not configured"):
        llm.openai_model(Settings(openai_model="m"))
    model = llm.openai_model(Settings(openai_api_key="sk-test", openai_model="m", openai_seed="0"))
    assert (model.max_retries, model.seed, model.temperature) == (0, 0, None)


# ── prompt ─────────────────────────────────────────────────────


def test_prompt_fingerprint_matches_version():
    assert prompt.fingerprint() == prompt.PROMPT_FINGERPRINTS[prompt.PROMPT_VERSION]
    values = list(prompt.PROMPT_FINGERPRINTS.values())
    assert len(values) == len(set(values))


def test_observation_keys_match_fingerprinted_keys(with_a):
    add_run(with_a, "run_keys", input_ref=CONFLICT)
    with db.read() as conn:
        data = build_observation(conn, with_a, "run_keys").data
    assert tuple(sorted(data)) == prompt.OBSERVATION_KEYS
    assert all("search_key" not in a and "spec_hash" not in a for a in data["attempts"])


def test_system_prompt_does_not_order_l0_first(pack):
    assert "L0부터" not in prompt.render_system(pack)


# ── 현장 문구의 Pack화 ─────────────────────────────


def test_origin_time_from_pack(pack):
    assert prompt.origin_time(pack) == "09:00"


def _pack_values(p) -> list[str]:
    """prompt 템플릿에 들어가면 안 되는 Pack 값. 사람 이름(actor name)은 역할 이름과 겹쳐 뺀다."""
    values = [p.site_id, p.timezone, p.horizon_start_utc, p.site_description, prompt.origin_time(p)]
    values += [r.resource_id for r in p.resources] + [z.zone_id for z in p.zones]
    values += [u.unit_id for u in p.units] + [u.name for u in p.units]
    values += [a.actor_id for a in p.actors]
    values += [r.rule_id for r in p.rules] + [r.display_name for r in p.rules]
    values += [*p.work_types] + [w.display_name for w in p.work_types.values()]
    values += [t.task_id for t in p.tasks] + [p.new_task.task_id]
    values += [d.task_id for d in p.demo_requests]
    return sorted(set(values))


def _found(values: list[str], text: str) -> list[str]:
    """토큰 경계로 찾는다(짧은 ID가 다른 낱말의 일부로 잡히지 않게)."""
    return [
        v for v in values if re.search(rf"(?<![0-9A-Za-z_-]){re.escape(v)}(?![0-9A-Za-z_-])", text)
    ]


def test_prompt_template_has_no_pack_values(pack):
    """렌더링 전 템플릿·Goal·머리말·전체 도구 스키마에 Pack 값이 없다."""
    tools = prompt.spec.tool_schemas(
        {
            name: {"level": list(prompt.spec.LEVELS)} if name == "SOLVE_WITH_SCOPE" else {}
            for name in prompt.spec.ACTIONS
        }
    )
    texts = {
        "system": prompt.SYSTEM,
        "goal": prompt.spec.GOAL,
        "header": prompt.OBS_HEADER,
        "tools": json.dumps(tools, ensure_ascii=False),
    }
    assert {k: _found(_pack_values(pack), v) for k, v in texts.items()} == {k: [] for k in texts}
    assert "{site_description}" in prompt.SYSTEM and "{origin_time}" in prompt.SYSTEM


def test_rendered_system_contains_site_values_once(pack):
    system = prompt.render_system(pack)
    assert system.count(pack.site_description) == 1
    assert system.count(f"첫날 {prompt.origin_time(pack)}") == 1
    assert "{" not in system.replace("{{", "")  # 빈 자리 없음


def test_second_pack_renders_its_own_site_values(pack, pack_copy):
    """두 번째 Pack(설명·원점 변경)으로 렌더링하면 그 값이 들어가고 shipyard 값은 없다."""

    def mutate(d):
        d["site_description"] = "여러 공정 팀이 라인·지게차·시간을 나눠 쓰는 공장"
        d["horizon_start_utc"] = "2026-10-11T23:00:00Z"  # Asia/Seoul 08:00

    _edit_yaml(pack_copy / "site.yaml", mutate)
    other = load_pack(pack_copy)
    system = prompt.render_system(other)
    assert other.site_description in system and "첫날 08:00" in system
    assert pack.site_description not in system and "첫날 09:00" not in system


# ── 네트워크 차단·live run ─────────────────────────────────────


def test_network_is_blocked_but_testclient_works():
    with pytest.raises(RuntimeError, match="network disabled"):
        httpx.HTTPTransport().handle_request(REQ)
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200


def test_live_run_exits_without_key(tmp_path, monkeypatch, capsys):
    out_dir = tmp_path / "live_runs"
    monkeypatch.setattr(live_run, "OUT_DIR", out_dir)
    assert live_run.main([]) == 2
    assert not out_dir.exists()
    assert "OPENAI_API_KEY" in capsys.readouterr().err


def _use(monkeypatch, router):
    """live run의 모델을 스크립트로 바꾼다. 메인(과 스크립트를 주지 않은 Coordination)은 기본 응답으로 돈다."""
    make = router.factory()
    monkeypatch.setattr(live_run, "openai_model", lambda settings: make())
    return Settings(openai_api_key="sk-test", openai_model="m", openai_temperature="0")


def test_live_run_flow_with_scripted_model(monkeypatch):
    """경로 A. 스크립트 흐름 확인. 실제 모델 대신 스크립트 모델을 넣는다(API 호출 없음)."""
    settings = _use(monkeypatch, Router(replanning=[solve("L0"), solve("L1")]))
    [r] = live_run.run_once(1, settings, "shipyard", raw=False)
    assert r.get("error") is None, r.get("error")
    c = r["success_criteria"]
    assert r["success"], c
    assert (r["l0_first"], r["first_solve_level"]) == (True, "L0")
    assert (c["replanning_done"], c["committed"], c["notices_sent"], c["main_closed"]) == (
        True,
        True,
        True,
        True,
    )
    assert (r["run_status"], r["end_reason"]) == ("SUCCEEDED", "CLOSE")
    # 메인이 고른 행동 순서와 쓴 step·호출 수가 기록에 남는다
    assert r["main"] == {
        "runs": 1,
        "steps": 5,
        "agent_calls": 3,
        "actions": [
            "CALL_AGENT(REPLANNING·UA)",
            "CALL_AGENT(COORDINATION·CONSULT)",
            "WAIT",
            "CALL_AGENT(COORDINATION·NOTICE)",
            "CLOSE",
        ],
    }
    assert r["agent_flags"] == {"main_auto_start": True}
    assert r["model_settings"]["temperature"] == 0.0 and "api_key" not in r["model_settings"]
    assert (r["request"], r["expected_outcome"], r["matches_expected"]) == ("A", "CANDIDATE", True)
    assert r["actual"]["level"] == "L1" and r["actual"]["moved"] == {
        "A": [60, "A-CR-01"],
        "C": [90, "A-CR-01"],
    }
    assert [e for e in r["events"] if "waive" in e] == [{"waive": ["C"], "status": "APPLIED"}]


def test_live_run_requests_in_sequence(monkeypatch):
    """--request N1,N2,N3,N4: 같은 DB에서 하나씩 확정한다. 기대값은 verify_demo_values와 같은 출처."""
    settings = _use(monkeypatch, Router(replanning=[solve("L0")] * 4))
    records = live_run.run_once(1, settings, "shipyard", False, ["N1", "N2", "N3", "N4"])
    assert [r.get("error") for r in records] == [None] * 4
    assert [r["request"] for r in records] == ["N1", "N2", "N3", "N4"]
    assert all(r["success"] and r["matches_expected"] and r["l0_first"] for r in records)
    assert [r["main"]["runs"] for r in records] == [1] * 4  # 요청마다 메인 하나
    assert [(r["actual"]["delay"], r["actual"]["work_delay"]) for r in records] == [
        (60, 60),
        (45, 45),
        (120, 120),
        (1140, 180),
    ]
    assert records[3]["actual"]["moved"] == {"N4": [2880, None]}
    assert records[3]["expected"]["L0"]["moved"] == {"N4": [2880, None]}


def test_live_run_no_solution_request_succeeds_by_escalation(monkeypatch):
    """--request N5: 모든 범위 INFEASIBLE이 기대값이므로 후보 없음 + 막힌 결과 + 메인 이관이 성공."""
    settings = _use(monkeypatch, Router(replanning=[solve("L0"), solve("L2"), escalate()]))
    [r] = live_run.run_once(1, settings, "shipyard", False, ["N5"])
    assert r.get("error") is None, r.get("error")
    assert r["expected_outcome"] == "ESCALATE"
    assert {e["status"] for e in r["expected"].values()} == {"INFEASIBLE"}
    assert r["actual"] is None
    assert (r["run_status"], r["end_reason"]) == ("ESCALATED", "ESCALATE")
    assert r["success"] and r["matches_expected"]
    c = r["success_criteria"]
    assert (c["no_candidate"], c["replanning_blocked"], c["main_escalated"]) == (True, True, True)


def test_live_run_rejects_unknown_request(capsys):
    with pytest.raises(SystemExit):
        live_run.main(["--request", "N1,X9"])
    assert "X9" in capsys.readouterr().err


def _plan_b_replies(*after_reply):
    ask = call(
        "ASK_TASK_OWNER",
        "자원 축 확인",
        task_id="A",
        axis="RESOURCE",
        allowed_values=["SITE-CR-01"],
        question="SITE-CR-01을 써도 되나요?",
    )
    return [
        solve("L0"),
        solve("L1"),
        call("LIST_ASSIGNABLE_RESOURCES", "A 자원 조회", task_id="A"),
        ask,
        *after_reply,
    ]


def test_live_run_path_b_with_scripted_model(monkeypatch):
    """--path B: 거절 → 메인이 재계획을 다시 부름 → LIST → ASK → 스크립트가 수락 → TRY → Beta → 승인 R1."""
    try_ = call("TRY_ALTERNATIVE_RESOURCE", "대체 자원", task_id="A", resource_id="SITE-CR-01")
    router = Router(replanning=_plan_b_replies(try_))
    settings = _use(monkeypatch, router)
    r = live_run.run_path(1, settings, "shipyard", False, "B")
    assert r.get("error") is None, r.get("error")
    c = r["success_criteria"]
    assert r["success"], c
    assert (c["alpha_pass"], c["recalled_after_reject"], c["beta_pass"]) == (True, True, True)
    assert (c["consultation_complete"], c["committed"], c["notices_sent"]) == (True, True, True)
    assert (r["run_status"], r["end_reason"], r["alpha_matches_expected"]) == (
        "SUCCEEDED",
        "CLOSE",
        True,
    )
    assert r["main"]["actions"] == [
        "CALL_AGENT(REPLANNING·UA)",
        "CALL_AGENT(COORDINATION·CONSULT)",
        "CALL_AGENT(REPLANNING·UA)",
        "WAIT",
        "CALL_AGENT(COORDINATION·NOTICE)",
        "CLOSE",
    ]
    assert (r["main"]["steps"], r["main"]["agent_calls"]) == (6, 4)
    # 사람 역할: Supervisor 거절 → 담당자 질문 수락 → 승인. 변경 요청에는 답하지 않는다
    assert [
        next(k for k in e if k in ("form", "reject", "reply", "approve")) for e in r["events"]
    ] == [
        "form",
        "reject",
        "reply",
        "approve",
    ]
    assert router.left()["REPLANNING"] == 0


def test_live_run_path_sets_main_auto_start_regardless_of_env(monkeypatch):
    """경로가 자동 시작과 현장의 지금을 명시한다. .env·환경변수가 꺼져 있어도 메인이 뜬다."""
    monkeypatch.setenv("MAIN_AUTO_START", "false")
    try_ = call("TRY_ALTERNATIVE_RESOURCE", "대체 자원", task_id="A", resource_id="SITE-CR-01")
    settings = _use(monkeypatch, Router(replanning=_plan_b_replies(try_)))
    r = live_run.run_path(1, settings, "shipyard", False, "B")
    assert r["success"], r.get("error") or r["success_criteria"]
    assert r["agent_flags"] == {"main_auto_start": True}
    assert r["site_now"] == "2026-10-12T00:00:00Z"
    assert os.environ["MAIN_AUTO_START"] == "false"  # 끝나면 되돌린다


def test_live_run_summary_line(monkeypatch, tmp_path, capsys):
    """요약 줄: 경로와 성공 수, 메인의 step·호출 수와 행동 순서."""
    record = {
        "index": 1,
        "path": "B",
        "success": True,
        "run_status": "SUCCEEDED",
        "end_reason": "CLOSE",
        "main": {"steps": 6, "agent_calls": 4, "actions": ["CALL_AGENT(REPLANNING·UA)", "CLOSE"]},
        "tokens_in": 1,
        "tokens_out": 1,
    }
    monkeypatch.setattr(live_run, "OUT_DIR", tmp_path)
    monkeypatch.setattr(live_run, "run_path", lambda *a: dict(record))
    monkeypatch.setattr(
        live_run,
        "get_settings",
        lambda: Settings(openai_api_key="sk-test", openai_model="m"),
    )
    assert live_run.main(["--path", "B"]) == 0
    out = capsys.readouterr().out
    assert "main=SUCCEEDED/CLOSE main_steps=6 calls=4" in out
    assert "CALL_AGENT(REPLANNING·UA) → CLOSE" in out
    assert "success 1/1 (path B), tokens 2" in out


def test_live_run_path_b_decline_ends_in_main_escalation(monkeypatch):
    """--path B-decline: 스크립트가 거절 → 같은 값을 다시 묻지 않음 → 막힌 결과 → 메인이 이관하면 성공."""
    settings = _use(monkeypatch, Router(replanning=_plan_b_replies(escalate())))
    r = live_run.run_path(1, settings, "shipyard", False, "B-decline")
    assert r.get("error") is None, r.get("error")
    c = r["success_criteria"]
    assert r["success"], c
    assert (c["decline_applied"], c["no_reask_after_decline"], c["replanning_blocked"]) == (
        True,
        True,
        True,
    )
    assert (r["run_status"], r["end_reason"]) == ("ESCALATED", "ESCALATE")
    assert r["main"]["actions"][-1] == "ESCALATE"


def test_live_run_path_needs_request_a(capsys):
    with pytest.raises(SystemExit):
        live_run.main(["--path", "B", "--request", "N1"])
    assert "runs only --request A" in capsys.readouterr().err
    # 옛 --coord 경로는 없다(B와 event에 합쳤다)
    with pytest.raises(SystemExit):
        live_run.main(["--path", "B", "--coord"])


def test_system_lists_every_action_with_open_condition(pack):
    """System의 "도구 전체와 열리는 조건" 절은 spec에서 생성한다. Pack 값은 넣지 않는다."""
    catalog = prompt.tool_catalog()
    system = prompt.render_system(pack)
    assert catalog in system
    for name, model in prompt.spec.ACTIONS.items():
        assert model.OPENS and f"- {name}: " in catalog and model.OPENS in catalog
    ids = [r.resource_id for r in pack.resources] + [r.rule_id for r in pack.rules]
    ids += [*pack.work_types, *(t.task_id for t in pack.tasks)]
    assert not [i for i in ids if len(i) >= 3 and i in catalog]
    assert "조회·확인으로 열 수 있는 대안" in system


def test_live_run_path_coord_with_scripted_model(monkeypatch):
    """--path coord: 변경 요청 → 스크립트 이견 → 초안 → 스크립트 확정 → 메인이 재계획을 다시 부름 → Beta → R1
    → 통지 → 메인 CLOSE."""

    def draft():
        with db.read() as conn:
            mid = conn.execute(
                "SELECT message_id FROM message WHERE type = 'CHANGE_REQUEST'"
            ).fetchone()[0]
        return call(
            "DRAFT_CONSTRAINT",
            "초안",
            message_id=mid,
            reason_code="TASK_IMMOVABLE",
            task_id="C",
            axes=["TIME", "RESOURCE"],
            message="C 고정 확인",
        )

    wait = call("WAIT_FOR_REPLIES", "대기")
    router = Router(
        replanning=[
            solve("L0"),
            solve("L1"),
            call("LIST_ASSIGNABLE_RESOURCES", "조회", task_id="A"),
            call(
                "ASK_TASK_OWNER",
                "확인",
                task_id="A",
                axis="RESOURCE",
                allowed_values=["SITE-CR-01"],
                question="SITE-CR-01?",
            ),
            call("TRY_ALTERNATIVE_RESOURCE", "시도", task_id="A", resource_id="SITE-CR-01"),
        ],
        coordination=[
            call("SEND_CHANGE_REQUEST", "요청", task_id="C", message="C 이동 안"),
            wait,
            draft,
            wait,
            call("SEND_NOTICE", "통지", actor_id="planner_a", task_ids=["A"], message="A 변경"),
            call("SEND_NOTICE", "통지", actor_id="planner_b", task_ids=["B"], message="B 유지"),
            done("통지 완료"),
        ],
    )
    settings = _use(monkeypatch, router)
    r = live_run.run_path(1, settings, "shipyard", False, "coord")
    assert r.get("error") is None, r.get("error")
    c = r["success_criteria"]
    assert r["success"], c
    assert (c["change_request_sent"], c["constraint_from_proposal"], c["beta_pass"]) == (
        True,
        True,
        True,
    )
    assert [e.get("type") for e in r["events"] if "reply" in e] == [
        "CHANGE_REQUEST",
        "CONFIRMATION",
        "QUESTION",
    ]
    assert r["main"]["actions"] == [
        "CALL_AGENT(REPLANNING·UA)",
        "CALL_AGENT(COORDINATION·CONSULT)",
        "CALL_AGENT(REPLANNING·UA)",
        "WAIT",
        "CALL_AGENT(COORDINATION·NOTICE)",
        "CLOSE",
    ]
    assert router.left() == {"REPLANNING": 0, "COORDINATION": 0, "EVENT_RESPONSE": 0, "INTAKE": 0}


def test_live_run_path_event_with_scripted_model(monkeypatch):
    """--path event: R1 스크립트 준비(메인 없음) → 신고 → 메인 → ER 조회·분석·제안 → 확인·해제 → Gamma → 협의 →
    R2 → 통지 → 메인 CLOSE."""
    router = Router(
        replanning=[solve("L0")],
        coordination=[
            call("SEND_CHANGE_REQUEST", "요청", task_id="E", message="E 15분 지연"),
            call("WAIT_FOR_REPLIES", "대기"),
            done("협의 완료"),
            call(
                "SEND_NOTICE", "통지", actor_id="planner_b", task_ids=["E", "D"], message="E 10:00"
            ),
            done("통지 완료"),
        ],
        event_response=[
            call("LOOKUP_TASKS", "조회", work_type="PAINTING"),
            call("ANALYZE_IMPACT", "분석", task_id="E", new_earliest_start=60),
            call(
                "PROPOSE_FACT_UPDATE",
                "제안",
                task_id="E",
                new_earliest_start=60,
                evidence="도장 10시부터",
            ),
        ],
    )
    settings = _use(monkeypatch, router)
    r = live_run.run_path(1, settings, "shipyard", False, "event")
    assert r.get("error") is None, r.get("error")
    c = r["success_criteria"]
    assert r["success"], c
    assert (c["fact_confirmed"], c["hold_fact_confirmed"], c["no_replanning_during_hold"]) == (
        True,
        True,
        True,
    )
    [gamma] = r["candidates"]
    assert (gamma["actual"]["changed"], gamma["actual"]["delay"]) == (1, 15)
    assert router.left() == {"REPLANNING": 0, "COORDINATION": 0, "EVENT_RESPONSE": 0, "INTAKE": 0}
    assert (r["run_status"], r["end_reason"]) == ("SUCCEEDED", "CLOSE")
    # 준비 구간(R1)의 Run은 기록에 넣지 않는다. 신고로 바뀐 작업(E)의 Unit으로 재계획한다
    assert [x["agent_type"] for x in r["runs"]] == [
        "MAIN",
        "EVENT_RESPONSE",
        "REPLANNING",
        "COORDINATION",
        "COORDINATION",
    ]
    assert r["main"]["actions"] == [
        "CALL_AGENT(EVENT_RESPONSE)",
        "WAIT",
        "CALL_AGENT(REPLANNING·UB)",
        "CALL_AGENT(COORDINATION·CONSULT)",
        "WAIT",
        "CALL_AGENT(COORDINATION·NOTICE)",
        "CLOSE",
    ]


INTAKE_VALUES = {
    "work_type": "LIFTING",
    "zone_id": "B",
    "duration": 30,
    "earliest_start": 0,
    "latest_start": 60,
    "latest_end": 90,
    "required_resource_type": "CRANE",
    "requested_resource_id": "A-CR-01",
}


@pytest.mark.parametrize("ambiguous", [False, True], ids=["clear", "ambiguous"])
def test_live_run_path_intake_with_scripted_model(monkeypatch, ambiguous):
    """--path intake [--ambiguous]: 질문(모호) → 값 확인 → 완료 → 작업 준비됨 → 메인 → Replanning Alpha."""
    ask = call(
        "ASK_CLARIFICATION",
        "질문",
        fields=field_judgments("zone_id", "resource"),
        question="구역·자원?",
    )
    intake = [
        *([ask] if ambiguous else []),
        call("REQUEST_CONFIRMATION", "확인", values=INTAKE_VALUES, message="이 값으로?"),
        call("COMPLETE_TASKSPEC", "완료", values=INTAKE_VALUES),
    ]
    router = Router(intake=intake, replanning=[solve("L0"), solve("L1")])
    settings = _use(monkeypatch, router)
    r = live_run.run_path(1, settings, "shipyard", False, "intake", ambiguous)
    assert r.get("error") is None, r.get("error")
    c = r["success_criteria"]
    assert r["success"], c
    assert (c["intake_succeeded"], c["main_started"], c["alpha_pass"]) == (True, True, True)
    assert c.get("asked_requester", True) is True and r["alpha_matches_expected"] is True
    assert r["path"] == ("intake-ambiguous" if ambiguous else "intake")
    assert r["answer_kinds"] == (["FIRST"] if ambiguous else [])
    # Intake는 메인 밖 입구다: 부모가 없다. 재계획은 메인이 불렀다
    parents = {x["agent_type"]: x["parent"] for x in r["runs"]}
    assert (parents["INTAKE"], parents["REPLANNING"]) == (False, True)
    assert router.left()["INTAKE"] == 0


def test_live_run_path_event_ambiguous_with_scripted_model(monkeypatch):
    """--path event --ambiguous: 모호 신고 → 조회 → ASK_REPORTER → 답 → E 10:15 수정안 → Gamma(지연 30) → R2."""
    router = Router(
        replanning=[solve("L0")],
        event_response=[
            call("LOOKUP_TASKS", "조회", work_type="PAINTING"),
            call("ASK_REPORTER", "되묻기", question="몇 시부터?"),
            call("ANALYZE_IMPACT", "분석", task_id="E", new_earliest_start=75),
            call(
                "PROPOSE_FACT_UPDATE", "제안", task_id="E", new_earliest_start=75, evidence="10:15"
            ),
        ],
    )
    settings = _use(monkeypatch, router)
    r = live_run.run_path(1, settings, "shipyard", False, "event", True)
    assert r.get("error") is None, r.get("error")
    c = r["success_criteria"]
    assert r["success"], c
    [gamma] = r["candidates"]
    assert (gamma["actual"]["changed"], gamma["actual"]["delay"]) == (1, 30)
    assert next(e["reply"] for e in r["events"] if "reply" in e) == "ANSWER"
    assert r["answer_kinds"] == ["FIRST"] and r["path"] == "event-ambiguous"


def test_live_run_human_answer_repeats_after_first():
    """사람 역할 답: 첫 질문은 scenario 답, 두 번째부터는 앞의 답이 전부라는 답."""
    assert live_run._human_answer(0, "B구역입니다.", "원문") == ("FIRST", "B구역입니다.")
    assert live_run._human_answer(1, "B구역입니다.", "원문") == (
        "REPEAT",
        "앞에서 답한 것이 전부입니다: B구역입니다. 나머지는 처음 문장 그대로입니다: 원문",
    )
