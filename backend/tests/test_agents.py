"""Agent 실행 계층. 스크립트 LLM. T43·T48·T49·T51."""

import json
import sqlite3

import pytest
from conftest import add_run
from langchain_core.messages import AIMessage
from scripted import ScriptedChatModel, call, escalate, solve

from app.agents import runtime
from app.agents.prompts.replanning import OBS_HEADER
from app.agents.specs import replanning as spec
from app.solver import cpsat
from app.store import db
from app.store.repos.commands import get_command_result
from app.store.repos.dispatch import list_jobs
from app.store.repos.records import get_candidate
from app.store.repos.runs import get_run, list_steps
from app.store.repos.site import bump_context_version

CONFLICT = {"conflict": {"rule_id": "SEP-LIFT-BELOW", "task_ids": ["A", "B"]}}


def _run(pack, replies, run_id="run_1", **changes):
    add_run(pack, run_id, input_ref=CONFLICT, **changes)
    model = ScriptedChatModel(replies)
    run = runtime.invoke(pack, {"run_id": run_id}, model)
    with db.read() as conn:
        steps = list_steps(conn, run_id)
    return run, steps, model


def _count(table):
    with db.read() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _guards(steps):
    return [(s["status"], s["result_kind"], (s["guard"] or {}).get("reason_code")) for s in steps]


# ── 전략 변경: L0 INFEASIBLE → L1 → WAIT ───────────────────────


def test_l0_infeasible_then_l1_candidate_waits(with_a):
    run, steps, model = _run(with_a, [solve("L0"), solve("L1", "L0 불가, 범위를 넓힌다")])

    assert (run.status, run.wait_kind, run.wait_generation) == (
        "WAITING_HUMAN",
        "CANDIDATE_OUTCOME",
        1,
    )
    assert run.budget_used == {
        "steps": 2,
        "llm_attempts": 2,
        "human_rounds": 0,
        "solver_calls": 2,
        "solver_seconds": 20.0,
        "agent_calls": 0,
    }
    assert _guards(steps) == [("COMPLETED", "CONTINUE", None), ("COMPLETED", "WAIT", None)]
    s1, s2 = steps
    assert s1["tool_result"]["stage1"]["status"] == "INFEASIBLE"
    assert s1["action"] == {
        "name": "SOLVE_WITH_SCOPE",
        "args": {"level": "L0", "skill": "BUILD_CANDIDATE"},
    }
    assert s2["decision_summary"] == "L0 불가, 범위를 넓힌다"
    assert (s2["model_id"], s2["prompt_version"], s2["llm_attempts"]) == (
        "scripted",
        "replanning-p26",
        1,
    )
    assert (s2["observed_context_version"], s2["observed_plan_revision"]) == (1, 0)
    assert s2["goal"] == spec.GOAL and s2["budget_remaining"]["solver_calls"] == 4

    # 두 번째 관찰은 첫 시도를 보고, L0은 더 고를 수 없다
    obs2 = s2["observation"]
    assert [a["scope_level"] for a in obs2["attempts"]] == ["L0"]
    assert "L0" not in obs2["untried_levels"]
    [solve_tool] = [
        t for t in model.calls[1]["tools"] if t["function"]["name"] == "SOLVE_WITH_SCOPE"
    ]
    assert "L0" not in solve_tool["function"]["parameters"]["properties"]["level"]["enum"]
    assert model.calls[0]["kwargs"] == {"tool_choice": "any", "parallel_tool_calls": False}
    header, body = model.calls[1]["messages"][1].content.split("\n", 1)
    assert header == OBS_HEADER
    human = json.loads(body)
    assert human == s2["observation"]  # 모델이 본 것 = 기록한 것
    assert human["attempts"][0]["stage1"]["status"] == "INFEASIBLE"

    cid = s2["state_changes"]["candidate_id"]
    assert run.wait_ref == cid
    with db.read() as conn:
        cand = get_candidate(conn, with_a.site_id, cid)
        jobs = [(j["kind"], j["dedupe_key"]) for j in list_jobs(conn, with_a.site_id)]
        key = get_command_result(conn, "run_1:2")
    assert {a.task_id: (a.start, a.resource_id) for a in cand.assignments}["A"] == (60, "A-CR-01")
    assert ("VALIDATE", f"VALIDATE:{cid}") in jobs
    assert key["command_type"] == "AGENT:SOLVE_WITH_SCOPE" and key["status"] == "APPLIED"


