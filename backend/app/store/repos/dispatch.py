"""dispatch_job 등록 (설계서 §5.1·§11.5, 부록 A.14). 처리(claim·완료)는 2단계 워커가 한다.

원인 도메인 트랜잭션 안에서 등록한다(I-18). 같은 dedupe_key는 한 번만 들어간다.
"""

import sqlite3
from typing import Any

from app.store.repos._rows import dumps, loads, rows


def register_job(
    tx: sqlite3.Connection,
    site_id: str,
    kind: str,
    dedupe_key: str,
    payload: dict[str, Any] | None = None,
    *,
    run_id: str | None = None,
    wait_generation: int | None = None,
) -> bool:
    """새로 등록했으면 True, 같은 dedupe_key(또는 PENDING RESUME)가 이미 있으면 False."""
    cur = tx.execute(
        "INSERT INTO dispatch_job (site_id, kind, run_id, wait_generation, payload, dedupe_key)"
        " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        (site_id, kind, run_id, wait_generation, dumps(payload or {}), dedupe_key),
    )
    return cur.rowcount == 1


def list_jobs(conn: sqlite3.Connection, site_id: str) -> list[dict[str, Any]]:
    found = rows(conn, "SELECT * FROM dispatch_job WHERE site_id = ? ORDER BY job_id", (site_id,))
    for r in found:
        r["payload"] = loads(r["payload"])
    return found
