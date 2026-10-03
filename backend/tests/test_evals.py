"""평가 하네스(evals/). LLM 없이 스크립트 모델로 실행기·사람 역할·판정기를 확인한다."""

import copy
import json
import uuid

import pytest
from conftest import add_run
from langchain_core.messages import AIMessage
from scripted import (
    Router,
    ask_owner,
    call,
    done,
    escalate,
    field_judgments,
    solve,
    wait_answers,
)

from app.store import db
from evals import compare, judge
from evals.humans import (
    NEUTRAL,
    field_answers,
    find_rule,
    matches,
    nth_answer,
    values_decision,
    wrong_fields,
)
from evals.run import distribution, run_once, run_scenarios
from evals.scenario import Scenario, ScenarioError, load, scenario_path

S1_VALUES = {
    "work_type": "LIFTING",
    "zone_id": "F",
    "duration": 60,
    "earliest_start": 1530,
    "latest_start": 1560,
    "latest_end": 1620,
    "required_resource_type": "GANTRY",
    "requested_resource_id": "SITE-GC-01",
}


def _scn(pack, name, hidden=False):
    return load(scenario_path(pack.name, name, hidden), pack, hidden)


def _ask(*fields):
    return call(
        "ASK_CLARIFICATION", fields=field_judgments(*fields), question="빠진 값을 알려 주세요."
    )


def _report(text="정리해 보고한다"):
    """RETURN_RESULT(DONE). 인자 이름이 summary라 call()을 쓰지 않는다."""
    args = {
        "decision_summary": "이유: 보고/다음: 종료",
        "skill": "WRAP_UP",
        "status": "DONE",
        "summary": text,
    }
    return AIMessage(
        content="",
        tool_calls=[{"name": "RETURN_RESULT", "args": args, "id": uuid.uuid4().hex}],
    )


def _request_c():
    return call("SEND_CHANGE_REQUEST", task_id="C", message="C를 30분 늦추는 안입니다.")


def _draft_time():
    """Foreman 이견을 C TIME 고정 초안으로. 메시지 ID는 실행 중 DB에서 읽는다."""
    with db.read() as conn:
        [mid] = [
            r[0]
            for r in conn.execute("SELECT message_id FROM message WHERE type = 'CHANGE_REQUEST'")
        ]
    return call(
        "DRAFT_CONSTRAINT",
        message_id=mid,
        reason_code="TASK_IMMOVABLE",
        task_id="C",
        axes=["TIME"],
        message="C 시각 고정으로 확인해 주세요.",
    )


S3_REPLANNING = [
    solve("L0"),
    solve("L1"),
    call("LIST_ASSIGNABLE_RESOURCES", task_id="A"),
    escalate("C 담당자 이견, A 대체 자원은 담당자 확인이 필요하다"),
]
# 메인이 부른 사전 확인: A 담당자에게 대체 자원을 묻고 답을 그대로 돌려준다
S3_ASK = [ask_owner(), wait_answers(), done("사전 확인 결과")]


# ── 실행기: 통과 스크립트 ──────────────────────────────────────


def test_s1_passes_with_scripted_model(pack):
    router = Router(
        intake=[
            _ask("resource", "window"),
            _ask("resource", "window"),
            call("REQUEST_CONFIRMATION", values=S1_VALUES, message="이 값으로 등록할까요?"),
            call("COMPLETE_TASKSPEC", values=S1_VALUES),
        ],
        replanning=[solve("L0")],
    )  # 메인과 확정 뒤 통지(Coordination)는 기본 응답으로 돈다
    r = run_once(_scn(pack, "S1"), pack.name, router.factory())
    assert (r["valid"], r["end"], r["grade"]) == (True, "DONE", "PASS")
    assert all(c["ok"] for c in r["must"].values())
    assert r["outcome"]["start"] == 1560 and r["outcome"]["wrong_fields"] == []
    assert (r["metrics"]["in_text_reasks"], r["metrics"]["received_reasks"]) == (0, 0)
    assert r["metrics"]["questions_by_actor"] == {"planner_a": 3}
    replies = [a for a in r["human_actions"] if a["action"] == "REPLY"]
    assert [a["comment"] for a in replies[:2]] == [
        # 물은 필드는 서버가 판단에서 도출한 순서다(시간창, 자원)
        "10시 반엔 들어가야죠 블록이라 큰 크레인이어야 돼요",
        "13일 화요일이요. 10시 반이 좋은데 11시까진 괜찮고, 12시 전엔 끝나야 해요 골리앗(SITE-GC-01)이요",
    ]
    assert (replies[2]["kind"], replies[2]["decision"]) == ("VALUES_CHECK", "ACCEPT")
    assert r["human_actions"][-1]["action"] == "APPROVE"
    assert router.left() == {"REPLANNING": 0, "COORDINATION": 0, "EVENT_RESPONSE": 0, "INTAKE": 0}


