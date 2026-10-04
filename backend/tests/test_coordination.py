"""Coordination Agent — 기본안 A 최소 경로.

스크립트 모델(Router)로 Replanning·Coordination Run을 함께 돌린다. Coordination은 메인이 부른다:
사건 → 메인 자동 시작을 켠 테스트(main_on)에서만 시작한다.
"""

import sqlite3
import uuid
from types import SimpleNamespace

import pytest
from conftest import LEGACY_PINNED, choose, reply_request
from langchain_core.messages import AIMessage
from scripted import Router, call, solve

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
from app.commands.messages import (
    ChangeAnswer,
    ChangeReplyRequest,
    ReplyRequest,
    reply_change_request,
    reply_message,
)
from app.commands.pins import TaskRef, pin_task
from app.commands.task_request import TaskRequestForm, submit_task_request
from app.coordinator.dispatcher import run_until_idle
from app.domain.consultation import change_hash, item_statuses
from app.domain.models import Assignment, ConsultationItem
from app.store import db
from app.store.repos.consultations import case_objections, consultation_view
from app.store.repos.dispatch import list_jobs
from app.store.repos.messages import insert_message
from app.store.repos.records import get_candidate, list_validations
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
    """그 메시지가 든 변경 요청 한 통 전체에 답한다 (ST-26)."""
    group = next(m["request_group_id"] for m in _messages() if m["message_id"] == message_id)
    return reply_request(pack, actor, group, decision, comment)


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
        "이유: C가 확인 대기다/다음: 답을 기다린다",
        actor_id="foreman_a2",
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
    assert _runs("COORDINATION") == []  # 고르기 전에는 협의가 나가지 않는다 (AG-29)
    choose(pack)
    run_until_idle(pack, model_factory=router.factory())
    assert router.left() == {"REPLANNING": 0, "COORDINATION": 0, "EVENT_RESPONSE": 0, "INTAKE": 0}
    [rp] = _runs("REPLANNING")
    [coord] = _runs("COORDINATION")
    alpha = coord.input_ref["candidate_id"]
    return SimpleNamespace(run_id=rp.run_id, case_id=rp.case_id, wait_ref=alpha), coord


