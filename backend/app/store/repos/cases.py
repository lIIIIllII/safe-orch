"""Case 수명: 사건 전달·메인 시작, wake·재개 claim·Case 종료와 대기열.

- deliver_event: 사건을 적고 열린 메인에게 전한다. 열린 메인이 없으면 같은 tx에서 메인을 만든다(AG-07).
- deliver_to_open_main: 열린 메인이 있을 때만 사건을 적고 전한다(고정·해제, AG-27).

- wake_run: 영향받는 Run의 wake_seq += 1, 대기 중이면 RESUME_RUN 등록(Run당 PENDING 1개).
- claim_resume: `WAITING_HUMAN ∧ wait_generation 일치` 조건부 claim.
- end_case_run: Run 종료 + 보낸 요청 정리(메시지 CANCELLED·제안 STALE) + 열린 Case가 없어지면 대기열 전부 승격.
- promote_queued: 대기 중인 QUEUED 작업을 전부 새 revision READY로(Consent 복사), context +1 한 번,
  RECHECK 등록 한 번. 열린 Case 중 접수는 QUEUED로 저장된다 (AG-07).
모든 함수는 호출한 쪽의 tx 안에서 돈다(트랜잭션 중첩 없음).
"""

import sqlite3
from typing import Any

from app.config import get_settings
from app.domain.ids import new_id
from app.domain.models import AgentRun, Consent
from app.packs.loader import LoadedPack
from app.store.repos._rows import loads, rows
from app.store.repos.calls import fingerprint
from app.store.repos.case_events import case_of, record_case_event
from app.store.repos.consents import insert_consent
from app.store.repos.dispatch import register_job
from app.store.repos.messages import insert_message
from app.store.repos.runs import (
    ACTIVE,
    CASE_AGENT_TYPES,
    end_run,
    get_run,
    has_open_case,
    insert_run,
    list_steps,
)
from app.store.repos.schedules import schedule_of_tasks
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


# ── 사건 전달 ──────────────────────────────────────────────────


def open_main(conn: sqlite3.Connection, site_id: str) -> AgentRun | None:
    """열린 메인 Run (site당 하나, ST-18)."""
    row = conn.execute(
        "SELECT run_id FROM agent_run WHERE site_id = ? AND agent_type = 'MAIN'"
        " AND status IN (?, ?)",
        (site_id, *ACTIVE),
    ).fetchone()
    return None if row is None else get_run(conn, row[0])


def deliver_event(
    tx: sqlite3.Connection,
    pack: LoadedPack,
    kind: str,
    dedupe_key: str,
    ref: dict[str, Any],
    origin_case_id: str | None = None,
) -> bool:
    """사건을 적고 메인에게 전한다. 사건이 생기는 tx 안에서 부른다. 새로 적었으면 True.

    사건은 열린 메인의 Case에 들어간다. 열린 메인이 없으면 이 tx에서 메인을 만들고 그 Case에 넣는다
    (닫힌 Case에서 온 사건도 새 메인이 받는다. 원래 Case는 ref.origin_case_id에 남긴다).
    메인이 하위 Run을 기다리는 동안에는 깨우지 않는다: 하위 Run이 끝날 때 깨어나 사건을 본다.
    자동 시작이 꺼져 있으면 기록만 한다.
    """
    site_id = pack.site_id
    if case_of(tx, site_id, dedupe_key) is not None:
        return False
    main = open_main(tx, site_id)
    started = False
    if main is None and get_settings().main_auto_start:
        main, started = _start_main(tx, pack, dedupe_key), True
    case_id = origin_case_id if main is None else main.case_id
    if origin_case_id is not None and origin_case_id != case_id:
        ref = {**ref, "origin_case_id": origin_case_id}
    record_case_event(tx, site_id, kind, dedupe_key, ref, case_id)
    if main is not None and not started and main.wait_kind != "CHILD_RUN":
        wake_run(tx, site_id, main.run_id)
    return True


def deliver_to_open_main(
    tx: sqlite3.Connection, pack: LoadedPack, kind: str, dedupe_key: str, ref: dict[str, Any]
) -> bool:
    """열린 메인이 있을 때만 사건을 적고 전한다(고정·해제). 열린 메인이 없으면 사건을 만들지 않는다.

    깨우는 규칙은 deliver_event와 같다: 메인이 하위 Run을 기다리는 동안에는 깨우지 않는다.
    """
    main = open_main(tx, pack.site_id)
    if main is None:
        return False
    if not record_case_event(tx, pack.site_id, kind, dedupe_key, ref, main.case_id):
        return False
    if main.wait_kind != "CHILD_RUN":
        wake_run(tx, pack.site_id, main.run_id)
    return True