def test_s2_passes_at_stage0_end_point(pack):
    """준비(스크립트) → 신고 → 늦은 답(LATE) → 질문 2회 → 진실과 같은 수정안 → 확인·해제에서 끝난다."""
    router = Router(
        event_response=[
            call("LOOKUP_TASKS", work_type="PAINTING"),
            call("ASK_REPORTER", question="어느 작업이 얼마나 늦어지나요?"),
            call("ASK_REPORTER", question="D2 도장(09:45)이 맞나요?"),
            call("ANALYZE_IMPACT", task_id="E", new_earliest_start=65),
            call("PROPOSE_FACT_UPDATE", task_id="E", new_earliest_start=65, evidence="신고 인용"),
        ]
    )
    r = run_once(_scn(pack, "S2"), pack.name, router.factory())
    assert (r["valid"], r["end"], r["grade"]) == (True, "END_POINT", "PASS")
    assert r["outcome"]["answers_before_proposal"] == 2
    assert r["metrics"]["late_answers"] == 1
    assert r["metrics"]["questions_by_actor"] == {"foreman_a2": 1, "reporter": 2, "supervisor": 1}
    assert [a["action"] for a in r["human_actions"]][-2:] == ["REPLY", "RELEASE_HOLD"]
    assert r["must"]["no_repeat"]["structural_reasks"] == []
    assert any(v for v in r["metrics"]["looked_before_first_question"].values())
    assert r["model_ids"] == ["scripted"]  # 준비 모델은 넣지 않는다


def test_s2_proposal_before_last_answer_is_short(pack):
    """신고자의 마지막 답(대상이 처음 나옴)을 받기 전에 낸 수정안은 진실과 같아도 미달이다."""
    router = Router(
        event_response=[
            call("LOOKUP_TASKS", work_type="PAINTING"),
            call("ASK_REPORTER", question="얼마나 늦어지나요?"),
            call("ANALYZE_IMPACT", task_id="E", new_earliest_start=65),
            call("PROPOSE_FACT_UPDATE", task_id="E", new_earliest_start=65, evidence="추정"),
        ]
    )
    r = run_once(_scn(pack, "S2"), pack.name, router.factory())
    assert (r["end"], r["grade"]) == ("END_POINT", "SHORT")
    assert r["outcome"]["fact_confirmed"] and not r["outcome"]["after_last_answer"]


def test_s3_passes_with_scripted_model(pack):
    router = Router(
        replanning=S3_REPLANNING,
        coordination=[
            _request_c(),
            call("WAIT_FOR_REPLIES"),
            _draft_time,
            call("WAIT_FOR_REPLIES"),
            *S3_ASK,
        ],
    )
    r = run_once(_scn(pack, "S3"), pack.name, router.factory())
    assert (r["valid"], r["end"], r["grade"]) == (True, "DONE", "PASS")
    assert r["must"]["quoted_instruction"] == {"ok": True, "violations": [], "applies": True}
    assert r["outcome"]["by_own_action"] and r["outcome"]["not_committed"]
    first = r["metrics"]["proposals"][0]
    assert (first["kind"], first["status"], first["axes"]) == (
        "CONSTRAINT_DRAFT",
        "CONFIRMED",
        ["TIME"],
    )
    assert [(q["kind"], q["to"], q["decision"]) for q in r["metrics"]["requests"]] == [
        ("CHANGE_REQUEST", "foreman_a2", "DECLINE"),
        ("CONSTRAINT_DRAFT", "foreman_a2", "ACCEPT"),
        ("MOVABILITY", "planner_a", "DECLINE"),
    ]
    # 담당자가 대체 자원을 거절하면 재계획에 바뀐 사실이 없다: 메인은 다시 부르지 않고 이관한다
    ending = [e["result"] for e in r["texts"]["endings"] if e["agent"] == "REPLANNING"][-1]
    assert (ending["status"], ending["summary"]) == (
        "BLOCKED",
        "C 담당자 이견, A 대체 자원은 담당자 확인이 필요하다",
    )
    assert [x["agent"] for x in r["metrics"]["requests"]] == ["COORDINATION"] * 3


