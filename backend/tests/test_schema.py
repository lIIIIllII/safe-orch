"""schema 제약·불변 트리거(T51)와 init_db."""

import json
import sqlite3

import pytest

from app.store import db

IMMUTABLE = ("snapshot", "search_spec", "solver_result", "candidate", "validation", "audit")


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _insert_chain(tx: sqlite3.Connection, site_id: str) -> None:
    """snapshot → search_spec → solver_result → candidate → validation 한 줄."""
    tx.execute(
        "INSERT INTO snapshot VALUES ('snap1', ?, 'h-snap', ?)", (site_id, json.dumps({"k": 1}))
    )
    tx.execute(
        "INSERT INTO search_spec VALUES ('ss1', ?, 'h-ss', 'snap1', 'L0', '{}', '{}', '{}', 'CHANGE_FIRST', 10, 'k-ss')",
        (site_id,),
    )
    tx.execute(
        "INSERT INTO solver_result VALUES ('sr1', ?, 'ss1', '{\"status\":\"OPTIMAL\"}', NULL, 1)",
        (site_id,),
    )
    tx.execute(
        "INSERT INTO candidate VALUES"
        " ('cand1', ?, 'snap1', 'ss1', 'h-ss', 'sr1', 0, 0, 'ph', '[]', 'h-cand', 'REPLAN', NULL)",
        (site_id,),
    )
    tx.execute("INSERT INTO validation VALUES ('val1', ?, 'cand1', 'PASS', '[]')", (site_id,))


# ── init_db ────────────────────────────────────────────────


def test_init_db_creates_v5_tables(temp_db):
    with db.read() as conn:
        assert db.get_schema_version(conn) == db.SCHEMA_VERSION == 25
        assert {
            "schema_meta",
            "site",
            "work_unit",
            "actor",
            "zone",
            "zone_relation",
            "resource",
            "pool",
            "task",
            "plan",
            *IMMUTABLE,
            "command_result",
            "decision",
            "task_pin",
            "schedule",
            "task_base",
            "bundle_plan",
            "event",
            "hold",
            "consultation",
            "case_event",
            "dispatch_job",
            "agent_run",
            "agent_step",
            "solver_job",
        } <= _tables(conn)


def test_init_db_is_idempotent_on_current_version():
    db.init_db()
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM schema_meta").fetchone()[0] == 1


def test_init_db_rejects_v1_db_without_writing(tmp_path, use_db_path):
    path = use_db_path(tmp_path / "v1.db")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE schema_meta (schema_version INTEGER NOT NULL)")
    old.execute("INSERT INTO schema_meta VALUES (1)")
    old.commit()
    old.close()

    with pytest.raises(db.SchemaVersionMismatchError, match="schema_version=1"):
        db.init_db()
    with db.read() as conn:
        assert _tables(conn) == {"schema_meta"}  # 새 테이블이 생기지 않는다
        assert db.get_schema_version(conn) == 1


def test_init_db_rejects_non_empty_db_without_schema_meta(tmp_path, use_db_path):
    path = use_db_path(tmp_path / "other.db")
    other = sqlite3.connect(path)
    other.execute("CREATE TABLE foo (x INTEGER)")
    other.commit()
    other.close()

    with pytest.raises(db.SchemaVersionMismatchError):
        db.init_db()
    with db.read() as conn:
        assert _tables(conn) == {"foo"}


def test_init_db_failure_leaves_zero_tables(tmp_path, use_db_path, monkeypatch):
    broken = tmp_path / "schema.sql"
    broken.write_text(
        db.SCHEMA_FILE.read_text(encoding="utf-8") + "\nCREATE TABLE broken (;\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(db, "SCHEMA_FILE", broken)
    use_db_path(tmp_path / "fresh.db")

    with pytest.raises(sqlite3.OperationalError):
        db.init_db()
    with db.read() as conn:
        assert not conn.in_transaction
        assert _tables(conn) == set()


# ── 제약 ───────────────────────────────────────────────────


def test_resource_capacity_must_be_1(seeded):
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"), db.write() as tx:
        tx.execute(
            "INSERT INTO resource VALUES (?, 'X-CR-02', 'X', 'CRANE', 'UA', '[\"UA\"]', '[\"*\"]', 2,"
            " '[[0,180]]', '{}', NULL, '')",
            (seeded.site_id,),
        )


def test_validation_status_stale_rejected(seeded):
    with db.write() as tx:
        _insert_chain(tx, seeded.site_id)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"), db.write() as tx:
        tx.execute(
            "INSERT INTO validation VALUES ('val2', ?, 'cand1', 'STALE', '[]')", (seeded.site_id,)
        )


def test_replan_candidate_requires_search_spec_and_result(seeded):
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"), db.write() as tx:
        tx.execute("INSERT INTO snapshot VALUES ('snap1', ?, 'h', '{}')", (seeded.site_id,))
        tx.execute(
            "INSERT INTO candidate VALUES"
            " ('c', ?, 'snap1', NULL, NULL, NULL, 0, 0, 'ph', '[]', 'h', 'REPLAN', NULL)",
            (seeded.site_id,),
        )


def test_plan_candidate_null_only_for_r0(seeded):
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"), db.write() as tx:
        tx.execute("INSERT INTO plan VALUES (?, 1, '[]', NULL, 0)", (seeded.site_id,))


def test_task_has_no_hazard_tags_column(temp_db):
    with db.read() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(task)")}
    assert "hazard_tags" not in cols


def test_zone_relation_rejects_same(seeded):
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"), db.write() as tx:
        tx.execute("INSERT INTO zone_relation VALUES (?, 'B', 'C', 'SAME')", (seeded.site_id,))


# ── T51 불변 테이블 ────────────────────────────────────────────


@pytest.mark.parametrize("table", IMMUTABLE)
@pytest.mark.parametrize("op", ["UPDATE", "DELETE"])
def test_immutable_tables_reject_update_and_delete(seeded, table, op):
    with db.write() as tx:
        _insert_chain(tx, seeded.site_id)
    sql = f"UPDATE {table} SET site_id = site_id" if op == "UPDATE" else f"DELETE FROM {table}"
    with pytest.raises(sqlite3.IntegrityError, match=f"immutable: {table}"), db.write() as tx:
        tx.execute(sql)
    with db.read() as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] >= 1
