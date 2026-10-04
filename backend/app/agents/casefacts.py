"""메인이 보는 Case 사실 (읽기 전용). 메인 관찰과 메인 도구의 유효성이 같이 쓴다.

엮인 충돌(공유 작업으로 묶은 충돌 그룹), 재계획에 쓰는 사실(움직일 수 있는 작업·미시도 범위·이전 결과),
이 Case의 후보·검증·협의·통지 상태, Hold, 하위 Run 결과, 이 Case의 열린 일, 지금 받아들여지는 호출.
판정은 모두 사실 조건이다(순서 없음).
"""

import sqlite3
from typing import Any

from app.agents.specs import main as main_spec
from app.agents.types import APPROACHES
from app.domain.canonical import canonical_hash
from app.domain.groups import ConflictGroup, conflict_groups, movable_task_ids
from app.domain.models import AgentRun, Conflict, Snapshot, SnapshotContent
from app.packs.loader import LoadedPack
from app.rules.engine import detect_conflicts, separation_links
from app.solver.search_spec import SearchSpecError, build_search_spec
from app.store.repos._rows import loads, rows
from app.store.repos.calls import call_key, fingerprint, last_result
from app.store.repos.case_events import list_case_events
from app.store.repos.consultations import (
    candidate_state,
    case_objections,
    consultation_view,
    contested_changes,
)
from app.store.repos.decisions import is_chosen, list_case_rejections, list_decisions
from app.store.repos.events import get_event
from app.store.repos.messages import list_fact_updates
from app.store.repos.pins import preferred_windows
from app.store.repos.plans import get_plan, get_plan_by_candidate
from app.store.repos.records import find_reconfirm_candidate, get_candidate, list_validations
from app.store.repos.runs import approach_attempts, get_run, list_steps, tried_search_keys
from app.store.repos.site import get_site
from app.store.repos.snapshots import build_snapshot_content
from app.store.repos.tasks import list_current_tasks

LEVELS = ("L0", "L1", "L2")
OPEN_ITEM = ("PENDING", "OBJECTED")
CHILD_RESULTS = 3  # 관찰에 보이는 최근 하위 Run 결과 수
# 사람이 직접 한 조작의 사건: 한 번마다 사람이 만든 일 하나다 (AG-30)
HUMAN_EVENTS = (
    "TASK_PINNED",
    "TASK_UNPINNED",
    "TASK_MOVED",
    "TASK_REMOVED",
    "TASK_EDITED",
    "PREFERRED_WINDOW_SET",
    "PREFERRED_WINDOW_CLEARED",
)


def current_snapshot(conn: sqlite3.Connection, pack: LoadedPack) -> Snapshot:
    content = build_snapshot_content(conn, pack.site_id, pack)
    return Snapshot(snapshot_id="observe", snapshot_hash=canonical_hash(content), content=content)


def current_groups(
    conn: sqlite3.Connection, pack: LoadedPack
) -> tuple[Snapshot, SnapshotContent, list[ConflictGroup]]:
    """지금 사실에서 계산한 충돌 그룹."""
    snapshot = current_snapshot(conn, pack)
    facts = snapshot.facts()
    conflicts = detect_conflicts(snapshot, facts.check_assignments(), pack)
    return snapshot, facts, conflict_groups(conflicts)


# ── 통지 대상 ──────────────────────────────────────────────────


def notice_targets(
    conn: sqlite3.Connection, pack: LoadedPack, plan_revision: int
) -> list[dict[str, Any]]:
    """확정 Plan과 직전 Plan을 비교한 통지 대상.

    바뀐(새로 들어간) 작업의 담당자와, 새 Plan에서 SEPARATION Rule로 그 작업과 엮인 작업의 담당자.
    자원 공유는 넣지 않는다.
    """
    plan, prev = get_plan(conn, pack.site_id, plan_revision), None
    if plan_revision > 0:
        prev = get_plan(conn, pack.site_id, plan_revision - 1)
    if plan is None:
        return []
    before = {a.task_id: a for a in (prev.assignments if prev else ())}
    tasks = {t.task_id: t for t in list_current_tasks(conn, pack.site_id, pack)}
    placed = [a for a in plan.assignments if a.task_id in tasks]
    changed = [a.task_id for a in placed if before.get(a.task_id) != a]
    targets: dict[str, dict[str, Any]] = {}

    def add(task_id: str, reason: dict[str, Any]) -> None:
        owner = tasks[task_id].owner_actor_id
        t = targets.setdefault(owner, {"actor_id": owner, "task_ids": [], "reasons": []})
        if task_id not in t["task_ids"]:
            t["task_ids"].append(task_id)
        t["reasons"].append(reason)

    for tid in changed:
        add(tid, {"task_id": tid, "kind": "CHANGED"})
    for tid in changed:
        for other in placed:
            if other.task_id == tid or other.task_id in changed:
                continue
            for rule_id in separation_links(pack, tasks[tid], tasks[other.task_id]):
                add(
                    other.task_id,
                    {
                        "task_id": other.task_id,
                        "kind": "SAFETY_LINK",
                        "rule_id": rule_id,
                        "with_task_id": tid,
                    },
                )
    return [targets[k] for k in sorted(targets)]


