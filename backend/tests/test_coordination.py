"""Coordination Agent — 기본안 A 최소 경로.

스크립트 모델(Router)로 Replanning·Coordination Run을 함께 돌린다. Coordination은 메인이 부른다:
사건 → 메인 자동 시작을 켠 테스트(main_on)에서만 시작한다.
"""

import uuid
from types import SimpleNamespace

from conftest import LEGACY_PINNED, add_task, choose, make_task
from langchain_core.messages import AIMessage
from scripted import Router, ask_owner, blocked, call, done, solve, wait_answers

from app.agents.observers.coordination import _items
from app.agents.prompts import coordination as prompt
from app.api.state import build_state, candidate_view
from app.commands.approval import (
    ApproveRequest,
    RejectRequest,
    WaiveRequest,
    approve_and_commit,
    reject_candidate,
    waive,
)
from app.commands.events import EventReport, HoldRelease, receive_event, release_hold_command
from app.commands.messages import ReplyRequest, reply_message
from app.commands.pins import TaskRef, pin_task
from app.commands.task_request import TaskRequestForm, submit_task_request
from app.coordinator.dispatcher import run_until_idle
from app.domain.consultation import change_hash, item_statuses
from app.domain.models import Assignment, ConsultationItem
from app.store import db
from app.store.repos.consultations import consultation_view
from app.store.repos.dispatch import list_jobs, register_job
from app.store.repos.records import list_validations
from app.store.repos.runs import get_run, list_steps
from app.store.repos.site import bump_context_version, get_site
from app.store.repos.tasks import insert_task_revision, list_current_tasks

OBJECTION = "작업발판 연계 공정 확정"


def _key():
    return uuid.uuid4().hex


def _site(pack):
    with db.read() as conn:
        return get_site(conn, pack.site_id)


def _runs(agent_type=None):
    with db.read() as conn:
        ids = [r[0] for r in conn.execute("SELECT run_id FROM agent_run ORDER BY rowid")]
        runs = [get_run(conn, rid) for rid in ids]
    return [r for r in runs if agent_type is None or r.agent_type == agent_type]


def _steps(run_id):
    with db.read() as conn:
        return list_steps(conn, run_id)


def _messages(type_=None):
    with db.read() as conn:
        cur = conn.execute("SELECT * FROM message ORDER BY rowid")
        cols = [d[0] for d in cur.description]
        found = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    return [m for m in found if type_ is None or m["type"] == type_]


