"""schedule(일정 문서) 기록과 조회. 불변이다: 트리거로 UPDATE·DELETE가 막혀 있다 (ST-23)."""

import sqlite3
from typing import Any

from app.domain.canonical import canonical_json
from app.domain.models import TaskBase
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


def insert_task_base(
    tx: sqlite3.Connection,
    site_id: str,
    base: TaskBase,
    schedule_id: str | None = None,
    context_version: int = 0,
) -> None:
    """새 작업의 기준 위치 (CV-29). 일정으로 들어온 작업이면 어느 넣기에서 왔는지도 적는다 (ST-24).
    같은 작업에 다시 넣으면 그 행이 지금 값이 된다(카드에서 요청 시작 범위 고치기, AG-33)."""
    tx.execute(
        "INSERT INTO task_base (site_id, task_id, start_min, start_max, resource_id, origin,"
        " schedule_id, context_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            site_id,
            base.task_id,
            base.start,
            base.upper,
            base.resource_id,
            base.origin,
            schedule_id,
            context_version,
        ),
    )


def list_task_bases(conn: sqlite3.Connection, site_id: str) -> list[TaskBase]:
    """작업마다 지금의 기준 위치 (작업 ID순). 고친 기록이 있으면 마지막 것이다."""
    return [
        TaskBase(
            task_id=r["task_id"],
            start=r["start_min"],
            start_max=r["start_max"],
            resource_id=r["resource_id"],
            origin=r["origin"],
        )
        for r in rows(
            conn,
            "SELECT task_id, start_min, start_max, resource_id, origin FROM task_base"
            " WHERE site_id = ? AND base_id IN (SELECT MAX(base_id) FROM task_base"
            "  WHERE site_id = ? GROUP BY task_id) ORDER BY task_id",
            (site_id, site_id),
        )
    ]


def get_task_base(conn: sqlite3.Connection, site_id: str, task_id: str) -> dict[str, Any] | None:
    """그 작업의 지금 기준 위치 기록 {start, start_max, resource_id, origin, schedule_id}. 없으면 None."""
    found = rows(
        conn,
        "SELECT start_min AS start, start_max, resource_id, origin, schedule_id FROM task_base"
        " WHERE site_id = ? AND task_id = ? ORDER BY base_id DESC LIMIT 1",
        (site_id, task_id),
    )
    return found[0] if found else None


def base_range_changes(
    conn: sqlite3.Connection, site_id: str, since: int, until: int, task_ids: set[str]
) -> list[dict[str, Any]]:
    """현장 버전 since 뒤부터 until까지 요청 시작 범위가 고쳐진 작업 (작업 ID순, 사실 변경 표시용).

    before는 since 시점의 범위(그 뒤에 들어온 작업은 처음 요청한 범위), after는 until 시점의 범위다.
    둘이 같으면 내지 않는다. {kind, task_id, before, after}이고 범위는 {start, start_max, origin}이다."""
    history: dict[str, list[dict[str, Any]]] = {}
    for r in rows(
        conn,
        "SELECT task_id, start_min AS start, start_max, origin, context_version FROM task_base"
        " WHERE site_id = ? ORDER BY base_id",
        (site_id,),
    ):
        if r["task_id"] in task_ids and r["context_version"] <= until:
            history.setdefault(r["task_id"], []).append(r)
    out = []
    for task_id, found in sorted(history.items()):
        earlier = [r for r in found if r["context_version"] <= since]
        before, after = (earlier or found)[-1 if earlier else 0], found[-1]
        view = [{k: r[k] for k in ("start", "start_max", "origin")} for r in (before, after)]
        if view[0] != view[1]:
            out.append(
                {
                    "kind": "BASE_RANGE_CHANGED",
                    "task_id": task_id,
                    "before": view[0],
                    "after": view[1],
                }
            )
    return out


def schedule_of_tasks(conn: sqlite3.Connection, site_id: str) -> dict[str, str]:
    """작업 → 그 작업이 들어온 넣기(schedule_id). 일정으로 들어온 작업만 있다."""
    return {
        r["task_id"]: r["schedule_id"]
        for r in rows(
            conn,
            "SELECT task_id, schedule_id FROM task_base WHERE site_id = ?"
            " AND schedule_id IS NOT NULL",
            (site_id,),
        )
    }