def notified_actors(conn: sqlite3.Connection, site_id: str, plan_revision: int) -> set[str]:
    """그 Plan의 확정 통지를 이미 받은 사람 (어느 통지 Run이 보냈든)."""
    return {
        r[0]
        for r in conn.execute(
            "SELECT m.to_actor_id FROM message m JOIN agent_run r ON r.run_id = m.run_id"
            " WHERE m.site_id = ? AND m.type = 'NOTICE' AND r.agent_type = 'COORDINATION'"
            " AND json_extract(r.input_ref, '$.plan_revision') = ?",
            (site_id, plan_revision),
        )
    }


# ── 이 Case의 후보 ─────────────────────────────────────────────


def case_candidate_ids(conn: sqlite3.Connection, pack: LoadedPack, case_id: str) -> list[str]:
    """이 Case의 Run이 만든 후보, 이 Case의 사건이 가리키는 후보, 지금 사실의 재확인 후보."""
    site = get_site(conn, pack.site_id)
    assert site is not None
    ids = [
        r["candidate_id"]
        for r in rows(
            conn,
            "SELECT c.candidate_id FROM candidate c"
            " JOIN solver_job j ON j.solver_result_id = c.solver_result_id"
            " JOIN agent_run r ON r.run_id = j.run_id WHERE r.case_id = ? ORDER BY c.rowid",
            (case_id,),
        )
    ]
    for e in list_case_events(conn, pack.site_id):
        if e["case_id"] == case_id and e["kind"] == "CANDIDATE_DECIDED":
            ids.append(e["ref"]["candidate_id"])
    reconfirm = find_reconfirm_candidate(
        conn, pack.site_id, site.context_version, site.plan_revision
    )
    if reconfirm is not None:
        ids.append(reconfirm.candidate_id)
    return list(dict.fromkeys(ids))


def candidate_view(
    conn: sqlite3.Connection,
    pack: LoadedPack,
    candidate_id: str,
    facts: SnapshotContent | None = None,
) -> dict[str, Any]:
    """후보 하나의 검증·협의·결정·통지 상태 (서버 계산). facts를 주면 지금 기준에서 바꾸는 작업도 낸다."""
    site_id = pack.site_id
    candidate = get_candidate(conn, site_id, candidate_id)
    assert candidate is not None
    state = candidate_state(conn, site_id, candidate)
    validations = list_validations(conn, site_id, candidate_id)
    passed = any(v.status == "PASS" for v in validations)
    view = consultation_view(conn, site_id, candidate_id)
    live = not (state.stale or state.rejected or state.committed)
    found = list_decisions(conn, site_id, candidate_id)
    decisions = [d for d in found if d["type"] in ("APPROVE", "REJECT")]
    last = decisions[-1] if decisions else None
    plan = get_plan_by_candidate(conn, site_id, candidate_id)
    notice = None
    if plan is not None and candidate.kind == "REPLAN":
        targets = notice_targets(conn, pack, plan.plan_revision)
        sent = notified_actors(conn, site_id, plan.plan_revision)
        notice = {
            "plan_revision": plan.plan_revision,
            "targets": len(targets),
            "unsent": sum(1 for t in targets if t["actor_id"] not in sent),
        }
    items = {} if view is None else view.item_status
    changed: list[str] = []
    if facts is not None:
        base = facts.base_assignments()
        in_plan = {a.task_id for a in facts.plan.assignments}
        changed = sorted(
            a.task_id
            for a in candidate.assignments
            if a.task_id not in in_plan or base.get(a.task_id) != a
        )
    return {
        "candidate_id": candidate_id,
        "kind": candidate.kind,
        # 이 후보가 지금 계획에서 바꾸거나 새로 배치하는 작업
        "changed_task_ids": changed,
        "validation": validations[-1].status if validations else None,
        # 이 후보에 도달한 접근(같은 배치면 여럿)과 Supervisor가 골랐는가. 고른 안만 협의한다 (AG-28·AG-29)
        "approaches": sorted(
            {a["approach"] for a in approach_attempts(conn, candidate_id=candidate_id)}
        ),
        "chosen": is_chosen(conn, site_id, candidate_id),
        "live": live,
        "stale": state.stale,
        "consultation_status": None if view is None else view.status,
        "open_items": sorted(t for t, s in items.items() if s in OPEN_ITEM),
        # 거절·이견된 변경을 담고 있는가 (서버 계산, 표시만 한다, CV-26)
        "contested": contested_changes(conn, site_id, candidate) if live else [],
        # 검토 대기: 검증을 통과했고 살아 있으며 협의 항목이 만들어졌다 (승인은 사람만 한다)
        "review_pending": passed and live and view is not None,
        "decision": None
        if last is None
        else {
            "type": last["type"],
            "reason_code": last["reason_code"],
            "target_task_ids": last["target_task_ids"],
            # Supervisor가 쓴 문장은 인용 데이터다
            "quoted_comment": last["comment"] or None,
        },
        "notice": notice,
    }


