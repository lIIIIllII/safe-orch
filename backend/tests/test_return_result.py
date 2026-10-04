"""RETURN_RESULT: 결과 모양(길 묶음·needs), 서버 검증, 임시 연결, Intake 접수 미완 (AG-06·AG-23)."""

import pytest
from conftest import add_run
from langchain_core.messages import AIMessage
from pydantic import ValidationError
from scripted import Router, ScriptedChatModel, blocked, call, done, solve
from test_intake import VALUES_A, _complete, _intake, _messages, _runs, _steps, _task

from app.agents import runtime
from app.agents.specs import intake as intake_spec
from app.coordinator.dispatcher import run_until_idle
from app.domain.needs import Need, Path, ResultFields
from app.store import db
from app.store.repos.case_events import list_case_events
from app.store.repos.runs import list_steps

# ── 모양 (순수 모델) ───────────────────────────────────────────


@pytest.mark.parametrize(
    "need",
    [
        {"kind": "FACT_CHANGE", "field": "WINDOW", "task_id": "A"},
        {"kind": "FACT_CHANGE", "field": "QUANTITY", "pool_id": "P"},
        {"kind": "HUMAN_INFO", "actor_id": "reporter", "event_id": "evt_1"},
        {"kind": "HUMAN_DECISION", "candidate_id": "cand_1"},
    ],
)
def test_need_takes_the_references_of_its_kind(need):
    assert Need(**need).kind == need["kind"]


@pytest.mark.parametrize(
    "need",
    [
        {"kind": "BUDGET"},  # Budget 소진은 need가 아니다
        {"kind": "OWNER_CONSENT", "task_id": "A"},  # 담당자 확인은 need가 아니다(협의에서 받는다)
        {"kind": "OTHER_UNIT", "unit_id": "UB"},  # 다른 Unit은 need가 아니다 (AG-24)
        {"kind": "HUMAN_DECISION", "candidate_id": "c", "task_id": "A"},  # 다른 종류의 참조
        {"kind": "FACT_CHANGE", "field": "WINDOW", "resource_id": "R1"},  # 필드와 대상이 다르다
        {"kind": "FACT_CHANGE", "field": "WINDOW", "task_id": "A", "pool_id": "P"},
        {"kind": "HUMAN_INFO", "actor_id": "reporter"},
        {"kind": "HUMAN_DECISION"},
        {"kind": "HUMAN_DECISION", "candidate_id": "c", "comment": "자유 문장"},
    ],
)
def test_need_rejects_wrong_references(need):
    with pytest.raises(ValidationError):
        Need(**need)


def test_result_shape_paths_may_be_empty_and_done_has_none():
    path = Path(needs=[Need(kind="FACT_CHANGE", field="WINDOW", task_id="A")])
    assert ResultFields(status="BLOCKED", summary="해 없음").paths == []
    assert len(ResultFields(status="BLOCKED", summary="길 둘", paths=[path, path]).paths) == 2
    with pytest.raises(ValidationError):
        ResultFields(status="DONE", summary="끝", paths=[path])
    with pytest.raises(ValidationError):
        ResultFields(status="BLOCKED", summary="길 넷", paths=[path] * 4)
    with pytest.raises(ValidationError):
        Path(needs=[])


# ── Replanning: 길 묶음과 서버 검증 ────────────────────────────


def _invoke(pack, replies, run_id="run_r", **changes):
    add_run(pack, run_id, **changes)
    model = ScriptedChatModel(list(replies))
    run = runtime.invoke(pack, {"run_id": run_id}, model)
    with db.read() as conn:
        return run, list_steps(conn, run_id), model


