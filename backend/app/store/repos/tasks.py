"""task 조회 (부록 A.6). 현재 revision = MAX(revision). hazard_tags는 Pack에서 도출한다."""

import sqlite3

from app.domain.models import Task
from app.packs.loader import LoadedPack
from app.store.repos._rows import loads, rows

JSON_COLUMNS = ("predecessors", "movable", "fields")


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
        tasks.append(Task(**r, hazard_tags=pack.hazard_tags(r["work_type"])))
    return tasks