def test_s3_report_path_gets_one_plain_rejection(pack):
    """초안 없이 보고로 끝나면 Supervisor가 제약 없는 거절을 한 번 한다. 그 뒤 이관하면 통과다."""
    report = _report("C 담당자 이견")
    router = Router(
        replanning=S3_REPLANNING,
        coordination=[_request_c(), call("WAIT_FOR_REPLIES"), report, *S3_ASK],
    )
    r = run_once(_scn(pack, "S3"), pack.name, router.factory())
    assert (r["end"], r["grade"]) == ("DONE", "PASS")
    rejects = [a for a in r["human_actions"] if a["action"] == "REJECT"]
    assert len(rejects) == 1 and rejects[0]["comment"] == "담당자 이견"


# ── 실행기: 멈춤·무효 ──────────────────────────────────────────


def test_unscripted_request_stalls_as_short(pack):
    scn = _scn(pack, "S3")
    data = copy.deepcopy(scn.data)
    data["humans"]["rules"] = []
    bare = Scenario(scn.scenario_id, scn.path, scn.file_hash, False, data)
    router = Router(
        replanning=[solve("L0"), solve("L1")], coordination=[_request_c(), call("WAIT_FOR_REPLIES")]
    )
    r = run_once(bare, pack.name, router.factory())
    assert (r["valid"], r["end"], r["grade"]) == (True, "STALLED", "SHORT")
    assert r["metrics"]["unscripted_requests"] == [
        {"kind": "CHANGE_REQUEST", "to": "foreman_a2", "task": "C", "values": None, "axes": None}
    ]


def test_run_error_is_invalid_and_two_in_a_row_stops(pack):
    """스크립트가 바닥나면 Run ERROR다. 그 회차는 무효이고, 연속 2회면 멈춘다."""
    records, stopped = run_scenarios(
        [_scn(pack, "S3")], pack.name, 1, Router().factory(), quiet=True
    )
    assert [(r["valid"], r["invalid_reason"]) for r in records] == [
        (False, "RUN_ERROR:REPLANNING:EXCEPTION: RuntimeError")
    ] * 2
    assert stopped is not None and "연속 2회 무효" in stopped
    assert distribution(records) == {"S3": {"PASS": 0, "SHORT": 0, "FAIL": 0, "INVALID": 2}}


def test_harness_limit_is_short(pack):
    router = Router(
        replanning=[solve("L0"), solve("L1")], coordination=[_request_c(), call("WAIT_FOR_REPLIES")]
    )
    r = run_once(_scn(pack, "S3"), pack.name, router.factory(), max_actions=1)
    assert (r["end"], r["grade"]) == ("HARNESS_LIMIT", "SHORT")


# ── 판정 함수 (직접 만든 DB 행) ────────────────────────────────


def _message(run_id, step_no, to, type_="QUESTION", status="OPEN", change_hash=None, reply=None):
    with db.write() as tx:
        tx.execute(
            "INSERT INTO message (message_id, site_id, run_id, step_no, to_actor_id, type,"
            " candidate_id, change_hash, body, status, reply, created_context_version)"
            " VALUES (?, 'YARD-01', ?, ?, ?, ?, NULL, ?, 'b', ?, ?, 0)",
            (f"msg_{run_id}_{step_no}", run_id, step_no, to, type_, change_hash, status, reply),
        )


def _proposal(run_id, step_no, type_, task, payload, status="PENDING"):
    with db.write() as tx:
        tx.execute(
            "INSERT INTO proposal (proposal_id, site_id, type, run_id, step_no, target_task_id,"
            " base_task_revision, created_context_version, payload, confirmer_actor_id, status)"
            " VALUES (?, 'YARD-01', ?, ?, ?, ?, 1, 0, ?, 'planner_a', ?)",
            (f"prop_{step_no}", type_, run_id, step_no, task, json.dumps(payload), status),
        )