def test_blocked_result_keeps_paths_and_server_fills_the_rest(with_a):
    paths = [
        {"needs": [{"kind": "FACT_CHANGE", "field": "DURATION", "task_id": "A"}]},
        {"needs": [{"kind": "FACT_CHANGE", "field": "WINDOW", "task_id": "A"}]},
    ]
    run, steps, model = _invoke(with_a, [solve("L0"), blocked("L0에서 해가 없다", paths)])
    # 스키마에는 지금 쓸 수 있는 상태만 보인다
    [tool] = [t for t in model.calls[0]["tools"] if t["function"]["name"] == "RETURN_RESULT"]
    assert tool["function"]["parameters"]["properties"]["status"]["enum"] == ["BLOCKED"]
    result = dict(steps[-1]["tool_result"])
    # 서버가 붙인 열 수 있는 것은 모델이 엮은 길과 따로 남는다. need ID는 Run·길(p<순번> 또는 s)·순번이다
    openers = result.pop("openers")
    assert [(n["need_id"], n["kind"]) for n in openers] == [("run_r:s:0", "FACT_CHANGE")]
    assert result == {
        "status": "BLOCKED",
        "summary": "L0에서 해가 없다",
        "paths": [
            {
                "needs": [
                    {
                        "need_id": "run_r:p0:0",
                        "kind": "FACT_CHANGE",
                        "task_id": "A",
                        "field": "DURATION",
                    }
                ]
            },
            {
                "needs": [
                    {
                        "need_id": "run_r:p1:0",
                        "kind": "FACT_CHANGE",
                        "task_id": "A",
                        "field": "WINDOW",
                    }
                ]
            },
        ],
        "candidate_ids": [],
        "latest_validation": None,
    }
    # 전문 Agent는 이관 상태가 되지 않는다. 막힘으로 끝난다 (AG-06)
    assert (run.status, run.end_reason) == ("BLOCKED", "RETURN_BLOCKED")


def test_needs_are_checked_against_facts(with_a):
    bad = [
        {"kind": "FACT_CHANGE", "field": "WINDOW", "task_id": "NOPE"},
        {"kind": "FACT_CHANGE", "field": "PERMISSION", "resource_id": "NOPE"},
        {"kind": "FACT_CHANGE", "field": "QUANTITY", "pool_id": "NOPE"},
        {"kind": "HUMAN_INFO", "actor_id": "nobody", "task_id": "A"},
        {"kind": "HUMAN_DECISION", "candidate_id": "cand_none"},
    ]
    replies = [
        blocked("틀린 참조", [{"needs": bad[:2]}, {"needs": bad[2:]}]),
        blocked("풀 길 없음"),
    ]
    run, steps, _ = _invoke(with_a, replies)
    assert steps[0]["guard"] == {"verdict": "REJECTED", "reason_code": "NEED_INVALID"}
    assert [
        (n["path"], n["need"], n["reason"]) for n in steps[0]["tool_result"]["invalid_needs"]
    ] == [
        (0, 0, "TARGET_NOT_FOUND"),
        (0, 1, "TARGET_NOT_FOUND"),
        (1, 0, "TARGET_NOT_FOUND"),
        (1, 1, "ACTOR_NOT_FOUND"),
        (1, 2, "TARGET_NOT_FOUND"),
    ]
    # 거절은 형식 오류가 아니다. Run은 계속되고 다음 결과로 끝난다
    assert (steps[1]["result_kind"], run.status) == ("DONE", "BLOCKED")


def test_replanning_cannot_return_done_while_candidate_decision_is_open(with_a):
    run, steps, _ = _invoke(with_a, [done("끝"), blocked()])
    assert steps[0]["guard"]["reason_code"] == "ACTION_NOT_AVAILABLE"
    assert run.status == "BLOCKED"


def test_child_run_result_is_not_mapped_to_old_ending(with_a):
    """부모가 있는 Run은 임시 연결을 타지 않는다: BLOCKED로 끝나고 하위 Run 종료 사건이 남는다."""
    pack = with_a
    add_run(pack, "main", agent_type="MAIN", case_id="case_m")
    run, _, _ = _invoke(pack, [blocked()], "child", case_id="case_m", parent_run_id="main")
    assert (run.status, run.end_reason) == ("BLOCKED", "RETURN_BLOCKED")
    with db.read() as conn:
        [event] = list_case_events(conn, pack.site_id)
    assert (event["kind"], event["case_id"], event["ref"]["status"]) == (
        "CHILD_RUN_ENDED",
        "case_m",
        "BLOCKED",
    )


