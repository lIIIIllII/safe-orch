"""repos 공용: 조회 결과를 dict로 읽는다. 연결의 row_factory는 바꾸지 않는다."""

import json
import sqlite3
from typing import Any


def rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cur = conn.execute(sql, params)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def loads(text: str | None) -> Any:
    return None if text is None else json.loads(text)
