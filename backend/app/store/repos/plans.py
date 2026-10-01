"""plan 조회 (부록 A.6). 현재 plan = site.plan_revision의 plan."""

import sqlite3

from app.domain.models import Plan
from app.store.repos._rows import dumps, loads, rows


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


def get_plan_by_candidate(conn: sqlite3.Connection, site_id: str, candidate_id: str) -> Plan | None:
    found = rows(
        conn,
        "SELECT plan_revision, assignments, candidate_id, committed_context_version FROM plan"
        " WHERE site_id = ? AND candidate_id = ?",
        (site_id, candidate_id),
    )
    if not found:
        return None
    r = found[0]
    r["assignments"] = loads(r["assignments"])
    return Plan(**r)


def get_plan(conn: sqlite3.Connection, site_id: str, plan_revision: int) -> Plan | None:
    found = rows(
        conn,
        "SELECT plan_revision, assignments, candidate_id, committed_context_version FROM plan"
        " WHERE site_id = ? AND plan_revision = ?",
        (site_id, plan_revision),
    )
    if not found:
        return None
    r = found[0]
    r["assignments"] = loads(r["assignments"])
    return Plan(**r)


def insert_plan(tx: sqlite3.Connection, site_id: str, plan: Plan) -> None:
    tx.execute(
        "INSERT INTO plan (site_id, plan_revision, assignments, candidate_id,"
        " committed_context_version) VALUES (?, ?, ?, ?, ?)",
        (
            site_id,
            plan.plan_revision,
            dumps([a.model_dump() for a in plan.assignments]),
            plan.candidate_id,
            plan.committed_context_version,
        ),
    )
