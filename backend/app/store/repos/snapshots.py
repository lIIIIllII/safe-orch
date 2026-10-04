"""Snapshot 생성·저장. content는 canonical JSON, snapshot_hash = canonical_hash(content)."""

import sqlite3
from typing import Any

from app.domain.canonical import canonical_hash
from app.domain.ids import new_id
from app.domain.models import Snapshot, SnapshotContent
from app.packs.loader import LoadedPack
from app.store.repos._rows import rows
from app.store.repos.consents import list_current_consents
from app.store.repos.events import list_active_holds
from app.store.repos.pins import list_active_pins, list_preferred_windows
from app.store.repos.plans import get_current_plan, get_plan
from app.store.repos.records import get_candidate, get_snapshot, insert_snapshot
from app.store.repos.resources import list_pools, list_resources
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
    tasks = tuple(t for t in list_current_tasks(conn, site_id, pack) if t.lifecycle == "READY")
    ready = {t.task_id for t in tasks}
    content = SnapshotContent(
        site_id=site.site_id,
        pack_hash=site.pack_hash,
        horizon_minutes=site.horizon_minutes,
        work_intervals=pack.work_intervals,  # Pack에서 (pack_hash로 고정)
        context_version=site.context_version,
        plan_revision=site.plan_revision,
        tasks=tasks,
        resources=tuple(list_resources(conn, site_id)),
        pools=tuple(list_pools(conn, site_id)),
        zones=tuple(zones),
        zone_relations=tuple(list_zone_relations(conn, site_id)),
        plan={"plan_revision": plan.plan_revision, "assignments": plan.assignments},
        holds=tuple(list_active_holds(conn, site_id)),
        pins=tuple(list_active_pins(conn, site_id)),
        consents=tuple(list_current_consents(conn, site_id)),
        # 희망 영역은 계산에 들어가는 사실이다 (ST-22). READY 작업의 것만 넣는다
        preferred_windows=tuple(
            w for w in list_preferred_windows(conn, site_id) if w.task_id in ready
        ),
    )
    return content.model_dump(mode="json")


def create_snapshot(tx: sqlite3.Connection, site_id: str, pack: LoadedPack) -> Snapshot:
    content = build_snapshot_content(tx, site_id, pack)
    snapshot = Snapshot(
        snapshot_id=new_id("snap"), snapshot_hash=canonical_hash(content), content=content
    )
    insert_snapshot(tx, site_id, snapshot)
    return snapshot


def seed_snapshot_id(site_id: str) -> str:
    """seed 때 만든 Snapshot의 ID: R0를 확정할 때의 사실."""
    return f"snap_seed_{site_id}"


def plan_facts(
    conn: sqlite3.Connection, site_id: str, plan_revision: int
) -> SnapshotContent | None:
    """그 Plan을 확정할 때의 사실: 그 Plan이 된 후보의 Snapshot, R0는 seed 때의 Snapshot.
    사실 변경 표시의 기준이다."""
    plan = get_plan(conn, site_id, plan_revision)
    if plan is None:
        return None
    snapshot_id = seed_snapshot_id(site_id)
    if plan.candidate_id is not None:
        candidate = get_candidate(conn, site_id, plan.candidate_id)
        if candidate is None:
            return None
        snapshot_id = candidate.snapshot_id
    snapshot = get_snapshot(conn, snapshot_id)
    return None if snapshot is None else snapshot.facts()
