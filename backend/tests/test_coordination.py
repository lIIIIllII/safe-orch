"""Coordination Agent — 기본안 A 최소 경로 (설계서 §18.2.2, 부록 A.24).

스크립트 모델(Router)로 Replanning·Coordination Run을 함께 돌린다. 설정 COORDINATION_ENABLED를 켠
테스트만 Coordination이 시작한다(기본값 꺼짐 = 기본안 B, 기존 테스트·골든 그대로).
"""

import uuid

from langchain_core.messages import AIMessage
from scripted import Router, call, solve

from app.agents.prompts import coordination as prompt
from app.api.state import build_state
from app.commands.approval import (
    ApproveRequest,
    RejectRequest,
    WaiveRequest,
    approve_and_commit,
    reject_candidate,
    waive,
)
from app.commands.messages import ReplyRequest, reply_message
from app.commands.task_request import TaskRequestForm, submit_task_request
from app.coordinator.dispatcher import run_until_idle
from app.domain.consultation import item_statuses
from app.domain.models import Assignment, ConsultationItem
from app.store import db
from app.store.repos.consultations import consultation_view
from app.store.repos.decisions import list_constraints
from app.store.repos.dispatch import list_jobs
from app.store.repos.records import list_validations
from app.store.repos.runs import get_run, list_steps
from app.store.repos.site import get_site

OBJECTION = "작업발판 연계 공정 확정"  # scenario demo_rejections[0].comment와 같은 문장


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
    """REPORT_TO_SUPERVISOR의 인자 이름이 summary라 call()을 쓰지 않는다."""
    args = {"decision_summary": summary, "summary": text}
    return AIMessage(
        content="",
        tool_calls=[{"name": "REPORT_TO_SUPERVISOR", "args": args, "id": uuid.uuid4().hex}],
    )


def _wait():
    return call("WAIT_FOR_REPLIES", "이유: 요청을 보냈다/다음: 답을 본다")


def _draft(message_id, axes=("TIME", "RESOURCE"), reason_code="TASK_IMMOVABLE", task_id="C"):
    return call(
        "DRAFT_CONSTRAINT",
        "이유: 담당자가 C 고정을 요구한다/다음: 확인을 기다린다",
        message_id=message_id,
        reason_code=reason_code,
        task_id=task_id,
        axes=list(axes),
        message="C를 지금 시각·자원에 고정하는 제약으로 확인해 주세요.",
    )


def _alpha_consulting(pack):
    """폼 A → L0·L1 → Alpha(WAIT) → PASS → Coordination이 A2에게 변경 요청 → 답 대기."""
    _submit_a(pack)
    router = Router(replanning=[solve("L0"), solve("L1")], coordination=[_request_c(), _wait()])
    run_until_idle(pack, model_factory=router.factory())
    assert router.left() == {"REPLANNING": 0, "COORDINATION": 0, "EVENT_RESPONSE": 0, "INTAKE": 0}
    [rp] = _runs("REPLANNING")
    [coord] = _runs("COORDINATION")
    return rp, coord


def _objected(pack, comment=OBJECTION):
    """A2가 변경 요청에 이견(DECLINE + 사유)으로 답한다."""
    [cr] = _messages("CHANGE_REQUEST")
    out = _reply(pack, "foreman_a2", cr["message_id"], "DECLINE", comment)
    assert out.status == "APPLIED"
    return cr


# ── 기본안 A 전체 ──────────────────────────────────────────────


