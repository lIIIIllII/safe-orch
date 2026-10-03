"""Case 수명: wake·재개 claim·Case 종료와 대기열.

- wake_run: 영향받는 Run의 wake_seq += 1, 대기 중이면 RESUME_RUN 등록(Run당 PENDING 1개).
- claim_resume: `WAITING_HUMAN ∧ wait_generation 일치` 조건부 claim.
- end_case_run: Run 종료 + 보낸 요청 정리(메시지 CANCELLED·제안 STALE) + 열린 Case가 없어지면 대기열 1건 승격.
- promote_queued: 가장 먼저 접수된 QUEUED 작업을 새 revision READY로(Consent 복사),
  context +1, RECHECK 등록. 열린 Case 중 폼은 QUEUED로 저장된다.
모든 함수는 호출한 쪽의 tx 안에서 돈다(트랜잭션 중첩 없음).
"""

import sqlite3
from typing import Any

from app.domain.ids import new_id
from app.domain.models import Consent
from app.packs.loader import LoadedPack
from app.store.repos._rows import loads, rows
from app.store.repos.consents import insert_consent
from app.store.repos.dispatch import register_job
from app.store.repos.runs import ACTIVE, CASE_AGENT_TYPES, end_run, get_run, has_open_case
from app.store.repos.site import bump_context_version, get_site, list_actors
from app.store.repos.tasks import insert_task_revision, list_current_tasks


def recheck_key(context_version: int, plan_revision: int) -> str:
    """RECHECK dedupe 키. 승인은 context를 바꾸지 않으므로 plan을 함께 넣는다."""
    return f"RECHECK:ctx{context_version}:plan{plan_revision}"


def register_recheck(tx: sqlite3.Connection, site_id: str, cause: dict[str, Any]) -> bool:
    site = get_site(tx, site_id)
    assert site is not None
    return register_job(
        tx,
        site_id,
        "RECHECK",
        recheck_key(site.context_version, site.plan_revision),
        {"cause": cause},
    )


# ── wake와 재개 ────────────────────────────────────────────────


def wake_run(tx: sqlite3.Connection, site_id: str, run_id: str) -> bool:
    """wake_seq += 1. 대기 중이면 RESUME_RUN(run_id, wait_generation)을 등록한다.

    RUNNING이면 작업을 만들지 않는다. 늘어난 wake_seq를 대기 진입 재확인이 잡는다.
    종료된 Run이면 아무것도 하지 않고 False.
    """
    row = tx.execute(
        "UPDATE agent_run SET wake_seq = wake_seq + 1 WHERE run_id = ? AND status IN (?, ?)"
        " RETURNING status, wait_generation",
        (run_id, *ACTIVE),
    ).fetchone()
    if row is None:
        return False
    status, generation = row
    if status == "WAITING_HUMAN":
        register_job(
            tx,
            site_id,
            "RESUME_RUN",
            f"RESUME_RUN:{run_id}:{generation}",
            {"run_id": run_id, "wait_generation": generation},
            run_id=run_id,
            wait_generation=generation,
        )
    return True


def claim_resume(tx: sqlite3.Connection, run_id: str, wait_generation: int) -> bool:
    """대기 중이고 세대가 같을 때만 RUNNING으로. 0행이면 무효(이미 재개됨 또는 오래된 세대)."""
    row = tx.execute(
        "UPDATE agent_run SET status = 'RUNNING', wait_kind = NULL, wait_ref = NULL"
        " WHERE run_id = ? AND status = 'WAITING_HUMAN' AND wait_generation = ?"
        " RETURNING run_id",
        (run_id, wait_generation),
    ).fetchone()
    return row is not None


# ── Case 종료 ──────────────────────────────────────────────────


