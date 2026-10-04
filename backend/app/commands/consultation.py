"""Consultation 생성. 2단계 BUILD_CONSULTATION 핸들러가 호출한다.

사람 명령이 아니므로 CommandResult·Audit를 남기지 않는다. 후보 snapshot의 사실로만
계산하므로 같은 후보에서 언제 불러도 같은 item이 나온다. 이미 있으면 기존 item을 돌려준다.
"""

import sqlite3

from app.domain.consultation import build_items
from app.domain.models import ConsultationItem
from app.store.repos.consultations import get_consultation_items, insert_consultation
from app.store.repos.records import get_candidate, get_snapshot, list_validations


def build_consultation(
    tx: sqlite3.Connection, site_id: str, candidate_id: str
) -> tuple[ConsultationItem, ...]:
    existing = get_consultation_items(tx, site_id, candidate_id)
    if existing is not None:
        return existing
    candidate = get_candidate(tx, site_id, candidate_id)
    if candidate is None:
        raise LookupError(f"candidate {candidate_id} not found")
    if not any(v.status == "PASS" for v in list_validations(tx, site_id, candidate_id)):
        raise ValueError(f"candidate {candidate_id} has no PASS validation")
    snapshot = get_snapshot(tx, candidate.snapshot_id)
    if snapshot is None:
        raise LookupError(f"snapshot {candidate.snapshot_id} not found")
    items = build_items(snapshot.facts(), candidate)
    insert_consultation(tx, site_id, candidate_id, items)
    return items
