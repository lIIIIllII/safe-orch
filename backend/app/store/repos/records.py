"""불변 객체 INSERT와 Solver 결과 등록 (부록 A.10·A.11·A.13).

snapshot·search_spec·solver_result·candidate·validation은 트리거로 UPDATE·DELETE가 막혀 있다.
"""

import sqlite3

from app.domain.models import (
    Candidate,
    SearchSpec,
    Snapshot,
    SolverResult,
    Validation,
    ValidationCheck,
)
from app.store.repos._rows import dumps, loads, rows
from app.store.repos.dispatch import register_job


class StaleError(RuntimeError):
    """Solver 계산 중 context_version·plan_revision이 바뀌었다. 결과를 버린다."""


def insert_snapshot(tx: sqlite3.Connection, site_id: str, snapshot: Snapshot) -> None:
    tx.execute(
        "INSERT INTO snapshot (snapshot_id, site_id, snapshot_hash, content) VALUES (?, ?, ?, ?)",
        (snapshot.snapshot_id, site_id, snapshot.snapshot_hash, dumps(snapshot.content)),
    )


def insert_search_spec(tx: sqlite3.Connection, site_id: str, spec: SearchSpec) -> None:
    tx.execute(
        "INSERT INTO search_spec (search_spec_id, site_id, hash, snapshot_id, acting_unit_id,"
        " scope_level, axes, resource_alternatives, time_limit_s, search_key)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            spec.search_spec_id,
            site_id,
            spec.hash,
            spec.snapshot_id,
            spec.acting_unit_id,
            spec.scope_level,
            dumps({k: v.model_dump() for k, v in spec.axes.items()}),
            dumps({k: list(v) for k, v in spec.resource_alternatives.items()}),
            spec.time_limit_s,
            spec.search_key,
        ),
    )


def insert_solver_result(tx: sqlite3.Connection, site_id: str, result: SolverResult) -> None:
    tx.execute(
        "INSERT INTO solver_result (solver_result_id, site_id, search_spec_id, stage1, stage2,"
        " chosen_stage) VALUES (?, ?, ?, ?, ?, ?)",
        (
            result.solver_result_id,
            site_id,
            result.search_spec_id,
            dumps(result.stage1),
            None if result.stage2 is None else dumps(result.stage2),
            result.chosen_stage,
        ),
    )


def insert_candidate(tx: sqlite3.Connection, site_id: str, candidate: Candidate) -> None:
    tx.execute(
        "INSERT INTO candidate (candidate_id, site_id, snapshot_id, search_spec_id,"
        " search_spec_hash, solver_result_id, base_plan_revision, context_version, pack_hash,"
        " assignments, candidate_hash, kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            candidate.candidate_id,
            site_id,
            candidate.snapshot_id,
            candidate.search_spec_id,
            candidate.search_spec_hash,
            candidate.solver_result_id,
            candidate.base_plan_revision,
            candidate.context_version,
            candidate.pack_hash,
            dumps([a.model_dump() for a in candidate.assignments]),
            candidate.candidate_hash,
            candidate.kind,
        ),
    )


def insert_validation(tx: sqlite3.Connection, site_id: str, validation: Validation) -> None:
    """Validator 결과 등록 (부록 A.13). 버전은 다시 확인하지 않는다(STALE은 조회 시 계산)."""
    tx.execute(
        "INSERT INTO validation (validation_id, site_id, candidate_id, status, checks)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            validation.validation_id,
            site_id,
            validation.candidate_id,
            validation.status,
            dumps([c.model_dump(mode="json") for c in validation.checks]),
        ),
    )


def register_solver_outcome(
    tx: sqlite3.Connection,
    snapshot: Snapshot,
    result: SolverResult,
    candidate: Candidate | None,
) -> None:
    """tx 안에서 site 버전이 snapshot과 같은지 다시 확인하고 SolverResult(+Candidate)를 등록한다.

    SearchSpec은 Solver 호출 전에 insert_search_spec으로 저장돼 있어야 한다(FK).
    """
    facts = snapshot.facts()
    row = tx.execute(
        "SELECT context_version, plan_revision FROM site WHERE site_id = ?", (facts.site_id,)
    ).fetchone()
    if row is None or tuple(row) != (facts.context_version, facts.plan_revision):
        raise StaleError(
            f"site {facts.site_id} is at {tuple(row) if row else None}, snapshot was "
            f"(context {facts.context_version}, plan {facts.plan_revision})"
        )
    insert_solver_result(tx, facts.site_id, result)
    if candidate is not None:
        insert_candidate(tx, facts.site_id, candidate)
        # 후속 검증은 후보를 등록한 tx에서 등록한다 (I-18, 부록 A.14)
        register_job(
            tx,
            facts.site_id,
            "VALIDATE",
            f"VALIDATE:{candidate.candidate_id}",
            {"candidate_id": candidate.candidate_id},
        )


def get_snapshot(conn: sqlite3.Connection, snapshot_id: str) -> Snapshot | None:
    found = rows(conn, "SELECT * FROM snapshot WHERE snapshot_id = ?", (snapshot_id,))
    if not found:
        return None
    r = found[0]
    return Snapshot(
        snapshot_id=r["snapshot_id"], snapshot_hash=r["snapshot_hash"], content=loads(r["content"])
    )


def get_candidate(conn: sqlite3.Connection, site_id: str, candidate_id: str) -> Candidate | None:
    found = rows(
        conn,
        "SELECT * FROM candidate WHERE site_id = ? AND candidate_id = ?",
        (site_id, candidate_id),
    )
    if not found:
        return None
    r = found[0]
    r.pop("site_id")
    r["assignments"] = loads(r["assignments"])
    return Candidate(**r)


def list_validations(conn: sqlite3.Connection, site_id: str, candidate_id: str) -> list[Validation]:
    return [
        Validation(
            validation_id=r["validation_id"],
            candidate_id=r["candidate_id"],
            status=r["status"],
            checks=tuple(ValidationCheck(**c) for c in loads(r["checks"])),
        )
        for r in rows(
            conn,
            "SELECT * FROM validation WHERE site_id = ? AND candidate_id = ? ORDER BY rowid",
            (site_id, candidate_id),
        )
    ]


def get_search_spec(conn: sqlite3.Connection, search_spec_id: str) -> SearchSpec | None:
    found = rows(conn, "SELECT * FROM search_spec WHERE search_spec_id = ?", (search_spec_id,))
    if not found:
        return None
    r = found[0]
    r.pop("site_id")
    r["axes"] = loads(r["axes"])
    r["resource_alternatives"] = loads(r["resource_alternatives"])
    return SearchSpec(**r)


def find_reconfirm_candidate(
    conn: sqlite3.Connection, site_id: str, context_version: int, plan_revision: int
) -> Candidate | None:
    """같은 (context_version, plan_revision)의 RECONFIRM 후보 (부록 A.15 재사용)."""
    found = rows(
        conn,
        "SELECT candidate_id FROM candidate WHERE site_id = ? AND kind = 'RECONFIRM'"
        " AND context_version = ? AND base_plan_revision = ? ORDER BY rowid LIMIT 1",
        (site_id, context_version, plan_revision),
    )
    return get_candidate(conn, site_id, found[0]["candidate_id"]) if found else None