def cancel_requests(tx: sqlite3.Connection, run_id: str) -> None:
    """Run이 끝나면 보낸 요청은 효력을 잃는다: 메시지 OPEN → CANCELLED, 제안 PENDING → STALE.

    통지(NOTICE)는 답을 받는 요청이 아니므로 OPEN으로 남긴다.
    """
    tx.execute(
        "UPDATE message SET status = 'CANCELLED'"
        " WHERE run_id = ? AND status = 'OPEN' AND type <> 'NOTICE'",
        (run_id,),
    )
    tx.execute(
        "UPDATE proposal SET status = 'STALE' WHERE run_id = ? AND status = 'PENDING'", (run_id,)
    )


def close_case(tx: sqlite3.Connection, pack: LoadedPack) -> str | None:
    """열린 Case가 없으면 대기열 1건을 올린다. 올린 task_id."""
    if has_open_case(tx, pack.site_id):
        return None
    return promote_queued(tx, pack)


def end_case_run(
    tx: sqlite3.Connection,
    pack: LoadedPack,
    run_id: str,
    status: str,
    end_reason: str,
    from_statuses: tuple[str, ...] = ACTIVE,
) -> bool:
    """Run 종료(조건부) + 보낸 요청 정리. 열린 Run(RUNNING·WAITING)이 끝났을 때만 대기열을 올린다.

    이미 끝난 Run(예: ERROR → CANCELLED)은 Case가 이미 닫혔으므로 대기열을 다시 올리지 않는다.
    """
    before = get_run(tx, run_id)
    if before is None or not end_run(tx, run_id, status, end_reason, from_statuses):
        return False
    cancel_requests(tx, run_id)
    if before.status in ACTIVE and before.agent_type in CASE_AGENT_TYPES:
        close_case(tx, pack)
    return True


def end_candidate_runs(
    tx: sqlite3.Connection, pack: LoadedPack, candidate_id: str, status: str, end_reason: str
) -> list[str]:
    """후보에 걸린 열린 협의 Run(COORDINATION, phase CONSULT)을 끝낸다. 끝낸 run_id."""
    ids = [
        r[0]
        for r in tx.execute(
            "SELECT run_id FROM agent_run WHERE site_id = ? AND agent_type = 'COORDINATION'"
            " AND status IN (?, ?) AND json_extract(input_ref, '$.phase') = 'CONSULT'"
            " AND json_extract(input_ref, '$.candidate_id') = ? ORDER BY rowid",
            (pack.site_id, *ACTIVE, candidate_id),
        )
    ]
    return [rid for rid in ids if end_case_run(tx, pack, rid, status, end_reason)]


def supervisor_actor(conn: sqlite3.Connection, pack: LoadedPack) -> Any:
    """actor_id가 가장 작은 SUPERVISOR (Coordination·Event Response의 acting_unit, 사실 수정 확인자)."""
    found = sorted(
        (a for a in list_actors(conn, pack.site_id) if "SUPERVISOR" in a.roles),
        key=lambda a: a.actor_id,
    )
    return found[0] if found else None


def register_event_response(
    tx: sqlite3.Connection, pack: LoadedPack, event_id: str, hold_id: str
) -> bool:
    """Event Response START_RUN. Event 접수 tx 안에서 부른다. Event마다 새 Case."""
    supervisor = supervisor_actor(tx, pack)
    if supervisor is None:
        return False
    payload = {
        "agent_type": "EVENT_RESPONSE",
        "event_id": event_id,
        "hold_id": hold_id,
        "case_id": new_id("case"),
        "acting_unit_id": supervisor.unit_id,
    }
    return register_job(
        tx, pack.site_id, "START_RUN", f"START_RUN:EVENT_RESPONSE:{event_id}", payload
    )