# ── Intake: 접수 미완 (AG-06) ──────────────────────────────────


def test_intake_blocked_ends_as_incomplete_and_notifies_requester(seeded):
    pack = seeded
    assert _intake(pack).status == "APPLIED"
    need = {"kind": "HUMAN_INFO", "actor_id": "planner_a", "task_id": "A"}
    wrong = {"kind": "HUMAN_INFO", "actor_id": "planner_b", "task_id": "A"}
    replies = [
        _complete({**VALUES_A, "requested_resource_id": "B-CR-01"}),
        blocked("요청자가 아닌 사람", [{"needs": [wrong]}]),
        blocked("쓸 수 있는 자원을 찾지 못했다", [{"needs": [need]}]),
    ]
    run_until_idle(pack, model_factory=Router(intake=replies).factory())
    [run] = _runs("INTAKE")
    steps = _steps(run.run_id)
    assert steps[1]["tool_result"]["invalid_needs"][0]["reason"] == "ACTOR_MISMATCH"
    assert (run.status, run.end_reason) == ("BLOCKED", "RETURN_BLOCKED")
    assert steps[-1]["tool_result"]["reason_codes"] == ["RESOURCE_NOT_AUTHORIZED"]
    [notice] = _messages("NOTICE")
    assert (notice["to_actor_id"], notice["run_id"], notice["status"]) == (
        "planner_a",
        run.run_id,
        "OPEN",
    )
    assert "A 접수가 완료되지 않았습니다(사유: RESOURCE_NOT_AUTHORIZED)" in notice["body"]
    assert notice["agent_text"] == "쓸 수 있는 자원을 찾지 못했다"
    # 작업도, 메인에게 갈 사건도, 재검사도 생기지 않는다
    assert _task(pack, "A") is None
    with db.read() as conn:
        assert list_case_events(conn, pack.site_id) == []
        assert (
            conn.execute("SELECT COUNT(*) FROM dispatch_job WHERE kind = 'RECHECK'").fetchone()[0]
            == 0
        )


def test_intake_server_ended_runs_also_notify_requester(seeded, monkeypatch):
    """완료가 아닌 종료는 모두 접수 미완이다: 형식 오류 2회(BLOCKED)와 Budget 소진에도 요청자에게 알린다."""
    pack = seeded
    assert _intake(pack).status == "APPLIED"
    bad = AIMessage(content="도구를 부르지 않는다")
    run_until_idle(pack, model_factory=Router(intake=[bad, bad]).factory())
    [run] = _runs("INTAKE")
    assert (run.status, run.end_reason) == ("BLOCKED", "MALFORMED_TWICE")
    [notice] = _messages("NOTICE")
    assert (notice["to_actor_id"], notice["run_id"], notice["agent_text"]) == (
        "planner_a",
        run.run_id,
        None,
    )
    assert "A 접수가 완료되지 않았습니다(사유: MALFORMED_TWICE)" in notice["body"]

    monkeypatch.setitem(intake_spec.SPEC.budget, "steps", 1)
    assert _intake(pack, task_id="A2").status == "APPLIED"
    lookup = call("LOOKUP_RESOURCE", resource_type="CRANE")
    run_until_idle(pack, model_factory=Router(intake=[lookup]).factory())
    second = _runs("INTAKE")[-1]
    assert (second.status, second.end_reason) == ("BUDGET_EXHAUSTED", "BUDGET_EXHAUSTED")
    last = _messages("NOTICE")[-1]
    assert last["run_id"] == second.run_id and "사유: BUDGET_EXHAUSTED" in last["body"]
    # Supervisor에게는 아무것도 가지 않는다(이관 없음)
    assert {m["to_actor_id"] for m in _messages("NOTICE")} == {"planner_a"}
