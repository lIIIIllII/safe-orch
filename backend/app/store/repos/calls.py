"""전문 Agent 호출의 키와 사실 지문 (AG-27).

호출 키 = Agent 종류 + 참조. 사실 지문 = 그 호출에 관련된 사실의 hash다: 현장 버전(context, plan),
같은 키로 부른 Run들이 만든 후보(또는 참조한 후보)에 대한 모든 승인·거절, 그 Run·후보에 온 답, 그 Run이 낸
제안의 상태, 보낸 통지, ACTIVE Hold. 제약 없는 거절이나 담당자 답처럼 현장 버전을 올리지 않는 변화도
지문을 바꾼다. 하위 Run이 끝날 때의 지문을 사건에 적어 두고, 같은 키로 다시 부를 때 지금 지문과 비교한다.
"""

import sqlite3
from typing import Any

from app.domain.canonical import canonical_hash
from app.store.repos._rows import loads
from app.store.repos.site import get_site

# 결과가 없는 종료. 같은 키의 재호출을 막지 않는다
NO_RESULT = ("ERROR", "CANCELLED")


def call_key(agent_type: str, refs: dict[str, Any]) -> str:
    """Agent 종류와 참조로 만든 키. 참조: Replanning group_id·acting_unit_id, Coordination
    phase·candidate_id(통지는 plan_revision, 사전 확인은 need_ids), Event Response event_id."""
    names = {
        "REPLANNING": ("group_id", "acting_unit_id"),
        "COORDINATION": ("phase", "candidate_id", "plan_revision", "need_ids"),
        "EVENT_RESPONSE": ("event_id",),
    }[agent_type]

    def text(value: Any) -> str:
        return ",".join(sorted(value)) if isinstance(value, list) else str(value)

    return ":".join([agent_type, *(text(refs[n]) for n in names if refs.get(n) not in (None, []))])


def _column(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[Any]:
    return [r[0] for r in conn.execute(sql, params)]


def fingerprint(conn: sqlite3.Connection, site_id: str, key: str, candidate_id: str | None) -> str:
    site = get_site(conn, site_id)
    assert site is not None
    runs = _column(
        conn,
        "SELECT run_id FROM agent_run WHERE site_id = ?"
        " AND json_extract(input_ref, '$.call_key') = ? ORDER BY rowid",
        (site_id, key),
    )
    marks = ", ".join("?" for _ in runs) or "NULL"
    candidates = _column(
        conn,
        "SELECT c.candidate_id FROM solver_job j JOIN candidate c"
        f" ON c.solver_result_id = j.solver_result_id WHERE j.run_id IN ({marks}) ORDER BY c.rowid",
        tuple(runs),
    )
    if candidate_id is not None:
        candidates.append(candidate_id)
    cmarks = ", ".join("?" for _ in candidates) or "NULL"
    return canonical_hash(
        {
            "versions": [site.context_version, site.plan_revision],
            "decisions": _column(
                conn,
                # 승인·거절(제약 없는 거절 포함). 협의 항목 수용(WAIVE)은 후보를 바꾸지 않는다
                f"SELECT decision_id FROM decision WHERE candidate_id IN ({cmarks})"
                " AND type <> 'WAIVE' ORDER BY rowid",
                tuple(candidates),
            ),
            "answers": _column(
                conn,
                "SELECT message_id FROM message WHERE status = 'ANSWERED'"
                f" AND (run_id IN ({marks}) OR candidate_id IN ({cmarks})) ORDER BY rowid",
                (*runs, *candidates),
            ),
            "proposals": _column(
                conn,
                f"SELECT proposal_id || ':' || status FROM proposal WHERE run_id IN ({marks})"
                " ORDER BY rowid",
                tuple(runs),
            ),
            "notices": _column(
                conn,
                f"SELECT message_id FROM message WHERE type = 'NOTICE' AND run_id IN ({marks})"
                " ORDER BY rowid",
                tuple(runs),
            ),
            "holds": _column(
                conn,
                "SELECT hold_id FROM hold WHERE site_id = ? AND status = 'ACTIVE' ORDER BY rowid",
                (site_id,),
            ),
        }
    )


def last_result(conn: sqlite3.Connection, site_id: str, key: str) -> dict[str, Any] | None:
    """같은 키로 부른 마지막 Run(현장 전체, Case 무관)과 그 Run이 끝날 때의 지문.

    결과 없이 끝난 Run(ERROR·취소)과 아직 열린 Run은 건너뛴다. {run_id, status, end_reason,
    end_fingerprint}. end_fingerprint는 하위 Run 종료 사건에 적힌 값이다(없으면 None).
    """
    row = conn.execute(
        "SELECT run_id, status, end_reason FROM agent_run WHERE site_id = ?"
        " AND json_extract(input_ref, '$.call_key') = ?"
        " AND status NOT IN ('RUNNING', 'WAITING_HUMAN', ?, ?) ORDER BY rowid DESC LIMIT 1",
        (site_id, key, *NO_RESULT),
    ).fetchone()
    if row is None:
        return None
    event = conn.execute(
        "SELECT ref FROM case_event WHERE site_id = ? AND dedupe_key = ?",
        (site_id, f"CHILD_RUN_ENDED:{row[0]}"),
    ).fetchone()
    ref = loads(event[0]) if event else {}
    return {
        "run_id": row[0],
        "status": row[1],
        "end_reason": row[2],
        "end_fingerprint": ref.get("fingerprint"),
    }