def register_coordination(
    tx: sqlite3.Connection,
    pack: LoadedPack,
    phase: str,
    key: str,
    candidate_id: str,
    case_id: str,
    **extra: Any,
) -> bool:
    """Coordination START_RUN 등록. 원인 tx 안에서 부른다.

    phase CONSULT(협의)·NOTICE(통지). Case는 후보 Run의 case_id를 잇고, acting_unit은 SUPERVISOR의 Unit이다
    (Pack ID를 코드에 두지 않는다). acting_actor는 없다.
    """
    supervisor = supervisor_actor(tx, pack)
    if supervisor is None:
        return False
    unit = supervisor.unit_id
    payload = {
        "agent_type": "COORDINATION",
        "phase": phase,
        "candidate_id": candidate_id,
        "case_id": case_id,
        "acting_unit_id": unit,
        **extra,
    }
    return register_job(tx, pack.site_id, "START_RUN", key, payload)


def stale_active_runs(tx: sqlite3.Connection, pack: LoadedPack, end_reason: str) -> list[str]:
    """열린 Run을 STALE로(Event 접수). 마지막 Run이 닫힐 때 대기열 1건을 올린다.

    Work Intake Run은 뺀다: 폼이 Hold 중에도 접수되듯 Intake의 값은 아직 사실이 아니고, 완료할 때
    검증을 다시 한다.
    """
    ids = [
        r[0]
        for r in tx.execute(
            "SELECT run_id FROM agent_run WHERE site_id = ? AND status IN (?, ?)"
            " AND agent_type <> 'INTAKE' ORDER BY rowid",
            (pack.site_id, *ACTIVE),
        )
    ]
    for run_id in ids:
        end_case_run(tx, pack, run_id, "STALE", end_reason)
    return ids


# ── 대기열 ─────────────────────────────────────────────────────


def queued_task_ids(conn: sqlite3.Connection, site_id: str) -> list[str]:
    """현재 revision이 QUEUED인 작업, 접수 순서(QUEUED revision 행의 rowid)."""
    return [
        r["task_id"]
        for r in rows(
            conn,
            "SELECT t.task_id FROM task t WHERE t.site_id = ? AND t.lifecycle = 'QUEUED'"
            " AND t.revision = (SELECT MAX(revision) FROM task"
            "  WHERE site_id = t.site_id AND task_id = t.task_id)"
            " ORDER BY t.rowid",
            (site_id,),
        )
    ]


def copy_consents(
    tx: sqlite3.Connection,
    site_id: str,
    task_id: str,
    from_revision: int,
    to_revision: int,
    context_version: int,
    axes: tuple[str, ...] = ("TIME", "RESOURCE"),
) -> list[str]:
    """값이 바뀌지 않은 축의 Consent를 새 revision으로 복사한다(같은 source_ref)."""
    out = []
    for r in rows(
        tx,
        "SELECT * FROM consent WHERE site_id = ? AND task_id = ? AND task_revision = ?"
        " ORDER BY rowid",
        (site_id, task_id, from_revision),
    ):
        if r["axis"] not in axes:
            continue
        consent = Consent(
            consent_id=new_id("cns"),
            task_id=task_id,
            task_revision=to_revision,
            owner_actor_id=r["owner_actor_id"],
            axis=r["axis"],
            scope=loads(r["scope"]),
            source_ref=r["source_ref"],
        )
        insert_consent(tx, site_id, consent, context_version)
        out.append(consent.consent_id)
    return out


def promote_queued(tx: sqlite3.Connection, pack: LoadedPack) -> str | None:
    """가장 먼저 접수된 QUEUED 작업 1건을 READY로 올리고 context +1, RECHECK. 없으면 None."""
    site_id = pack.site_id
    ids = queued_task_ids(tx, site_id)
    if not ids:
        return None
    task = next(t for t in list_current_tasks(tx, site_id, pack) if t.task_id == ids[0])
    revision = task.revision + 1
    insert_task_revision(
        tx, site_id, task.model_copy(update={"revision": revision, "lifecycle": "READY"})
    )
    context_version = bump_context_version(tx, site_id)
    copy_consents(tx, site_id, task.task_id, task.revision, revision, context_version)
    register_recheck(
        tx,
        site_id,
        {"kind": "QUEUE", "task_id": task.task_id, "actor_id": task.owner_actor_id},
    )
    return task.task_id
