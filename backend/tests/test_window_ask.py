"""시간창 완화 확인 (부록 A.29). 자원 경로가 닫힌 뒤 Replanning이 작업 담당자에게 창을 넓힐지 묻는다.

기본안 B 변형: Alpha를 C 고정으로 거절 → LIST → ASK(SITE-CR-01) → 담당자 DECLINE → ASK_WINDOW_CHANGE(A,
10:00/10:30 → 10:30/11:00) → 수락 → 새 revision·TIME Consent·Context +1·wake → L0 다시 열림 → A 10:30
A-CR-01 → PASS → R1. FACT_UPDATE의 origin(EVENT·OWNER)별 확인·폐기·STALE도 여기서 고정한다.
"""

import json

from scripted import Router, call, escalate, solve
from test_event_response import _analyze, _lookup, _messages, _proposals, _r1, _report
from test_event_response import _runs as _runs_of
from test_resume import (
    _approve,
    _ask_waiting,
    _factory,
    _message,
    _names,
    _proposal,
    _reply,
    _run,
    _runs,
    _site,
    _steps,
    _submit,
    _task,
    _validation,
)

from app.agents.specs import replanning as spec
from app.api.state import build_state
from app.commands.runs import CancelRun, cancel_run
from app.coordinator.dispatcher import run_until_idle
from app.domain import fact_update
from app.store import db
from app.store.repos.consultations import consultation_view
from app.store.repos.records import get_candidate
from app.store.repos.tasks import insert_task_revision

WINDOW_TEXT = (
    "A(인양) 작업의 시간창을 넓히시겠습니까? 시작 한도 10/12(월) 10:00 → 10/12(월) 10:30, "
    "종료 한도 10/12(월) 10:30 → 10/12(월) 11:00. 넓히면 재계획이 넓힌 시간창 안에서 다시 계산합니다"
    "(지금 현장 정보로는 10/12(월) 10:30 시작 자리가 있습니다)."
)


def _ask_window(task_id="A"):
    return call(
        "ASK_WINDOW_CHANGE", "시간창 확인", task_id=task_id, question="10:30부터 해도 될까요?"
    )


def _window_waiting(pack):
    """기본안 B에서 SITE-CR-01을 거절 → 시간창 질문을 보내고 기다린다."""
    waiting = _ask_waiting(pack)
    assert _reply(pack, waiting.wait_ref, "DECLINE", comment="SITE-CR-01은 정비 중").status == (
        "APPLIED"
    )
    run_until_idle(pack, model_factory=_factory(_ask_window()))
    run = _run(waiting.run_id)
    assert (run.status, run.wait_kind) == ("WAITING_HUMAN", "MESSAGE")
    return run


def _consents(task_id, revision):
    with db.read() as conn:
        return conn.execute(
            "SELECT axis, scope, source_ref FROM consent WHERE task_id = ? AND task_revision = ?"
            " ORDER BY rowid",
            (task_id, revision),
        ).fetchall()


# ── 여는 조건 (A.29 2) ─────────────────────────────────────────


def test_window_ask_closed_while_resource_route_open(seeded):
    """기본안 B: 조회 대상·RESOURCE 질문 값이 남아 있는 동안에는 시간창 질문도 선택지 계산도 없다."""
    pack = seeded
    waiting = _ask_waiting(pack)
    for step in _steps(waiting.run_id):
        assert "ASK_WINDOW_CHANGE" not in _names(step)
        assert step["observation"]["window_options"] == []


