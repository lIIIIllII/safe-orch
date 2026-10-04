"""Pack → 초기 데이터 seed. write() 안에서 tx를 받아 호출한다."""

import sqlite3

from app.domain.canonical import canonical_hash
from app.domain.models import Snapshot
from app.packs.loader import LoadedPack
from app.store.repos._rows import dumps
from app.store.repos.records import insert_snapshot
from app.store.repos.snapshots import build_snapshot_content, seed_snapshot_id


class SeedError(RuntimeError):
    """site가 이미 있다. seed는 빈 DB에서만 한다."""


def seed_pack(tx: sqlite3.Connection, pack: LoadedPack) -> None:
    if tx.execute("SELECT COUNT(*) FROM site").fetchone()[0]:
        raise SeedError("site already seeded; reset the DB first")
    sid = pack.site_id
    tx.execute(
        "INSERT INTO site (site_id, pack_hash, horizon_start_utc, horizon_minutes,"
        " context_version, plan_revision) VALUES (?, ?, ?, ?, 0, 0)",
        (sid, pack.pack_hash, pack.horizon_start_utc, pack.horizon_minutes),
    )
    tx.executemany(
        "INSERT INTO work_unit (site_id, unit_id, name, unit_type) VALUES (?, ?, ?, ?)",
        [(sid, u.unit_id, u.name, u.unit_type) for u in pack.units],
    )
    tx.executemany(
        "INSERT INTO actor (site_id, actor_id, name, unit_id, roles) VALUES (?, ?, ?, ?, ?)",
        [(sid, a.actor_id, a.name, a.unit_id, dumps(list(a.roles))) for a in pack.actors],
    )
    tx.executemany(
        "INSERT INTO zone (site_id, zone_id) VALUES (?, ?)",
        [(sid, z.zone_id) for z in pack.zones],
    )
    # 로더가 ADJACENT를 이미 양방향으로 만들어 두었다.
    tx.executemany(
        "INSERT INTO zone_relation (site_id, zone_a, zone_b, relation) VALUES (?, ?, ?, ?)",
        [(sid, r.zone_a, r.zone_b, r.relation) for r in pack.zone_relations],
    )
    tx.executemany(
        "INSERT INTO resource (site_id, resource_id, display_name, resource_type, owner_unit_id,"
        " allowed_unit_ids, allowed_zone_ids, capacity, available_intervals, attributes,"
        " cost_per_hour, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                sid,
                r.resource_id,
                r.display_name,
                r.resource_type,
                r.owner_unit_id,
                dumps(list(r.allowed_unit_ids)),
                dumps(list(r.allowed_zone_ids)),
                r.capacity,
                dumps([list(iv) for iv in r.available_intervals]),
                dumps(r.model_dump(mode="json")["attributes"]),
                r.cost_per_hour,
                r.note,
            )
            for r in pack.resources
        ],
    )
    tx.executemany(
        "INSERT INTO pool (site_id, pool_id, kind, display_name, owner_unit_id, allowed_unit_ids,"
        " quantity, cost_per_hour) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                sid,
                p.pool_id,
                p.kind,
                p.display_name,
                p.owner_unit_id,
                dumps(list(p.allowed_unit_ids)),
                p.quantity,
                p.cost_per_hour,
            )
            for p in pack.pools
        ],
    )
    tx.executemany(
        "INSERT INTO task (site_id, task_id, revision, unit_id, owner_actor_id, work_type,"
        " zone_id, duration, earliest_start, latest_start, latest_end, required_resource_type,"
        " requested_resource_id, resource_requirements, pool_demands, predecessors, fields,"
        " lifecycle) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                sid,
                t.task_id,
                t.revision,
                t.unit_id,
                t.owner_actor_id,
                t.work_type,
                t.zone_id,
                t.duration,
                t.earliest_start,
                t.latest_start,
                t.latest_end,
                t.required_resource_type,
                t.requested_resource_id,
                dumps([r.model_dump() for r in t.resource_requirements]),
                dumps([d.model_dump() for d in t.pool_demands]),
                dumps([p.model_dump() for p in t.predecessors]),
                dumps({k: v.model_dump() for k, v in t.fields.items()}),
                t.lifecycle,
            )
            for t in pack.tasks
        ],
    )
    tx.execute(
        "INSERT INTO plan (site_id, plan_revision, assignments, candidate_id,"
        " committed_context_version) VALUES (?, 0, ?, NULL, 0)",
        (sid, dumps([a.model_dump() for a in pack.plan_r0])),
    )
    # R0를 확정할 때의 사실. 사실 변경 표시의 기준이다(뒤의 Plan은 그 후보의 Snapshot이 기준)
    content = build_snapshot_content(tx, sid, pack)
    insert_snapshot(
        tx,
        sid,
        Snapshot(
            snapshot_id=seed_snapshot_id(sid),
            snapshot_hash=canonical_hash(content),
            content=content,
        ),
    )
    tx.execute(
        "INSERT INTO audit (site_id, command, actor_id, before_context_version,"
        " after_context_version, before_plan_revision, after_plan_revision, reason_code, payload)"
        " VALUES (?, 'SEED', NULL, 0, 0, 0, 0, NULL, ?)",
        (sid, dumps({"pack": pack.name, "pack_hash": pack.pack_hash})),
    )
