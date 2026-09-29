"""scripts.reset_db (부록 A.3). 테스트는 임시 DB 파일만 쓴다."""

from app.store import db
from scripts import reset_db


def _marker_exists() -> bool:
    with db.read() as conn:
        return (
            conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'marker'").fetchone()[0]
            == 1
        )


def _add_marker():
    with db.write() as tx:
        tx.execute("CREATE TABLE marker (x INTEGER)")


def test_wrong_phrase_keeps_file(temp_db):
    _add_marker()
    for answer in ("", "reset safe_orch", "RESET safe_orch ", "RESET"):
        assert reset_db.main([], input_fn=lambda _prompt, a=answer: a) == 1
        assert temp_db.exists()
        assert _marker_exists()


def test_correct_phrase_recreates_and_seeds(temp_db, pack):
    _add_marker()
    assert reset_db.main(["--pack", "shipyard"], input_fn=lambda _p: "RESET safe_orch") == 0
    assert not _marker_exists()
    with db.read() as conn:
        assert db.get_schema_version(conn) == db.SCHEMA_VERSION
        assert conn.execute("SELECT site_id, pack_hash FROM site").fetchall() == [
            (pack.site_id, pack.pack_hash)
        ]


def test_invalid_pack_does_not_prompt_or_delete(temp_db):
    _add_marker()

    def never(_prompt):
        raise AssertionError("must not prompt")

    assert reset_db.main(["--pack", "no_such_pack"], input_fn=never) == 1
    assert _marker_exists()
