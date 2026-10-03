"""Coordinator 핸들러. RECHECK·VALIDATE·BUILD_CONSULTATION·START_RUN·RESUME_RUN·CONTINUE_RUN.

핸들러는 job 1건을 처리하고, 효과와 job DONE을 같은 write 트랜잭션에서 기록한다. 같은 job을
두 번 처리해도 효과는 1회다. app.solver를 import하지 않는다(Solver는 Replanning Run이 부른다).
agents는 runtime만 import한다. START_RUN은 Run을 만든 tx를 커밋한 뒤 tx 밖에서 그래프를 부른다.
"""

import sqlite3
from typing import Any

from app.agents import runtime
from app.commands.consultation import build_consultation
from app.domain.canonical import canonical_hash
from app.domain.hashes import candidate_hash
from app.domain.ids import new_id
from app.domain.models import AgentRun, Candidate, Snapshot, SnapshotContent
from app.packs.loader import LoadedPack
from app.rules.engine import detect_conflicts
from app.store import db
from app.store.repos.case_events import record_case_event
from app.store.repos.cases import claim_resume, end_case_run, wake_run
from app.store.repos.consultations import candidate_state
from app.store.repos.dispatch import mark_done, register_job, set_job_run
from app.store.repos.events import get_hold, list_active_holds
from app.store.repos.plans import get_current_plan
from app.store.repos.records import (
    find_reconfirm_candidate,
    get_candidate,
    get_search_spec,
    get_snapshot,
    insert_candidate,
    insert_validation,
    list_validations,
)
from app.store.repos.runs import (
    get_run,
    insert_run,
    mark_restart,
    run_for_solver_result,
    set_contract_version,
)
from app.store.repos.site import get_site
from app.store.repos.snapshots import build_snapshot_content, create_snapshot
from app.store.repos.tasks import list_current_tasks
from app.validator.validator import validate

Job = dict[str, Any]


def recover_running_runs(pack: LoadedPack) -> list[str]:
    """기동 복구 (ST-19). RUNNING으로 남은 Run마다 예약만 된 step을 정리하고 CONTINUE_RUN을 등록한다.

    워커가 하나라 기동 시점의 RUNNING Run은 모두 중단된 것이다. 완료된 step은 그대로 두고 관찰부터
    다시 부르므로 커밋된 Action은 다시 실행되지 않는다. 등록한 run_id 목록.
    """
    out = []
    with db.write() as tx:
        ids = [
            r[0]
            for r in tx.execute(
                "SELECT run_id FROM agent_run WHERE site_id = ? AND status = 'RUNNING'"
                " ORDER BY rowid",
                (pack.site_id,),
            )
        ]
        for run_id in ids:
            count = mark_restart(tx, run_id)
            if count is not None:
                key = f"CONTINUE_RUN:{run_id}:{count}"
                register_job(
                    tx, pack.site_id, "CONTINUE_RUN", key, {"run_id": run_id}, run_id=run_id
                )
                out.append(run_id)
    return out


def _reconfirm_candidate(snapshot_id: str, snapshot_hash: str, facts: SnapshotContent) -> Candidate:
    """배정 = snapshot.base_assignments()."""
    assignments = tuple(facts.base_assignments().values())
    return Candidate(
        candidate_id=new_id("cand"),
        snapshot_id=snapshot_id,
        search_spec_id=None,
        search_spec_hash=None,
        solver_result_id=None,
        base_plan_revision=facts.plan_revision,
        context_version=facts.context_version,
        pack_hash=facts.pack_hash,
        assignments=assignments,
        candidate_hash=candidate_hash(
            assignments,
            facts.plan_revision,
            facts.context_version,
            snapshot_hash,
            None,
            facts.pack_hash,
        ),
        kind="RECONFIRM",
    )


def _register_validate(tx: sqlite3.Connection, site_id: str, candidate_id: str) -> None:
    register_job(
        tx, site_id, "VALIDATE", f"VALIDATE:{candidate_id}", {"candidate_id": candidate_id}
    )