def test_resource_decline_then_window_accept_to_r1(seeded):
    pack = seeded
    run = _window_waiting(pack)
    steps = _steps(run.run_id)
    s_win = steps[-1]
    assert _names(s_win) == ["ASK_WINDOW_CHANGE"]  # 이관은 닫혀 있다 (A.30)
    [option] = s_win["observation"]["window_options"]
    assert (option["task_id"], option["fit_start"], option["fit_start_clock"]) == (
        "A",
        90,
        "10/12(월) 10:30",
    )
    assert option["current"] == {
        "latest_start": 60,
        "latest_end": 90,
        "latest_start_clock": "10/12(월) 10:00",
        "latest_end_clock": "10/12(월) 10:30",
    }
    assert (option["proposed"]["latest_start"], option["proposed"]["latest_end"]) == (90, 120)

    message = _message(pack, run.wait_ref)
    assert (message["type"], message["to_actor_id"], message["body"]) == (
        "QUESTION",
        "planner_a",
        WINDOW_TEXT,
    )
    assert message["agent_text"] == "10:30부터 해도 될까요?"
    proposal = _proposal(pack, message["proposal_id"])
    assert (proposal["type"], proposal["confirmer_actor_id"], proposal["base_task_revision"]) == (
        "FACT_UPDATE",
        "planner_a",
        1,
    )
    assert proposal["payload"] == {
        "origin": "OWNER",
        "axis": "TIME",
        "allowed_values": ["90/120"],
        "changes": [
            {"field": "latest_start", "old_value": 60, "new_value": 90},
            {"field": "latest_end", "old_value": 90, "new_value": 120},
        ],
        "fit_start": 90,
    }
    assert run.human_rounds_used == 2

    # 담당자(요청자)가 확인한다: Hold 없이, 새 revision·window 확인 값·TIME Consent 새로·RESOURCE 복사
    ctx = _site(pack).context_version
    out = _reply(pack, run.wait_ref, comment="10:30도 괜찮습니다")
    assert out.status == "APPLIED" and out.result_refs["woke"] is True
    a = _task(pack, "A")
    assert (a.revision, a.earliest_start, a.latest_start, a.latest_end) == (2, 0, 90, 120)
    window = a.fields["window"]
    assert (window.value["latest_start"], window.value["latest_end"], window.status) == (
        90,
        120,
        "CONFIRMED",
    )
    assert window.source_ref == f"proposal:{proposal['proposal_id']}"
    assert _site(pack).context_version == ctx + 1
    consents = _consents("A", 2)
    assert [(c[0], c[2].split(":")[0]) for c in consents] == [
        ("RESOURCE", "form"),
        ("TIME", "message"),
    ]
    assert '"start_max": 90' in consents[1][1] and '"start_min": 0' in consents[1][1]
    assert _proposal(pack, message["proposal_id"])["status"] == "CONFIRMED"
    assert _run(run.run_id).status == "WAITING_HUMAN"  # 끝내지 않고 깨운다(재개 대기)

    # 창이 바뀌어 실효 탐색 키가 달라졌다 → L0·L1·L2 다시 열림 → L0로 A 10:30 A-CR-01
    run_until_idle(pack, model_factory=_factory(solve("L0")))
    done = _run(run.run_id)
    s_solve = _steps(run.run_id)[-1]
    assert s_solve["observation"]["untried_levels"] == ["L0", "L1", "L2"]
    assert [(s["action"] or {}).get("name") for s in _steps(run.run_id)] == [
        "SOLVE_WITH_SCOPE",
        "SOLVE_WITH_SCOPE",
        "LIST_ASSIGNABLE_RESOURCES",
        "ASK_TASK_OWNER",
        "ASK_WINDOW_CHANGE",
        "SOLVE_WITH_SCOPE",
    ]
    assert (done.solver_calls_used, done.human_rounds_used) == (3, 2)
    with db.read() as conn:
        cand = get_candidate(conn, pack.site_id, done.wait_ref)
        view = consultation_view(conn, pack.site_id, done.wait_ref)
    placed = {x.task_id: (x.start, x.resource_id) for x in cand.assignments}
    assert placed["A"] == (90, "A-CR-01") and placed["C"] == (60, "A-CR-01")
    assert s_solve["tool_result"]["stage1"] == {"status": "OPTIMAL", "changed": 1}
    assert s_solve["tool_result"]["stage2"]["delay"] == 90
    assert _validation(pack, done.wait_ref).status == "PASS"
    assert view.items_status == "COMPLETE"  # A: 새 TIME Consent가 10:30을 덮는다
    assert _approve(pack, done.wait_ref).status == "APPLIED"
    assert (_run(run.run_id).status, _site(pack).plan_revision) == ("SUCCEEDED", 1)


def test_state_inbox_shows_owner_window_change(seeded):
    """요청자 받은 요청: origin OWNER 카드(시작 한도·종료 한도 옛 값 → 새 값), 서버 문구 (A.29 3·S3)."""
    pack = seeded
    run = _window_waiting(pack)
    with db.read() as conn:
        state = build_state(conn, pack, "planner_a")
    [card] = [m for m in state["inbox"] if m["message_id"] == run.wait_ref]
    assert (card["type"], card["proposal_type"], card["status"], card["body"]) == (
        "QUESTION",
        "FACT_UPDATE",
        "OPEN",
        WINDOW_TEXT,
    )
    assert card["fact"] == {
        "origin": "OWNER",
        "changes": [
            {"field": "latest_start", "old_value": 60, "new_value": 90},
            {"field": "latest_end", "old_value": 90, "new_value": 120},
        ],
    }


