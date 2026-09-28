"""SQLite 연결과 트랜잭션 (설계서 §5.4).

- 연결은 스레드별. isolation_level=None으로 자동 트랜잭션을 끄고 명시적으로 연다.
- 모든 쓰기는 write() 안에서만. BEGIN IMMEDIATE가 site lock 역할을 한다.
- 트랜잭션 중첩 금지: 하위 함수에는 tx(연결)를 인자로 넘긴다.
"""

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.config import get_settings

SCHEMA_VERSION = 1
SCHEMA_FILE = Path(__file__).with_name("schema.sql")

_local = threading.local()


class NestedTransactionError(RuntimeError):
    """이미 열린 트랜잭션 안에서 write()를 다시 호출했다."""


class StoreBusyError(RuntimeError):
    """잠금 대기가 timeout을 넘었다. 클라이언트는 같은 멱등 키로 재시도한다 (RETRYABLE_ERROR)."""


class SchemaVersionMismatchError(RuntimeError):
    """DB의 schema_version이 코드와 다르다. 자동 초기화하지 않고 기동을 멈춘다."""


def connect() -> sqlite3.Connection:
    """현재 스레드의 연결을 반환한다. 없거나 DB_PATH가 바뀌었으면 새로 연다."""
    path = get_settings().db_path
    conn: sqlite3.Connection | None = getattr(_local, "conn", None)
    if conn is not None and getattr(_local, "path", None) == path:
        return conn
    close()
    conn = sqlite3.connect(path, isolation_level=None, timeout=5)
    conn.execute("PRAGMA foreign_keys = ON")
    _local.conn = conn
    _local.path = path
    return conn


def close() -> None:
    """현재 스레드의 연결을 닫는다."""
    conn: sqlite3.Connection | None = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
    _local.conn = None
    _local.path = None


@contextmanager
def write() -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ~ COMMIT/ROLLBACK. 중첩 호출은 NestedTransactionError."""
    conn = connect()
    if conn.in_transaction:
        raise NestedTransactionError("write() called inside an open transaction; pass tx instead")
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as e:
        if "locked" in str(e) or "busy" in str(e):
            raise StoreBusyError(str(e)) from e
        raise
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def read() -> Iterator[sqlite3.Connection]:
    """읽기용 연결. 쓰기는 write()에서만 한다."""
    yield connect()


def get_schema_version(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT schema_version FROM schema_meta").fetchone()
    return None if row is None else row[0]


def init_db() -> None:
    """schema.sql을 적용하고 schema_version을 확인한다."""
    conn = connect()
    conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
    with write() as tx:
        version = get_schema_version(tx)
        if version is None:
            tx.execute("INSERT INTO schema_meta (schema_version) VALUES (?)", (SCHEMA_VERSION,))
            return
    if version != SCHEMA_VERSION:
        raise SchemaVersionMismatchError(f"DB schema_version={version}, expected {SCHEMA_VERSION}")
