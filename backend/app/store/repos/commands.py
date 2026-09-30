"""command_result(멱등 결과)와 audit 기록 (설계서 §5.4·§11.3-6, 부록 A.14)."""

import sqlite3
from typing import Any

from app.store.repos._rows import dumps, loads, rows


def get_command_result(conn: sqlite3.Connection, idempotency_key: str) -> dict[str, Any] | None:
    found = rows(conn, "SELECT * FROM command_result WHERE idempotency_key = ?", (idempotency_key,))
    if not found:
        return None
    r = found[0]
    for col in ("reason_codes", "result_refs", "response"):
        r[col] = loads(r[col])
    return r


def insert_command_result(
    tx: sqlite3.Connection,
    site_id: str,
    idempotency_key: str,
    command_type: str,
    actor_id: str,
    request_hash: str,
    status: str,
    reason_codes: list[str],
    result_refs: dict[str, Any],
    response: dict[str, Any],
) -> None:
    tx.execute(
        "INSERT INTO command_result (idempotency_key, site_id, command_type, actor_id,"
        " request_hash, status, reason_codes, result_refs, response)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            idempotency_key,
            site_id,
            command_type,
            actor_id,
            request_hash,
            status,
            dumps(reason_codes),
            dumps(result_refs),
            dumps(response),
        ),
    )


def insert_audit(
    tx: sqlite3.Connection,
    site_id: str,
    command: str,
    actor_id: str | None,
    before: tuple[int, int],
    after: tuple[int, int],
    reason_code: str | None,
    payload: dict[str, Any],
) -> None:
    """before·after = (context_version, plan_revision)."""
    tx.execute(
        "INSERT INTO audit (site_id, command, actor_id, before_context_version,"
        " after_context_version, before_plan_revision, after_plan_revision, reason_code, payload)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            site_id,
            command,
            actor_id,
            before[0],
            after[0],
            before[1],
            after[1],
            reason_code,
            dumps(payload),
        ),
    )