def test_window_decline_then_escalate(seeded):
    """B-decline 재정의(A.29 6): 자원·시간창 질문을 모두 거절하면 다시 묻지 않고 이관한다(질문 2회)."""
    pack = seeded
    run = _window_waiting(pack)
    ctx = _site(pack).context_version
    out = _reply(pack, run.wait_ref, "DECLINE", comment="10:30 이후는 안 된다")
    assert out.status == "APPLIED" and out.result_refs["woke"] is True
    message = _message(pack, run.wait_ref)
    assert _proposal(pack, message["proposal_id"])["status"] == "DISCARDED"
    assert (_site(pack).context_version, _task(pack, "A").revision) == (ctx, 1)
    run_until_idle(pack, model_factory=_factory(_ask_window(), escalate("담당자가 모두 거절했다")))
    retry, last = _steps(run.run_id)[-2:]
    assert retry["guard"]["reason_code"] == "ACTION_NOT_AVAILABLE"
    assert _names(last) == ["ESCALATE_NO_SOLUTION"]
    replies = last["observation"]["human_replies"]
    assert [(h["axis"], h["allowed_values"], h["decision"]) for h in replies] == [
        ("RESOURCE", ["SITE-CR-01"], "DECLINE"),
        ("TIME", ["90/120"], "DECLINE"),
    ]
    ended = _run(run.run_id)
    assert (ended.status, ended.end_reason, ended.human_rounds_used) == (
        "ESCALATED",
        "ESCALATE_NO_SOLUTION",
        2,
    )


def test_window_choices_conditions(seeded):
    """선택지가 있어도 미시도 범위·자원 경로·열린 질문·라운드·TIME 고정·거절한 값이면 닫힌다."""
    pack = seeded
    run = _window_waiting(pack)
    obs = _steps(run.run_id)[-1]["observation"]
    assert spec.choices(obs)["WINDOW"] == {"A": "90/120"}
    closed = {
        "untried": {**obs, "untried_levels": ["L1"]},
        "rounds": {**obs, "budget_remaining": {**obs["budget_remaining"], "human_rounds": 0}},
        "open": {**obs, "human_replies": [{**obs["human_replies"][0], "status": "OPEN"}]},
        "frozen": {
            **obs,
            "constraints": [*obs["constraints"], {"task_id": "A", "frozen_axes": ["TIME"]}],
        },
        "declined": {
            **obs,
            "human_replies": [
                *obs["human_replies"],
                {"task_id": "A", "axis": "TIME", "allowed_values": ["90/120"], "status": "ANSWERED",
                 "decision": "DECLINE"},
            ],
        },
        "resource": {
            **obs,
            "assignable_resources": [
                {
                    **obs["assignable_resources"][0],
                    "assignable": [
                        *obs["assignable_resources"][0]["assignable"],
                        {"resource_id": "X-1"},
                    ],
                }
            ],
        },
    }  # fmt: skip
    for name, o in closed.items():
        assert spec.choices(o)["WINDOW"] == {}, name
        assert "ASK_WINDOW_CHANGE" not in spec.available_actions(o), name


def test_n5_asks_window_after_resource_route_then_escalates_on_decline(seeded):
    """N5 재정의(A.29 6): K의 대체 자원이 없어 자원 경로가 닫히면 N5의 시간창 질문이 열린다.
    요청자(planner_b)가 거절하면 이관한다(기대값 ESCALATE 유지)."""
    pack = seeded
    assert _submit(pack, "N5").status == "APPLIED"
    run_until_idle(
        pack,
        model_factory=_factory(
            solve("L0"),
            solve("L2"),
            call("LIST_ASSIGNABLE_RESOURCES", "K 자원 조회", task_id="K"),
            _ask_window("N5"),
        ),
    )
    [run] = _runs()
    steps = _steps(run.run_id)
    assert [s["guard"]["reason_code"] for s in steps] == [None] * 4
    assert "ASK_WINDOW_CHANGE" not in _names(steps[2])  # K 조회 전(자원 경로 열림)
    assert _names(steps[3]) == ["ASK_WINDOW_CHANGE"]
    [option] = steps[3]["observation"]["window_options"]
    assert (option["task_id"], option["fit_start"], option["proposed"]["latest_end"]) == (
        "N5",
        1560,
        1620,
    )
    message = _message(pack, _run(run.run_id).wait_ref)
    assert message["to_actor_id"] == "planner_b"
    assert _reply(pack, message["message_id"], "DECLINE", actor="planner_b").status == "APPLIED"
    run_until_idle(pack, model_factory=_factory(escalate()))
    ended = _run(run.run_id)
    assert (ended.status, ended.human_rounds_used) == ("ESCALATED", 1)


# ── FACT_UPDATE origin별 확인·폐기·STALE (A.29 3) ───────────────