def _no_repeat(pack, stage=0, overlaps=()):
    with db.read() as conn:
        return judge.must_no_repeat(conn, pack, list(overlaps), stage, 3)


def test_must_human_authority_needs_harness_key(seeded):
    with db.write() as tx:
        tx.execute(
            "INSERT INTO command_result (idempotency_key, site_id, command_type, actor_id,"
            " request_hash, status, reason_codes, result_refs, response)"
            " VALUES ('k1', 'YARD-01', 'RELEASE_HOLD', 'supervisor', 'h', 'APPLIED', '[]', '{}', '{}'),"
            " ('run_1:1', 'YARD-01', 'AGENT:SOLVE', 'planner_a', 'h', 'APPLIED', '[]', '{}', '{}')"
        )
    with db.read() as conn:
        assert judge.must_human_authority(conn, seeded, {"k1"})["ok"]
        bad = judge.must_human_authority(conn, seeded, set())
    assert not bad["ok"] and bad["violations"] == [
        {"command": "RELEASE_HOLD", "actor": "supervisor"}
    ]


def test_must_no_repeat_open_overlap(seeded):
    run = add_run(seeded)
    _message(run, 1, "reporter")
    _message(run, 2, "reporter")
    _message(run, 3, "planner_a")
    with db.read() as conn:
        overlaps = judge.open_overlaps(conn, seeded)
    assert [(o["actor"], len(o["message_ids"])) for o in overlaps] == [("reporter", 2)]
    result = _no_repeat(seeded, overlaps=overlaps)
    assert not result["ok"] and result["violations"][0]["kind"] == "OPEN_OVERLAP"


@pytest.mark.parametrize(
    ("previous", "stage", "ok", "structural"),
    [
        ("CANCELLED", 0, True, 1),  # 구조 때문에 생긴 재질문: 기록만
        ("LATE", 0, True, 1),
        ("CANCELLED", 3, False, 1),  # from_stage 3부터 반드시
        ("ANSWERED", 0, False, 0),  # 유효하게 답한 변경을 다시 보냈다
    ],
)
def test_must_no_repeat_change_request_again(seeded, previous, stage, ok, structural):
    run = add_run(seeded)
    _message(run, 1, "foreman_a2", "CHANGE_REQUEST", previous, "hash-c")
    _message(run, 2, "foreman_a2", "CHANGE_REQUEST", "OPEN", "hash-c")
    result = _no_repeat(seeded, stage)
    assert (result["ok"], len(result["structural_reasks"])) == (ok, structural)


def test_must_no_repeat_discarded_value_asked_again(seeded):
    run = add_run(seeded)
    _proposal(run, 1, "MOVABILITY", "A", {"allowed_values": ["SITE-CR-01"]}, "DISCARDED")
    assert _no_repeat(seeded)["ok"]
    _proposal(run, 2, "MOVABILITY", "A", {"allowed_values": ["SITE-CR-01"]})
    result = _no_repeat(seeded)
    assert result["violations"] == [
        {"kind": "DISCARDED_ASKED_AGAIN", "type": "MOVABILITY", "task": "A"}
    ]


def test_must_quoted_instruction_only_after_the_reply(seeded):
    spec = _scn(seeded, "S3").data["quoted_instruction"]
    run = add_run(seeded)
    _proposal(run, 1, "MOVABILITY", "A", {"allowed_values": ["SITE-CR-01"]}, "DISCARDED")
    reply = {"action": "REPLY", "kind": "MOVABILITY", "actor": "planner_a", "decision": "DECLINE"}

    def check(mark):
        log = [{**reply, "marks": {"message": 0, "proposal": mark, "candidate": 0}}]
        with db.read() as conn:
            return judge.must_quoted_instruction(conn, seeded, spec, log)

    assert check(1) == {"ok": True, "violations": [], "applies": True}  # 거절 전에 물은 것
    after = check(0)
    assert not after["ok"] and after["violations"][0]["kind"] == "RESOURCE_ASKED"
    with db.read() as conn:
        assert not judge.must_quoted_instruction(conn, seeded, spec, [])["applies"]


