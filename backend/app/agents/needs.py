"""결과에 담긴 needs의 서버 검증 (AG-23). 참조가 가리키는 대상이 지금 사실에 있는지만 본다.

모양(종류별 참조)은 app.domain.needs가 검사한다. 여기서는 DB 사실과 대조한다: 작업·자원·풀·사람·
신고·후보·메시지가 있는지, 물은 상대가 그 Run의 상대인지.
순서는 보지 않는다.
"""

import sqlite3
from typing import Any

from app.domain.models import AgentRun
from app.domain.needs import FACT_TARGET, Need, Path
from app.packs.loader import LoadedPack
from app.store.repos.events import get_event
from app.store.repos.messages import get_message
from app.store.repos.records import get_candidate
from app.store.repos.resources import list_pools, list_resources
from app.store.repos.site import list_actors
from app.store.repos.tasks import list_current_tasks

NEED_INVALID = "NEED_INVALID"


class _Facts:
    """검증에 쓰는 현재 사실. 필요한 것만 한 번씩 읽는다."""

    def __init__(self, conn: sqlite3.Connection, pack: LoadedPack, run: AgentRun):
        self.conn, self.pack, self.run = conn, pack, run
        site_id = pack.site_id
        self.tasks = {
            t.task_id: t for t in list_current_tasks(conn, site_id, pack) if t.lifecycle == "READY"
        }
        self.resources = {r.resource_id: r for r in list_resources(conn, site_id)}
        self.pools = {p.pool_id for p in list_pools(conn, site_id)}
        self.actors = {a.actor_id for a in list_actors(conn, site_id)}

    def has_task(self, task_id: str) -> bool:
        """현재 계산 대상 작업, 또는 이 Intake Run이 접수 중인 작업."""
        own = self.run.agent_type == "INTAKE" and task_id == self.run.input_ref.get("task_id")
        return task_id in self.tasks or own


def _check(f: _Facts, need: Need) -> str | None:
    """need 하나의 거절 사유. 없으면 None."""
    run = f.run
    if need.kind == "FACT_CHANGE":
        target = FACT_TARGET[need.field or "WINDOW"]
        value = getattr(need, target)
        found = {
            "task_id": f.has_task(value),
            "resource_id": value in f.resources,
            "pool_id": value in f.pools,
        }[target]
        return None if found else "TARGET_NOT_FOUND"
    if need.kind == "HUMAN_INFO":
        if need.actor_id not in f.actors:
            return "ACTOR_NOT_FOUND"
        # 물은 상대는 그 Run의 상대다: Intake는 요청자, Event Response는 신고자
        if run.agent_type == "INTAKE":
            ref = run.input_ref
            ok = (need.actor_id, need.task_id) == (
                ref.get("requester_actor_id"),
                ref.get("task_id"),
            )
            return None if ok else "ACTOR_MISMATCH"
        if need.event_id is not None:
            event = get_event(f.conn, f.pack.site_id, need.event_id)
            if event is None:
                return "TARGET_NOT_FOUND"
            mismatch = run.agent_type == "EVENT_RESPONSE" and (
                need.event_id != run.input_ref.get("event_id")
                or need.actor_id != event["reporter_actor_id"]
            )
            return "ACTOR_MISMATCH" if mismatch else None
        return None if f.has_task(need.task_id or "") else "TASK_NOT_FOUND"
    # HUMAN_DECISION
    site_id = f.pack.site_id
    if need.candidate_id is not None:
        found = get_candidate(f.conn, site_id, need.candidate_id) is not None
    elif need.event_id is not None:
        found = get_event(f.conn, site_id, need.event_id) is not None
    else:
        found = get_message(f.conn, site_id, need.message_id or "") is not None
    return None if found else "TARGET_NOT_FOUND"


def invalid_needs(
    conn: sqlite3.Connection, pack: LoadedPack, run: AgentRun, paths: list[Path]
) -> list[dict[str, Any]]:
    """서버 검증에 걸린 need 목록 [{path, need, kind, reason}]. 비면 모두 유효하다."""
    if not paths:
        return []
    facts = _Facts(conn, pack, run)
    out = []
    for i, path in enumerate(paths):
        for j, need in enumerate(path.needs):
            reason = _check(facts, need)
            if reason is not None:
                out.append({"path": i, "need": j, "kind": need.kind, "reason": reason})
    return out
