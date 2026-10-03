"""resource 조회."""

import sqlite3

from app.domain.models import Resource
from app.store.repos._rows import loads, rows


def list_resources(conn: sqlite3.Connection, site_id: str) -> list[Resource]:
    found = rows(conn, "SELECT * FROM resource WHERE site_id = ? ORDER BY resource_id", (site_id,))
    out = []
    for r in found:
        r.pop("site_id")
        for col in ("allowed_unit_ids", "allowed_zone_ids", "available_intervals", "attributes"):
            r[col] = loads(r[col])
        out.append(Resource(**r))
    return out