def rejection_facts(conn: sqlite3.Connection, case_id: str) -> dict[str, Any]:
    """이 Case 후보에 대한 거절 사실: 거절 수와 마지막 거절."""
    found = list_case_rejections(conn, case_id)
    last = found[-1] if found else None
    return {
        "count": len(found),
        "last": None
        if last is None
        else {k: last[k] for k in ("candidate_id", "reason_code", "target_task_ids")},
    }


def human_work(conn: sqlite3.Connection, site_id: str, case_id: str) -> int:
    """이 Case에서 사람이 새 일을 만든 횟수 (AG-30). 저장하지 않고 사건·기록에서 센다.

    사람에게 막힌 안(Supervisor 거절이나 담당자 이견) 하나, 작업 고정·고정 해제·직접 이동·없애기·카드에서 값
    고치기(확인)·희망 영역 그리기·지우기 한 번,
    Supervisor가 고른 안을 다른 안으로 바꾼 것 한 번이 각각 1이다. 막힌 안을 떠나 새로 고른 것은 그 안에서 이미
    셌으므로 세지 않는다. Agent의 행동(재호출, 가드 거절, 재계획 결과)은 여기 들어오지 않는다.
    """
    turned_down = {r["candidate_id"] for r in list_case_rejections(conn, case_id)}
    turned_down |= {o["candidate_id"] for o in case_objections(conn, site_id, case_id)}
    count, chosen = len(turned_down), None
    for e in list_case_events(conn, site_id):
        if e["case_id"] != case_id:
            continue
        if e["kind"] in HUMAN_EVENTS:
            count += 1
        elif e["kind"] == "CANDIDATE_CHOSEN":
            if chosen not in (None, e["ref"]["candidate_id"]) and chosen not in turned_down:
                count += 1
            chosen = e["ref"]["candidate_id"]
    return count


def budget_limits(conn: sqlite3.Connection, site_id: str, main: AgentRun) -> dict[str, int]:
    """이 메인 Run의 한도: 기본값 + 사람이 만든 일 × 한 바퀴분 (AG-30)."""
    return main_spec.budget_limits(human_work(conn, site_id, main.case_id))


# ── 하위 Run 결과 ──────────────────────────────────────────────


def run_result(conn: sqlite3.Connection, run: AgentRun) -> dict[str, Any]:
    """끝난 Run의 결과. 스스로 돌려준 결과는 마지막 step에 있고, 서버가 끝낸 Run은 서버가 만든다 (ST-20)."""
    steps = [s for s in list_steps(conn, run.run_id) if s["status"] == "COMPLETED"]
    last = steps[-1] if steps else None
    if (
        last is not None
        and (last["action"] or {}).get("name") == "RETURN_RESULT"
        and last["guard"]["verdict"] == "ACCEPTED"
    ):
        result = dict(last["tool_result"])
        # 요약은 전문 Agent가 쓴 문장이다(인용 데이터)
        result["quoted_summary"] = result.pop("summary", None)
        return {"by": "AGENT", **result}
    result: dict[str, Any] = {
        "by": "SERVER",
        "status": "DONE" if run.status == "SUCCEEDED" else "BLOCKED",
        "paths": [],
    }
    if run.agent_type == "REPLANNING":
        # 결과를 돌려주지 못하고 끝난 재계획(Budget 소진 등)이 만든 후보 가운데 살아 있는 것
        result["candidate_ids"] = _live_candidates_of(conn, run.run_id)
    return result


