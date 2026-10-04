"""case_event 기록과 조회.

사건이 생기는 트랜잭션 안에서 적는다. 같은 dedupe_key는 한 번만 들어간다. 처리 상태는 저장하지 않는다.
Case는 주어진 값, 없으면 열린 Case(열린 Case Agent Run)의 것, 그것도 없으면 새 Case다.
"""

import sqlite3
from typing import Any

from app.domain.ids import new_id
from app.store.repos._rows import dumps, loads, rows
from app.store.repos.runs import CASE_AGENT_TYPES
from app.store.repos.site import get_site

KINDS = (
    "TASK_READY",
    "EVENT_REPORTED",
    "HOLD_RELEASED",
    "CANDIDATE_DECIDED",
    "CHILD_RUN_ENDED",
    "TASK_REQUEST_WITHDRAWN",
    "TASK_PINNED",
    "TASK_UNPINNED",
    "CANDIDATE_CHOSEN",
    "TASK_MOVED",
    "TASK_REMOVED",
    "TASK_EDITED",
    "SCHEDULE_IMPORTED",
)


def open_case_id(conn: sqlite3.Connection, site_id: str) -> str | None:
    """열린 Case의 case_id (가장 먼저 열린 것). 없으면 None."""
    marks = ", ".join("?" for _ in CASE_AGENT_TYPES)
    row = conn.execute(
        f"SELECT case_id FROM agent_run WHERE site_id = ? AND agent_type IN ({marks})"
        " AND status IN ('RUNNING', 'WAITING_HUMAN') ORDER BY rowid LIMIT 1",
        (site_id, *CASE_AGENT_TYPES),
    ).fetchone()
    return None if row is None else row[0]


def record_case_event(
    tx: sqlite3.Connection,
    site_id: str,
    kind: str,
    dedupe_key: str,
    ref: dict[str, Any],
    case_id: str | None = None,
) -> bool:
    """새로 적었으면 True, 같은 dedupe_key가 이미 있으면 False."""
    site = get_site(tx, site_id)
    assert site is not None
    cur = tx.execute(
        "INSERT INTO case_event (site_id, kind, ref, case_id, dedupe_key, created_context_version)"
        " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        (
            site_id,
            kind,
            dumps(ref),
            case_id or open_case_id(tx, site_id) or new_id("case"),
            dedupe_key,
            site.context_version,
        ),
    )
    return cur.rowcount == 1


def case_of(conn: sqlite3.Connection, site_id: str, dedupe_key: str) -> str | None:
    """그 사건이 들어간 Case."""
    row = conn.execute(
        "SELECT case_id FROM case_event WHERE site_id = ? AND dedupe_key = ?", (site_id, dedupe_key)
    ).fetchone()
    return None if row is None else row[0]


def list_case_events(conn: sqlite3.Connection, site_id: str) -> list[dict[str, Any]]:
    found = rows(conn, "SELECT * FROM case_event WHERE site_id = ? ORDER BY seq", (site_id,))
    for r in found:
        r["ref"] = loads(r["ref"])
    return found
