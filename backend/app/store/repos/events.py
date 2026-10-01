"""event·hold 기록과 조회 (설계서 §10, 부록 A.14). event는 불변, hold는 ACTIVE → RELEASED만."""

import sqlite3
from typing import Any

from app.domain.models import HoldRef
from app.store.repos._rows import rows


def get_event_by_source(
    conn: sqlite3.Connection, site_id: str, source_event_id: str
) -> dict[str, Any] | None:
    found = rows(
        conn,
        "SELECT e.*, h.hold_id FROM event e JOIN hold h ON h.event_id = e.event_id"
        " WHERE e.site_id = ? AND e.source_event_id = ?",
        (site_id, source_event_id),
    )
    return found[0] if found else None


def insert_event(
    tx: sqlite3.Connection,
    site_id: str,
    event_id: str,
    source_event_id: str,
    event_type: str,
    reporter_actor_id: str,
    text: str,
    target_task_id: str | None,
    body_hash: str,
    context_version: int,
) -> None:
    tx.execute(
        "INSERT INTO event (event_id, site_id, source_event_id, event_type, reporter_actor_id,"
        " text, target_task_id, body_hash, context_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            site_id,
            source_event_id,
            event_type,
            reporter_actor_id,
            text,
            target_task_id,
            body_hash,
            context_version,
        ),
    )


def insert_hold(
    tx: sqlite3.Connection,
    site_id: str,
    hold_id: str,
    event_id: str,
    scope: str,
    task_id: str | None,
    context_version: int,
) -> None:
    tx.execute(
        "INSERT INTO hold (hold_id, site_id, event_id, scope, task_id, status,"
        " created_context_version) VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?)",
        (hold_id, site_id, event_id, scope, task_id, context_version),
    )


def get_hold(conn: sqlite3.Connection, site_id: str, hold_id: str) -> dict[str, Any] | None:
    found = rows(conn, "SELECT * FROM hold WHERE site_id = ? AND hold_id = ?", (site_id, hold_id))
    return found[0] if found else None


def list_active_holds(conn: sqlite3.Connection, site_id: str) -> list[HoldRef]:
    return [
        HoldRef(hold_id=r["hold_id"], scope=r["scope"], task_id=r["task_id"])
        for r in rows(
            conn,
            "SELECT hold_id, scope, task_id FROM hold WHERE site_id = ? AND status = 'ACTIVE'"
            " ORDER BY rowid",
            (site_id,),
        )
    ]


def release_hold(
    tx: sqlite3.Connection,
    site_id: str,
    hold_id: str,
    resolution: str,
    released_by: str,
    context_version: int,
) -> None:
    """ACTIVE인 Hold 하나를 RELEASED로. 해당 행이 없으면 LookupError."""
    cur = tx.execute(
        "UPDATE hold SET status = 'RELEASED', resolution = ?, released_by = ?,"
        " released_context_version = ? WHERE site_id = ? AND hold_id = ? AND status = 'ACTIVE'",
        (resolution, released_by, context_version, site_id, hold_id),
    )
    if cur.rowcount != 1:
        raise LookupError(f"hold {hold_id} is not ACTIVE")


def get_event(conn: sqlite3.Connection, site_id: str, event_id: str) -> dict[str, Any] | None:
    found = rows(
        conn, "SELECT * FROM event WHERE site_id = ? AND event_id = ?", (site_id, event_id)
    )
    return found[0] if found else None