def recheck(pack: LoadedPack, job: Job) -> None:
    """ACTIVE Hold 없음 ∧ (Plan이 현재 Context보다 뒤처짐 ∨ Plan 밖 READY 작업 있음) ∧ 충돌 없음일 때
    RECONFIRM 후보 + VALIDATE를 만든다. 한 write 트랜잭션. 충돌이 있으면 아무것도 하지 않는다:
    재계획을 부를지는 메인이 판단한다."""
    site_id = pack.site_id
    with db.write() as tx:
        site = get_site(tx, site_id)
        plan = get_current_plan(tx, site_id)
        assert site is not None and plan is not None
        ctx, rev = site.context_version, site.plan_revision
        in_plan = {a.task_id for a in plan.assignments}
        pending_requests = any(
            t.lifecycle == "READY" and t.task_id not in in_plan
            for t in list_current_tasks(tx, site_id, pack)
        )
        if list_active_holds(tx, site_id) or (
            plan.committed_context_version == ctx and not pending_requests
        ):
            pass  # 다시 확인할 것이 없다
        elif (existing := find_reconfirm_candidate(tx, site_id, ctx, rev)) is not None:
            _register_validate(tx, site_id, existing.candidate_id)
        else:
            # 충돌 검사는 저장하지 않은 Snapshot으로 한다. 재확인 후보를 만들 때만 저장한다
            content = build_snapshot_content(tx, site_id, pack)
            probe = Snapshot(
                snapshot_id="recheck", snapshot_hash=canonical_hash(content), content=content
            )
            if not detect_conflicts(probe, probe.facts().check_assignments(), pack):
                snapshot = create_snapshot(tx, site_id, pack)
                facts = snapshot.facts()
                candidate = _reconfirm_candidate(
                    snapshot.snapshot_id, snapshot.snapshot_hash, facts
                )
                insert_candidate(tx, site_id, candidate)
                _register_validate(tx, site_id, candidate.candidate_id)
        mark_done(tx, job["job_id"])


def validate_candidate(pack: LoadedPack, job: Job) -> None:
    """Validator는 트랜잭션 밖에서 돌리고, 등록·후속 job·DONE은 한 write 트랜잭션.

    이미 validation이 있는 후보는 다시 검증하지 않는다. STALE 후보도 검증한다.
    """
    site_id = pack.site_id
    candidate_id = job["payload"]["candidate_id"]
    validation = None
    with db.read() as conn:
        if not list_validations(conn, site_id, candidate_id):
            candidate = get_candidate(conn, site_id, candidate_id)
            if candidate is None:
                raise LookupError(f"candidate {candidate_id} not found")
            snapshot = get_snapshot(conn, candidate.snapshot_id)
            if snapshot is None:
                raise LookupError(f"snapshot {candidate.snapshot_id} not found")
            spec = None
            if candidate.search_spec_id is not None:
                spec = get_search_spec(conn, candidate.search_spec_id)
                if spec is None:
                    raise LookupError(f"search_spec {candidate.search_spec_id} not found")
            validation = validate(snapshot, candidate, spec, pack)
    with db.write() as tx:
        stored = list_validations(tx, site_id, candidate_id)
        if not stored and validation is not None:
            insert_validation(tx, site_id, validation)
            stored = [validation]
        # Solver 후보가 C01–C10 FAIL이면 모델·검증 불일치로 Run ERROR.
        # 그 밖의 검증 결과(PASS 포함)는 후보를 만든 Run을 깨운다: Run은 검증까지만 산다 (AG-25).
        candidate = get_candidate(tx, site_id, candidate_id)
        if (
            candidate is not None
            and candidate.kind == "REPLAN"
            and candidate.solver_result_id is not None
        ):
            run_id = run_for_solver_result(tx, candidate.solver_result_id)
            failed = any(c.status == "FAIL" for v in stored for c in v.checks)
            if run_id is not None and failed:
                end_case_run(tx, pack, run_id, "ERROR", "MODEL_VALIDATION_MISMATCH")
            elif run_id is not None:
                wake_run(tx, site_id, run_id)
        if any(v.status == "PASS" for v in stored):
            register_job(
                tx,
                site_id,
                "BUILD_CONSULTATION",
                f"BUILD_CONSULTATION:{candidate_id}",
                {"candidate_id": candidate_id},
            )
        mark_done(tx, job["job_id"])


