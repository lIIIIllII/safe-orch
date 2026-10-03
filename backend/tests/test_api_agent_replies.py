"""Agent가 보낸 요청에 대한 사람 응답을 API 경로(TestClient)로 한 번씩 부른다.

명령 함수 테스트는 API 본문 모델을 지나지 않는다. 그래서 API 본문이 명령 계층과 다르면(예: ReplyBody에
ANSWER가 없음) 화면에서만 422가 났다. 여기서는 화면이 보내는 본문 그대로 HTTP로 부른다.
시나리오 준비는 각 Agent 테스트의 도우미를 쓴다.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from scripted import Router, call
from test_coordination import _alpha_consulting, _draft, _objected, _wait
from test_event_response import _lookup, _r1, _report, _to_proposal
from test_intake import _ask, _intake
from test_resume import _ask_waiting

from app.api.commands import (
    ApproveBody,
    CommentBody,
    RejectBody,
    ReleaseBody,
    ReplyBody,
    WaiveBody,
    WithdrawBody,
)
from app.commands.approval import ApproveRequest, RejectRequest, WaiveRequest
from app.commands.events import HoldRelease
from app.commands.messages import ProposalDecision, ReplyRequest
from app.commands.task_request import TaskWithdraw
from app.coordinator.dispatcher import run_until_idle
from app.main import app
from app.store import db


@pytest.fixture
def client(seeded):
    with TestClient(app) as c:
        yield c


def _post(client, url, actor, body):
    headers = {"X-Actor": actor, "Idempotency-Key": uuid.uuid4().hex}
    return client.post(f"/api/{url}", json=body, headers=headers)


def _reply(client, actor, message_id, decision, comment=""):
    """화면(Inbox)이 보내는 본문 그대로."""
    return _post(
        client, f"messages/{message_id}/reply", actor, {"decision": decision, "comment": comment}
    )


def _rows(table, type_):
    with db.read() as conn:
        cur = conn.execute(f"SELECT * FROM {table} WHERE type = ? ORDER BY rowid", (type_,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def _open_question():
    [q] = [m for m in _rows("message", "QUESTION") if m["status"] == "OPEN"]
    return q


def _reasons(res):
    return res.status_code, res.json()["reason_codes"]


# ── 자유 텍스트 답 (ANSWER) ───────────────────────────────────


def test_answer_intake_question_via_api(seeded, client):
    """Intake 확인 질문: ACCEPT → 409 INVALID_DECISION, 빈 답 → 409 COMMENT_REQUIRED, ANSWER → 200 APPLIED."""
    assert _intake(seeded, seeded.demo_intakes[1].text).status == "APPLIED"
    run_until_idle(seeded, model_factory=Router(intake=[_ask()]).factory())
    q = _open_question()
    assert q["proposal_id"] is None
    assert _reasons(_reply(client, "planner_a", q["message_id"], "ACCEPT")) == (
        409,
        ["INVALID_DECISION"],
    )
    assert _reasons(_reply(client, "planner_a", q["message_id"], "ANSWER", " ")) == (
        409,
        ["COMMENT_REQUIRED"],
    )
    res = _reply(client, "planner_a", q["message_id"], "ANSWER", seeded.demo_intakes[1].answer)
    assert (res.status_code, res.json()["status"]) == (200, "APPLIED"), res.text


def test_answer_reporter_question_via_api(seeded, client, main_on):
    """신고자 확인 질문(ER ASK_REPORTER)에 ANSWER → 200 APPLIED."""
    _r1(seeded)
    _report(seeded, seeded.demo_events[1].text)
    ask = call("ASK_REPORTER", "이유: 시각이 없다/다음: 답을 본다", question="몇 시부터인가요?")
    run_until_idle(seeded, model_factory=Router(event_response=[_lookup(), ask]).factory())
    q = _open_question()
    assert q["to_actor_id"] == "reporter"
    res = _reply(client, "reporter", q["message_id"], "ANSWER", seeded.demo_events[1].answer)
    assert (res.status_code, res.json()["status"]) == (200, "APPLIED"), res.text


def test_answer_on_proposal_question_is_rejected_via_api(seeded, client, main_on):
    """제안이 붙은 질문(담당자 이동 가능 여부)에는 ANSWER를 쓸 수 없다 → 409 INVALID_DECISION."""
    waiting = _ask_waiting(seeded)
    res = _reply(client, "planner_a", waiting.wait_ref, "ANSWER", "SITE-CR-01 써도 됩니다")
    assert _reasons(res) == (409, ["INVALID_DECISION"])


# ── Coordination: 변경 요청 답, 제약 초안 확정·폐기 ─────


def test_change_request_accept_via_api(seeded, client, main_on):
    _alpha_consulting(seeded)
    [cr] = _rows("message", "CHANGE_REQUEST")
    res = _reply(client, "foreman_a2", cr["message_id"], "ACCEPT")
    assert (res.status_code, res.json()["status"]) == (200, "APPLIED"), res.text


def test_change_request_objection_via_api(seeded, client, main_on):
    """이견(DECLINE)은 사유가 필요하다 → 빈 사유 409 COMMENT_REQUIRED, 사유 있으면 200."""
    _alpha_consulting(seeded)
    [cr] = _rows("message", "CHANGE_REQUEST")
    assert _reasons(_reply(client, "foreman_a2", cr["message_id"], "DECLINE")) == (
        409,
        ["COMMENT_REQUIRED"],
    )
    res = _reply(client, "foreman_a2", cr["message_id"], "DECLINE", "작업발판 연계 공정 확정")
    assert (res.status_code, res.json()["status"]) == (200, "APPLIED"), res.text


def _drafted(pack):
    _alpha_consulting(pack)
    cr = _objected(pack)
    run_until_idle(
        pack, model_factory=Router(coordination=[_draft(cr["message_id"]), _wait()]).factory()
    )
    [confirm] = _rows("message", "CONFIRMATION")
    [draft] = _rows("proposal", "FEEDBACK_CONSTRAINT")
    return confirm, draft


def test_draft_constraint_confirm_via_api(seeded, client, main_on):
    confirm, _ = _drafted(seeded)
    res = _reply(client, "foreman_a2", confirm["message_id"], "ACCEPT")
    assert (res.status_code, res.json()["status"]) == (200, "APPLIED"), res.text
    assert _rows("proposal", "FEEDBACK_CONSTRAINT")[0]["status"] == "CONFIRMED"


def test_draft_constraint_discard_via_api(seeded, client, main_on):
    """제안 폐기 경로(/proposals/{id}/discard)도 reply와 같은 처리다."""
    _, draft = _drafted(seeded)
    res = _post(client, f"proposals/{draft['proposal_id']}/discard", "foreman_a2", {"comment": ""})
    assert (res.status_code, res.json()["status"]) == (200, "APPLIED"), res.text
    assert _rows("proposal", "FEEDBACK_CONSTRAINT")[0]["status"] == "DISCARDED"


# ── Event Response: 사실 수정 확인·폐기, FACT_CONFIRMED 해제 ─


def _release(client, hold_id, resolution):
    from app.store.repos.site import get_site

    with db.read() as conn:
        ctx = get_site(conn, "YARD-01").context_version
    body = {"resolution": resolution, "expected_context_version": ctx, "comment": ""}
    return _post(client, f"holds/{hold_id}/release", "supervisor", body)


def test_fact_update_confirm_and_fact_confirmed_release_via_api(seeded, client, main_on):
    refs, _ = _to_proposal(seeded)
    # 확인 전 FACT_CONFIRMED 해제는 거절
    assert _reasons(_release(client, refs["hold_id"], "FACT_CONFIRMED")) == (
        409,
        ["FACT_NOT_CONFIRMED"],
    )
    [confirm] = _rows("message", "CONFIRMATION")
    res = _reply(client, "supervisor", confirm["message_id"], "ACCEPT")
    assert (res.status_code, res.json()["status"]) == (200, "APPLIED"), res.text
    assert _rows("proposal", "FACT_UPDATE")[0]["status"] == "CONFIRMED"
    res = _release(client, refs["hold_id"], "FACT_CONFIRMED")
    assert (res.status_code, res.json()["status"]) == (200, "APPLIED"), res.text


def test_fact_update_discard_via_api(seeded, client, main_on):
    _to_proposal(seeded)
    [confirm] = _rows("message", "CONFIRMATION")
    res = _reply(client, "supervisor", confirm["message_id"], "DECLINE")
    assert (res.status_code, res.json()["status"]) == (200, "APPLIED"), res.text
    assert _rows("proposal", "FACT_UPDATE")[0]["status"] == "DISCARDED"


# ── API 본문 모델과 명령 모델의 일치 ──────────────────────────


@pytest.mark.parametrize(
    ("api_body", "command"),
    [
        (ReplyBody, ReplyRequest),
        (ReleaseBody, HoldRelease),
        (ApproveBody, ApproveRequest),
        (RejectBody, RejectRequest),
        (WaiveBody, WaiveRequest),
        (WithdrawBody, TaskWithdraw),
        (CommentBody, ProposalDecision),
    ],
)
def test_api_body_fields_match_command(api_body, command):
    """API 본문의 필드는 명령 모델의 같은 필드와 타입·기본값이 같다(경로 id만 API가 합친다)."""
    for name, field in api_body.model_fields.items():
        assert name in command.model_fields, (api_body.__name__, name)
        other = command.model_fields[name]
        assert field.annotation == other.annotation, (api_body.__name__, name)
        assert field.default == other.default, (api_body.__name__, name)
