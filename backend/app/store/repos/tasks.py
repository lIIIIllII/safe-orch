"""task 조회. 현재 revision = MAX(revision). hazard_tags와 기본 요구 조건은 Pack에서 도출한다."""

import sqlite3

from app.domain.models import Task
from app.packs.loader import LoadedPack
from app.store.repos._rows import dumps, loads, rows

JSON_COLUMNS = ("resource_requirements", "predecessors", "movable", "fields")


def list_current_tasks(conn: sqlite3.Connection, site_id: str, pack: LoadedPack) -> list[Task]:
    found = rows(
        conn,
        "SELECT t.* FROM task t"
        " WHERE t.site_id = ? AND t.revision ="
        "  (SELECT MAX(revision) FROM task WHERE site_id = t.site_id AND task_id = t.task_id)"
        " ORDER BY t.task_id",
        (site_id,),
    )
    tasks = []
    for r in found:
        r.pop("site_id")
        for col in JSON_COLUMNS:
            r[col] = loads(r[col])
        tasks.append(
            Task(
                **r,
                hazard_tags=pack.hazard_tags(r["work_type"]),
                default_requirements=pack.default_requirements(r["work_type"]),
            )
        )
    return tasks


def insert_task_revision(tx: sqlite3.Connection, site_id: str, task: Task) -> None:
    """작업 추가·변경 = 새 revision INSERT. revision은 현재 + 1이어야 한다.

    context_version 증가(bump_context_version)는 호출하는 명령이 같은 tx에서 한다.
    hazard_tags와 default_requirements는 저장하지 않는다.
    """
    current = tx.execute(
        "SELECT MAX(revision) FROM task WHERE site_id = ? AND task_id = ?", (site_id, task.task_id)
    ).fetchone()[0]
    expected = (current or 0) + 1
    if task.revision != expected:
        raise ValueError(f"task {task.task_id}: revision {task.revision} != expected {expected}")
    tx.execute(
        "INSERT INTO task (site_id, task_id, revision, unit_id, owner_actor_id, work_type,"
        " zone_id, duration, earliest_start, latest_start, latest_end, required_resource_type,"
        " requested_resource_id, resource_requirements, predecessors, movable, fields, lifecycle)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            site_id,
            task.task_id,
            task.revision,
            task.unit_id,
            task.owner_actor_id,
            task.work_type,
            task.zone_id,
            task.duration,
            task.earliest_start,
            task.latest_start,
            task.latest_end,
            task.required_resource_type,
            task.requested_resource_id,
            dumps([r.model_dump() for r in task.resource_requirements]),
            dumps([p.model_dump() for p in task.predecessors]),
            dumps(task.movable.model_dump()),
            dumps({k: v.model_dump() for k, v in task.fields.items()}),
            task.lifecycle,
        ),
    )