def build_consultation_job(pack: LoadedPack, job: Job) -> None:
    """Consultation 생성. 협의 Run을 부를지는 메인이 판단한다."""
    candidate_id = job["payload"]["candidate_id"]
    with db.write() as tx:
        build_consultation(tx, pack.site_id, candidate_id)
        mark_done(tx, job["job_id"])


def _start_allowed(tx: sqlite3.Connection, pack: LoadedPack, payload: dict[str, Any]) -> bool:
    """START_RUN 처리 시점 재확인. agent_type별로 다르다."""
    site = get_site(tx, pack.site_id)
    assert site is not None
    if payload["agent_type"] == "INTAKE":
        # 요청한 task_id가 아직 없을 때만. 열린 Case·Hold와 무관하다(폼과 같다).
        return not tx.execute(
            "SELECT 1 FROM task WHERE site_id = ? AND task_id = ?",
            (pack.site_id, payload.get("task_id")),
        ).fetchone()
    if payload["agent_type"] == "EVENT_RESPONSE":
        # 그 Event의 Hold가 아직 걸려 있을 때만. 열린 Case와 무관하다.
        hold = get_hold(tx, pack.site_id, payload.get("hold_id") or "")
        return hold is not None and hold["status"] == "ACTIVE"
    if payload["agent_type"] != "COORDINATION":
        # 재계획: Hold가 없고, 메인이 부를 때 본 사실 그대로일 때만
        return not list_active_holds(tx, pack.site_id) and (
            payload.get("context_version"),
            payload.get("plan_revision"),
        ) == (site.context_version, site.plan_revision)
    if payload.get("phase") == "NOTICE":
        return payload.get("plan_revision") == site.plan_revision
    if payload.get("phase") == "ASK":
        # 사전 확인(후보 없음): Hold가 없고, 메인이 부를 때 본 현장 정보 그대로일 때만
        return (
            not list_active_holds(tx, pack.site_id)
            and payload.get("context_version") == site.context_version
        )
    candidate = get_candidate(tx, pack.site_id, payload["candidate_id"])
    if candidate is None or list_active_holds(tx, pack.site_id):
        return False
    state = candidate_state(tx, pack.site_id, candidate)
    return not (state.stale or state.rejected or state.committed)


CALL_REFS = (
    "agent_type",
    "group_id",
    "acting_unit_id",
    "phase",
    "candidate_id",
    "plan_revision",
    "event_id",
    "need_ids",
)


def _parent_waits(tx: sqlite3.Connection, parent_id: str, child_id: str) -> bool:
    """부른 메인이 이 하위 Run을 기다리고 있는가."""
    parent = get_run(tx, parent_id)
    return (
        parent is not None
        and parent.status == "WAITING_HUMAN"
        and (parent.wait_kind, parent.wait_ref) == ("CHILD_RUN", child_id)
    )


