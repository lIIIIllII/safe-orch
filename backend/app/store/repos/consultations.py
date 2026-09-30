"""consultation 기록과 후보·협의 상태 조회 (설계서 §8·§9.3, 부록 A.14).

후보 상태(STALE·REJECTED·COMMITTED)와 Consultation 상태는 저장하지 않고 여기서 계산한다.
"""

import sqlite3
from dataclasses import dataclass

from app.domain.consultation import (
    ConsultationStatus,
    ItemStatus,
    consultation_status,
    item_statuses,
    items_status,
)
from app.domain.models import Candidate, ConsultationItem
from app.store.repos._rows import dumps, loads, rows
from app.store.repos.decisions import list_decisions
from app.store.repos.plans import get_plan_by_candidate
from app.store.repos.records import get_candidate, list_validations
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
    statuses = item_statuses(items, waived)
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
    )


def list_review_queue(conn: sqlite3.Connection, site_id: str) -> list[str]:
    """검토 대기 = PASS ∧ Consultation 있음 ∧ STALE·REJECTED·COMMITTED 아님 (OPEN 포함, 부록 A.14)."""
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