def _free_site_crane():
    """기준 장면은 공용 크레인을 첫날에 못 쓴다. 자원을 바꿔 푸는 흐름을 볼 때 첫날에도 쓸 수 있게 한다."""
    with db.write() as tx:
        tx.execute(
            "UPDATE resource SET available_intervals = '[[0, 3360]]'"
            " WHERE resource_id = 'SITE-CR-01'"
        )


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
    Beta(A의 자원이 바뀜) → Supervisor가 고름 → A 담당자 협의 → R1 → 통지 2건 → 메인 CLOSE."""
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
    assert view.item_status == {"C": "PENDING"}
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

    # 담당자 A2가 타임라인에서 C를 고정한다: context +1, Alpha는 STALE. 무효가 된 안의 협의 Run은
    # 서버가 바로 끝낸다 (AG-27·ST-22). 이미 한 답(이견)은 변경에 묶여 남는다 (ST-15)
    ctx = _site(pack).context_version
    out = pin_task(pack, "foreman_a2", _key(), TaskRef(task_id="C"))
    assert out.status == "APPLIED"
    assert _site(pack).context_version == ctx + 1
    ended = _runs("COORDINATION")[0]
    assert (ended.status, ended.end_reason) == ("STALE", f"CANDIDATE_INVALID:{alpha}")
    assert _messages("CHANGE_REQUEST")[0]["status"] == "ANSWERED"
    with db.read() as conn:
        events = [r[0] for r in conn.execute("SELECT kind FROM case_event ORDER BY seq")]
    # 고정은 열린 메인의 Case에 사건으로 들어가고, 끝난 협의 Run도 사건이 되어 메인을 깨운다
    assert events[-2:] == ["TASK_PINNED", "CHILD_RUN_ENDED"]

    # 메인이 깨어나 재계획을 다시 부른다(이견 사유는 Case에 쌓여 재계획 관찰에 간다, CV-26).
    # C가 고정이라 A는 시각만으로는 풀리지 않는다. 공용 크레인을 쓸 수 있으면 자원을 바꿔 푼다 (AG-34)
    _free_site_crane()
    run_until_idle(pack, model_factory=Router(replanning=[solve("L0")]).factory())
    assert [s["action"]["name"] for s in _steps(coord.run_id)] == [
        "SEND_CHANGE_REQUEST",
        "WAIT_FOR_REPLIES",
    ]  # 끝난 협의 Run을 다시 깨워 결과를 쓰게 하지 않는다 (ST-20)
    assert _messages("QUESTION") == []  # 사전 확인은 없다
    first, second = _runs("REPLANNING")
    assert (second.case_id, second.status) == (first.case_id, "SUCCEEDED")
    beta = _candidate_of(second.run_id)
    steps = _steps(second.run_id)
    assert [(s["action"] or {}).get("name") for s in steps] == ["SOLVE_WITH_SCOPE", "RETURN_RESULT"]
    assert (second.steps_used, second.solver_calls_used, second.human_rounds_used) == (2, 1, 0)
    assert steps[0]["observation"]["rejections"] == []  # 고정은 거절이 아니라 사람이 직접 걸었다
    assert [o["quoted_comment"] for o in steps[0]["observation"]["objections"]] == [OBJECTION]
    acting = {t["task_id"]: t for t in steps[0]["observation"]["tasks"]}
    assert acting["C"]["pinned"] == {"pinned_by": "foreman_a2", "by_role": "OWNER"}
    assert acting["A"]["pinned"] is None
    with db.read() as conn:
        placed = {
            a.task_id: (a.start, a.resource_id)
            for a in get_candidate(conn, pack.site_id, beta).assignments
        }
    assert (placed["A"], placed["C"]) == ((60, "SITE-CR-01"), (60, "A-CR-01"))
    # 요청 자원(A-CR-01)은 기준 자원이다: 다른 자원으로 바뀐 안은 고른 안의 협의에서 A 담당자에게 간다
    assert _view(pack, beta).item_status == {"A": "PENDING"}
    choose(pack, beta)
    run_until_idle(pack, model_factory=Router().factory())
    request = _messages("CHANGE_REQUEST")[-1]
    assert (request["to_actor_id"], request["candidate_id"]) == ("planner_a", beta)
    assert _reply(pack, "planner_a", request["message_id"]).status == "APPLIED"
    run_until_idle(pack, model_factory=Router().factory())
    assert _view(pack, beta).items_status == "COMPLETE"
    assert [r.input_ref["phase"] for r in _runs("COORDINATION")] == ["CONSULT", "CONSULT"]

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
        ("COORDINATION", "CONSULT"),
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

    # C가 고정이라 A는 자원을 바꿔 풀린다. 바뀐 자원은 A 담당자의 협의를 거친다 (AG-34)
    _free_site_crane()
    run_until_idle(pack, model_factory=Router(replanning=[solve("L0")]).factory())
    second = _runs("REPLANNING")[-1]
    assert (second.steps_used, second.solver_calls_used, second.human_rounds_used) == (2, 1, 0)
    beta = _candidate_of(second.run_id)
    choose(pack, beta)
    run_until_idle(pack, model_factory=Router().factory())
    request = _messages("CHANGE_REQUEST")[-1]
    assert _reply(pack, "planner_a", request["message_id"]).status == "APPLIED"
    run_until_idle(pack, model_factory=Router().factory())
    assert _approve(pack, beta).status == "APPLIED"
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
        choose(pack)  # 새 후보를 골라야 협의가 나간다 (AG-29)
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
    assert (view.item_status, view.status) == ({"C": "ACCEPTED"}, "COMPLETE")
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
    assert src["at"] and sorted(sources) == ["C"]
    assert (obs_c["prior_answer"], obs_c["requests"]) == (True, [])
    # 협의 항목에 모두 답이 있어도 고른 뒤에 승인한다 (AG-29)
    assert _approve(pack, beta).reason_codes == ("CANDIDATE_NOT_CHOSEN",)
    choose(pack, beta)
    run_until_idle(pack, model_factory=Router().factory())
    # 고른 안의 항목에 모두 답이 있으면 메인은 협의를 부르지 않고 승인을 기다린다
    waiting = _runs("MAIN")[-1]
    assert (waiting.status, waiting.wait_kind) == ("WAITING_HUMAN", "HUMAN_DECISION")
    assert len(_runs("COORDINATION")) == 1
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
    choose(pack, beta)
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


# ── 변경 요청 한 통과 그 답 (ST-26) ───────────────────────────


def _two_item_request(pack):
    """협의 중인 한 통에 항목을 하나 더 넣는다: 한 step에 행 둘, 변경(change_hash)은 서로 다르다.
    기준 장면의 Alpha는 항목이 C 하나라, 여러 항목의 답은 행을 직접 더해 본다."""
    _, coord = _alpha_consulting(pack)
    [cr] = _messages("CHANGE_REQUEST")
    with db.write() as tx:
        insert_message(
            tx,
            pack.site_id,
            "msg_second",
            run_id=cr["run_id"],
            step_no=cr["step_no"],
            to_actor_id=cr["to_actor_id"],
            type_="CHANGE_REQUEST",
            proposal_id=None,
            body="두 번째 항목",
            agent_text=cr["agent_text"],
            context_version=cr["created_context_version"],
            candidate_id=cr["candidate_id"],
            change_hash="hash-second",
            request_group_id=cr["request_group_id"],
        )
    return coord, cr


def _reply_items(pack, group, answers, actor="foreman_a2", key=None):
    body = ChangeReplyRequest(
        request_group_id=group,
        answers=tuple(ChangeAnswer(message_id=m, decision=d, comment=c) for m, d, c in answers),
    )
    return reply_change_request(pack, actor, key or _key(), body)


def _statuses():
    return [m["status"] for m in _messages("CHANGE_REQUEST")]


def test_request_is_one_per_owner_and_filled_by_server(seeded, main_on):
    """변경 요청은 담당자 한 명에게 한 통이다. Agent는 담당자만 고르고 항목은 서버가 채운다."""
    _, coord = _alpha_consulting(seeded)
    first = _steps(coord.run_id)[0]
    [tool] = [
        t for t in first["available_actions"] if t["function"]["name"] == "SEND_CHANGE_REQUEST"
    ]
    params = tool["function"]["parameters"]["properties"]
    assert params["actor_id"]["enum"] == ["foreman_a2"] and "task_id" not in params
    assert first["observation"]["owners"] == [
        {
            "actor_id": "foreman_a2",
            "items": [{"task_id": "C", "status": "PENDING"}],
            "unsent": ["C"],
            "waiting": False,
        }
    ]
    [cr] = _messages("CHANGE_REQUEST")
    result = first["tool_result"]
    assert (result["request_group_id"], result["to_actor_id"], result["task_ids"]) == (
        cr["request_group_id"],
        "foreman_a2",
        ["C"],
    )
    # 보낸 뒤에는 보낼 항목이 남은 담당자가 없다: 다시 보내는 도구가 열리지 않는다
    waiting = _steps(coord.run_id)[1]
    assert [t["function"]["name"] for t in waiting["available_actions"]] == [
        "WAIT_FOR_REPLIES",
        "RETURN_RESULT",
    ]
    assert waiting["observation"]["owners"][0]["waiting"] is True


def test_whole_request_is_answered_at_once(seeded, main_on):
    """한 통의 항목 전부를 항목별 수락·이견으로 한 번에 답한다: Run 깨우기 한 번, 효과 한 번."""
    pack = seeded
    coord, cr = _two_item_request(pack)
    wake = _runs("COORDINATION")[0].wake_seq
    key = _key()
    answers = [(cr["message_id"], "ACCEPT", ""), ("msg_second", "DECLINE", OBJECTION)]
    out = _reply_items(pack, cr["request_group_id"], answers, key=key)
    assert out.status == "APPLIED" and out.result_refs["declined"] == ["msg_second"]
    assert _statuses() == ["ANSWERED", "ANSWERED"]
    assert _runs("COORDINATION")[0].wake_seq == wake + 1
    assert _view(pack, cr["candidate_id"]).item_status == {"C": "ACCEPTED"}
    with db.read() as conn:
        [objection] = case_objections(conn, pack.site_id, coord.case_id)
    assert objection["quoted_comment"] == OBJECTION
    # 같은 명령을 다시 보내도 효과는 한 번이다. 다른 결정으로는 바꾸지 못한다
    assert _reply_items(pack, cr["request_group_id"], answers, key=key).status == "REPLAYED"
    assert _reply_items(pack, cr["request_group_id"], answers).status == "REPLAYED"
    assert _runs("COORDINATION")[0].wake_seq == wake + 1
    flipped = [(cr["message_id"], "DECLINE", "어렵다"), ("msg_second", "DECLINE", OBJECTION)]
    out = _reply_items(pack, cr["request_group_id"], flipped)
    assert out.reason_codes == ("ALREADY_ANSWERED",)


def test_request_reply_must_cover_every_item(seeded, main_on):
    """부분 답은 없다: 빠진 항목이나 그 통에 없는 항목이 있으면 명령 전체를 거절한다."""
    pack = seeded
    _, cr = _two_item_request(pack)
    group = cr["request_group_id"]
    only_one = [(cr["message_id"], "ACCEPT", "")]
    assert _reply_items(pack, group, only_one).reason_codes == ("REPLY_INCOMPLETE",)
    other = [*only_one, ("msg_second", "ACCEPT", ""), ("msg_else", "ACCEPT", "")]
    assert _reply_items(pack, group, other).reason_codes == ("ITEM_NOT_FOUND",)
    twice = [*only_one, *only_one]
    assert _reply_items(pack, group, twice).reason_codes == ("ITEM_NOT_FOUND", "REPLY_INCOMPLETE")
    both = [*only_one, ("msg_second", "ACCEPT", "")]
    assert _reply_items(pack, group, both, actor="planner_a").reason_codes == ("NOT_AUTHORIZED",)
    assert _reply_items(pack, "req_none", both).reason_codes == ("REQUEST_NOT_FOUND",)
    assert _statuses() == ["OPEN", "OPEN"]


def test_every_objection_in_a_request_needs_a_reason(seeded, main_on):
    """이견인 항목마다 사유가 필요하다. 하나라도 비면 명령 전체가 거절되고 수락한 항목도 남지 않는다."""
    pack = seeded
    _, cr = _two_item_request(pack)
    answers = [(cr["message_id"], "ACCEPT", ""), ("msg_second", "DECLINE", "  ")]
    out = _reply_items(pack, cr["request_group_id"], answers)
    assert (out.status, out.reason_codes) == ("REJECTED", ("COMMENT_REQUIRED",))
    assert _statuses() == ["OPEN", "OPEN"]
    assert _view(pack, cr["candidate_id"]).item_status == {"C": "PENDING"}


def test_single_message_reply_refuses_change_request(seeded, main_on):
    """메시지 하나씩 답하는 옛 길은 변경 요청을 받지 않는다(부분 답이 들어오는 길이 된다)."""
    pack = seeded
    _alpha_consulting(pack)
    [cr] = _messages("CHANGE_REQUEST")
    body = ReplyRequest(message_id=cr["message_id"], decision="ACCEPT")
    assert reply_message(pack, "foreman_a2", _key(), body).reason_codes == ("REPLY_BY_REQUEST",)
    assert reply_message(pack, "planner_a", _key(), body).reason_codes == ("NOT_AUTHORIZED",)
    assert _statuses() == ["OPEN"]


def test_one_change_is_in_a_request_once(seeded, main_on):
    """한 통에 같은 변경은 한 번만 담긴다(DB 유일성)."""
    pack = seeded
    _two_item_request(pack)
    [cr, second] = _messages("CHANGE_REQUEST")
    assert (cr["run_id"], cr["step_no"]) == (second["run_id"], second["step_no"])
    with pytest.raises(sqlite3.IntegrityError), db.write() as tx:
        insert_message(
            tx,
            pack.site_id,
            "msg_third",
            run_id=cr["run_id"],
            step_no=cr["step_no"],
            to_actor_id=cr["to_actor_id"],
            type_="CHANGE_REQUEST",
            proposal_id=None,
            body="같은 변경",
            agent_text=None,
            context_version=cr["created_context_version"],
            candidate_id=cr["candidate_id"],
            change_hash="hash-second",
            request_group_id=cr["request_group_id"],
        )


# ── 안이 무효가 될 때 (ST-15·ST-22) ───────────────────────────


def test_invalid_plan_ends_its_consultation_at_once(seeded, main_on):
    """현장 버전이 오르면 그 전의 안은 무효다. 협의 Run은 깨워서 결과를 쓰게 하지 않고 서버가 바로
    끝내며, 열린 요청은 취소되고 그 뒤의 답은 LATE로만 남는다."""
    pack = seeded
    rp, coord = _alpha_consulting(pack)
    assert pin_task(pack, "supervisor", _key(), TaskRef(task_id="C")).status == "APPLIED"
    [ended] = _runs("COORDINATION")
    assert (ended.status, ended.end_reason) == ("STALE", f"CANDIDATE_INVALID:{rp.wait_ref}")
    assert ended.steps_used == coord.steps_used  # 끝내려고 LLM을 부르지 않는다 (ST-20)
    [cr] = _messages("CHANGE_REQUEST")
    assert cr["status"] == "CANCELLED"
    late = _reply(pack, "foreman_a2", cr["message_id"], "DECLINE", OBJECTION)
    assert late.status == "APPLIED" and late.result_refs["late"] is True
    assert _statuses() == ["LATE"]
    with db.read() as conn:
        assert case_objections(conn, pack.site_id, coord.case_id) == []
        events = [r[0] for r in conn.execute("SELECT kind FROM case_event ORDER BY seq")]
    assert events[-1] == "CHILD_RUN_ENDED"  # 메인이 깨어나 다음을 판단한다


def test_reply_rechecks_that_the_plan_is_alive(seeded, main_on):
    """답 명령은 후보가 살아 있는지 다시 본다. 정리를 거치지 않고 무효가 된 안의 열린 요청에 온 답도
    LATE로만 남는다: 수락이든 이견이든 유효한 답이 아니고 Run을 깨우지 않는다."""
    pack = seeded
    rp, coord = _alpha_consulting(pack)
    with db.write() as tx:
        bump_context_version(tx, pack.site_id)  # 명령을 거치지 않고 버전만 올린다
    [cr] = _messages("CHANGE_REQUEST")
    [run] = _runs("COORDINATION")
    assert (cr["status"], run.status) == ("OPEN", "WAITING_HUMAN")
    with db.read() as conn:
        resumes = len([j for j in list_jobs(conn, pack.site_id) if j["kind"] == "RESUME_RUN"])
    out = _reply(pack, "foreman_a2", cr["message_id"], "DECLINE", OBJECTION)
    assert out.status == "APPLIED" and out.result_refs["late"] is True
    assert _statuses() == ["LATE"]
    assert _runs("COORDINATION")[0].wake_seq == run.wake_seq
    assert _view(pack, rp.wait_ref).item_status == {"C": "PENDING"}
    with db.read() as conn:
        assert case_objections(conn, pack.site_id, coord.case_id) == []
        after = len([j for j in list_jobs(conn, pack.site_id) if j["kind"] == "RESUME_RUN"])
    assert after == resumes


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
    # 받은 편지함: 변경 요청은 한 통이고, 항목 표의 값은 서버가 협의 항목에서 채운다
    [request] = [m for m in a2["inbox"] if m["type"] == "CHANGE_REQUEST"]
    assert (request["candidate_id"], request["status"]) == (rp.wait_ref, "ANSWERED")
    assert request["request_group_id"] == item["request"]["request_group_id"]
    assert request["plan_label"] == cand["plan_label"] == "1안"
    [row] = request["items"]
    assert (row["task_id"], row["status"], row["reply"]["decision"]) == ("C", "ANSWERED", "DECLINE")
    assert (row["before"]["start"], row["after"]["start"]) == (60, 90)
    assert request["agent_text"] == "A 인양을 위해 C를 30분 늦추는 안입니다."
    # 검토 패널: 이 안의 협의가 열려 있고 요청을 보낸 담당자 1명이 답했다
    assert (cand["consulting"]["asked"], cand["consulting"]["answered"]) == (1, 1)
