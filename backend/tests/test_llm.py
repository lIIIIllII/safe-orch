"""실제 모델 연결 (설계서 §11.2·§11.6, 부록 A.17). 실제 API는 부르지 않는다."""

import httpx
import openai
import pytest
from conftest import add_run
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from scripted import ScriptedChatModel, call, escalate, solve

from app.agents import llm, runtime
from app.agents.observe import build_observation
from app.agents.prompts import replanning as prompt
from app.config import Settings
from app.main import app
from app.store import db
from app.store.repos.runs import list_steps
from scripts import live_run

REQ = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
CONFLICT = {"conflict": {"rule_id": "SEP-LIFT-BELOW", "task_ids": ["A", "B"]}}


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
    assert (run.status, run.end_reason) == ("ESCALATED", "LLM_ERROR_TWICE")
    assert [s["llm_attempts"] for s in steps] == [2, 2] and run.llm_attempts_used == 4
    assert run.solver_calls_used == 0


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
    assert all("spec_hash" not in a for a in data["attempts"])


def test_system_prompt_does_not_order_l0_first():
    assert "L0부터" not in prompt.SYSTEM


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


def test_live_run_flow_with_scripted_model(monkeypatch):
    """스크립트 흐름 확인. 실제 모델 대신 스크립트 모델을 넣는다(API 호출 없음)."""
    monkeypatch.setattr(
        live_run, "openai_model", lambda s: ScriptedChatModel([solve("L0"), solve("L1")])
    )
    settings = Settings(openai_api_key="sk-test", openai_model="m", openai_temperature="0")
    [r] = live_run.run_once(1, settings, "shipyard", raw=False)
    assert r.get("error") is None, r.get("error")
    assert (r["success"], r["l0_first"], r["first_solve_level"]) == (True, True, "L0")
    assert r["committed"] == {"status": "APPLIED", "reason_codes": []}
    assert (r["run_status"], r["end_reason"]) == ("SUCCEEDED", "COMMITTED:1")
    assert [(s["action"], s["level"], s["result_kind"]) for s in r["steps"]] == [
        ("SOLVE_WITH_SCOPE", "L0", "CONTINUE"),
        ("SOLVE_WITH_SCOPE", "L1", "WAIT"),
    ]
    assert r["model_settings"]["temperature"] == 0.0 and "api_key" not in r["model_settings"]
    assert r["success_criteria"]["forbidden_actions"] == 0
    assert (r["request"], r["expected_outcome"], r["matches_expected"]) == ("A", "CANDIDATE", True)
    assert r["actual"]["level"] == "L1" and r["actual"]["moved"] == {
        "A": [60, "A-CR-01"],
        "C": [90, "A-CR-01"],
    }


def _scripted_each_run(*replies):
    """START_RUN마다 새 스크립트 모델(같은 응답 순서)."""
    return lambda s: ScriptedChatModel(list(replies))


def test_live_run_requests_in_sequence(monkeypatch):
    """--request N1,N2,N3,N4: 같은 DB에서 하나씩 확정한다. 기대값은 verify_demo_values와 같은 출처."""
    monkeypatch.setattr(live_run, "openai_model", _scripted_each_run(solve("L0")))
    settings = Settings(openai_api_key="sk-test", openai_model="m")
    records = live_run.run_once(1, settings, "shipyard", False, ["N1", "N2", "N3", "N4"])
    assert [r.get("error") for r in records] == [None] * 4
    assert [r["request"] for r in records] == ["N1", "N2", "N3", "N4"]
    assert all(r["success"] and r["matches_expected"] and r["l0_first"] for r in records)
    assert [r["committed"]["status"] for r in records] == ["APPLIED"] * 4
    assert [(r["actual"]["delay"], r["actual"]["work_delay"]) for r in records] == [
        (60, 60),
        (45, 45),
        (120, 120),
        (1140, 180),
    ]
    assert records[3]["actual"]["moved"] == {"N4": [2880, None]}
    assert records[3]["expected"]["L0"]["moved"] == {"N4": [2880, None]}


