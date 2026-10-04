"""consultation 기록과 후보·협의 상태 조회.

후보 상태(STALE·REJECTED·COMMITTED)와 Consultation 상태는 저장하지 않고 여기서 계산한다.
"""

import sqlite3
from dataclasses import dataclass
from typing import Any

from app.domain.consultation import (
    ConsultationStatus,
    ItemStatus,
    consultation_status,
    item_statuses,
    items_status,
)
from app.domain.models import Candidate, ConsultationItem
from app.store.repos._rows import dumps, loads, rows
from app.store.repos.decisions import list_case_rejections, list_decisions
from app.store.repos.messages import answer_sources, change_answers, list_requests_for_changes
from app.store.repos.plans import get_plan_by_candidate
from app.store.repos.records import get_candidate, list_validations
from app.store.repos.runs import get_run, run_for_solver_result
from app.store.repos.site import get_site


@dataclass(frozen=True)
class CandidateState:
    committed: bool
    rejected: bool
    stale_plan: bool
    stale_context: bool

    @property
    def stale(self) -> bool:
        return self.stale_plan or self.stale_context


@dataclass(frozen=True)
class ConsultationView:
    candidate_id: str
    items: tuple[ConsultationItem, ...]
    item_status: dict[str, ItemStatus]
    items_status: ConsultationStatus  # item만 본 상태 (승인 8단계)
    status: ConsultationStatus  # COMMITTED·STALE·REJECTED까지 반영한 표시 상태
    # task_id → 그 item 상태를 만든 담당자 답의 출처. prior = 다른 후보의 요청에 한 답이 넘어왔다
    answer_from: dict[str, dict[str, Any]]


def insert_consultation(
    tx: sqlite3.Connection, site_id: str, candidate_id: str, items: tuple[ConsultationItem, ...]
) -> None:
    tx.execute(
        "INSERT INTO consultation (candidate_id, site_id, items) VALUES (?, ?, ?)",
        (candidate_id, site_id, dumps([i.model_dump() for i in items])),
    )


def get_consultation_items(
    conn: sqlite3.Connection, site_id: str, candidate_id: str
) -> tuple[ConsultationItem, ...] | None:
    found = rows(
        conn,
        "SELECT items FROM consultation WHERE site_id = ? AND candidate_id = ?",
        (site_id, candidate_id),
    )
    if not found:
        return None
    return tuple(ConsultationItem(**i) for i in loads(found[0]["items"]))


def candidate_state(conn: sqlite3.Connection, site_id: str, candidate: Candidate) -> CandidateState:
    """STALE = 후보의 context_version·base_plan_revision이 현재 site 값과 다름."""
    site = get_site(conn, site_id)
    if site is None:
        raise LookupError(f"site {site_id} not found")
    return CandidateState(
        committed=get_plan_by_candidate(conn, site_id, candidate.candidate_id) is not None,
        rejected=bool(list_decisions(conn, site_id, candidate.candidate_id, "REJECT")),
        stale_plan=candidate.base_plan_revision != site.plan_revision,
        stale_context=candidate.context_version != site.context_version,
    )


def consultation_view(
    conn: sqlite3.Connection, site_id: str, candidate_id: str
) -> ConsultationView | None:
    """Consultation이 없거나 후보가 없으면 None."""
    candidate = get_candidate(conn, site_id, candidate_id)
    items = get_consultation_items(conn, site_id, candidate_id)
    if candidate is None or items is None:
        return None
    waived = [
        tid
        for d in list_decisions(conn, site_id, candidate_id, "WAIVE")
        for tid in d["target_task_ids"]
    ]
    # 답은 후보가 아니라 변경(change_hash)으로 찾는다. 같은 변경이면 다른 후보·Case의 답도 적용된다
    requests = list_requests_for_changes(conn, site_id, [i.change_hash for i in items])
    answers = change_answers(requests)
    statuses = item_statuses(items, waived, answers)  # type: ignore[arg-type]
    sources = answer_sources(requests)
    answer_from = {
        i.task_id: {
            "message_id": src["message_id"],
            "candidate_id": src["candidate_id"],
            "actor_id": src["reply"].get("actor_id"),
            "at": src["reply"].get("at"),
            "prior": src["candidate_id"] != candidate_id,
        }
        for i in items
        if statuses[i.task_id] != "WAIVED" and (src := sources.get(i.change_hash)) is not None
    }
    state = candidate_state(conn, site_id, candidate)
    return ConsultationView(
        candidate_id=candidate_id,
        items=items,
        item_status=statuses,
        items_status=items_status(statuses.values()),
        status=consultation_status(
            statuses.values(),
            committed=state.committed,
            stale=state.stale,
            rejected=state.rejected,
        ),
        answer_from=answer_from,
    )