def _live_candidates_of(conn: sqlite3.Connection, run_id: str) -> list[str]:
    """그 Run이 만들었거나 같은 배치로 도달한 후보 가운데 살아 있는 것(무효·거절·확정이 아닌 것)."""
    out: list[str] = []
    for r in rows(
        conn,
        "SELECT j.site_id, COALESCE(c.candidate_id, j.same_candidate_id) AS candidate_id"
        " FROM solver_job j LEFT JOIN candidate c ON c.solver_result_id = j.solver_result_id"
        " WHERE j.run_id = ? ORDER BY j.rowid",
        (run_id,),
    ):
        cid = r["candidate_id"]
        candidate = None if cid is None else get_candidate(conn, r["site_id"], cid)
        if candidate is None or cid in out:
            continue
        state = candidate_state(conn, r["site_id"], candidate)
        if not (state.stale or state.rejected or state.committed):
            out.append(cid)
    return out


def child_results(
    conn: sqlite3.Connection, pack: LoadedPack, main: AgentRun
) -> list[dict[str, Any]]:
    """이 메인이 부른 하위 Run과 결과 (최근 것만). 시작되지 못한 호출도 넣는다."""
    out: list[dict[str, Any]] = []
    for e in list_case_events(conn, pack.site_id):
        ref = e["ref"]
        if e["kind"] != "CHILD_RUN_ENDED" or ref.get("parent_run_id") != main.run_id:
            continue
        run = get_run(conn, ref["run_id"])
        entry: dict[str, Any] = {"run_id": ref["run_id"], "run_status": ref["status"]}
        if run is None:
            # 시작 조건이 맞지 않아 Run이 만들어지지 않았다
            entry.update({"call": ref.get("call"), "end_reason": ref.get("reason"), "result": None})
        else:
            entry.update(
                {
                    "call": {k: v for k, v in run.input_ref.items() if k in CALL_REFS},
                    "end_reason": run.end_reason,
                    "result": run_result(conn, run),
                }
            )
        out.append(entry)
    return out[-CHILD_RESULTS:]


CALL_REFS = (
    "agent_type",
    "approach",
    "phase",
    "candidate_id",
    "plan_revision",
    "event_id",
)


def open_child(conn: sqlite3.Connection, main_run_id: str) -> str | None:
    row = conn.execute(
        "SELECT run_id FROM agent_run WHERE parent_run_id = ?"
        " AND status IN ('RUNNING', 'WAITING_HUMAN') LIMIT 1",
        (main_run_id,),
    ).fetchone()
    return None if row is None else row[0]


def pending_child(conn: sqlite3.Connection, site_id: str, main_run_id: str) -> bool:
    """부른 하위 Run이 아직 시작되지 않았다(START_RUN 대기)."""
    return bool(
        conn.execute(
            "SELECT 1 FROM dispatch_job WHERE site_id = ? AND kind = 'START_RUN'"
            " AND status IN ('PENDING', 'CLAIMED')"
            " AND json_extract(payload, '$.parent_run_id') = ?",
            (site_id, main_run_id),
        ).fetchone()
    )


# ── Case 사실 전체 ─────────────────────────────────────────────


def same_facts(
    conn: sqlite3.Connection, site_id: str, key: str, candidate_id: str | None = None
) -> bool:
    """같은 키로 부른 마지막 Run이 끝난 뒤 관련 사실이 하나도 바뀌지 않았다 (AG-24)."""
    last = last_result(conn, site_id, key)
    if last is None or last["end_fingerprint"] is None:
        return False
    return last["end_fingerprint"] == fingerprint(conn, site_id, key, candidate_id)


