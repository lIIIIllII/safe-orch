"""RETURN_RESULT: 결과 모양(길 묶음·needs), 서버 검증, 임시 연결, Intake 접수 미완 (AG-06·AG-23)."""

import pytest
from conftest import add_run
from langchain_core.messages import AIMessage
from pydantic import ValidationError
from scripted import Router, ScriptedChatModel, blocked, call, done, solve
from test_intake import _complete, _intake, _messages, _reply, _request, _runs, _steps, _task

from app.agents import runtime
from app.agents.observers import replanning as replanning_observer
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
        {"kind": "OWNER_CONSENT", "task_id": "A", "axis": "RESOURCE", "values": ["R1"]},
        {"kind": "OWNER_CONSENT", "task_id": "A", "axis": "TIME"},
        {"kind": "OTHER_UNIT", "group_id": "grp_1", "unit_id": "UB"},
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
        {"kind": "OWNER_CONSENT", "task_id": "A"},  # 축이 없다
        {"kind": "OWNER_CONSENT", "task_id": "A", "axis": "TIME", "values": ["R1"]},
        {"kind": "OTHER_UNIT", "unit_id": "UB"},  # 충돌 그룹이 없다
        {
            "kind": "OTHER_UNIT",
            "group_id": "g",
            "unit_id": "UB",
            "task_id": "A",
        },  # 다른 종류의 참조
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
    path = Path(needs=[Need(kind="OTHER_UNIT", group_id="grp_1", unit_id="UB")])
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
        {"needs": [{"kind": "OWNER_CONSENT", "task_id": "A", "axis": "RESOURCE"}]},
        {"needs": [{"kind": "FACT_CHANGE", "field": "WINDOW", "task_id": "A"}]},
    ]
    run, steps, model = _invoke(with_a, [solve("L0"), blocked("L0에서 해가 없다", paths)])
    # 스키마에는 지금 쓸 수 있는 상태만 보인다
    [tool] = [t for t in model.calls[0]["tools"] if t["function"]["name"] == "RETURN_RESULT"]
    assert tool["function"]["parameters"]["properties"]["status"]["enum"] == ["BLOCKED"]
    assert steps[-1]["tool_result"] == {
        "status": "BLOCKED",
        "summary": "L0에서 해가 없다",
        "paths": [
            {"needs": [{"kind": "OWNER_CONSENT", "task_id": "A", "axis": "RESOURCE"}]},
            {"needs": [{"kind": "FACT_CHANGE", "task_id": "A", "field": "WINDOW"}]},
        ],
        "candidate_ids": [],
        "latest_validation": None,
    }
    # 전문 Agent는 이관 상태가 되지 않는다. 막힘으로 끝난다 (AG-06)
    assert (run.status, run.end_reason) == ("BLOCKED", "RETURN_BLOCKED")


