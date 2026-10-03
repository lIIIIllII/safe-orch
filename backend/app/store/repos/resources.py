"""resource 조회."""

import sqlite3

from app.domain.models import Resource
from app.store.repos._rows import loads, rows


def list_resources(conn: sqlite3.Connection, site_id: str) -> list[Resource]:
    found = rows(conn, "SELECT * FROM resource WHERE site_id = ? ORDER BY resource_id", (site_id,))
    out = []
    for r in found:
        r.pop("site_id")
        r["allowed_unit_ids"] = loads(r["allowed_unit_ids"])
        r["available_intervals"] = loads(r["available_intervals"])
        out.append(Resource(**r))
    return out