def test_live_run_no_solution_request_succeeds_by_escalation(monkeypatch):
    """--request N5: 모든 범위 INFEASIBLE이 기대값이므로 후보 없음 + ESCALATE_NO_SOLUTION 종료가 성공."""
    monkeypatch.setattr(
        live_run, "openai_model", _scripted_each_run(solve("L0"), solve("L2"), escalate())
    )
    settings = Settings(openai_api_key="sk-test", openai_model="m")
    [r] = live_run.run_once(1, settings, "shipyard", False, ["N5"])
    assert r.get("error") is None, r.get("error")
    assert r["expected_outcome"] == "ESCALATE"
    assert {e["status"] for e in r["expected"].values()} == {"INFEASIBLE"}
    assert (r["actual"], r["committed"]) == (None, None)
    assert r["run_status"] == "ESCALATED" and r["end_reason"].startswith("ESCALATE_NO_SOLUTION")
    assert r["success"] and r["matches_expected"]
    assert r["success_criteria"]["pass_reached"] is False


def test_live_run_rejects_unknown_request(capsys):
    with pytest.raises(SystemExit):
        live_run.main(["--request", "N1,X9"])
    assert "X9" in capsys.readouterr().err


def _shared_script(*replies):
    """START_RUN·RESUME_RUN이 같은 스크립트를 이어서 쓴다(기본안 B는 재개가 두 번 있다)."""
    model = ScriptedChatModel(list(replies))
    return lambda s: model


def _plan_b_script(*after_reply):
    ask = call(
        "ASK_TASK_OWNER",
        "자원 축 확인",
        task_id="A",
        axis="RESOURCE",
        allowed_values=["SITE-CR-01"],
        question="SITE-CR-01을 써도 되나요?",
    )
    return _shared_script(
        solve("L0"),
        solve("L1"),
        solve("L0", "C 고정 반영"),
        call("LIST_ASSIGNABLE_RESOURCES", "A 자원 조회", task_id="A"),
        ask,
        *after_reply,
    )


def test_live_run_path_b_with_scripted_model(monkeypatch):
    """--path B: 거절 → 재개 → LIST → ASK → 스크립트가 수락 → TRY → Beta → 승인 R1 (A.21 9)."""
    try_ = call("TRY_ALTERNATIVE_RESOURCE", "대체 자원", task_id="A", resource_id="SITE-CR-01")
    monkeypatch.setattr(live_run, "openai_model", _plan_b_script(try_))
    settings = Settings(openai_api_key="sk-test", openai_model="m")
    r = live_run.run_path_b(1, settings, "shipyard", False, decline=False)
    assert r.get("error") is None, r.get("error")
    c = r["success_criteria"]
    assert r["success"], c
    assert (r["run_status"], r["end_reason"], r["step_count"]) == ("SUCCEEDED", "COMMITTED:1", 6)
    assert c["ask_uses_listed_alternative"] and c["no_try_before_accept"]
    assert (r["alpha_matches_expected"], r["beta_matches_expected"]) == (True, True)
    assert r["first_action_after_reject"] == "SOLVE_WITH_SCOPE"
    assert [e.get("reply") for e in r["events"] if "reply" in e] == ["ACCEPT"]
    assert r["beta"]["moved"] == {"A": [60, "SITE-CR-01"]}


def test_live_run_path_b_decline_ends_in_escalation(monkeypatch):
    """--path B-decline: 스크립트가 거절 → 같은 질문 미노출 → 이관이면 성공."""
    monkeypatch.setattr(live_run, "openai_model", _plan_b_script(escalate()))
    settings = Settings(openai_api_key="sk-test", openai_model="m")
    r = live_run.run_path_b(1, settings, "shipyard", False, decline=True)
    assert r.get("error") is None, r.get("error")
    assert r["success"], r["success_criteria"]
    assert (r["run_status"], r["committed"], r["messages"]) == ("ESCALATED", None, 1)
    assert r["success_criteria"]["no_ask_or_try_after_decline"]


def test_live_run_path_needs_request_a(capsys):
    with pytest.raises(SystemExit):
        live_run.main(["--path", "B", "--request", "N1"])
    assert "--path B" in capsys.readouterr().err
