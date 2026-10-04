"""Agent가 보낸 요청에 대한 사람 응답을 API 경로(TestClient)로 한 번씩 부른다.

명령 함수 테스트는 API 본문 모델을 지나지 않는다. 그래서 API 본문이 명령 계층과 다르면 화면에서만 422가
났다. 여기서는 화면이 보내는 본문 그대로 HTTP로 부른다.
시나리오 준비는 각 Agent 테스트의 도우미를 쓴다.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from test_coordination import _alpha_consulting
from test_event_response import _to_proposal

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


def _reasons(res):
    return res.status_code, res.json()["reason_codes"]


# ── Coordination: 변경 요청 답 ─────


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


def test_free_text_answer_is_not_a_decision_via_api(seeded, client, main_on):
    """자유 텍스트 답(ANSWER)은 없다: 신고자 되묻기가 없어졌다. 본문 검증에서 걸린다 → 422."""
    _alpha_consulting(seeded)
    [cr] = _rows("message", "CHANGE_REQUEST")
    res = _reply(client, "foreman_a2", cr["message_id"], "ANSWER", "옮겨도 됩니다")
    assert res.status_code == 422


def test_reply_api_route(seeded, client, main_on):
    """받는 사람이 아니면 403, 없는 메시지 404, 답한 뒤 다른 결정 409, 없는 제안 404."""
    _alpha_consulting(seeded)
    [cr] = _rows("message", "CHANGE_REQUEST")
    assert _reasons(_reply(client, "planner_a", cr["message_id"], "ACCEPT")) == (
        403,
        ["NOT_AUTHORIZED"],
    )
    assert _reply(client, "foreman_a2", "msg_none", "ACCEPT").status_code == 404
    res = _reply(client, "foreman_a2", cr["message_id"], "ACCEPT")
    assert (res.status_code, res.json()["status"]) == (200, "APPLIED"), res.text
    assert _reasons(_reply(client, "foreman_a2", cr["message_id"], "DECLINE", "어렵다")) == (
        409,
        ["ALREADY_ANSWERED"],
    )
    assert _post(client, "proposals/prop_none/discard", "foreman_a2", {}).status_code == 404


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