def test_plan_a_full_e2e(seeded, coordination_on):
    """Alpha → 변경 요청 → 이견 → 제약 초안 → A2 확정 → Replanning 재개 → Beta → R1 → 통지 2건."""
    pack = seeded
    rp, coord = _alpha_consulting(pack)
    alpha = rp.wait_ref
    assert (rp.status, rp.wait_kind) == ("WAITING_HUMAN", "CANDIDATE_OUTCOME")
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

    router = Router(coordination=[_draft(cr["message_id"]), _wait()])
    run_until_idle(pack, model_factory=router.factory())
    s_draft = _steps(coord.run_id)[2]
    obs_c = next(i for i in s_draft["observation"]["items"] if i["task_id"] == "C")
    assert obs_c["requests"][0]["quoted_comment"] == OBJECTION
    assert obs_c["changed_axes"] == ["TIME"]
    [draft] = _proposals("FEEDBACK_CONSTRAINT")
    [confirm] = _messages("CONFIRMATION")
    assert (draft["status"], draft["confirmer_actor_id"], draft["target_task_id"]) == (
        "PENDING",
        "foreman_a2",
        "C",
    )
    assert confirm["proposal_id"] == draft["proposal_id"] and confirm["to_actor_id"] == "foreman_a2"
    view = _view(pack, alpha)
    assert (view.item_status["C"], view.items_status) == ("OBJECTION_DRAFT_PENDING", "BLOCKED")
    assert _approve(pack, alpha).reason_codes == ("CONSULTATION_INCOMPLETE",)
    no_waive = waive(
        pack, "supervisor", _key(), WaiveRequest(candidate_id=alpha, task_ids=("C",), comment="x")
    )
    assert no_waive.reason_codes == ("ITEM_NOT_WAIVABLE",)  # OBJECTED 계열은 수용 불가

    ctx = _site(pack).context_version
    out = _reply(pack, "foreman_a2", confirm["message_id"], "ACCEPT")
    assert out.status == "APPLIED" and out.result_refs["replanning_run_id"] == rp.run_id
    assert _site(pack).context_version == ctx + 1
    with db.read() as conn:
        [fc] = list_constraints(conn, pack.site_id)
    assert (fc.task_id, fc.frozen_axes, fc.source_type, fc.source_id) == (
        "C",
        ("RESOURCE", "TIME"),
        "PROPOSAL",
        draft["proposal_id"],
    )
    [ended] = _runs("COORDINATION")
    assert (ended.status, ended.end_reason) == ("STALE", f"CONSTRAINT:{fc.constraint_id}")
    assert _runs("REPLANNING")[0].wake_seq == 1

    # 이후는 Scene 3과 같다: C 고정 관찰 → LIST → ASK → 수락 → TRY → Beta
    ask = call(
        "ASK_TASK_OWNER",
        "자원 축 확인",
        task_id="A",
        axis="RESOURCE",
        allowed_values=["SITE-CR-01"],
        question="SITE-CR-01을 써도 되나요?",
    )
    listing = call("LIST_ASSIGNABLE_RESOURCES", "A 자원 조회", task_id="A")
    run_until_idle(pack, model_factory=Router(replanning=[listing, ask]).factory())
    [question] = _messages("QUESTION")
    assert _reply(pack, "planner_a", question["message_id"], "ACCEPT").status == "APPLIED"
    try_beta = call(
        "TRY_ALTERNATIVE_RESOURCE", "SITE-CR-01 시도", task_id="A", resource_id="SITE-CR-01"
    )
    run_until_idle(pack, model_factory=Router(replanning=[try_beta]).factory())
    [rp] = _runs("REPLANNING")
    beta = rp.wait_ref
    steps = _steps(rp.run_id)
    assert [(s["action"] or {}).get("name") for s in steps] == [
        "SOLVE_WITH_SCOPE",
        "SOLVE_WITH_SCOPE",
        "LIST_ASSIGNABLE_RESOURCES",
        "ASK_TASK_OWNER",
        "TRY_ALTERNATIVE_RESOURCE",
    ]
    assert (rp.steps_used, rp.solver_calls_used, rp.human_rounds_used) == (5, 3, 1)
    # Replanning 관찰에는 같은 Case의 Coordination 메시지가 섞이지 않는다 (A.24 3)
    replies = steps[4]["observation"]["human_replies"]
    assert [r["message_id"] for r in replies] == [question["message_id"]]
    assert steps[2]["observation"]["rejections"] == []  # 제약은 거절이 아니라 확인에서 왔다
    assert [c["source_type"] for c in steps[2]["observation"]["constraints"]] == ["PROPOSAL"]
    assert _view(pack, beta).items_status == "COMPLETE"
    assert len(_runs("COORDINATION")) == 1  # Beta에는 동의 대기가 없어 협의 Run이 없다

    assert _approve(pack, beta).status == "APPLIED"
    assert _runs("REPLANNING")[0].status == "SUCCEEDED"
    with db.read() as conn:
        keys = [j["dedupe_key"] for j in list_jobs(conn, pack.site_id) if j["kind"] == "START_RUN"]
    assert "START_RUN:COORDINATION:NOTICE:plan1" in keys

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
        "REPORT_TO_SUPERVISOR",
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


# ── 설정을 켠 채 기본안 B (Supervisor 구조화 거절) ──────────────