def _proposals(type_):
    with db.read() as conn:
        cur = conn.execute("SELECT * FROM proposal WHERE type = ? ORDER BY rowid", (type_,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def _view(pack, cand_id):
    with db.read() as conn:
        return consultation_view(conn, pack.site_id, cand_id)


def _validation_id(pack, cand_id):
    with db.read() as conn:
        [v] = list_validations(conn, pack.site_id, cand_id)
    return v.validation_id


def _submit_a(pack):
    a = pack.new_task.model_dump(exclude={"requested", "unit_id", "owner_actor_id", "movable"})
    out = submit_task_request(pack, "planner_a", _key(), TaskRequestForm(**a))
    assert out.status == "APPLIED"


def _reply(pack, actor, message_id, decision="ACCEPT", comment=""):
    body = ReplyRequest(message_id=message_id, decision=decision, comment=comment)
    return reply_message(pack, actor, _key(), body)


def _approve(pack, cand_id):
    body = ApproveRequest(
        candidate_id=cand_id,
        validation_id=_validation_id(pack, cand_id),
        expected_context_version=_site(pack).context_version,
    )
    return approve_and_commit(pack, "supervisor", _key(), body)


def _request_c():
    return call(
        "SEND_CHANGE_REQUEST",
        "이유: C가 동의 대기다/다음: 답을 기다린다",
        task_id="C",
        message="A 인양을 위해 C를 30분 늦추는 안입니다.",
    )


def _report(text, summary="이유: 정리해 보고한다/다음: 종료"):
    """RETURN_RESULT(DONE). 인자 이름이 summary라 call()을 쓰지 않는다."""
    args = {"decision_summary": summary, "skill": "WRAP_UP", "status": "DONE", "summary": text}
    return AIMessage(
        content="",
        tool_calls=[{"name": "RETURN_RESULT", "args": args, "id": uuid.uuid4().hex}],
    )


def _wait():
    return call("WAIT_FOR_REPLIES", "이유: 요청을 보냈다/다음: 답을 본다")


def _alpha_consulting(pack):
    """폼 A → 메인 → Replanning(L0·L1 → Alpha → 검증 → DONE) → 메인이 협의를 부른다 → Coordination이
    A2에게 변경 요청 → 답 대기. (Replanning Run과 Alpha 후보, 협의 Run)."""
    _submit_a(pack)
    router = Router(replanning=[solve("L0"), solve("L1")], coordination=[_request_c(), _wait()])
    run_until_idle(pack, model_factory=router.factory())
    assert _runs("COORDINATION") == []  # 고르기 전에는 협의가 나가지 않는다 (AG-28)
    choose(pack)
    run_until_idle(pack, model_factory=router.factory())
    assert router.left() == {"REPLANNING": 0, "COORDINATION": 0, "EVENT_RESPONSE": 0, "INTAKE": 0}
    [rp] = _runs("REPLANNING")
    [coord] = _runs("COORDINATION")
    alpha = coord.input_ref["candidate_id"]
    return SimpleNamespace(run_id=rp.run_id, case_id=rp.case_id, wait_ref=alpha), coord


def _candidate_of(run_id):
    ids = [(s["tool_result"] or {}).get("candidate_id") for s in _steps(run_id)]
    return [c for c in ids if c][-1]


def _objected(pack, comment=OBJECTION):
    """A2가 변경 요청에 이견(DECLINE + 사유)으로 답한다."""
    [cr] = _messages("CHANGE_REQUEST")
    out = _reply(pack, "foreman_a2", cr["message_id"], "DECLINE", comment)
    assert out.status == "APPLIED"
    return cr


# ── 기본안 A 전체 ──────────────────────────────────────────────


def test_plan_a_full_e2e(seeded, main_on):
    """Alpha → 변경 요청 → 이견 → A2가 C를 고정 → 협의가 결과를 돌려줌 → 메인이 재계획을 다시 부름 →
    Beta → R1 → 통지 2건 → 메인 CLOSE."""
    pack = seeded
    rp, coord = _alpha_consulting(pack)
    alpha = rp.wait_ref
    [main] = _runs("MAIN")
    assert (main.wait_kind, main.wait_ref, coord.parent_run_id) == (
        "CHILD_RUN",
        coord.run_id,
        main.run_id,
    )
    assert (coord.status, coord.wait_kind, coord.wait_ref) == (
        "WAITING_HUMAN",
        "CONSULTATION",
        alpha,
    )
    assert (coord.case_id, coord.input_ref["phase"]) == (rp.case_id, "CONSULT")
    [cr] = _messages("CHANGE_REQUEST")
    assert (cr["to_actor_id"], cr["status"], cr["candidate_id"]) == ("foreman_a2", "OPEN", alpha)
    assert cr["body"].startswith(
        "재계획 후보가 C(인양) 작업을 바꿉니다: 시작 10/12 10:00 → 10/12 10:30."
    )
    view = _view(pack, alpha)
    assert view.item_status == {"A": "COVERED", "C": "PENDING"}
    assert cr["change_hash"] == next(i.change_hash for i in view.items if i.task_id == "C")

    _objected(pack)
    assert _view(pack, alpha).item_status["C"] == "OBJECTED"
    assert _runs("COORDINATION")[0].wake_seq == 1

    view = _view(pack, alpha)
    assert (view.item_status["C"], view.items_status) == ("OBJECTED", "BLOCKED")
    assert _approve(pack, alpha).reason_codes == ("CONSULTATION_INCOMPLETE",)
    no_waive = waive(
        pack, "supervisor", _key(), WaiveRequest(candidate_id=alpha, task_ids=("C",), comment="x")
    )
    assert no_waive.reason_codes == ("ITEM_NOT_WAIVABLE",)  # OBJECTED는 수용 불가

    # 담당자 A2가 타임라인에서 C를 고정한다: context +1, Alpha는 STALE, 협의 Run이 깨어난다 (AG-27)
    ctx = _site(pack).context_version
    out = pin_task(pack, "foreman_a2", _key(), TaskRef(task_id="C"))
    assert out.status == "APPLIED"
    assert _site(pack).context_version == ctx + 1
    assert _runs("COORDINATION")[0].wake_seq == 2
    with db.read() as conn:
        events = [r[0] for r in conn.execute("SELECT kind FROM case_event ORDER BY seq")]
    assert events[-1] == "TASK_PINNED"  # 열린 메인의 Case에 사건으로 들어간다

    # 협의는 이견을 결과에 담아 돌려주고(제약을 만들지 않는다), 메인이 깨어나 재계획을 다시 부른다.
    # 이후: C 고정 관찰 → LIST → 막힘 → 사전 확인(Coordination) → 수락 → TRY → Beta
    listing = call("LIST_ASSIGNABLE_RESOURCES", "A 자원 조회", task_id="A")
    run_until_idle(pack, model_factory=Router(replanning=[listing, blocked()]).factory())
    ended = _runs("COORDINATION")[0]
    assert (ended.status, ended.end_reason) == ("SUCCEEDED", "RETURN_DONE")
    s_back = _steps(coord.run_id)[-1]
    obs_c = next(i for i in s_back["observation"]["items"] if i["task_id"] == "C")
    assert obs_c["requests"][0]["quoted_comment"] == OBJECTION
    assert s_back["observation"]["candidate"]["live"] is False
    [question] = _messages("QUESTION")
    # 담당자 질문은 Coordination 사전 확인 Run에서만 나온다 (AG-09)
    asking = _runs("COORDINATION")[-1]
    assert (question["run_id"], asking.input_ref["phase"]) == (asking.run_id, "ASK")
    assert _reply(pack, "planner_a", question["message_id"], "ACCEPT").status == "APPLIED"
    try_beta = call(
        "TRY_ALTERNATIVE_RESOURCE", "SITE-CR-01 시도", task_id="A", resource_id="SITE-CR-01"
    )
    run_until_idle(pack, model_factory=Router(replanning=[try_beta]).factory())
    first, second, third = _runs("REPLANNING")
    assert (third.case_id, second.status, third.status) == (first.case_id, "BLOCKED", "SUCCEEDED")
    beta = _candidate_of(third.run_id)
    steps = _steps(second.run_id)
    names = [[(s["action"] or {}).get("name") for s in _steps(r.run_id)] for r in (second, third)]
    assert names == [
        ["LIST_ASSIGNABLE_RESOURCES", "RETURN_RESULT"],
        ["TRY_ALTERNATIVE_RESOURCE", "RETURN_RESULT"],
    ]
    assert (third.steps_used, third.solver_calls_used, third.human_rounds_used) == (2, 1, 0)
    assert steps[0]["observation"]["rejections"] == []  # 고정은 거절이 아니라 사람이 직접 걸었다
    acting = {t["task_id"]: t for t in steps[0]["observation"]["acting_tasks"]}
    assert acting["C"]["pinned"] == {"pinned_by": "foreman_a2", "by_role": "OWNER"}
    assert acting["A"]["pinned"] is None
    assert _view(pack, beta).items_status == "COMPLETE"
    # Beta에는 동의 대기가 없어 협의를 부르지 않는다: 협의(Alpha) + 사전 확인
    assert [r.input_ref["phase"] for r in _runs("COORDINATION")] == ["CONSULT", "ASK"]

    assert _approve(pack, beta).status == "APPLIED"

    notices = [
        call(
            "SEND_NOTICE",
            "이유: A가 바뀌었다/다음: B 담당자",
            actor_id="planner_a",
            task_ids=["A"],
            message="A는 SITE-CR-01로 10:00에 시작합니다.",
        ),
        call(
            "SEND_NOTICE",
            "이유: B가 A와 안전 규칙으로 엮였다/다음: 보고",
            actor_id="planner_b",
            task_ids=["B"],
            message="B는 A 인양 전에 마쳐 주세요.",
        ),
        _report("통지 2건 완료"),
    ]
    run_until_idle(pack, model_factory=Router(coordination=notices).factory())
    notice_run = _runs("COORDINATION")[-1]
    assert (notice_run.status, notice_run.end_reason, notice_run.input_ref["phase"]) == (
        "SUCCEEDED",
        "RETURN_DONE",
        "NOTICE",
    )
    first = _steps(notice_run.run_id)[0]["observation"]["notice_targets"]
    assert [(t["actor_id"], t["task_ids"], [r["kind"] for r in t["reasons"]]) for t in first] == [
        ("planner_a", ["A"], ["CHANGED"]),
        ("planner_b", ["B"], ["SAFETY_LINK"]),
    ]
    assert first[1]["reasons"][0]["rule_id"] == "SEP-LIFT-BELOW"
    sent = _messages("NOTICE")
    assert [(m["to_actor_id"], m["status"]) for m in sent] == [
        ("planner_a", "OPEN"),  # 통지는 Run이 끝나도 정리하지 않는다
        ("planner_b", "OPEN"),
    ]
    assert "안전 규칙 '인양–하부 작업 분리'" in sent[1]["body"]
    # 통지가 끝나 이 Case의 열린 일이 없다. 메인이 끝낸다
    [main] = _runs("MAIN")
    assert (main.status, main.end_reason) == ("SUCCEEDED", "CLOSE")
    calls = [
        (s["action"]["args"]["agent"], s["action"]["args"]["phase"])
        for s in _steps(main.run_id)
        if s["action"]["name"] == "CALL_AGENT"
    ]
    assert calls == [
        ("REPLANNING", None),
        ("COORDINATION", "CONSULT"),
        ("REPLANNING", None),
        ("COORDINATION", "ASK"),
        ("REPLANNING", None),
        ("COORDINATION", "NOTICE"),
    ]


# ── 설정을 켠 채 기본안 B (Supervisor 구조화 거절) ──────────────


def test_supervisor_reject_during_consultation_runs_plan_b(seeded, main_on):
    """협의 중 Supervisor가 Alpha를 거절하고 C를 고정하면 협의 Run은 STALE, 이후 기본안 B로 끝까지 간다."""
    pack = seeded
    rp, _ = _alpha_consulting(pack)
    alpha = rp.wait_ref
    x = pack.demo_rejections[0]
    body = RejectRequest(
        candidate_id=alpha,
        validation_id=_validation_id(pack, alpha),
        reason_code=x.reason_code,
        target_task_ids=x.target_task_ids,
        comment=x.comment,
    )
    assert reject_candidate(pack, "supervisor", _key(), body).status == "APPLIED"
    assert pin_task(pack, "supervisor", _key(), TaskRef(task_id="C")).status == "APPLIED"
    [ended] = _runs("COORDINATION")
    assert (ended.status, ended.end_reason) == ("STALE", f"REJECTED:{alpha}")
    [cr] = _messages("CHANGE_REQUEST")
    assert cr["status"] == "CANCELLED"
    late = _reply(pack, "foreman_a2", cr["message_id"], "DECLINE", OBJECTION)
    assert late.status == "APPLIED" and late.result_refs["late"] is True

    listing = call("LIST_ASSIGNABLE_RESOURCES", "A 자원 조회", task_id="A")
    run_until_idle(pack, model_factory=Router(replanning=[listing, blocked()]).factory())
    [question] = _messages("QUESTION")
    assert _reply(pack, "planner_a", question["message_id"]).status == "APPLIED"
    try_beta = call(
        "TRY_ALTERNATIVE_RESOURCE", "SITE-CR-01 시도", task_id="A", resource_id="SITE-CR-01"
    )
    run_until_idle(pack, model_factory=Router(replanning=[try_beta]).factory())
    second = _runs("REPLANNING")[-1]
    assert (second.steps_used, second.solver_calls_used, second.human_rounds_used) == (2, 1, 0)
    assert _approve(pack, _candidate_of(second.run_id)).status == "APPLIED"
    assert _site(pack).plan_revision == 1
    run_until_idle(pack, model_factory=Router().factory())  # 통지 → 메인 CLOSE
    assert _runs("MAIN")[0].status == "SUCCEEDED"


# ── 이견은 결과에 담아 돌려준다 ───────────────────────────────


def test_objection_is_returned_in_result(seeded, main_on):
    """이견은 결과에 담겨 돌아간다. Agent에게는 고정 도구가 없고 고정은 생기지 않는다 (AG-27)."""
    pack = seeded
    rp, coord = _alpha_consulting(pack)
    _objected(pack, "오전에는 다른 일이 겹쳐 가능하면 원래 시간이 좋겠습니다")
    report = _report(
        "A2는 C 이동에 선호상 이견. 작업 고정 요구는 아님.",
        "이유: 고정 요구가 아닌 선호다/다음: Supervisor 판단",
    )
    run_until_idle(pack, model_factory=Router(coordination=[report]).factory())
    [done] = _runs("COORDINATION")
    assert (done.status, done.end_reason) == ("SUCCEEDED", "RETURN_DONE")
    s = _steps(coord.run_id)[-1]
    assert [t["function"]["name"] for t in s["available_actions"]] == ["RETURN_RESULT"]
    assert s["action"]["name"] == "RETURN_RESULT"
    # 모델은 요약만 쓰고 협의 상태는 서버가 채운다
    result = s["tool_result"]
    assert (result["status"], result["phase"], result["open_items"]) == ("DONE", "CONSULT", ["C"])
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM task_pin").fetchone()[0] == len(LEGACY_PINNED)
    view = _view(pack, rp.wait_ref)
    assert (view.item_status["C"], view.items_status) == ("OBJECTED", "BLOCKED")
    # 메인은 Supervisor의 결정을 기다린다(거절하면 다시 재계획을 부를 수 있다)
    [main] = _runs("MAIN")
    assert (main.status, main.wait_kind) == ("WAITING_HUMAN", "HUMAN_DECISION")


# ── 담당자 답은 변경(change_hash)에 묶인다 (ST-15, CV-13) ──


def _report_event(pack, source="ev1"):
    out = receive_event(
        pack,
        "reporter",
        _key(),
        EventReport(source_event_id=source, event_type="OTHER", text="확인 필요"),
    )
    assert out.status == "APPLIED"
    return out.result_refs["hold_id"]


def _release_no_change(pack, hold_id):
    """변경 없음으로 Hold를 푼다. 해제 사건은 열려 있는 같은 메인이 받는다."""
    body = HoldRelease(
        hold_id=hold_id,
        resolution="NO_CHANGE",
        expected_context_version=_site(pack).context_version,
    )
    assert release_hold_command(pack, "supervisor", _key(), body).status == "APPLIED"


def _replan_after_release(pack, coordination=()):
    """해제 뒤 같은 메인이 재계획을 다시 부른다(같은 Case의 새 Run). 신고로 무효가 된 후보의 탐색(L1)은
    미시도로 돌아와 다시 계산되고, 후보가 없던 탐색(L0)은 해 본 탐색으로 남는다 (CV-13). 새 후보 ID."""
    router = Router(replanning=[solve("L1")], coordination=list(coordination))
    run_until_idle(pack, model_factory=router.factory())
    assert router.left()["REPLANNING"] == 0
    if coordination:
        choose(pack)  # 새 후보를 골라야 협의가 나간다 (AG-28)
        run_until_idle(pack, model_factory=router.factory())
    [main] = _runs("MAIN")
    first, second = _runs("REPLANNING")
    assert second.case_id == first.case_id == main.case_id
    steps = _steps(second.run_id)
    untried = steps[0]["observation"]["untried_levels"]
    assert "L1" in untried and "L0" not in untried
    assert [(s["result_kind"], s["guard"]["reason_code"]) for s in steps] == [
        ("WAIT", None),
        ("DONE", None),
    ]
    return _candidate_of(second.run_id)


def test_accept_carries_to_same_change_in_new_candidate(seeded, main_on):
    """수락한 변경이 새 후보에 그대로 있으면 처음부터 ACCEPTED다. 다시 묻지 않는다."""
    pack = seeded
    rp, coord = _alpha_consulting(pack)
    [cr] = _messages("CHANGE_REQUEST")
    assert _reply(pack, "foreman_a2", cr["message_id"]).status == "APPLIED"
    run_until_idle(pack, model_factory=Router(coordination=[_report("C 수락")]).factory())
    with db.read() as conn:
        [own] = [i for i in _items(conn, pack, coord.run_id, rp.wait_ref) if i["task_id"] == "C"]
    assert (own["status"], own["prior_answer"]) == ("ACCEPTED", False)

    _release_no_change(pack, _report_event(pack))
    # Coordination 응답을 주지 않는다. 협의 Run이 뜨면 스크립트 소진으로 ERROR가 된다
    beta = _replan_after_release(pack)
    assert beta != rp.wait_ref
    view = _view(pack, beta)
    assert (view.item_status, view.status) == ({"A": "COVERED", "C": "ACCEPTED"}, "COMPLETE")
    assert len(_messages("CHANGE_REQUEST")) == 1 and len(_runs("COORDINATION")) == 1
    with db.read() as conn:
        shown = candidate_view(conn, pack.site_id, beta)
        [obs_c] = [i for i in _items(conn, pack, coord.run_id, beta) if i["task_id"] == "C"]
    sources = {i["task_id"]: i["answer_source"] for i in shown["consultation"]["items"]}
    src = sources["C"]
    assert (src["message_id"], src["candidate_id"], src["actor_id"], src["prior"]) == (
        cr["message_id"],
        rp.wait_ref,
        "foreman_a2",
        True,
    )
    assert src["at"] and sources["A"] is None
    assert (obs_c["prior_answer"], obs_c["requests"]) == (True, [])
    assert _approve(pack, beta).status == "APPLIED"


def test_objection_carries_and_candidate_waits_for_supervisor(seeded, main_on):
    """이견 낸 변경이 새 후보에 있으면 처음부터 OBJECTED다. 다시 나가지 않고 Supervisor를 기다린다."""
    pack = seeded
    _alpha_consulting(pack)
    _objected(pack, "이번 주는 좀 어렵네요")
    run_until_idle(pack, model_factory=Router(coordination=[_report("C 이견")]).factory())

    _release_no_change(pack, _report_event(pack))
    beta = _replan_after_release(pack)
    view = _view(pack, beta)
    assert (view.item_status["C"], view.status) == ("OBJECTED", "BLOCKED")
    assert view.answer_from["C"]["prior"] is True
    # 새 협의 Run은 다시 묻지 않고 끝난다. 메인은 Supervisor의 결정을 기다린다
    assert len(_messages("CHANGE_REQUEST")) == 1
    waiting = _runs("MAIN")[-1]
    assert (waiting.status, waiting.wait_kind) == ("WAITING_HUMAN", "HUMAN_DECISION")
    out = _approve(pack, beta)
    assert (out.status, out.reason_codes) == ("REJECTED", ("CONSULTATION_INCOMPLETE",))


def test_cancelled_and_late_answers_do_not_carry(seeded, main_on):
    """신고로 취소된 요청과 Hold 중 늦은 답은 같은 변경이 다시 나와도 세지 않는다. 다시 묻는다."""
    pack = seeded
    _alpha_consulting(pack)
    [cr] = _messages("CHANGE_REQUEST")
    hold_id = _report_event(pack)
    assert _messages("CHANGE_REQUEST")[0]["status"] == "CANCELLED"
    assert _view(pack, cr["candidate_id"]).answer_from == {}
    late = _reply(pack, "foreman_a2", cr["message_id"])
    assert late.status == "APPLIED" and late.result_refs["late"] is True
    _release_no_change(pack, hold_id)

    beta = _replan_after_release(pack, [_request_c(), _wait()])
    old, new = _messages("CHANGE_REQUEST")
    assert (old["status"], new["status"], new["candidate_id"]) == ("LATE", "OPEN", beta)
    assert old["change_hash"] == new["change_hash"]
    view = _view(pack, beta)
    assert view.item_status["C"] == "PENDING" and view.answer_from == {}


def test_answer_does_not_carry_when_task_revision_changes(seeded, main_on):
    """작업 revision이 바뀌면 다른 변경이다. 수락이 넘어오지 않는다."""
    pack = seeded
    _alpha_consulting(pack)
    [cr] = _messages("CHANGE_REQUEST")
    assert _reply(pack, "foreman_a2", cr["message_id"]).status == "APPLIED"
    run_until_idle(pack, model_factory=Router(coordination=[_report("C 수락")]).factory())
    with db.write() as tx:
        c = next(t for t in list_current_tasks(tx, pack.site_id, pack) if t.task_id == "C")
        insert_task_revision(tx, pack.site_id, c.model_copy(update={"revision": c.revision + 1}))
        bump_context_version(tx, pack.site_id)
    _release_no_change(pack, _report_event(pack))

    beta = _replan_after_release(pack, [_request_c(), _wait()])
    old, new = _messages("CHANGE_REQUEST")
    assert old["change_hash"] != new["change_hash"]
    assert _view(pack, beta).item_status["C"] == "PENDING"


def test_change_hash_covers_revision_and_both_assignments():
    before = Assignment(task_id="C", start=60, end=90, resource_id="A-CR-01")
    after = Assignment(task_id="C", start=90, end=120, resource_id="A-CR-01")
    moved = Assignment(task_id="C", start=75, end=105, resource_id="A-CR-01")
    base = change_hash("C", 1, before, after)
    assert change_hash("C", 1, before, after) == base
    assert len({base, change_hash("C", 2, before, after), change_hash("C", 1, moved, after)}) == 3


# ── 서버 검사 ──────────────────────────────────────────────────


def test_change_request_objection_needs_comment(seeded, main_on):
    pack = seeded
    _alpha_consulting(pack)
    [cr] = _messages("CHANGE_REQUEST")
    out = _reply(pack, "foreman_a2", cr["message_id"], "DECLINE", "  ")
    assert (out.status, out.reason_codes) == ("REJECTED", ("COMMENT_REQUIRED",))


def test_main_auto_start_off_starts_nothing(seeded):
    """설정이 꺼져 있으면(테스트 기준값) 사건은 기록만 되고 메인도 전문 Agent도 뜨지 않는다."""
    _submit_a(seeded)
    run_until_idle(seeded, model_factory=Router().factory())
    assert _runs() == []
    with db.read() as conn:
        kinds = [j["kind"] for j in list_jobs(conn, seeded.site_id)]
        events = [r[0] for r in conn.execute("SELECT kind FROM case_event")]
    assert kinds == ["RECHECK"] and events == ["TASK_READY"]


# ── 단위 ───────────────────────────────────────────────────────


def _item(task_id, h):
    a = Assignment(task_id=task_id, start=0, end=30, resource_id=None)
    return ConsultationItem(
        task_id=task_id,
        task_revision=1,
        owner_actor_id="x",
        before=a,
        after=a,
        change_hash=h,
        base_status="PENDING",
    )


def test_item_statuses_with_answers():
    items = [_item("A", "ha"), _item("C", "hc"), _item("E", "he")]
    answers = {"hc": "OBJECTED", "he": "ACCEPTED", "ha": "OBJECTED"}
    assert item_statuses(items, ["A"], answers) == {
        "A": "WAIVED",  # WAIVE가 먼저
        "C": "OBJECTED",
        "E": "ACCEPTED",
    }
    assert item_statuses(items, [], None) == {"A": "PENDING", "C": "PENDING", "E": "PENDING"}


def test_coordination_prompt_fingerprint_and_keys(seeded, main_on):
    assert prompt.fingerprint() == prompt.PROMPT_FINGERPRINTS[prompt.PROMPT_VERSION]
    _, coord = _alpha_consulting(seeded)
    obs = _steps(coord.run_id)[0]["observation"]
    assert tuple(sorted(obs)) == prompt.OBSERVATION_KEYS
    system = prompt.render_system(seeded)
    assert seeded.site_description in system and "첫날 09:00" in system
    assert "제약" not in system  # Agent는 고정·제약을 만들지 않는다 (AG-27)


def test_state_shows_item_request_and_inbox_types(seeded, main_on):
    """state: 검토 패널 항목의 변경 요청·인용된 이견, Inbox의 후보."""

    pack = seeded
    rp, _ = _alpha_consulting(pack)
    _objected(pack)
    with db.read() as conn:
        a2 = build_state(conn, pack, "foreman_a2")
    cand = next(c for c in a2["candidates"] if c["candidate_id"] == rp.wait_ref)
    item = next(i for i in cand["consultation"]["items"] if i["task_id"] == "C")
    assert item["item_status"] == "OBJECTED"
    assert (item["request"]["decision"], item["request"]["quoted_comment"]) == (
        "DECLINE",
        OBJECTION,
    )
    by_type = {m["type"]: m for m in a2["inbox"]}
    assert by_type["CHANGE_REQUEST"]["candidate_id"] == rp.wait_ref


# ── 사전 확인 단계 (phase ASK, 후보 없음) ──────────────────────


def _need(need_id, task_id, values=("SITE-CR-01",)):
    return {
        "need_id": need_id,
        "kind": "OWNER_CONSENT",
        "task_id": task_id,
        "axis": "RESOURCE",
        "values": list(values),
    }


def test_ask_phase_sorts_by_owner_and_returns_answers(with_a):
    """같은 담당자의 need 여럿을 한 호출로 처리한다: need마다 질문 하나, 담당자별로 정렬. 결과는 서버가
    채운다(수락한 값·거절·미응답). 답을 다 받기 전에는 DONE이 없고, 응답을 받을 수 없으면 BLOCKED다."""
    pack = with_a
    add_task(pack, make_task(pack, task_id="A2"))  # planner_a의 작업이 둘(A, A2)
    needs = [_need("n:s:0", "A"), _need("n:s:1", "C"), _need("n:s:2", "A2")]
    with db.write() as tx:
        payload = {
            "agent_type": "COORDINATION",
            "phase": "ASK",
            "need_ids": [n["need_id"] for n in needs],
            "needs": needs,
            "acting_unit_id": "SITE",
            "context_version": get_site(tx, pack.site_id).context_version,
        }
        register_job(tx, pack.site_id, "START_RUN", f"START_RUN:{_key()}", payload)
    run_until_idle(pack, model_factory=Router().factory())
    [run] = _runs("COORDINATION")
    steps = _steps(run.run_id)
    assert [s["action"]["name"] for s in steps] == ["ASK_OWNER"] * 3 + ["WAIT_FOR_REPLIES"]
    first = steps[0]["observation"]
    assert (first["phase"], first["open_skills"]) == ("ASK", ["PRE_CONFIRM", "WRAP_UP"])
    assert [(a["owner_actor_id"], a["task_id"], a["status"]) for a in first["asks"]] == [
        ("foreman_a2", "C", "UNASKED"),
        ("planner_a", "A", "UNASKED"),
        ("planner_a", "A2", "UNASKED"),
    ]
    sent = _messages("QUESTION")
    assert [(m["to_actor_id"], m["status"]) for m in sent] == [
        ("foreman_a2", "OPEN"),
        ("planner_a", "OPEN"),
        ("planner_a", "OPEN"),
    ]
    assert (run.status, run.wait_kind, run.wait_ref) == (
        "WAITING_HUMAN",
        "MESSAGE",
        sent[0]["message_id"],
    )
    assert [p["type"] for p in _proposals("MOVABILITY")] == ["MOVABILITY"] * 3

    # planner_a: A 수락, A2 거절. C 담당자는 답하지 않는다
    assert _reply(pack, "planner_a", sent[1]["message_id"], "ACCEPT").status == "APPLIED"
    assert _reply(pack, "planner_a", sent[2]["message_id"], "DECLINE").status == "APPLIED"
    info = {"kind": "HUMAN_INFO", "actor_id": "foreman_a2", "task_id": "C"}
    router = Router(
        coordination=[done("다 받았다"), blocked("C 담당자 미응답", [{"needs": [info]}])]
    )
    run_until_idle(pack, model_factory=router.factory())
    s_done, s_blocked = _steps(run.run_id)[-2:]
    # 답을 기다리는 확인이 남아 있으면 DONE은 유효하지 않다
    assert s_done["guard"] == {"verdict": "REJECTED", "reason_code": "ACTION_NOT_AVAILABLE"}
    assert [(a["task_id"], a["status"]) for a in s_done["observation"]["asks"]] == [
        ("C", "OPEN"),
        ("A", "ACCEPTED"),
        ("A2", "DECLINED"),
    ]
    result = s_blocked["tool_result"]
    assert (result["status"], result["paths"][0]["needs"][0]["kind"]) == ("BLOCKED", "HUMAN_INFO")
    assert [(a["task_id"], a["result"], a["accepted_values"]) for a in result["asks"]] == [
        ("C", "NO_REPLY", []),
        ("A", "ACCEPTED", ["SITE-CR-01"]),
        ("A2", "DECLINED", []),
    ]
    ended = _runs("COORDINATION")[0]
    assert (ended.status, ended.end_reason) == ("BLOCKED", "RETURN_BLOCKED")
    assert [m["status"] for m in _messages("QUESTION")] == ["CANCELLED", "ANSWERED", "ANSWERED"]


def _start_ask(pack, needs):
    """메인 없이 Coordination 사전 확인 Run을 시작시킨다(START_RUN 등록)."""
    with db.write() as tx:
        payload = {
            "agent_type": "COORDINATION",
            "phase": "ASK",
            "need_ids": [n["need_id"] for n in needs],
            "needs": needs,
            "acting_unit_id": "SITE",
            "context_version": get_site(tx, pack.site_id).context_version,
        }
        register_job(tx, pack.site_id, "START_RUN", f"START_RUN:{_key()}", payload)


def test_ask_owner_refuses_declined_value_while_other_needs_remain(with_a):
    """다른 확인이 남아 있어 사전 확인 스킬이 열려 있어도, 담당자가 거절한 값(같은 작업 revision)을 다시
    물으면 VALUE_DECLINED로 거절된다. 같은 요청은 두 번 나가지 않는다 (AG-09)."""
    pack = with_a
    add_task(pack, make_task(pack, task_id="A2"))
    # 앞 사전 확인에서 A 담당자가 SITE-CR-01을 거절했다
    _start_ask(pack, [_need("n1:s:0", "A")])
    run_until_idle(pack, model_factory=Router().factory())
    [question] = _messages("QUESTION")
    assert _reply(pack, "planner_a", question["message_id"], "DECLINE").status == "APPLIED"
    run_until_idle(pack, model_factory=Router().factory())
    assert _runs("COORDINATION")[0].status == "SUCCEEDED"

    # 다음 사전 확인(다른 Run): 같은 값의 need와 아직 묻지 않은 need(A2)를 함께 받는다
    _start_ask(pack, [_need("n2:s:0", "A"), _need("n2:s:1", "A2")])
    router = Router(coordination=[ask_owner(0), ask_owner(1), wait_answers()])
    run_until_idle(pack, model_factory=router.factory())
    run = _runs("COORDINATION")[-1]
    s_declined, s_ask, _ = _steps(run.run_id)
    asks = {a["task_id"]: a for a in s_declined["observation"]["asks"]}
    assert (asks["A"]["status"], asks["A"]["reason"], asks["A2"]["status"]) == (
        "NOT_ASKABLE",
        "VALUE_DECLINED",
        "UNASKED",
    )
    assert "PRE_CONFIRM" in s_declined["observation"]["open_skills"]  # A2가 남아 스킬은 열려 있다
    tool = next(
        t["function"]
        for t in s_declined["available_actions"]
        if t["function"]["name"] == "ASK_OWNER"
    )
    assert tool["parameters"]["properties"]["need_id"]["enum"] == ["n2:s:1"]
    assert s_declined["guard"] == {"verdict": "REJECTED", "reason_code": "VALUE_DECLINED"}
    assert (s_ask["guard"]["verdict"], s_ask["action"]["args"]["need_id"]) == ("ACCEPTED", "n2:s:1")
    # A에 나간 질문은 처음 하나뿐이다
    with db.read() as conn:
        sent = conn.execute(
            "SELECT p.target_task_id FROM message m JOIN proposal p"
            " ON p.proposal_id = m.proposal_id WHERE m.type = 'QUESTION' ORDER BY m.rowid"
        ).fetchall()
    assert [r[0] for r in sent] == ["A", "A2"]
    assert run.status == "WAITING_HUMAN"