def _start_main(tx: sqlite3.Connection, pack: LoadedPack, trigger: str) -> AgentRun | None:
    """메인 Run을 만들고 CONTINUE_RUN으로 부른다. acting unit은 Supervisor의 Unit을 적어 두지만
    권한 판정에는 쓰지 않는다."""
    supervisor = supervisor_actor(tx, pack)
    if supervisor is None:
        return None
    run = AgentRun(
        run_id=new_id("run"),
        agent_type="MAIN",
        case_id=new_id("case"),
        acting_actor_id=None,
        acting_unit_id=supervisor.unit_id,
        input_ref={"trigger": trigger},
        exec_contract_version="",
        status="RUNNING",
    )
    insert_run(tx, pack.site_id, run)
    register_job(
        tx,
        pack.site_id,
        "CONTINUE_RUN",
        f"CONTINUE_RUN:{run.run_id}:0",
        {"run_id": run.run_id},
        run_id=run.run_id,
    )
    return run


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


def close_case(tx: sqlite3.Connection, pack: LoadedPack) -> list[str]:
    """열린 Case가 없으면 대기 중인 접수를 전부 올린다. 올린 task_id들."""
    if has_open_case(tx, pack.site_id):
        return []
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
    if before.status in ACTIVE and before.parent_run_id is not None:
        # 하위 Run이 끝났다(자기 행동이든 서버가 끝냈든). 부른 쪽 Case의 사건이고, 부른 메인을 깨운다.
        # 끝날 때의 사실 지문을 적어 둔다: 같은 호출을 다시 받을지 판정하는 기준이다 (AG-24)
        ref = {"run_id": run_id, "parent_run_id": before.parent_run_id, "status": status}
        key = before.input_ref.get("call_key")
        if key is not None:
            ref["fingerprint"] = fingerprint(
                tx, pack.site_id, key, before.input_ref.get("candidate_id")
            )
        record_case_event(
            tx, pack.site_id, "CHILD_RUN_ENDED", f"CHILD_RUN_ENDED:{run_id}", ref, before.case_id
        )
        wake_run(tx, pack.site_id, before.parent_run_id)
    if before.status in ACTIVE and before.agent_type == "MAIN":
        # 메인이 끝나면 열린 하위 Run도 같이 끝낸다(취소·오류). 보낸 요청도 정리된다
        for (child,) in tx.execute(
            "SELECT run_id FROM agent_run WHERE parent_run_id = ? AND status IN (?, ?)",
            (run_id, *ACTIVE),
        ).fetchall():
            if end_run(tx, child, "CANCELLED", "PARENT_ENDED", ACTIVE):
                cancel_requests(tx, child)
    if before.status in ACTIVE:
        _notify_end(tx, pack, before, status, end_reason)
    if before.status in ACTIVE and before.agent_type in CASE_AGENT_TYPES:
        close_case(tx, pack)
    return True


def _notify_end(
    tx: sqlite3.Connection, pack: LoadedPack, run: AgentRun, status: str, end_reason: str
) -> None:
    """서버 문구 통지가 필요한 종료.

    - Intake가 완료가 아닌 종료(BLOCKED·BUDGET_EXHAUSTED)로 끝나면 요청자에게 접수 미완을 알린다 (AG-06).
    - 메인이 스스로 끝내지 못하면(Budget 소진·오류·형식 오류 2회) Supervisor에게 알린다. 그 Case의 남은
      일은 다음 메인에 넘기지 않는다 (AG-07).
    통지는 step이 아니므로 마지막 step 다음 번호에 붙인다.
    """
    site = get_site(tx, pack.site_id)
    assert site is not None
    to_actor, body, agent_text = None, "", None
    if run.agent_type == "INTAKE" and status in ("BLOCKED", "BUDGET_EXHAUSTED"):
        steps = [s for s in list_steps(tx, run.run_id) if s["status"] == "COMPLETED"]
        result = (steps[-1]["tool_result"] or {}) if steps and status == "BLOCKED" else {}
        codes = result.get("reason_codes") or [end_reason]
        to_actor = run.input_ref.get("requester_actor_id")
        body = intake_incomplete_text(run.input_ref.get("task_id"), codes)
        agent_text = result.get("summary")
    elif run.agent_type == "MAIN" and (
        status in ("BUDGET_EXHAUSTED", "ERROR") or end_reason.endswith("_TWICE")
    ):
        supervisor = supervisor_actor(tx, pack)
        to_actor = None if supervisor is None else supervisor.actor_id
        body = main_ended_text(status, end_reason)
    if to_actor is None:
        return
    used = tx.execute(
        "SELECT COALESCE(MAX(step_no), 0) FROM message WHERE run_id = ?", (run.run_id,)
    ).fetchone()[0]
    insert_message(
        tx,
        pack.site_id,
        new_id("msg"),
        run_id=run.run_id,
        step_no=max(run.last_step_no, used) + 1,
        to_actor_id=to_actor,
        type_="NOTICE",
        proposal_id=None,
        body=body,
        agent_text=agent_text,
        context_version=site.context_version,
    )