def test_supervisor_reject_during_consultation_runs_plan_b(seeded, coordination_on):
    """협의 중 Supervisor가 Alpha를 구조화 거절하면 협의 Run은 STALE, 이후 기본안 B로 끝까지 간다."""
    pack = seeded
    rp, _ = _alpha_consulting(pack)
    alpha = rp.wait_ref
    x = pack.demo_rejections[0]
    body = RejectRequest(
        candidate_id=alpha,
        validation_id=_validation_id(pack, alpha),
        reason_code=x.reason_code,
        target_task_ids=x.target_task_ids,
        axes=x.axes,
        comment=x.comment,
    )
    assert reject_candidate(pack, "supervisor", _key(), body).status == "APPLIED"
    [ended] = _runs("COORDINATION")
    assert (ended.status, ended.end_reason) == ("STALE", f"REJECTED:{alpha}")
    [cr] = _messages("CHANGE_REQUEST")
    assert cr["status"] == "CANCELLED"
    late = _reply(pack, "foreman_a2", cr["message_id"], "DECLINE", OBJECTION)
    assert late.status == "APPLIED" and late.result_refs["late"] is True

    listing = call("LIST_ASSIGNABLE_RESOURCES", "A 자원 조회", task_id="A")
    ask = call(
        "ASK_TASK_OWNER",
        "자원 축 확인",
        task_id="A",
        axis="RESOURCE",
        allowed_values=["SITE-CR-01"],
        question="SITE-CR-01을 써도 되나요?",
    )
    run_until_idle(pack, model_factory=Router(replanning=[listing, ask]).factory())
    [question] = _messages("QUESTION")
    assert _reply(pack, "planner_a", question["message_id"]).status == "APPLIED"
    try_beta = call(
        "TRY_ALTERNATIVE_RESOURCE", "SITE-CR-01 시도", task_id="A", resource_id="SITE-CR-01"
    )
    run_until_idle(pack, model_factory=Router(replanning=[try_beta]).factory())
    [rp] = _runs("REPLANNING")
    assert (rp.steps_used, rp.solver_calls_used, rp.human_rounds_used) == (5, 3, 1)
    assert _approve(pack, rp.wait_ref).status == "APPLIED"
    assert _site(pack).plan_revision == 1
    assert _runs("REPLANNING")[0].status == "SUCCEEDED"


# ── 이견이 작업 고정 요구가 아닐 때 (D10 Agent 판단 근거) ─────────


def test_objection_without_fix_request_is_reported_not_drafted(seeded, coordination_on):
    """선호·일정 불만 같은 이견에는 초안 대신 Supervisor 보고를 고를 수 있다. 제약은 생기지 않는다."""
    pack = seeded
    rp, coord = _alpha_consulting(pack)
    _objected(pack, "오전에는 다른 일이 겹쳐 가능하면 원래 시간이 좋겠습니다")
    report = _report(
        "A2는 C 이동에 선호상 이견. 작업 고정 요구는 아님.",
        "이유: 고정 요구가 아닌 선호다/다음: Supervisor 판단",
    )
    run_until_idle(pack, model_factory=Router(coordination=[report]).factory())
    [done] = _runs("COORDINATION")
    assert (done.status, done.end_reason) == ("SUCCEEDED", "REPORT_TO_SUPERVISOR")
    s = _steps(coord.run_id)[-1]
    assert "DRAFT_CONSTRAINT" in [t["function"]["name"] for t in s["available_actions"]]
    assert s["action"]["name"] == "REPORT_TO_SUPERVISOR"
    with db.read() as conn:
        assert list_constraints(conn, pack.site_id) == []
    view = _view(pack, rp.wait_ref)
    assert (view.item_status["C"], view.items_status) == ("OBJECTED", "BLOCKED")
    assert _runs("REPLANNING")[0].status == "WAITING_HUMAN"  # Supervisor가 거절해 재탐색할 수 있다


# ── 서버 검사 ──────────────────────────────────────────────────


def test_draft_constraint_server_checks(seeded, coordination_on):
    """바뀐 축(C는 TIME)이 없는 축, 다른 사유 코드, 다른 작업은 ACTION_NOT_AVAILABLE이다 (A.24 7)."""
    pack = seeded
    _, coord = _alpha_consulting(pack)
    cr = _objected(pack)
    mid = cr["message_id"]
    router = Router(
        coordination=[
            _draft(mid, axes=("RESOURCE",)),
            _draft(mid, reason_code="PREFERENCE"),
            _draft(mid, task_id="A"),
            call("ESCALATE", "이유: 초안을 만들 수 없다/다음: 이관", reason="검사 확인"),
        ]
    )
    run_until_idle(pack, model_factory=router.factory())
    guards = [(s["result_kind"], s["guard"]["reason_code"]) for s in _steps(coord.run_id)[2:]]
    assert guards == [
        ("REJECTED", "ACTION_NOT_AVAILABLE"),
        ("REJECTED", "ACTION_NOT_AVAILABLE"),
        ("REJECTED", "ACTION_NOT_AVAILABLE"),
        ("DONE", None),
    ]
    assert _proposals("FEEDBACK_CONSTRAINT") == []
    assert _runs("COORDINATION")[0].end_reason == "ESCALATE"


