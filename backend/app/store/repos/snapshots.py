"""Snapshot 생성·저장 (부록 A.10). content는 canonical JSON, snapshot_hash = canonical_hash(content)."""

import sqlite3
from typing import Any

from app.domain.canonical import canonical_hash
from app.domain.ids import new_id
from app.domain.models import Snapshot, SnapshotContent
from app.packs.loader import LoadedPack
from app.store.repos._rows import rows
from app.store.repos.plans import get_current_plan
from app.store.repos.records import insert_snapshot
from app.store.repos.resources import list_resources
from app.store.repos.site import get_site, list_zone_relations
from app.store.repos.tasks import list_current_tasks


def build_snapshot_content(
    conn: sqlite3.Connection, site_id: str, pack: LoadedPack
) -> dict[str, Any]:
    site = get_site(conn, site_id)
    if site is None:
        raise LookupError(f"site {site_id} not found")
    plan = get_current_plan(conn, site_id)
    zones = [
        r["zone_id"]
        for r in rows(
            conn, "SELECT zone_id FROM zone WHERE site_id = ? ORDER BY zone_id", (site_id,)
        )
    ]
    content = SnapshotContent(
        site_id=site.site_id,
        pack_hash=site.pack_hash,
        horizon_minutes=site.horizon_minutes,
        context_version=site.context_version,
        plan_revision=site.plan_revision,
        tasks=tuple(t for t in list_current_tasks(conn, site_id, pack) if t.lifecycle == "READY"),
        resources=tuple(list_resources(conn, site_id)),
        zones=tuple(zones),
        zone_relations=tuple(list_zone_relations(conn, site_id)),
        plan={"plan_revision": plan.plan_revision, "assignments": plan.assignments},
        holds=(),  # 해당 테이블이 생기면 채운다
        constraints=(),
        consents=(),
    )
    return content.model_dump(mode="json")


def create_snapshot(tx: sqlite3.Connection, site_id: str, pack: LoadedPack) -> Snapshot:
    content = build_snapshot_content(tx, site_id, pack)
    snapshot = Snapshot(
        snapshot_id=new_id("snap"), snapshot_hash=canonical_hash(content), content=content
    )
    insert_snapshot(tx, site_id, snapshot)
    return snapshot
