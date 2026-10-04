"""task_pin(작업 고정)·preferred_window(희망 영역) 기록과 조회.

고정은 ACTIVE → RELEASED만, 희망 영역은 ACTIVE → CLEARED만(트리거). 작업당 ACTIVE는 하나다.
고정은 Snapshot에 들어가는 사실이고, 희망 영역은 Agent 관찰과 화면에만 보인다 (AG-27).
"""

import sqlite3
from typing import Any

from app.domain.models import Pin
from app.store.repos._rows import rows


def insert_pin(
    tx: sqlite3.Connection, site_id: str, pin: Pin, pinned_at: str, context_version: int
) -> None:
    tx.execute(
        "INSERT INTO task_pin (pin_id, site_id, task_id, status, pinned_by, by_role, pinned_at,"
        " created_context_version) VALUES (?, ?, ?, 'ACTIVE', ?, ?, ?, ?)",
        (pin.pin_id, site_id, pin.task_id, pin.pinned_by, pin.by_role, pinned_at, context_version),
    )


def release_pin(
    tx: sqlite3.Connection,
    site_id: str,
    pin_id: str,
    released_by: str,
    released_at: str,
    context_version: int,
) -> None:
    """ACTIVE인 고정 하나를 RELEASED로. 해당 행이 없으면 LookupError."""
    cur = tx.execute(
        "UPDATE task_pin SET status = 'RELEASED', released_by = ?, released_at = ?,"
        " released_context_version = ? WHERE site_id = ? AND pin_id = ? AND status = 'ACTIVE'",
        (released_by, released_at, context_version, site_id, pin_id),
    )
    if cur.rowcount != 1:
        raise LookupError(f"pin {pin_id} is not ACTIVE")


def list_active_pins(conn: sqlite3.Connection, site_id: str) -> list[Pin]:
    return [
        Pin(
            pin_id=r["pin_id"], task_id=r["task_id"], pinned_by=r["pinned_by"], by_role=r["by_role"]
        )
        for r in rows(
            conn,
            "SELECT pin_id, task_id, pinned_by, by_role FROM task_pin"
            " WHERE site_id = ? AND status = 'ACTIVE' ORDER BY rowid",
            (site_id,),
        )
    ]


def active_pin_views(conn: sqlite3.Connection, site_id: str) -> dict[str, dict[str, Any]]:
    """작업 → 걸린 고정(누가·언제). 화면용."""
    return {
        r["task_id"]: r
        for r in rows(
            conn,
            "SELECT pin_id, task_id, pinned_by, by_role, pinned_at FROM task_pin"
            " WHERE site_id = ? AND status = 'ACTIVE' ORDER BY rowid",
            (site_id,),
        )
    }


def clear_preferred_window(
    tx: sqlite3.Connection, site_id: str, task_id: str, cleared_by: str, cleared_at: str
) -> bool:
    """그 작업의 ACTIVE 희망 영역을 CLEARED로. 지운 것이 있으면 True."""
    cur = tx.execute(
        "UPDATE preferred_window SET status = 'CLEARED', cleared_by = ?, cleared_at = ?"
        " WHERE site_id = ? AND task_id = ? AND status = 'ACTIVE'",
        (cleared_by, cleared_at, site_id, task_id),
    )
    return cur.rowcount == 1


def insert_preferred_window(
    tx: sqlite3.Connection,
    site_id: str,
    window_id: str,
    task_id: str,
    start: int,
    end: int,
    set_by: str,
    set_at: str,
) -> None:
    tx.execute(
        "INSERT INTO preferred_window (window_id, site_id, task_id, start_min, end_min, status,"
        " set_by, set_at) VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, ?)",
        (window_id, site_id, task_id, start, end, set_by, set_at),
    )


def preferred_windows(conn: sqlite3.Connection, site_id: str) -> dict[str, dict[str, Any]]:
    """작업 → 희망 영역 {start, end, set_by, set_at}."""
    return {
        r["task_id"]: {
            "start": r["start_min"],
            "end": r["end_min"],
            "set_by": r["set_by"],
            "set_at": r["set_at"],
        }
        for r in rows(
            conn,
            "SELECT task_id, start_min, end_min, set_by, set_at FROM preferred_window"
            " WHERE site_id = ? AND status = 'ACTIVE' ORDER BY rowid",
            (site_id,),
        )
    }
