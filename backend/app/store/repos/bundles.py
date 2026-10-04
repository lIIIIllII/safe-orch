"""bundle_plan(일정 검토 Agent가 낸 묶음안) 기록과 조회. 불변이고 그때의 Snapshot에 묶인다 (ST-07)."""

import sqlite3
from typing import Any

from app.store.repos._rows import dumps, loads, rows


def insert_bundle_plan(
    tx: sqlite3.Connection,
    site_id: str,
    bundle_plan_id: str,
    snapshot_id: str,
    case_id: str,
    run_id: str,
    step_no: int,
    context_version: int,
    plan_revision: int,
    content: dict[str, Any],
) -> None:
    """content: {groups, relations, bundles, quoted_opinion}. 묶음과 관계는 서버 값, 메모와 의견은 모델
    문장이다."""
    tx.execute(
        "INSERT INTO bundle_plan (bundle_plan_id, site_id, snapshot_id, case_id, run_id, step_no,"
        " context_version, plan_revision, content) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            bundle_plan_id,
            site_id,
            snapshot_id,
            case_id,
            run_id,
            step_no,
            context_version,
            plan_revision,
            dumps(content),
        ),
    )


def list_bundle_plans(
    conn: sqlite3.Connection,
    site_id: str,
    *,
    case_id: str | None = None,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """묶음안 (만든 순서). Case나 Run으로 거른다. content의 키를 펼쳐 돌려준다."""
    sql = "SELECT * FROM bundle_plan WHERE site_id = ?"
    params: tuple[Any, ...] = (site_id,)
    for column, value in (("case_id", case_id), ("run_id", run_id)):
        if value is not None:
            sql += f" AND {column} = ?"
            params += (value,)
    out = []
    for r in rows(conn, sql + " ORDER BY rowid", params):
        out.append({**{k: v for k, v in r.items() if k != "content"}, **loads(r["content"])})
    return out


def get_bundle_plan(
    conn: sqlite3.Connection, site_id: str, bundle_plan_id: str
) -> dict[str, Any] | None:
    found = rows(
        conn,
        "SELECT * FROM bundle_plan WHERE site_id = ? AND bundle_plan_id = ?",
        (site_id, bundle_plan_id),
    )
    if not found:
        return None
    r = found[0]
    return {**{k: v for k, v in r.items() if k != "content"}, **loads(r["content"])}