def test_same_effective_spec_is_not_retried_within_case(with_a):
    _run(with_a, [solve("L0"), solve("L1")])
    # 같은 Case의 새 Run: L0·L1은 앞 Run이 이미 시도했다 (CV-13)
    run, steps, model = _run(
        with_a, [solve("L1"), escalate()], run_id="run_2", case_id="case_run_1"
    )
    assert steps[0]["observation"]["untried_levels"] == ["L2"]
    # 앞 Run이 한 같은 계산이다: Solver를 부르지 않고 그때의 결과를 돌려준다
    assert _guards(steps)[0] == ("COMPLETED", "REJECTED", "ALREADY_TRIED")
    previous = steps[0]["tool_result"]
    assert previous["first"] == {"run_id": "run_1", "step_no": 2, "this_run": False}
    assert (previous["scope_level"], previous["stage1"]["status"]) == ("L1", "OPTIMAL")
    assert previous["candidate_id"] is not None
    assert steps[1]["observation"]["last_guard"]["previous"] == previous
    assert run.status == "BLOCKED" and run.solver_calls_used == 0
    # 다른 Case의 Run: 같은 탐색을 다시 계산할 수 있다
    run, steps, model = _run(with_a, [solve("L0"), escalate()], run_id="run_3")
    assert model.tool_names(0)[0] == "SOLVE_WITH_SCOPE"
    assert _guards(steps)[0] == ("COMPLETED", "CONTINUE", None)
    assert run.solver_calls_used == 1


def test_repeated_scope_returns_previous_result_then_escalate(with_a):
    """이미 한 범위를 다시 부르면 Solver도 Solver Budget도 쓰지 않고 그때의 결과를 돌려준다 (CV-13)."""
    run, steps, _ = _run(with_a, [solve("L0"), solve("L0"), escalate("해가 없다")])
    assert _guards(steps) == [
        ("COMPLETED", "CONTINUE", None),
        ("COMPLETED", "REJECTED", "ALREADY_TRIED"),
        ("COMPLETED", "DONE", None),
    ]
    assert steps[1]["tool_result"] == {
        "already_tried": True,
        "first": {"run_id": "run_1", "step_no": 1, "this_run": True},
        "scope_level": "L0",
        "conditions": {},
        "objective": "CHANGE_FIRST",
        "job_status": "REGISTERED",
        "stage1": {"status": "INFEASIBLE", "changed": None},
        "stage2": None,
        "candidate_id": None,
        "same_as_candidate_id": None,
    }
    # 다음 관찰에 그 결과가 보이고, 되풀이한 호출은 계산·Snapshot·탐색 기록을 남기지 않는다
    assert steps[2]["observation"]["last_guard"]["previous"] == steps[1]["tool_result"]
    # Snapshot은 seed 때의 것 하나와 첫 계산의 것 하나다
    assert (_count("solver_job"), _count("search_spec"), _count("snapshot")) == (1, 1, 2)
    assert (run.status, run.end_reason, run.solver_calls_used) == (
        "BLOCKED",
        "RETURN_BLOCKED",
        1,
    )
    result = dict(steps[2]["tool_result"])
    # 모델이 길을 비워도 서버가 계산한 열 수 있는 것이 붙는다(need ID는 Run·s·순번, AG-23)
    assert result.pop("openers") == [
        {
            "need_id": "run_1:s:0",
            "kind": "FACT_CHANGE",
            "field": "PERMISSION",
            "resource_id": "B-CR-01",
        },
    ]
    assert result == {
        "status": "BLOCKED",
        "summary": "해가 없다",
        "paths": [],
        "candidate_ids": [],
        "latest_validation": None,
    }
    assert steps[2]["observation"]["openers"] == [
        {k: v for k, v in n.items() if k != "need_id"} for n in steps[2]["tool_result"]["openers"]
    ]
    assert steps[2]["observation"]["last_guard"]["reason_code"] == "ALREADY_TRIED"