def intake_incomplete_text(task_id: str | None, codes: list[str]) -> str:
    """접수 미완 통지의 서버 문구."""
    return (
        f"작업 요청 {task_id} 접수가 완료되지 않았습니다(사유: {', '.join(codes)}). "
        "작업은 만들어지지 않았습니다. 값을 확인해 다시 요청해 주세요."
    )


def main_ended_text(status: str, end_reason: str) -> str:
    """메인이 스스로 끝내지 못했을 때의 서버 문구."""
    return (
        f"메인 Agent가 일을 마치지 못하고 끝났습니다({status}: {end_reason}). 이 Case의 남은 일은 다음 "
        "메인에 넘겨지지 않습니다. Agent 활동에서 남은 충돌과 후보를 확인해 주세요."
    )


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


def stale_active_runs(tx: sqlite3.Connection, pack: LoadedPack, end_reason: str) -> list[str]:
    """열린 전문 Agent Run을 STALE로(Event 접수). 메인은 무효로 만들지 않는다: 하위 Run이 끝나면
    깨어나 신고를 본다.

    Work Intake Run은 뺀다: 폼이 Hold 중에도 접수되듯 Intake의 값은 아직 사실이 아니고, 완료할 때
    검증을 다시 한다.
    """
    ids = [
        r[0]
        for r in tx.execute(
            "SELECT run_id FROM agent_run WHERE site_id = ? AND status IN (?, ?)"
            " AND agent_type NOT IN ('INTAKE', 'MAIN') ORDER BY rowid",
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


def promote_queued(tx: sqlite3.Connection, pack: LoadedPack) -> list[str]:
    """대기 중인 접수(QUEUED 작업)를 전부 한 번에 READY로 올린다: context +1 한 번, RECHECK 한 번 (AG-07).

    사건은 접수 단위다. 폼·Work Intake로 온 작업은 작업마다 작업 준비됨, 일정으로 온 작업은 일정마다
    일정 넣기 사건 하나다(ST-24). 첫 사건이 메인을 띄우고 나머지는 그 메인의 Case에 들어간다.
    올린 task_id들(접수 순서). 없으면 빈 목록."""
    site_id = pack.site_id
    ids = queued_task_ids(tx, site_id)
    if not ids:
        return []
    context_version = bump_context_version(tx, site_id)
    tasks = {t.task_id: t for t in list_current_tasks(tx, site_id, pack)}
    revisions = {}
    for tid in ids:
        task = tasks[tid]
        revisions[tid] = task.revision + 1
        insert_task_revision(
            tx, site_id, task.model_copy(update={"revision": revisions[tid], "lifecycle": "READY"})
        )
        copy_consents(tx, site_id, tid, task.revision, revisions[tid], context_version)
    register_recheck(tx, site_id, {"kind": "QUEUE", "task_ids": ids})
    sources = schedule_of_tasks(tx, site_id)
    delivered: set[str] = set()
    for tid in ids:
        schedule_id = sources.get(tid)
        if schedule_id is None:
            cause = {"kind": "QUEUE", "task_id": tid, "actor_id": tasks[tid].owner_actor_id}
            deliver_event(tx, pack, "TASK_READY", f"TASK_READY:{tid}:{revisions[tid]}", cause)
        elif schedule_id not in delivered:
            delivered.add(schedule_id)
            ref = {
                "kind": "QUEUE",
                "schedule_id": schedule_id,
                "task_ids": [t for t in ids if sources.get(t) == schedule_id],
                "changed_task_ids": [],
                "actor_id": tasks[tid].owner_actor_id,
            }
            deliver_event(
                tx, pack, "SCHEDULE_IMPORTED", f"SCHEDULE_IMPORTED:{schedule_id}:queue", ref
            )
    return ids
