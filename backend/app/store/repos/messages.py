"""proposal·message 기록과 조회 (설계서 §5.1·§9.4, 부록 A.21).

제안을 먼저 만들고 메시지가 proposal_id로 가리킨다. 상태 전이는 트리거가 막는다(PENDING에서 한 번,
OPEN → ANSWERED·CANCELLED, CANCELLED → LATE). Run 종료 시 정리는 cases.cancel_requests가 한다.
"""

import sqlite3
from typing import Any

from app.store.repos._rows import dumps, loads, rows


def insert_proposal(
    tx: sqlite3.Connection,
    site_id: str,
    proposal_id: str,
    *,
    type_: str,
    run_id: str,
    step_no: int,
    target_task_id: str,
    base_task_revision: int,
    context_version: int,
    payload: dict[str, Any],
    confirmer_actor_id: str,
) -> None:
    tx.execute(
        "INSERT INTO proposal (proposal_id, site_id, type, run_id, step_no, target_task_id,"
        " base_task_revision, created_context_version, payload, confirmer_actor_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            proposal_id,
            site_id,
            type_,
            run_id,
            step_no,
            target_task_id,
            base_task_revision,
            context_version,
            dumps(payload),
            confirmer_actor_id,
        ),
    )


def insert_message(
    tx: sqlite3.Connection,
    site_id: str,
    message_id: str,
    *,
    run_id: str,
    step_no: int,
    to_actor_id: str,
    type_: str,
    proposal_id: str | None,
    body: str,
    agent_text: str | None,
    context_version: int,
) -> None:
    tx.execute(
        "INSERT INTO message (message_id, site_id, run_id, step_no, to_actor_id, type,"
        " proposal_id, body, agent_text, created_context_version)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            message_id,
            site_id,
            run_id,
            step_no,
            to_actor_id,
            type_,
            proposal_id,
            body,
            agent_text,
            context_version,
        ),
    )


def _proposal(r: dict[str, Any]) -> dict[str, Any]:
    r["payload"] = loads(r["payload"])
    r["result_ref"] = loads(r["result_ref"])
    return r


def _message(r: dict[str, Any]) -> dict[str, Any]:
    r["reply"] = loads(r["reply"])
    return r


def get_proposal(conn: sqlite3.Connection, site_id: str, proposal_id: str) -> dict[str, Any] | None:
    found = rows(
        conn,
        "SELECT * FROM proposal WHERE site_id = ? AND proposal_id = ?",
        (site_id, proposal_id),
    )
    return _proposal(found[0]) if found else None


def get_message(conn: sqlite3.Connection, site_id: str, message_id: str) -> dict[str, Any] | None:
    found = rows(
        conn, "SELECT * FROM message WHERE site_id = ? AND message_id = ?", (site_id, message_id)
    )
    return _message(found[0]) if found else None


def message_for_proposal(conn: sqlite3.Connection, proposal_id: str) -> dict[str, Any] | None:
    found = rows(conn, "SELECT * FROM message WHERE proposal_id = ?", (proposal_id,))
    return _message(found[0]) if found else None


def set_message_reply(
    tx: sqlite3.Connection,
    message_id: str,
    status: str,
    reply: dict[str, Any],
    context_version: int,
) -> None:
    """OPEN → ANSWERED 또는 CANCELLED → LATE (트리거가 다른 전이를 막는다)."""
    tx.execute(
        "UPDATE message SET status = ?, reply = ?, answered_context_version = ?"
        " WHERE message_id = ?",
        (status, dumps(reply), context_version, message_id),
    )


def decide_proposal(
    tx: sqlite3.Connection,
    proposal_id: str,
    status: str,
    actor_id: str,
    context_version: int,
    result_ref: dict[str, Any] | None = None,
) -> None:
    """PENDING → CONFIRMED·DISCARDED (한 번만)."""
    tx.execute(
        "UPDATE proposal SET status = ?, decided_by = ?, decided_context_version = ?,"
        " result_ref = ? WHERE proposal_id = ? AND status = 'PENDING'",
        (
            status,
            actor_id,
            context_version,
            None if result_ref is None else dumps(result_ref),
            proposal_id,
        ),
    )


_JOINED = (
    "SELECT m.message_id, m.run_id, m.step_no, m.to_actor_id, m.type, m.status, m.body,"
    " m.agent_text, m.reply, m.created_context_version, m.answered_context_version,"
    " p.proposal_id, p.type AS proposal_type, p.status AS proposal_status, p.target_task_id,"
    " p.payload FROM message m LEFT JOIN proposal p ON p.proposal_id = m.proposal_id"
)


def _joined(r: dict[str, Any]) -> dict[str, Any]:
    r["reply"] = loads(r["reply"])
    r["payload"] = loads(r["payload"]) or {}
    return r


def list_run_replies(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    """이 Run이 보낸 질문과 답 (Observation human_replies). comment는 인용 필드로만 (A.17·A.21)."""
    out = []
    for r in rows(conn, _JOINED + " WHERE m.run_id = ? ORDER BY m.step_no", (run_id,)):
        r = _joined(r)
        reply = r["reply"] or {}
        out.append(
            {
                "message_id": r["message_id"],
                "task_id": r["target_task_id"],
                "axis": r["payload"].get("axis"),
                "allowed_values": r["payload"].get("allowed_values", []),
                "status": r["status"],
                "decision": reply.get("decision"),
                "quoted_comment": reply.get("comment"),
            }
        )
    return out


def list_inbox(conn: sqlite3.Connection, site_id: str, actor_id: str) -> list[dict[str, Any]]:
    """X-Actor 본인에게 온 메시지 (§12 "본인", A.21 7). 서버 문구(body)와 모델 문구(agent_text)를 나눈다."""
    out = []
    for r in rows(
        conn,
        _JOINED + " WHERE m.site_id = ? AND m.to_actor_id = ? ORDER BY m.rowid DESC",
        (site_id, actor_id),
    ):
        r = _joined(r)
        payload = r.pop("payload")
        out.append(
            {
                **r,
                "task_id": r.pop("target_task_id"),
                "axis": payload.get("axis"),
                "allowed_values": payload.get("allowed_values", []),
            }
        )
    return out