# ── T48 MALFORMED ──────────────────────────────────────────────


def test_malformed_once_then_corrected(with_a):
    run, steps, _ = _run(with_a, [AIMessage(content="L1로 하겠습니다"), solve("L1")])
    assert _guards(steps) == [
        ("COMPLETED", "REJECTED", "MALFORMED"),
        ("COMPLETED", "WAIT", None),
    ]
    assert steps[1]["observation"]["last_guard"]["reason_code"] == "MALFORMED"
    assert run.status == "WAITING_HUMAN" and run.llm_attempts_used == 2


@pytest.mark.parametrize(
    "bad",
    [
        AIMessage(content="", tool_calls=[*solve("L0").tool_calls, *solve("L1").tool_calls]),
        call("SOLVE_WITH_SCOPE", level="L9"),
        AIMessage(
            content="",
            tool_calls=[{"name": "SOLVE_WITH_SCOPE", "args": {"level": "L0"}, "id": "x"}],
        ),
        call("APPROVE_CANDIDATE", candidate_id="c"),
    ],
    ids=["two_calls", "bad_enum", "no_summary", "unknown_action"],
)
def test_malformed_twice_escalates(with_a, bad):
    run, steps, _ = _run(with_a, [bad, bad])
    assert _guards(steps) == [
        ("COMPLETED", "REJECTED", "MALFORMED"),
        ("COMPLETED", "DONE", "MALFORMED"),
    ]
    assert (run.status, run.end_reason) == ("BLOCKED", "MALFORMED_TWICE")
    assert run.solver_calls_used == 0 and _count("solver_job") == 0


def test_malformed_count_restarts_after_other_result(with_a):
    """바로 앞 COMPLETED step만 본다. 사이에 다른 거절(이미 한 탐색)이 있으면 다시 센다."""
    bad = AIMessage(content="L1로 하겠습니다")
    run, steps, _ = _run(with_a, [solve("L0"), bad, solve("L0"), bad, escalate()])
    assert _guards(steps) == [
        ("COMPLETED", "CONTINUE", None),
        ("COMPLETED", "REJECTED", "MALFORMED"),
        ("COMPLETED", "REJECTED", "ALREADY_TRIED"),
        ("COMPLETED", "REJECTED", "MALFORMED"),
        ("COMPLETED", "DONE", None),
    ]
    assert (run.status, run.end_reason) == ("BLOCKED", "RETURN_BLOCKED")


def test_decision_summary_is_truncated_not_rejected(with_a):
    _, steps, _ = _run(with_a, [solve("L1", "가" * 300)])
    assert steps[0]["result_kind"] == "WAIT"
    assert steps[0]["decision_summary"] == "가" * spec.SUMMARY_MAX


# ── T49 Budget ─────────────────────────────────────────────────


def test_step_budget_exhausted(with_a):
    replies = [solve("L0")] + [solve("L0")] * (spec.MAX_STEPS - 1)
    run, steps, _ = _run(with_a, replies)
    assert len(steps) == spec.MAX_STEPS
    assert (run.status, run.end_reason, run.steps_used) == (
        "BUDGET_EXHAUSTED",
        "BUDGET_EXHAUSTED",
        spec.MAX_STEPS,
    )