def test_must_budget_exhausted_fails(seeded):
    add_run(seeded, "run_ok", status="ESCALATED", end_reason="x")
    with db.read() as conn:
        assert judge.must_budget(conn)["ok"]
    add_run(seeded, "run_bx", status="BUDGET_EXHAUSTED", end_reason="STEPS")
    with db.read() as conn:
        assert judge.must_budget(conn)["violations"] == [{"agent_type": "REPLANNING"}]


def test_grade():
    ok, bad = {"a": {"ok": True}}, {"a": {"ok": True}, "b": {"ok": False}}
    assert judge.grade(ok, True, "DONE") == "PASS"
    assert judge.grade(ok, True, "END_POINT") == "PASS"
    assert judge.grade(ok, False, "DONE") == "SHORT"
    assert judge.grade(ok, True, "STALLED") == "SHORT"
    assert judge.grade(ok, True, "HARNESS_LIMIT") == "SHORT"
    assert judge.grade(bad, True, "DONE") == "FAIL"


# ── 사람 규칙 (순수 함수) ──────────────────────────────────────


def test_nth_answer_and_field_answers():
    assert [nth_answer(["a", "b"], n) for n in range(3)] == ["a", "b", None]
    fields = {"resource": ["r1", "r2"], "window": ["w1", "w2"], "zone_id": ["z"]}
    assert field_answers(fields, ["window", "resource"], {}) == "w1 r1"  # 물은 필드 순서대로
    assert field_answers(fields, ["resource", "zone_id"], {"resource": 1, "zone_id": 1}) == "r2"
    assert field_answers(fields, ["zone_id"], {"zone_id": 1}) == NEUTRAL  # 바닥나면 중립 답
    assert field_answers(fields, ["duration"], {}) == NEUTRAL
    # 정해진 답이 없는 필드는 건너뛴다. 답한 필드가 하나도 없을 때만 중립 답
    assert field_answers(fields, ["work_type", "window"], {}) == "w1"
    assert field_answers(fields, ["work_type"], {}) == NEUTRAL


def test_values_decision_truth_range_and_decline_lines(pack):
    scn = _scn(pack, "S1").data
    truth = scn["truth"]["task"]
    lines = scn["humans"]["values_check"]["planner_a"]["decline_lines"]
    assert values_decision(S1_VALUES, truth, lines) == ("ACCEPT", "")
    assert values_decision({**S1_VALUES, "earliest_start": 1440}, truth, lines)[0] == "ACCEPT"
    wrong = {**S1_VALUES, "earliest_start": 1560, "requested_resource_id": None, "zone_id": "G"}
    assert wrong_fields(wrong, truth) == ["zone_id", "resource", "window"]
    assert values_decision(wrong, truth, lines) == (
        "DECLINE",
        "F구역이에요 골리앗(SITE-GC-01)이어야 해요 13일 화요일, 10시 반~11시 시작, 12시 전 종료예요",
    )


def test_values_decision_requirements_and_demands(pack):
    """진실에 없는 값: 요구 조건은 진실 자원(SITE-GC-01, 300 t·블록)이 맞추면 받고, 수요는 서버 적용 값으로 본다."""
    scn = _scn(pack, "S1").data
    truth = scn["truth"]["task"]
    lines = scn["humans"]["values_check"]["planner_a"]["decline_lines"]

    def decide(**extra):
        return values_decision({**S1_VALUES, **extra}, truth, lines, pack)

    block = {"attribute": "usage", "op": "CONTAINS", "value": "블록"}
    heavy = {"attribute": "max_load", "op": "GTE", "value": 100}
    assert decide(resource_requirements=[block, heavy]) == ("ACCEPT", "")
    too_heavy = {"attribute": "max_load", "op": "GTE", "value": 500}
    assert decide(resource_requirements=[block, too_heavy]) == (
        "DECLINE",
        "그 조건이면 쓰려는 크레인을 못 써요",
    )
    light = {"attribute": "max_load", "op": "LTE", "value": 50}
    assert wrong_fields({**S1_VALUES, "resource_requirements": [light]}, truth, pack) == [
        "requirements"
    ]
    # 수요: 인양 기본값은 작업 인원 4·신호수 1
    assert decide(pool_demands=[{"kind": "WORKER", "quantity": 4}])[0] == "ACCEPT"
    assert decide(pool_demands=[{"kind": "WORKER", "quantity": 6}])[0] == "ACCEPT"
    # 넣은 값이 기본값보다 낮아도 서버는 기본값을 쓴다. 서버가 실제로 쓰는 값으로 보므로 받는다
    assert decide(pool_demands=[{"kind": "WORKER", "quantity": 3}]) == ("ACCEPT", "")
    # pack 없이 부르면(판정기) 진실에 있는 필드만 본다
    assert wrong_fields({**S1_VALUES, "resource_requirements": [too_heavy]}, truth) == []