def test_needs_are_checked_against_facts(with_a):
    with db.read() as conn:
        obs = replanning_observer.build_observation(conn, with_a, add_run(with_a, "probe"))
    gid = obs.data["group"]["group_id"]
    assert obs.data["group"]["unit_ids"] == ["UA", "UB"]
    bad = [
        {"kind": "OWNER_CONSENT", "task_id": "NOPE", "axis": "TIME"},
        {"kind": "OWNER_CONSENT", "task_id": "A", "axis": "RESOURCE", "values": ["B-CR-01"]},
        {"kind": "OTHER_UNIT", "group_id": gid, "unit_id": "UA"},
        {"kind": "OTHER_UNIT", "group_id": gid, "unit_id": "SITE"},
        {"kind": "OTHER_UNIT", "group_id": "grp_none", "unit_id": "UB"},
        {"kind": "FACT_CHANGE", "field": "QUANTITY", "pool_id": "NOPE"},
        {"kind": "HUMAN_INFO", "actor_id": "nobody", "task_id": "A"},
        {"kind": "HUMAN_DECISION", "candidate_id": "cand_none"},
    ]
    replies = [
        blocked("틀린 참조", [{"needs": bad[:4]}, {"needs": bad[4:]}]),
        # 그 그룹에 작업을 가진 다른 Unit은 유효한 참조다
        blocked(
            "다른 Unit", [{"needs": [{"kind": "OTHER_UNIT", "group_id": gid, "unit_id": "UB"}]}]
        ),
    ]
    run, steps, _ = _invoke(with_a, replies)
    assert steps[0]["guard"] == {"verdict": "REJECTED", "reason_code": "NEED_INVALID"}
    assert [
        (n["path"], n["need"], n["reason"]) for n in steps[0]["tool_result"]["invalid_needs"]
    ] == [
        (0, 0, "TASK_NOT_FOUND"),
        (0, 1, "RESOURCE_NOT_ELIGIBLE"),
        (0, 2, "SAME_UNIT"),
        (0, 3, "UNIT_NOT_IN_GROUP"),
        (1, 0, "GROUP_NOT_FOUND"),
        (1, 1, "TARGET_NOT_FOUND"),
        (1, 2, "ACTOR_NOT_FOUND"),
        (1, 3, "TARGET_NOT_FOUND"),
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
    run_until_idle(pack, model_factory=Router(intake=[_request()]).factory())
    [confirm] = _messages("CONFIRMATION")
    assert _reply(pack, confirm["message_id"], "DECLINE", "취소합니다").status == "APPLIED"
    need = {"kind": "HUMAN_INFO", "actor_id": "planner_a", "task_id": "A"}
    wrong = {"kind": "HUMAN_INFO", "actor_id": "planner_b", "task_id": "A"}
    replies = [
        blocked("요청자가 아닌 사람", [{"needs": [wrong]}]),
        blocked("요청자가 값 확인을 거절했다", [{"needs": [need]}]),
    ]
    run_until_idle(pack, model_factory=Router(intake=replies).factory())
    [run] = _runs("INTAKE")
    steps = _steps(run.run_id)
    assert steps[1]["tool_result"]["invalid_needs"][0]["reason"] == "ACTOR_MISMATCH"
    assert (run.status, run.end_reason) == ("BLOCKED", "RETURN_BLOCKED")
    assert steps[-1]["tool_result"]["reason_codes"] == ["REQUESTER_DECLINED"]
    [notice] = _messages("NOTICE")
    assert (notice["to_actor_id"], notice["run_id"], notice["status"]) == (
        "planner_a",
        run.run_id,
        "OPEN",
    )
    assert "A 접수가 완료되지 않았습니다(사유: REQUESTER_DECLINED)" in notice["body"]
    assert notice["agent_text"] == "요청자가 값 확인을 거절했다"
    # 작업도, 메인에게 갈 사건도, 재검사도 생기지 않는다
    assert _task(pack, "A") is None
    with db.read() as conn:
        assert list_case_events(conn, pack.site_id) == []
        assert (
            conn.execute("SELECT COUNT(*) FROM dispatch_job WHERE kind = 'RECHECK'").fetchone()[0]
            == 0
        )


def test_intake_observation_says_when_completion_is_possible(seeded):
    """완료 가능은 완료 도구의 유효성과 같은 사실이다. 남은 사람 라운드가 0이어도 true면 완료된다."""
    pack = seeded
    assert _intake(pack).status == "APPLIED"
    ask = call(
        "ASK_CLARIFICATION",
        fields={
            f: {"status": "MISSING" if f == "zone_id" else "RECEIVED", "value": None}
            for f in ("work_type", "zone_id", "duration", "window", "resource")
        },
        question="구역을 알려 주세요.",
    )
    for _ in range(2):
        run_until_idle(pack, model_factory=Router(intake=[ask]).factory())
        [q] = [m for m in _messages("QUESTION") if m["status"] == "OPEN"]
        assert _reply(pack, q["message_id"], "ANSWER", "B 구역").status == "APPLIED"
    run_until_idle(pack, model_factory=Router(intake=[_request()]).factory())
    [confirm] = _messages("CONFIRMATION")
    assert _reply(pack, confirm["message_id"]).status == "APPLIED"
    run_until_idle(pack, model_factory=Router(intake=[_complete()]).factory())
    [run] = _runs("INTAKE")
    steps = _steps(run.run_id)
    assert [s["observation"]["can_complete"] for s in steps] == [False, False, False, True]
    last = steps[-1]["observation"]
    assert (last["human_rounds"]["remaining"], last["can_complete"]) == (0, True)
    assert (run.status, _task(pack, "A").lifecycle) == ("SUCCEEDED", "READY")


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