def untried_levels(snapshot: Snapshot, conflicts: list[Conflict], tried: set[str]) -> list[str]:
    """이 Case에서 아직 시도하지 않은 탐색 범위."""
    out = []
    for level in LEVELS if conflicts else ():
        try:
            key = build_search_spec(snapshot, conflicts, level).search_key
        except SearchSpecError:
            continue
        if key not in tried:
            out.append(level)
    return out


def build(conn: sqlite3.Connection, pack: LoadedPack, main: AgentRun) -> dict[str, Any]:
    """메인 관찰의 사실 부분과 유효성 판정에 쓰는 값."""
    site_id, case_id = pack.site_id, main.case_id
    site = get_site(conn, site_id)
    assert site is not None
    snapshot, facts, groups = current_groups(conn, pack)
    in_plan = {a.task_id for a in facts.plan.assignments}
    tried = tried_search_keys(conn, site_id, case_id)

    events = [
        {"seq": e["seq"], "kind": e["kind"], "ref": e["ref"], "new": e["seq"] > main.last_event_seq}
        for e in list_case_events(conn, site_id)
        if e["case_id"] == case_id
    ]
    holds = []
    for h in rows(
        conn,
        "SELECT * FROM hold WHERE site_id = ? AND status = 'ACTIVE' ORDER BY rowid",
        (site_id,),
    ):
        event = get_event(conn, site_id, h["event_id"])
        updates = list_fact_updates(conn, site_id, event_id=h["event_id"])
        holds.append(
            {
                "hold_id": h["hold_id"],
                "event_id": h["event_id"],
                "event_type": None if event is None else event["event_type"],
                "scope": h["scope"],
                "task_id": h["task_id"],
                # 그 신고의 사실 수정안 상태 (없으면 빈 목록)
                "fact_updates": [p["status"] for p in updates],
            }
        )
    hold_active = bool(holds)
    held_tasks = {h["task_id"] for h in holds if h["task_id"]}

    wanted = preferred_windows(conn, site_id)
    calls: list[dict[str, Any]] = []
    # 엮인 충돌: 설명이다. 재계획은 그룹이 아니라 현장의 충돌 전체를 한 번에 푼다 (AG-24)
    group_views = [
        {
            "group_id": g.group_id,
            "task_ids": list(g.task_ids),
            "rule_ids": sorted({c.rule_id for c in g.conflicts}),
            "held_task_ids": sorted(set(g.task_ids) & held_tasks),
        }
        for g in groups
    ]
    conflicts = [c for g in groups for c in g.conflicts]
    involved = sorted({tid for g in groups for tid in g.task_ids})
    # 마지막 결과는 어느 접근이든 마지막에 부른 재계획의 것이다
    last = last_result(conn, site_id, "REPLANNING", any_approach=True)
    unchanged = last is not None and same_facts(conn, site_id, last["call_key"])
    last_view = None
    if last is not None:
        run = get_run(conn, last["run_id"])
        assert run is not None
        result = run_result(conn, run)
        last_view = {
            "approach": run.input_ref.get("approach"),
            "run_status": last["status"],
            "end_reason": last["end_reason"],
            "result_status": result["status"],
            # 전문 Agent가 엮은 길과 서버가 붙인 열 수 있는 것. need마다 need_id가 있다
            "paths": result["paths"],
            "openers": result.get("openers", []),
            # 그 Run이 끝난 뒤 관련 사실이 바뀌었는가 (바뀌지 않았으면 같은 호출은 거절된다)
            "facts_changed": not unchanged,
        }
    movable = movable_task_ids(groups, facts.pins)
    replanning = {
        # 충돌에 걸린 작업 가운데 고정되지 않아 움직일 수 있는 것. 재계획은 고정되지 않은 작업만 옮긴다
        "movable_task_ids": movable,
        "request_task_ids": [t for t in involved if t not in in_plan],
        # 희망 영역이 있는 작업 (지연의 기준이다. 희망 우선 접근이 먼저 줄이는 것, ST-22)
        "preferred_task_ids": [t for t in involved if t in wanted],
        "untried_levels": untried_levels(snapshot, conflicts, tried),
        "last_result": last_view,
    }
    # 움직일 수 있는 작업이 없으면 재계획으로 바뀌는 것이 없다 (AG-02). 접근마다 호출 키가 다르다:
    # 같은 접근·같은 사실의 재호출만 거절된다 (AG-24)
    if not hold_active and movable:
        calls += [
            {"agent": "REPLANNING", "approach": a}
            for a in APPROACHES
            if not same_facts(conn, site_id, call_key("REPLANNING", {"approach": a}))
        ]

    candidates = [
        candidate_view(conn, pack, cid, facts) for cid in case_candidate_ids(conn, pack, case_id)
    ]
    pending = [c for c in candidates if c["review_pending"]]
    for view in group_views:
        # 이 그룹의 작업을 바꾸는 검토 대기 후보 (사람의 결정을 기다리는 중이다)
        view["review_candidates"] = [
            c["candidate_id"] for c in pending if set(c["changed_task_ids"]) & set(view["task_ids"])
        ]
    for c in candidates:
        cid = c["candidate_id"]
        # 협의는 Supervisor가 고른 안만 한다 (AG-29)
        consult = c["chosen"] and c["validation"] == "PASS" and c["live"] and c["open_items"]
        if consult and not hold_active:
            key = call_key("COORDINATION", {"phase": "CONSULT", "candidate_id": cid})
            if not same_facts(conn, site_id, key, cid):
                calls.append({"agent": "COORDINATION", "phase": "CONSULT", "candidate_id": cid})
        notice = c["notice"]
        if notice and notice["unsent"] and notice["plan_revision"] == site.plan_revision:
            refs = {
                "phase": "NOTICE",
                "candidate_id": cid,
                "plan_revision": notice["plan_revision"],
            }
            # 통지 Run이 아무것도 보내지 않고 끝났으면 같은 호출을 다시 받지 않는다
            if not same_facts(conn, site_id, call_key("COORDINATION", refs), cid):
                calls.append({"agent": "COORDINATION", "phase": "NOTICE", "candidate_id": cid})
    for h in holds:
        if h["event_type"] == "DELAY":
            key = call_key("EVENT_RESPONSE", {"event_id": h["event_id"]})
            if not same_facts(conn, site_id, key):
                calls.append({"agent": "EVENT_RESPONSE", "event_id": h["event_id"]})

    # 이 Case의 열린 일 (CLOSE의 사실 조건). 다른 Case가 남긴 것은 넣지 않는다
    # 이 Case가 맡은 작업: 준비된 요청과, 담당자가 카드에서 값을 고친 작업 (AG-33)
    case_tasks = {
        e["ref"].get("task_id") for e in events if e["kind"] in ("TASK_READY", "TASK_EDITED")
    }
    case_events = {e["ref"].get("event_id") for e in events if e["kind"] == "EVENT_REPORTED"}
    for p in rows(conn, "SELECT payload, target_task_id FROM proposal WHERE type = 'FACT_UPDATE'"):
        if loads(p["payload"]).get("event_id") in case_events:
            case_tasks.add(p["target_task_id"])
    ready = set(facts.task_map())
    open_work: list[dict[str, Any]] = []
    for c in candidates:
        if c["review_pending"]:
            open_work.append({"kind": "REVIEW_PENDING", "candidate_id": c["candidate_id"]})
        notice = c["notice"]
        if notice and notice["unsent"] and notice["plan_revision"] == site.plan_revision:
            open_work.append({"kind": "NOTICE_UNSENT", "candidate_id": c["candidate_id"]})
    for tid in sorted(t for t in case_tasks if t in ready and t not in in_plan):
        placed = [c["candidate_id"] for c in pending if tid in c["changed_task_ids"]]
        # placed_by: 이 작업을 배치한 검토 대기 후보
        open_work.append({"kind": "TASK_UNPLANNED", "task_id": tid, "placed_by": placed})
    for g in groups:
        if set(g.task_ids) & (case_tasks & in_plan):
            open_work.append({"kind": "CONFLICT", "group_id": g.group_id})
    for h in holds:
        if h["event_id"] in case_events:
            open_work.append({"kind": "HOLD_ACTIVE", "hold_id": h["hold_id"]})

    return {
        "events": events,
        "groups": group_views,
        "replanning": replanning,
        "holds": holds,
        "candidates": candidates,
        "rejections": rejection_facts(conn, case_id),
        "child_results": child_results(conn, pack, main),
        "calls": calls,
        "open_work": open_work,
        # 기다릴 것: 사람의 승인·거절을 기다리는 후보, 사람이 풀어야 하는 Hold
        "waiting_for": {
            "candidates": [c["candidate_id"] for c in candidates if c["review_pending"]],
            "holds": [h["hold_id"] for h in holds],
        },
    }