def test_solver_budget_exhausted_leaves_only_escalate(with_a):
    add_run(with_a, "run_s", input_ref=CONFLICT)
    with db.write() as tx:
        tx.execute(
            "UPDATE agent_run SET solver_calls_used = ? WHERE run_id = 'run_s'",
            (spec.MAX_SOLVER_CALLS,),
        )
    model = ScriptedChatModel([escalate()])
    run = runtime.invoke(with_a, {"run_id": "run_s"}, model)
    assert model.tool_names(0) == [
        "LIST_ASSIGNABLE_RESOURCES",
        "RETURN_RESULT",
    ]
    assert run.status == "BLOCKED"


# ── 버전·Run 상태 재확인 ───────────────────────────────────────


def test_stale_observation_is_not_executed(with_a):
    def event_during_llm():
        with db.write() as tx:
            bump_context_version(tx, with_a.site_id)
        return solve("L0")

    run, steps, _ = _run(with_a, [event_during_llm, escalate()])
    assert _guards(steps)[0] == ("COMPLETED", "REJECTED", "STALE_OBSERVATION")
    assert steps[1]["observed_context_version"] == 2
    assert run.solver_calls_used == 0 and _count("solver_job") == 0


def test_stale_observation_applies_to_escalate(with_a):
    def event_during_llm():
        with db.write() as tx:
            bump_context_version(tx, with_a.site_id)
        return escalate("해가 없다")

    run, steps, _ = _run(with_a, [event_during_llm, escalate("다시 보고도 해가 없다")])
    assert _guards(steps) == [
        ("COMPLETED", "REJECTED", "STALE_OBSERVATION"),
        ("COMPLETED", "DONE", None),
    ]
    assert steps[1]["tool_result"]["summary"] == "다시 보고도 해가 없다"
    assert run.status == "BLOCKED"


def test_stale_snapshot_at_registration(with_a, monkeypatch):
    real = cpsat.solve

    def solve_then_change(*args):
        result = real(*args)
        with db.write() as tx:
            bump_context_version(tx, with_a.site_id)
        return result

    monkeypatch.setattr(cpsat, "solve", solve_then_change)
    run, steps, _ = _run(with_a, [solve("L1"), escalate()])
    assert _guards(steps)[0] == ("COMPLETED", "CONTINUE", "STALE_SNAPSHOT")
    with db.read() as conn:
        assert conn.execute("SELECT status FROM solver_job").fetchone()[0] == "STALE"
    assert _count("candidate") == 0 and run.status == "BLOCKED"


def test_run_made_inactive_during_llm_aborts_step(with_a):
    def stale_during_llm():
        with db.write() as tx:
            tx.execute("UPDATE agent_run SET status = 'STALE', end_reason = 'EVENT:test'")
        return solve("L1")

    run, steps, _ = _run(with_a, [stale_during_llm])
    assert (steps[0]["status"], steps[0]["abort_reason"]) == ("ABORTED", "RUN_INACTIVE")
    assert (run.status, run.end_reason) == ("STALE", "EVENT:test")
    assert _count("solver_job") == 0


def test_model_exception_marks_run_error(with_a):
    run, steps, _ = _run(with_a, [])  # 스크립트 소진 → 예외
    assert (run.status, run.end_reason) == ("ERROR", "EXCEPTION: RuntimeError")
    assert [(s["status"], s["abort_reason"]) for s in steps] == [
        ("ABORTED", "EXCEPTION: RuntimeError")
    ]


# ── T43 그래프 입력 ────────────────────────────────────────────


def test_t43_graph_input_rejects_extra_keys(with_a):
    add_run(with_a, "run_x", input_ref=CONFLICT)
    model = ScriptedChatModel([solve("L1")])
    with pytest.raises(ValueError, match="run_id"):
        runtime.invoke(with_a, {"run_id": "run_x", "approved": True}, model)
    with db.read() as conn:
        assert get_run(conn, "run_x").status == "RUNNING" and list_steps(conn, "run_x") == []
    assert model.calls == [] and _count("plan") == 1


