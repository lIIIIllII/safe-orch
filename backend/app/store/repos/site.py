"""site·구역 관계 조회 (부록 A.6). conn을 인자로 받고 트랜잭션을 열지 않는다."""

import sqlite3

from app.domain.models import Site, ZoneRelation
from app.packs.loader import LoadedPack
from app.store.repos._rows import rows


class PackMismatchError(RuntimeError):
    """seed된 site의 pack_hash(또는 site_id)가 로드한 Pack과 다르다. 자동 reset하지 않는다."""


def get_site(conn: sqlite3.Connection, site_id: str) -> Site | None:
    found = rows(conn, "SELECT * FROM site WHERE site_id = ?", (site_id,))
    return Site(**found[0]) if found else None


def ensure_pack_matches(conn: sqlite3.Connection, pack: LoadedPack) -> None:
    """seed된 site가 있으면 Pack과 같은지 확인한다. 비어 있으면 통과."""
    for s in rows(conn, "SELECT site_id, pack_hash FROM site"):
        if s["site_id"] != pack.site_id or s["pack_hash"] != pack.pack_hash:
            raise PackMismatchError(
                f"DB site {s['site_id']} pack_hash={s['pack_hash']} != "
                f"pack {pack.name} site {pack.site_id} pack_hash={pack.pack_hash}; "
                "reset the DB (scripts.reset_db) to change the pack"
            )


def list_zone_relations(conn: sqlite3.Connection, site_id: str) -> list[ZoneRelation]:
    return [
        ZoneRelation(**{k: r[k] for k in ("zone_a", "zone_b", "relation")})
        for r in rows(
            conn,
            "SELECT zone_a, zone_b, relation FROM zone_relation WHERE site_id = ?"
            " ORDER BY zone_a, zone_b",
            (site_id,),
        )
    ]


def bump_context_version(tx: sqlite3.Connection, site_id: str) -> int:
    """context_version += 1 (§5.2). 새 값을 반환한다. write() 안에서만 호출한다."""
    row = tx.execute(
        "UPDATE site SET context_version = context_version + 1 WHERE site_id = ?"
        " RETURNING context_version",
        (site_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"site {site_id} not found")
    return row[0]