def test_change_request_objection_needs_comment(seeded, coordination_on):
    pack = seeded
    _alpha_consulting(pack)
    [cr] = _messages("CHANGE_REQUEST")
    out = _reply(pack, "foreman_a2", cr["message_id"], "DECLINE", "  ")
    assert (out.status, out.reason_codes) == ("REJECTED", ("COMMENT_REQUIRED",))


def test_draft_discard_returns_to_objected_and_wakes(seeded, coordination_on):
    pack = seeded
    rp, _ = _alpha_consulting(pack)
    cr = _objected(pack)
    run_until_idle(
        pack, model_factory=Router(coordination=[_draft(cr["message_id"]), _wait()]).factory()
    )
    [confirm] = _messages("CONFIRMATION")
    assert _reply(pack, "foreman_a2", confirm["message_id"], "DECLINE").status == "APPLIED"
    assert _view(pack, rp.wait_ref).item_status["C"] == "OBJECTED"
    assert _proposals("FEEDBACK_CONSTRAINT")[0]["status"] == "DISCARDED"
    run_until_idle(pack, model_factory=Router(coordination=[escalate_coord()]).factory())
    assert _runs("COORDINATION")[0].status == "ESCALATED"


def escalate_coord():
    return call("ESCALATE", "이유: 확인이 거절되었다/다음: 이관", reason="담당자가 초안을 폐기")


def test_coordination_off_keeps_review_queue(seeded):
    """설정이 꺼져 있으면(기본값) Coordination을 시작하지 않는다(기본안 B, 기존 동작)."""
    _submit_a(seeded)
    run_until_idle(seeded, model_factory=Router(replanning=[solve("L0"), solve("L1")]).factory())
    assert _runs("COORDINATION") == []
    with db.read() as conn:
        kinds = [j["dedupe_key"] for j in list_jobs(conn, seeded.site_id)]
    assert not [k for k in kinds if "COORDINATION" in k]


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
    answers = {"hc": "OBJECTION_DRAFT_PENDING", "he": "ACCEPTED", "ha": "OBJECTED"}
    assert item_statuses(items, ["A"], answers) == {
        "A": "WAIVED",  # WAIVE가 먼저
        "C": "OBJECTION_DRAFT_PENDING",
        "E": "ACCEPTED",
    }
    assert item_statuses(items, [], None) == {"A": "PENDING", "C": "PENDING", "E": "PENDING"}


def test_coordination_prompt_fingerprint_and_keys(seeded, coordination_on):
    assert prompt.fingerprint() == prompt.PROMPT_FINGERPRINTS[prompt.PROMPT_VERSION]
    _, coord = _alpha_consulting(seeded)
    obs = _steps(coord.run_id)[0]["observation"]
    assert tuple(sorted(obs)) == prompt.OBSERVATION_KEYS
    system = prompt.render_system(seeded)
    assert seeded.site_description in system and "첫날 09:00" in system
    assert "이견이면" not in system  # 초안을 지시하지 않는다 (A.24)


def test_state_shows_item_request_and_inbox_types(seeded, coordination_on):
    """state: 검토 패널 항목의 변경 요청·인용된 이견·초안 축, Inbox의 후보·초안 축 (A.24 화면)."""

    pack = seeded
    rp, _ = _alpha_consulting(pack)
    cr = _objected(pack)
    run_until_idle(
        pack, model_factory=Router(coordination=[_draft(cr["message_id"]), _wait()]).factory()
    )
    with db.read() as conn:
        a2 = build_state(conn, pack, "foreman_a2")
    cand = next(c for c in a2["candidates"] if c["candidate_id"] == rp.wait_ref)
    item = next(i for i in cand["consultation"]["items"] if i["task_id"] == "C")
    assert item["item_status"] == "OBJECTION_DRAFT_PENDING"
    assert (item["request"]["decision"], item["request"]["quoted_comment"]) == (
        "DECLINE",
        OBJECTION,
    )
    assert item["request"]["draft"]["axes"] == ["RESOURCE", "TIME"]
    by_type = {m["type"]: m for m in a2["inbox"]}
    assert by_type["CHANGE_REQUEST"]["candidate_id"] == rp.wait_ref
    assert (by_type["CONFIRMATION"]["status"], by_type["CONFIRMATION"]["axes"]) == (
        "OPEN",
        ["RESOURCE", "TIME"],
    )