# ── T51 트리거 ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("sql", "match"),
    [
        ("UPDATE agent_run SET status = 'RUNNING'", "terminal status"),
        ("UPDATE agent_run SET steps_used = 0", "must not decrease"),
        ("UPDATE agent_run SET last_step_no = 0", "must not decrease"),
        ("DELETE FROM agent_run", "no delete"),
        ("UPDATE agent_step SET decision_summary = 'x'", "only RESERVED"),
        ("DELETE FROM agent_step", "no delete"),
        ("UPDATE solver_job SET status = 'RESERVED'", "only RESERVED"),
    ],
)
def test_t51_run_step_triggers(with_a, sql, match):
    _run(with_a, [solve("L0"), escalate()])  # ESCALATED, step 2개, solver_job 1개
    with pytest.raises(sqlite3.IntegrityError, match=match), db.write() as tx:
        tx.execute(sql)


def test_t51_error_run_can_continue_but_terminal_cannot(with_a):
    run, _, _ = _run(with_a, [])
    assert run.status == "ERROR"
    with db.write() as tx:  # continue: ERROR → RUNNING은 허용
        tx.execute("UPDATE agent_run SET status = 'RUNNING', end_reason = NULL")


# ── agent_type 등록부 ──────────────────────────────


def test_registry_binds_replanning_spec_prompt_observer_executor():
    from app.agents.registry import BINDINGS

    assert sorted(BINDINGS) == ["COORDINATION", "EVENT_RESPONSE", "INTAKE", "MAIN", "REPLANNING"]
    assert all(t == b.spec.agent_type for t, b in BINDINGS.items())
    binding = BINDINGS["REPLANNING"]
    assert binding.spec.agent_type == spec.AGENT_TYPE == "REPLANNING"
    assert binding.spec is spec.SPEC
    assert (binding.spec.goal, binding.spec.recursion_limit) == (spec.GOAL, spec.RECURSION_LIMIT)
    assert dict(binding.spec.budget) == {
        "steps": spec.MAX_STEPS,
        "llm_attempts": spec.MAX_LLM_ATTEMPTS,
        "solver_calls": spec.MAX_SOLVER_CALLS,
    }
    assert binding.prompt.PROMPT_VERSION == "replanning-p26"
    assert runtime.exec_contract_version("REPLANNING") == "replanning-c9"
    assert runtime.exec_contract_version("COORDINATION") == "coordination-c7"
    assert runtime.exec_contract_version("EVENT_RESPONSE") == "event-response-c7"
    assert runtime.exec_contract_version("INTAKE") == "intake-c12"
    assert runtime.exec_contract_version("ASSISTANT") == "AGENT_TYPE_NOT_REGISTERED"


def test_unregistered_agent_type_ends_run_as_error_without_graph(with_a):
    """등록되지 않은 agent_type은 그래프를 부르지 않고 ERROR로 끝낸다(열린 Case로 남지 않음)."""
    add_run(with_a, "run_assistant", agent_type="ASSISTANT", input_ref=CONFLICT)
    model = ScriptedChatModel([solve("L0")])
    run = runtime.invoke(with_a, {"run_id": "run_assistant"}, model)
    assert (run.status, run.end_reason) == ("ERROR", "AGENT_TYPE_NOT_REGISTERED: ASSISTANT")
    assert model.calls == [] and run.steps_used == 0 and _count("agent_step") == 0


def test_main_auto_start_is_on_by_default(monkeypatch):
    """운영 기본값: 사건 → 메인 자동 시작은 켜짐이고 설정은 끄는 스위치다.

    테스트는 .env를 읽지 않고 conftest가 끈다. 환경변수를 지우면 코드 기본값이 보인다.
    """
    from app.config import Settings

    monkeypatch.delenv("MAIN_AUTO_START")
    assert Settings().main_auto_start is True
    monkeypatch.setenv("MAIN_AUTO_START", "false")
    assert Settings().main_auto_start is False
