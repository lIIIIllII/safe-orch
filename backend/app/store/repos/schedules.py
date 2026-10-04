"""schedule(일정 문서) 기록과 조회. 불변이다: 트리거로 UPDATE·DELETE가 막혀 있다 (ST-23)."""

import sqlite3
from typing import Any

from app.domain.canonical import canonical_json
from app.domain.schedule import content_hash
from app.store.repos._rows import loads, rows


def insert_schedule(
    tx: sqlite3.Connection,
    site_id: str,
    schedule_id: str,
    kind: str,
    plan_revision: int,
    actor_id: str,
    created_at: str,
    document: dict[str, Any],
) -> str:
    """문서를 정규화 JSON 원문으로 남긴다. kind: 꺼낸 것 EXPORT·넣은 것 IMPORT. 본문 hash를 돌려준다."""
    digest = content_hash(document)
    tx.execute(
        "INSERT INTO schedule (schedule_id, site_id, kind, content_hash, plan_revision, actor_id,"
        " created_at, content) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            schedule_id,
            site_id,
            kind,
            digest,
            plan_revision,
            actor_id,
            created_at,
            canonical_json(document).decode("utf-8"),
        ),
    )
    return digest


def get_schedule(conn: sqlite3.Connection, site_id: str, schedule_id: str) -> dict[str, Any] | None:
    """{schedule_id, kind, content_hash, plan_revision, actor_id, created_at, document}."""
    found = rows(
        conn,
        "SELECT schedule_id, kind, content_hash, plan_revision, actor_id, created_at, content"
        " FROM schedule WHERE site_id = ? AND schedule_id = ?",
        (site_id, schedule_id),
    )
    if not found:
        return None
    r = found[0]
    r["document"] = loads(r.pop("content"))
    return r
