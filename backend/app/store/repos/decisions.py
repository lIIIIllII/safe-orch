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
    batch_id: str | None = None,
) -> None:
    tx.execute(
        "INSERT INTO decision (decision_id, site_id, type, candidate_id, validation_id, actor_id,"
        " reason_code, target_task_ids, comment, context_version, batch_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            batch_id,
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
    """이 Case의 후보에 대한 거절(후보마다 하나). 자유 텍스트는 인용 필드로. batch_id는 [모두 거절] 한 번으로
    함께 거절된 묶음이다(하나씩 거절은 None)."""
    found = rows(
        conn,
        "SELECT d.candidate_id, d.reason_code, d.target_task_ids, d.comment, d.batch_id"
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
            "batch_id": r["batch_id"],
        }
        for r in found
    ]


def case_rejection_reasons(conn: sqlite3.Connection, case_id: str) -> list[dict[str, Any]]:
    """이 Case에서 사람이 남긴 거절 사유(Observation rejections). 사유 하나가 한 줄이다 (CV-26):
    [모두 거절]로 여러 안을 한 번에 거절한 것은 한 줄이고 candidate_ids에 그 안들이 모두 있다."""
    out: list[dict[str, Any]] = []
    by_batch: dict[str, dict[str, Any]] = {}
    for r in list_case_rejections(conn, case_id):
        batch = r.pop("batch_id")
        if batch is not None and batch in by_batch:
            by_batch[batch]["candidate_ids"].append(r["candidate_id"])
            continue
        entry = {**r, "candidate_ids": [r["candidate_id"]]}
        if batch is not None:
            by_batch[batch] = entry
        out.append(entry)
    return out


def chosen_by_case(conn: sqlite3.Connection, site_id: str) -> dict[str, str]:
    """Case마다 Supervisor가 마지막으로 고른 후보 (AG-29). Case = 그 후보를 만든 Run의 Case이고,
    만든 Run이 없는 후보(재확인)는 후보 자신이 키다."""
    out: dict[str, str] = {}
    for r in rows(
        conn,
        "SELECT d.candidate_id, a.case_id FROM decision d"
        " JOIN candidate c ON c.candidate_id = d.candidate_id"
        " LEFT JOIN solver_job j ON j.solver_result_id = c.solver_result_id"
        " LEFT JOIN agent_run a ON a.run_id = j.run_id"
        " WHERE d.site_id = ? AND d.type = 'CHOOSE' ORDER BY d.rowid",
        (site_id,),
    ):
        out[r["case_id"] or r["candidate_id"]] = r["candidate_id"]
    return out


def is_chosen(conn: sqlite3.Connection, site_id: str, candidate_id: str) -> bool:
    return candidate_id in chosen_by_case(conn, site_id).values()