def test_origin_rules_table():
    assert fact_update.origin({"field": "earliest_start"}) == "EVENT"  # origin 없는 옛 행
    event, owner = fact_update.RULES["EVENT"], fact_update.RULES["OWNER"]
    assert (event.confirmer, sorted(event.fields), event.needs_hold, event.ends_run) == (
        "SUPERVISOR",
        ["earliest_start"],
        True,
        True,
    )
    assert (owner.confirmer, sorted(owner.fields), owner.needs_hold, owner.ends_run) == (
        "OWNER",
        ["latest_end", "latest_start"],
        False,
        False,
    )
    assert (event.new_time_consent, owner.new_time_consent) == (False, True)


def test_owner_only_owner_confirms_and_stale_proposal(seeded):
    pack = seeded
    run = _window_waiting(pack)
    assert _reply(pack, run.wait_ref, actor="supervisor").reason_codes == ("NOT_AUTHORIZED",)
    a = _task(pack, "A")
    with db.write() as tx:
        insert_task_revision(tx, pack.site_id, a.model_copy(update={"revision": a.revision + 1}))
    assert _reply(pack, run.wait_ref).reason_codes == ("STALE_PROPOSAL",)


def test_owner_late_reply_after_run_cancelled(seeded):
    pack = seeded
    run = _window_waiting(pack)
    message = _message(pack, run.wait_ref)
    assert cancel_run(pack, "supervisor", "k-cancel", CancelRun(run_id=run.run_id)).status == (
        "APPLIED"
    )
    assert _proposal(pack, message["proposal_id"])["status"] == "STALE"
    out = _reply(pack, run.wait_ref)
    assert (out.status, out.result_refs["late"]) == ("APPLIED", True)
    assert _task(pack, "A").revision == 1


def _event_proposal(pack):
    _r1(pack)
    refs = _report(pack)
    replies = [_lookup(), _analyze(), call(
        "PROPOSE_FACT_UPDATE", "제안", task_id="E", new_earliest_start=60, evidence="신고 인용"
    )]  # fmt: skip
    run_until_idle(pack, model_factory=Router(event_response=replies).factory())
    [confirm] = _messages("CONFIRMATION")
    [p] = _proposals("FACT_UPDATE")
    return refs, confirm, p


def test_event_origin_confirm_supervisor_hold_no_time_consent(seeded, event_response_on):
    pack = seeded
    _, confirm, p = _event_proposal(pack)
    assert (json.loads(p["payload"])["origin"], p["confirmer_actor_id"]) == ("EVENT", "supervisor")
    assert _reply(pack, confirm["message_id"], actor="planner_b").reason_codes == (
        "NOT_AUTHORIZED",
    )
    assert _reply(pack, confirm["message_id"], actor="supervisor").status == "APPLIED"
    e = _task(pack, "E")
    assert [c[0] for c in _consents("E", e.revision) if c[0] == "TIME"] == []
    [er] = _runs_of("EVENT_RESPONSE")
    assert (er.status, er.end_reason) == ("SUCCEEDED", f"FACT_CONFIRMED:{p['proposal_id']}")


def test_event_origin_discard_wakes_and_late_after_cancel(seeded, event_response_on):
    pack = seeded
    _, confirm, _ = _event_proposal(pack)
    ctx = _site(pack).context_version
    out = _reply(pack, confirm["message_id"], "DECLINE", actor="supervisor")
    assert out.status == "APPLIED" and out.result_refs["woke"] is True
    assert (_proposals("FACT_UPDATE")[0]["status"], _site(pack).context_version) == (
        "DISCARDED",
        ctx,
    )
    # 다시 제안(75)한 뒤 Run 취소 → 제안 STALE → 늦은 확인은 LATE
    replies = [_analyze(75), call(
        "PROPOSE_FACT_UPDATE", "제안", task_id="E", new_earliest_start=75, evidence="신고 인용"
    )]  # fmt: skip
    run_until_idle(pack, model_factory=Router(event_response=replies).factory())
    [er] = _runs_of("EVENT_RESPONSE")
    assert cancel_run(pack, "supervisor", "k-cancel-er", CancelRun(run_id=er.run_id)).status == (
        "APPLIED"
    )
    second = _proposals("FACT_UPDATE")[1]
    assert second["status"] == "STALE"
    message = next(
        m for m in _messages("CONFIRMATION") if m["proposal_id"] == second["proposal_id"]
    )
    out = _reply(pack, message["message_id"], actor="supervisor")
    assert (out.status, out.result_refs["late"]) == ("APPLIED", True)
    assert _task(pack, "E").earliest_start == 45
