"""consent 기록과 조회 (설계서 §5.1·§9.3, 부록 A.14). 불변이며 task revision에 묶인다."""

import sqlite3

from app.domain.models import Consent
from app.store.repos._rows import dumps, loads, rows


def insert_consent(
    tx: sqlite3.Connection, site_id: str, consent: Consent, context_version: int
) -> None:
    tx.execute(
        "INSERT INTO consent (consent_id, site_id, task_id, task_revision, owner_actor_id, axis,"
        " scope, source_ref, created_context_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            consent.consent_id,
            site_id,
            consent.task_id,
            consent.task_revision,
            consent.owner_actor_id,
            consent.axis,
            dumps(consent.scope),
            consent.source_ref,
            context_version,
        ),
    )


def list_current_consents(conn: sqlite3.Connection, site_id: str) -> list[Consent]:
    """각 작업의 현재 revision에 대한 Consent (§9.3-2)."""
    return [
        Consent(
            consent_id=r["consent_id"],
            task_id=r["task_id"],
            task_revision=r["task_revision"],
            owner_actor_id=r["owner_actor_id"],
            axis=r["axis"],
            scope=loads(r["scope"]),
            source_ref=r["source_ref"],
        )
        for r in rows(
            conn,
            "SELECT c.* FROM consent c WHERE c.site_id = ? AND c.task_revision ="
            " (SELECT MAX(revision) FROM task WHERE site_id = c.site_id AND task_id = c.task_id)"
            " ORDER BY c.task_id, c.rowid",
            (site_id,),
        )
    ]
