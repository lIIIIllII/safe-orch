"""proposal·message 기록과 조회.

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
    run_id: str | None,
    step_no: int | None,
    to_actor_id: str,
    type_: str,
    proposal_id: str | None,
    body: str,
    agent_text: str | None,
    context_version: int,
    candidate_id: str | None = None,
    change_hash: str | None = None,
    request_group_id: str | None = None,
) -> None:
    """candidate_id·change_hash는 변경 요청(CHANGE_REQUEST)을 후보의 그 변경에 묶는다.
    request_group_id는 한 담당자에게 보낸 한 통의 키다: 한 통의 행은 변경마다 하나다 (ST-26).

    Run 없이 서버가 보내는 통지(직접 이동)는 run_id·step_no가 None이다."""
    tx.execute(
        "INSERT INTO message (message_id, site_id, run_id, step_no, to_actor_id, type,"
        " proposal_id, body, agent_text, created_context_version, candidate_id, change_hash,"
        " request_group_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            candidate_id,
            change_hash,
            request_group_id,
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
    " m.agent_text, m.reply, m.created_context_version, m.answered_context_version, m.candidate_id,"
    " m.change_hash, m.request_group_id,"
    " p.proposal_id, p.type AS proposal_type, p.status AS proposal_status, p.target_task_id,"
    " p.payload FROM message m LEFT JOIN proposal p ON p.proposal_id = m.proposal_id"
)


def _joined(r: dict[str, Any]) -> dict[str, Any]:
    r["reply"] = loads(r["reply"])
    r["payload"] = loads(r["payload"]) or {}
    return r


def list_inbox(conn: sqlite3.Connection, site_id: str, actor_id: str) -> list[dict[str, Any]]:
    """X-Actor 본인에게 온 메시지. 서버 문구(body)와 모델 문구(agent_text)를 나눈다."""
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
                # 사실 수정(FACT_UPDATE)의 필드·옛 값·새 값
                "fact": None
                if "field" not in payload
                else {k: payload[k] for k in ("field", "old_value", "new_value")},
            }
        )
    return out


# ── Coordination ───────────────────────────────────


def _change_requests(
    conn: sqlite3.Connection, site_id: str, where: str, params: tuple[Any, ...]
) -> list[dict[str, Any]]:
    """변경 요청과 담당자 답."""
    out = []
    for r in rows(
        conn,
        "SELECT message_id, run_id, step_no, to_actor_id, status, reply, change_hash, candidate_id,"
        " request_group_id"
        f" FROM message WHERE site_id = ? AND type = 'CHANGE_REQUEST' AND {where}"
        " ORDER BY rowid",
        (site_id, *params),
    ):
        r["reply"] = loads(r["reply"])
        out.append(r)
    return out


def list_change_requests(
    conn: sqlite3.Connection, site_id: str, candidate_id: str
) -> list[dict[str, Any]]:
    """후보에 묶인 변경 요청."""
    return _change_requests(conn, site_id, "candidate_id = ?", (candidate_id,))


def list_requests_for_changes(
    conn: sqlite3.Connection, site_id: str, change_hashes: list[str]
) -> list[dict[str, Any]]:
    """같은 변경(change_hash = 작업·revision·전후 배정)의 변경 요청. 후보와 Case를 가리지 않는다 (ST-15)."""
    if not change_hashes:
        return []
    marks = ", ".join("?" * len(change_hashes))
    return _change_requests(conn, site_id, f"change_hash IN ({marks})", tuple(change_hashes))


def list_request_group(
    conn: sqlite3.Connection, site_id: str, request_group_id: str
) -> list[dict[str, Any]]:
    """변경 요청 한 통의 행(변경마다 하나). 없으면 빈 목록."""
    return _change_requests(conn, site_id, "request_group_id = ?", (request_group_id,))


def cancel_request_group(tx: sqlite3.Connection, request_group_id: str) -> None:
    """한 통의 열린 행을 OPEN → CANCELLED로. 답한 행은 건드리지 않는다."""
    tx.execute(
        "UPDATE message SET status = 'CANCELLED' WHERE request_group_id = ? AND status = 'OPEN'",
        (request_group_id,),
    )


def change_answers(requests: list[dict[str, Any]]) -> dict[str, str]:
    """change_hash → 담당자 답에 따른 item 상태. 취소된 요청·늦은 답(CANCELLED·LATE)은 세지 않는다 (ST-15).

    ACCEPT → ACCEPTED, DECLINE(이견) → OBJECTED. 같은 변경에 답이 여럿이면 마지막 것.
    """
    out: dict[str, str] = {}
    for r in requests:
        if r["status"] != "ANSWERED":
            continue
        decision = (r["reply"] or {}).get("decision")
        if decision == "ACCEPT":
            out[r["change_hash"]] = "ACCEPTED"
        elif decision == "DECLINE":
            out[r["change_hash"]] = "OBJECTED"
    return out


def answer_sources(requests: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """change_hash → 그 상태를 만든 변경 요청(change_answers와 같은 규칙: 유효한 답 중 마지막)."""
    return {
        r["change_hash"]: r
        for r in requests
        if r["status"] == "ANSWERED" and (r["reply"] or {}).get("decision") in ("ACCEPT", "DECLINE")
    }


def list_run_messages(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    """이 Run이 보낸 메시지(유형·수신자·상태·답·결합 값·모델 문장)."""
    found = rows(
        conn,
        "SELECT message_id, step_no, to_actor_id, type, status, reply, proposal_id,"
        " candidate_id, change_hash, request_group_id, agent_text FROM message WHERE run_id = ?"
        " ORDER BY rowid",
        (run_id,),
    )
    for r in found:
        r["reply"] = loads(r["reply"])
    return found


def list_fact_updates(
    conn: sqlite3.Connection,
    site_id: str,
    *,
    event_id: str | None = None,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """FACT_UPDATE 제안 (Event 또는 Run 기준). payload에 event_id·old_value·new_value가 있다."""
    sql = "SELECT * FROM proposal WHERE site_id = ? AND type = 'FACT_UPDATE'"
    params: list[Any] = [site_id]
    if event_id is not None:
        sql += " AND json_extract(payload, '$.event_id') = ?"
        params.append(event_id)
    if run_id is not None:
        sql += " AND run_id = ?"
        params.append(run_id)
    return [_proposal(r) for r in rows(conn, sql + " ORDER BY rowid", tuple(params))]