def start_run(pack: LoadedPack, job: Job, model_factory: runtime.ModelFactory) -> str | None:
    """처리 시점에 Hold·열린 Case·(context, plan)을 다시 확인하고 맞으면 Run을 만든다.

    Run 생성·job.run_id·DONE은 tx 하나. 그래프는 커밋 후 tx 밖에서 부른다. 만든 run_id를 반환한다.
    """
    site_id = pack.site_id
    payload = job["payload"]
    with db.write() as tx:
        site = get_site(tx, site_id)
        assert site is not None
        run_id = None
        parent_id = payload.get("parent_run_id")
        if parent_id is not None and not _parent_waits(tx, parent_id, payload["run_id"]):
            pass  # 부른 메인이 이미 끝났거나 다른 것을 기다린다
        elif _start_allowed(tx, pack, payload):
            run_id = payload.get("run_id") or new_id("run")
            insert_run(
                tx,
                site_id,
                AgentRun(
                    run_id=run_id,
                    agent_type=payload["agent_type"],
                    parent_run_id=parent_id,
                    # 하위 Run은 부른 메인의 Case를 쓴다
                    case_id=payload.get("case_id") or new_id("case"),
                    acting_actor_id=payload.get("acting_actor_id"),
                    acting_unit_id=payload["acting_unit_id"],
                    input_ref={**payload, "job_id": job["job_id"]},
                    exec_contract_version=runtime.exec_contract_version(payload["agent_type"]),
                    status="RUNNING",
                ),
            )
            set_job_run(tx, job["job_id"], run_id)
        elif parent_id is not None:
            # 시작 조건이 맞지 않는다(Hold, 사실 변경 등). 부른 메인에게 사건으로 알리고 깨운다
            record_case_event(
                tx,
                site_id,
                "CHILD_RUN_ENDED",
                f"CHILD_RUN_ENDED:{payload['run_id']}",
                {
                    "run_id": payload["run_id"],
                    "parent_run_id": parent_id,
                    "status": "NOT_STARTED",
                    "reason": "START_NOT_ALLOWED",
                    "call": {k: payload[k] for k in CALL_REFS if k in payload},
                },
                payload.get("case_id"),
            )
            wake_run(tx, site_id, parent_id)
        mark_done(tx, job["job_id"])
    if run_id is None:
        return None
    try:
        model = model_factory()
    except Exception as e:  # noqa: BLE001 — 모델을 만들 수 없으면(키 없음 등) Run ERROR로 드러낸다
        with db.write() as tx:
            end_case_run(tx, pack, run_id, "ERROR", f"MODEL_UNAVAILABLE: {type(e).__name__}")
        return run_id
    runtime.invoke(pack, {"run_id": run_id}, model)
    return run_id


def resume_run(pack: LoadedPack, job: Job, model_factory: runtime.ModelFactory) -> bool:
    """RESUME_RUN: `WAITING_HUMAN ∧ wait_generation 일치` 조건부 claim과 job DONE을 tx 하나에서 한다.

    0행이면 무효(이미 재개됨·세대 불일치·종료됨). 1행이면 tx 밖에서 같은 run_id로
    그래프를 observe부터 새로 호출한다. 변화는 wake_seq로 보존된다.
    """
    run_id, generation = job["run_id"], job["wait_generation"]
    with db.write() as tx:
        claimed = claim_resume(tx, run_id, generation)
        mark_done(tx, job["job_id"])
    if not claimed:
        return False
    try:
        model = model_factory()
    except Exception as e:  # noqa: BLE001
        with db.write() as tx:
            end_case_run(tx, pack, run_id, "ERROR", f"MODEL_UNAVAILABLE: {type(e).__name__}")
        return True
    runtime.invoke(pack, {"run_id": run_id}, model)
    return True


def continue_run(pack: LoadedPack, job: Job, model_factory: runtime.ModelFactory) -> bool:
    """CONTINUE_RUN: Run이 아직 RUNNING이면 tx 밖에서 같은 run_id로 그래프를 observe부터 부른다."""
    run_id = job["run_id"]
    with db.write() as tx:
        run = get_run(tx, run_id)
        if run is not None:
            set_contract_version(tx, run_id, runtime.exec_contract_version(run.agent_type))
        mark_done(tx, job["job_id"])
    if run is None or run.status != "RUNNING":
        return False
    try:
        model = model_factory()
    except Exception as e:  # noqa: BLE001
        with db.write() as tx:
            end_case_run(tx, pack, run_id, "ERROR", f"MODEL_UNAVAILABLE: {type(e).__name__}")
        return True
    runtime.invoke(pack, {"run_id": run_id}, model)
    return True


HANDLERS = {
    "RECHECK": recheck,
    "VALIDATE": validate_candidate,
    "BUILD_CONSULTATION": build_consultation_job,
}
