"""decision 기록과 조회. 불변이다."""

import sqlite3
from typing import Any

from app.store.repos._rows import dumps, loads, rows


def insert_decision(
    tx: sqlite3.Connection,
    site_id: str,
    decision_id: str,
    type_: str,
    candidate_id: str,
    validation_id: str,
    actor_id: str,
    context_version: int,
    *,
    reason_code: str | None = None,
    target_task_ids: tuple[str, ...] = (),
    comment: str = "",
) -> None:
    tx.execute(
        "INSERT INTO decision (decision_id, site_id, type, candidate_id, validation_id, actor_id,"
        " reason_code, target_task_ids, comment, context_version)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            decision_id,
            site_id,
            type_,
            candidate_id,
            validation_id,
            actor_id,
            reason_code,
            dumps(list(target_task_ids)),
            comment,
            context_version,
        ),
    )


def list_decisions(
    conn: sqlite3.Connection, site_id: str, candidate_id: str, type_: str | None = None
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM decision WHERE site_id = ? AND candidate_id = ?"
    params: tuple[Any, ...] = (site_id, candidate_id)
    if type_ is not None:
        sql += " AND type = ?"
        params += (type_,)
    found = rows(conn, sql + " ORDER BY rowid", params)
    for r in found:
        r["target_task_ids"] = loads(r["target_task_ids"])
    return found


def rejected_candidate_ids(
    conn: sqlite3.Connection, site_id: str, context_version: int
) -> list[str]:
    """그 Context에서 만들어진 후보 중 거절된 것 (같은 assignments 재제안 Guard)."""
    return [
        r["candidate_id"]
        for r in rows(
            conn,
            "SELECT DISTINCT d.candidate_id FROM decision d"
            " JOIN candidate c ON c.candidate_id = d.candidate_id"
            " WHERE d.site_id = ? AND d.type = 'REJECT' AND c.context_version = ?",
            (site_id, context_version),
        )
    ]


def list_case_rejections(conn: sqlite3.Connection, case_id: str) -> list[dict[str, Any]]:
    """이 Case의 후보에 대한 거절 (Observation rejections). 자유 텍스트는 인용 필드로."""
    found = rows(
        conn,
        "SELECT d.candidate_id, d.reason_code, d.target_task_ids, d.comment"
        " FROM decision d JOIN candidate c ON c.candidate_id = d.candidate_id"
        " JOIN solver_job j ON j.solver_result_id = c.solver_result_id"
        " JOIN agent_run r ON r.run_id = j.run_id"
        " WHERE r.case_id = ? AND d.type = 'REJECT' ORDER BY d.rowid",
        (case_id,),
    )
    return [
        {
            "candidate_id": r["candidate_id"],
            "reason_code": r["reason_code"],
            "target_task_ids": loads(r["target_task_ids"]),
            "quoted_comment": r["comment"],
        }
        for r in found
    ]
