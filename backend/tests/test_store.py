import sqlite3
import threading
import time

import pytest

from app.store import db


def test_concurrent_increment_no_lost_update():
    with db.write() as tx:
        tx.execute("CREATE TABLE counter (id INTEGER PRIMARY KEY, value INTEGER NOT NULL)")
        tx.execute("INSERT INTO counter (id, value) VALUES (1, 0)")

    n = 50
    errors: list[Exception] = []

    def worker():
        try:
            for _ in range(n):
                with db.write() as tx:
                    value = tx.execute("SELECT value FROM counter WHERE id = 1").fetchone()[0]
                    time.sleep(0.001)  # 읽기와 쓰기 사이에 다른 스레드가 끼어들 틈을 준다
                    tx.execute("UPDATE counter SET value = ? WHERE id = 1", (value + 1,))
        except Exception as e:  # noqa: BLE001 — 스레드 예외를 메인 스레드로 전달
            errors.append(e)
        finally:
            db.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    with db.read() as conn:
        assert conn.execute("SELECT value FROM counter WHERE id = 1").fetchone()[0] == 2 * n


def test_nested_write_raises():
    with db.write(), pytest.raises(db.NestedTransactionError), db.write():
        pass


def test_nested_write_rolls_back_outer():
    with db.write() as tx:
        tx.execute("CREATE TABLE t (x INTEGER)")
    with pytest.raises(db.NestedTransactionError), db.write() as tx:
        tx.execute("INSERT INTO t VALUES (1)")
        with db.write():
            pass
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0


def test_foreign_keys_on():
    with db.read() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with db.write() as tx:
        tx.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        tx.execute(
            "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id))"
        )
    with pytest.raises(sqlite3.IntegrityError), db.write() as tx:
        tx.execute("INSERT INTO child (id, parent_id) VALUES (1, 999)")
