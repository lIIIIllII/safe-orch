"""plan 조회 (부록 A.6). 현재 plan = site.plan_revision의 plan."""

import sqlite3

from app.domain.models import Plan
from app.store.repos._rows import loads, rows


def get_current_plan(conn: sqlite3.Connection, site_id: str) -> Plan | None:
    found = rows(
        conn,
        "SELECT p.plan_revision, p.assignments, p.candidate_id, p.committed_context_version"
        " FROM plan p JOIN site s ON s.site_id = p.site_id AND s.plan_revision = p.plan_revision"
        " WHERE p.site_id = ?",
        (site_id,),
    )
    if not found:
        return None
    r = found[0]
    r["assignments"] = loads(r["assignments"])
    return Plan(**r)
