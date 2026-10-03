"""결과에 담긴 needs의 서버 검증 (AG-23). 참조가 가리키는 대상이 지금 사실에 있는지만 본다.

모양(종류별 참조)은 app.domain.needs가 검사한다. 여기서는 DB 사실과 대조한다: 작업·자원·풀·Unit·사람·
신고·후보·메시지가 있는지, 축이 제약으로 고정되지 않았는지, 자원 값이 적격이고 담당자가 거절한 값이
아닌지, Unit이 그 충돌 그룹에 작업을 가졌는지, 물은 상대가 그 Run의 상대인지. 순서는 보지 않는다.
"""

import sqlite3
from typing import Any

from app.domain.canonical import canonical_hash
from app.domain.eligibility import exclusion_reasons
from app.domain.groups import conflict_groups, movable_task_ids
from app.domain.models import AgentRun, Snapshot
from app.domain.needs import FACT_TARGET, Need, Path
from app.packs.loader import LoadedPack
from app.rules.engine import detect_conflicts
from app.store.repos.consents import list_current_consents
from app.store.repos.decisions import list_constraints
from app.store.repos.events import get_event
from app.store.repos.messages import declined_values, get_message
from app.store.repos.records import get_candidate
from app.store.repos.resources import list_pools, list_resources
from app.store.repos.site import list_actors
from app.store.repos.snapshots import build_snapshot_content
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
        self.frozen = {
            (c.task_id, axis) for c in list_constraints(conn, site_id) for axis in c.frozen_axes
        }
        self._group_units: dict[str, dict[str, bool]] | None = None

    def group_units(self) -> dict[str, dict[str, bool]]:
        """지금 충돌 그룹 → {그 그룹에 작업을 가진 Unit: 움직일 수 있는 작업이 있는가}."""
        if self._group_units is None:
            content = build_snapshot_content(self.conn, self.pack.site_id, self.pack)
            snapshot = Snapshot(
                snapshot_id="needs", snapshot_hash=canonical_hash(content), content=content
            )
            facts = snapshot.facts()
            conflicts = detect_conflicts(snapshot, facts.check_assignments(), self.pack)
            groups = conflict_groups(conflicts, facts.task_map())
            tasks = facts.task_map()
            self._group_units = {
                g.group_id: {
                    u: bool(movable_task_ids(g, u, tasks, facts.constraints)) for u in g.units
                }
                for g in groups
            }
        return self._group_units

    def declined(self, task_id: str) -> set[str]:
        """그 작업의 지금 revision에 담당자가 거절한 자원 값 (현장 전체, AG-09)."""
        task = self.tasks[task_id]
        return declined_values(self.conn, self.pack.site_id, task_id, task.revision)

    def has_task(self, task_id: str) -> bool:
        """현재 계산 대상 작업, 또는 이 Intake Run이 접수 중인 작업."""
        own = self.run.agent_type == "INTAKE" and task_id == self.run.input_ref.get("task_id")
        return task_id in self.tasks or own


def _check(f: _Facts, need: Need) -> str | None:
    """need 하나의 거절 사유. 없으면 None."""
    run = f.run
    if need.kind == "OWNER_CONSENT":
        task = f.tasks.get(need.task_id or "")
        if task is None:
            return "TASK_NOT_FOUND"
        if (task.task_id, need.axis) in f.frozen:
            return "AXIS_FROZEN"
        for rid in need.values:
            resource = f.resources.get(rid)
            if resource is None or exclusion_reasons(task, resource, task.unit_id):
                return "RESOURCE_NOT_ELIGIBLE"
        if set(need.values) & f.declined(task.task_id):
            return "VALUE_DECLINED"
        return None
    if need.kind == "OTHER_UNIT":
        units = f.group_units().get(need.group_id or "")
        if units is None:
            return "GROUP_NOT_FOUND"
        if need.unit_id == run.acting_unit_id:
            return "SAME_UNIT"
        if need.unit_id not in units:
            return "UNIT_NOT_IN_GROUP"
        # 움직일 수 있는 작업이 없는 Unit으로는 재계획해도 바뀌는 것이 없다
        return None if units[need.unit_id] else "UNIT_HAS_NO_MOVABLE_TASK"
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


def ask_refusals(
    conn: sqlite3.Connection, pack: LoadedPack, run: AgentRun, needs: list[Need]
) -> list[str | None]:
    """need마다 담당자 사전 확인으로 물을 수 없는 사유(물을 수 있으면 None). 메인의 호출 유효성과
    Coordination의 ASK_OWNER 유효성이 같이 쓴다. 사실 조건만 본다.

    물을 수 있는 것은 자원 축 OWNER_CONSENT뿐이다: 시간 축은 Solver가 움직이고 동의는 후보 협의에서
    받는다. 값이 지금도 유효하고(need 검증과 같다) 이미 동의 범위에 들어 있지 않아야 한다.
    """
    facts = _Facts(conn, pack, run)
    consented: dict[str, set[str]] = {}
    for c in list_current_consents(conn, pack.site_id):
        if c.axis == "RESOURCE":
            consented.setdefault(c.task_id, set()).update(c.scope.get("resource_ids", []))
    out: list[str | None] = []
    for need in needs:
        if need.kind != "OWNER_CONSENT":
            out.append("NOT_OWNER_CONSENT")
        elif need.axis != "RESOURCE":
            out.append("TIME_AXIS_NOT_ASKABLE")
        elif not need.values:
            out.append("NO_VALUES")
        elif (reason := _check(facts, need)) is not None:
            out.append(reason)
        elif facts.tasks[need.task_id or ""].movable.resource and set(need.values) <= consented.get(
            need.task_id or "", set()
        ):
            out.append("ALREADY_CONSENTED")
        else:
            out.append(None)
    return out
