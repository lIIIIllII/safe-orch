"""dispatch_job 등록과 처리 상태.

원인 도메인 트랜잭션 안에서 등록한다. 같은 dedupe_key는 한 번만 들어간다.
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


def job_exists(conn: sqlite3.Connection, site_id: str, dedupe_key: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM dispatch_job WHERE site_id = ? AND dedupe_key = ?", (site_id, dedupe_key)
        ).fetchone()
    )


def claim_next(
    tx: sqlite3.Connection, site_id: str, kinds: tuple[str, ...]
) -> dict[str, Any] | None:
    """kinds 중 job_id가 가장 작은 PENDING을 CLAIMED로 바꾸고 attempts += 1."""
    marks = ", ".join("?" for _ in kinds)
    found = rows(
        tx,
        "UPDATE dispatch_job SET status = 'CLAIMED', attempts = attempts + 1"
        " WHERE job_id = (SELECT job_id FROM dispatch_job WHERE site_id = ? AND status = 'PENDING'"
        f" AND kind IN ({marks}) ORDER BY job_id LIMIT 1) RETURNING *",
        (site_id, *kinds),
    )
    if not found:
        return None
    r = found[0]
    r["payload"] = loads(r["payload"])
    return r


def set_job_run(tx: sqlite3.Connection, job_id: int, run_id: str) -> None:
    tx.execute("UPDATE dispatch_job SET run_id = ? WHERE job_id = ?", (run_id, job_id))


def mark_done(tx: sqlite3.Connection, job_id: int) -> None:
    tx.execute("UPDATE dispatch_job SET status = 'DONE' WHERE job_id = ?", (job_id,))


def mark_failed_attempt(tx: sqlite3.Connection, job_id: int, error: str, max_attempts: int) -> str:
    """실패 기록. attempts < max_attempts면 PENDING(재시도), 아니면 FAILED. 새 status를 반환한다."""
    row = tx.execute(
        "UPDATE dispatch_job SET last_error = ?,"
        " status = CASE WHEN attempts < ? THEN 'PENDING' ELSE 'FAILED' END"
        " WHERE job_id = ? RETURNING status",
        (error, max_attempts, job_id),
    ).fetchone()
    return row[0]


def requeue_claimed(tx: sqlite3.Connection, site_id: str) -> int:
    """기동 시 CLAIMED로 남은 작업을 PENDING으로 되돌린다. 핸들러는 멱등이다."""
    cur = tx.execute(
        "UPDATE dispatch_job SET status = 'PENDING' WHERE site_id = ? AND status = 'CLAIMED'",
        (site_id,),
    )
    return cur.rowcount
