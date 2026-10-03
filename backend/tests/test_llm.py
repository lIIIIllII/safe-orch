"""실제 모델 연결. 실제 API는 부르지 않는다."""

import json
import re

import httpx
import openai
import pytest
import yaml
from conftest import add_run
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from scripted import (
    ScriptedChatModel,
    solve,
)

from app.agents import llm, runtime
from app.agents.observers.replanning import build_observation
from app.agents.prompts import replanning as prompt
from app.config import Settings
from app.main import app
from app.packs.loader import load_pack
from app.store import db
from app.store.repos.runs import list_steps

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


# ── 네트워크 차단 ─────────────────────────────────────


def test_network_is_blocked_but_testclient_works():
    with pytest.raises(RuntimeError, match="network disabled"):
        httpx.HTTPTransport().handle_request(REQ)
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200


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
    assert "계산·조회로 열 수 있는 대안" in system