def list_review_queue(conn: sqlite3.Connection, site_id: str) -> list[str]:
    """검토 대기 = PASS ∧ Consultation 있음 ∧ STALE·REJECTED·COMMITTED 아님 (OPEN 포함)."""
    out = []
    for r in rows(
        conn,
        "SELECT candidate_id FROM consultation WHERE site_id = ? ORDER BY rowid",
        (site_id,),
    ):
        cid = r["candidate_id"]
        candidate = get_candidate(conn, site_id, cid)
        if candidate is None or not any(
            v.status == "PASS" for v in list_validations(conn, site_id, cid)
        ):
            continue
        state = candidate_state(conn, site_id, candidate)
        if not (state.stale or state.rejected or state.committed):
            out.append(cid)
    return out


def case_objections(conn: sqlite3.Connection, site_id: str, case_id: str) -> list[dict[str, Any]]:
    """이 Case의 협의에서 담당자가 낸 이견 전부 (Case가 닫힐 때까지 쌓인다, CV-26). 문장은 인용이다.

    변경 요청에 DECLINE으로 답한 것. 취소된 요청에 온 늦은 답은 넣지 않는다 (ST-15).
    """
    out = []
    for r in rows(
        conn,
        "SELECT m.message_id, m.candidate_id, m.change_hash, m.to_actor_id, m.reply FROM message m"
        " JOIN agent_run r ON r.run_id = m.run_id"
        " WHERE m.site_id = ? AND r.case_id = ? AND m.type = 'CHANGE_REQUEST'"
        " AND m.status = 'ANSWERED' AND json_extract(m.reply, '$.decision') = 'DECLINE'"
        " ORDER BY m.rowid",
        (site_id, case_id),
    ):
        items = get_consultation_items(conn, site_id, r["candidate_id"]) or ()
        item = next((i for i in items if i.change_hash == r["change_hash"]), None)
        out.append(
            {
                "candidate_id": r["candidate_id"],
                "task_id": None if item is None else item.task_id,
                "owner_actor_id": r["to_actor_id"],
                "before": None if item is None else item.before.model_dump(),
                "after": None if item is None else item.after.model_dump(),
                "quoted_comment": (loads(r["reply"]) or {}).get("comment"),
            }
        )
    return out


def contested_changes(
    conn: sqlite3.Connection, site_id: str, candidate: Candidate
) -> list[dict[str, str]]:
    """이 후보가 담은 변경 가운데 거절·이견된 것 [{task_id, by}] (서버 계산, 표시만 한다, CV-26).

    OBJECTION: 같은 변경에 담당자 이견이 있다(항목 상태 OBJECTED). REJECTION: 이 후보를 만든 Case에서
    거절된 다른 후보가 대상 작업에 한 변경과 같은 변경이다. 같은 변경 = change hash(작업 revision·
    변경 전·후)가 같다. 서버는 사유를 해석하지 않고 이 후보를 거절하지도 않는다.
    """
    view = consultation_view(conn, site_id, candidate.candidate_id)
    if view is None:
        return []
    out = [
        {"task_id": i.task_id, "by": "OBJECTION"}
        for i in view.items
        if view.item_status[i.task_id] == "OBJECTED"
    ]
    maker_id = (
        run_for_solver_result(conn, candidate.solver_result_id)
        if candidate.solver_result_id
        else None
    )
    maker = get_run(conn, maker_id) if maker_id else None
    rejected: set[str] = set()
    for r in [] if maker is None else list_case_rejections(conn, maker.case_id):
        if r["candidate_id"] == candidate.candidate_id:
            continue
        items = get_consultation_items(conn, site_id, r["candidate_id"]) or ()
        rejected |= {i.change_hash for i in items if i.task_id in r["target_task_ids"]}
    out += [
        {"task_id": i.task_id, "by": "REJECTION"} for i in view.items if i.change_hash in rejected
    ]
    return sorted(out, key=lambda c: (c["task_id"], c["by"]))