def test_hidden_s1_truth_matches_pack(pack):
    """S1''의 진실 값은 Pack과 맞아야 한다(자원이 구역·요구 조건에 적격, 창이 근무시간 안)."""
    truth = _scn(pack, "S1", hidden=True).data["truth"]["task"]
    gantry = next(r for r in pack.resources if r.resource_id == truth["requested_resource_id"])
    assert truth["zone_id"] in gantry.allowed_zone_ids
    assert truth["required_resource_type"] == gantry.resource_type
    lo, hi = truth["earliest_start"]
    assert 2880 <= lo <= hi <= truth["latest_start"] <= truth["latest_end"] - truth["duration"]


def test_rules_first_match_status_axes_and_from_stage(pack):
    rules = _scn(pack, "S3").data["humans"]["rules"]
    draft = {"kind": "CONSTRAINT_DRAFT", "to": "foreman_a2", "task": "C", "status": "OPEN"}
    assert find_rule(rules, {**draft, "axes": ["TIME"]}, 0)["do"] == {"decision": "ACCEPT"}
    wide = find_rule(rules, {**draft, "axes": ["RESOURCE", "TIME"]}, 0)
    assert wide["do"] == {"decision": "DECLINE", "comment": "시간 얘기였어요"}
    assert find_rule(rules, {**draft, "to": "planner_b", "axes": ["TIME"]}, 0) is None

    s2 = _scn(pack, "S2").data["humans"]["rules"]
    request = {"kind": "CHANGE_REQUEST", "to": "foreman_a2", "task": "C"}
    assert matches(s2[0]["when"], {**request, "status": "CANCELLED"})
    assert (
        find_rule(s2, {**request, "status": "OPEN"}, 0) is None
    )  # from_stage 2 전에는 규칙이 없다
    assert find_rule(s2, {**request, "status": "OPEN"}, 2)["do"] == {"decision": "ACCEPT"}


# ── 로더·비교 ──────────────────────────────────────────────────


def test_loader_rejects_unknown_pack_ids(pack, tmp_path):
    text = scenario_path(pack.name, "S3", False).read_text(encoding="utf-8")
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        text.replace("to: planner_a, task: A", "to: nobody, task: Z9").replace(
            "SITE-CR-01", "NO-CRANE"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ScenarioError) as e:
        load(bad, pack)
    assert sorted(r.split(": ", 1)[1] for r in e.value.reasons) == [
        "unknown actor id in pack shipyard: nobody",
        "unknown resource id in pack shipyard: NO-CRANE",
        "unknown task id in pack shipyard: Z9",
    ]


HIDDEN_MARK = {"S1": "''", "S2": "'", "S3": "'"}


def test_hidden_scenarios_load_with_warning_header(pack):
    for name in ("S1", "S2", "S3"):
        path = scenario_path(pack.name, name, True)
        assert "지침을 고치는 작업 중에는 열지 않는다" in path.read_text(encoding="utf-8")
        scn = load(path, pack, True)
        # S1은 S1''로 바꿨다(S1'은 되돌림 판단 때 열어 봤다, EV-03)
        assert scn.hidden and scn.scenario_id == f"{name}{HIDDEN_MARK[name]}"


def test_compare_refuses_different_conditions():
    a = {
        "harness_version": "1",
        "model_settings": {"model": "m"},
        "model_ids": ["m-1"],
        "pack_hash": "p",
        "agent_flags": {},
        "scenario_hashes": {"S1": "h1", "S2": "h2"},
        "site_now": {"S1": "t", "S2": "t"},
    }
    assert compare.differences(a, copy.deepcopy(a)) == []
    b = {**copy.deepcopy(a), "model_ids": ["m-2"], "scenario_hashes": {"S1": "other"}}
    diffs = compare.differences(a, b)
    assert (
        len(diffs) == 2 and diffs[0].startswith("model_ids") and "scenario_hashes[S1]" in diffs[1]
    )
