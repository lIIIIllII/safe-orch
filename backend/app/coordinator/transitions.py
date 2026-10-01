"""Coordinator 핸들러 (설계서 §11.5, 부록 A.15·A.16). RECHECK·VALIDATE·BUILD_CONSULTATION·START_RUN.

핸들러는 job 1건을 처리하고, 효과와 job DONE을 같은 write 트랜잭션에서 기록한다. 같은 job을
두 번 처리해도 효과는 1회다. app.solver를 import하지 않는다(Solver는 Replanning Run이 부른다).
agents는 runtime만 import한다. START_RUN은 Run을 만든 tx를 커밋한 뒤 tx 밖에서 그래프를 부른다.
"""

import sqlite3
from typing import Any

from app.agents import runtime
from app.commands.consultation import build_consultation
from app.config import get_settings
from app.domain.hashes import candidate_hash
from app.domain.ids import new_id
from app.domain.models import AgentRun, Candidate, Conflict, SnapshotContent
from app.packs.loader import LoadedPack
from app.rules.engine import detect_conflicts
from app.store import db
from app.store.repos.cases import claim_resume, end_case_run, register_coordination, wake_run
from app.store.repos.consultations import candidate_state, consultation_view
from app.store.repos.dispatch import job_exists, mark_done, register_job, set_job_run
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
from app.store.repos.runs import get_run, has_open_case, insert_run, run_for_solver_result
from app.store.repos.site import get_site, list_actors
from app.store.repos.snapshots import create_snapshot
from app.store.repos.tasks import list_current_tasks
from app.validator.validator import validate

Job = dict[str, Any]


def choose_acting(
    facts: SnapshotContent, conflicts: list[Conflict], cause: dict[str, Any]
) -> tuple[str, Conflict]:
    """acting_unit과 주 충돌 (부록 A.15).

    cause 작업이 충돌에 있으면 그 Unit, 아니면 충돌 작업 중 Plan에 없는 요청 작업(task_id가 가장
    작은 것)의 Unit, 그것도 없으면 충돌 작업 중 task_id가 가장 작은 작업의 Unit (A.20 보충: 철회 뒤
    RECHECK처럼 cause 작업이 충돌에 없을 때 남은 요청의 요청자가 재계획한다).
    주 충돌 = detect_conflicts 순서에서 acting_unit 작업을 포함한 첫 충돌.
    """
    tasks = facts.task_map()
    in_conflict = sorted({tid for c in conflicts for tid in c.task_ids})
    in_plan = {a.task_id for a in facts.plan.assignments}
    requests = [tid for tid in in_conflict if tid not in in_plan]
    cause_task = cause.get("task_id")
    acting_task = cause_task if cause_task in in_conflict else (requests or in_conflict)[0]
    unit = tasks[acting_task].unit_id
    primary = next(c for c in conflicts if any(tasks[t].unit_id == unit for t in c.task_ids))
    return unit, primary


def _acting_actor(
    tx: sqlite3.Connection, site_id: str, unit: str, cause: dict[str, Any]
) -> str | None:
    if cause.get("kind") in ("FORM", "QUEUE", "INTAKE"):
        return cause.get("actor_id")  # 요청자 (대기열에서 올라온 요청도 요청자가 재계획한다, A.21)
    planners = [
        a.actor_id
        for a in list_actors(tx, site_id)
        if a.unit_id == unit and "UNIT_PLANNER" in a.roles
    ]
    return planners[0] if planners else None


def _reconfirm_candidate(snapshot_id: str, snapshot_hash: str, facts: SnapshotContent) -> Candidate:
    """배정 = snapshot.base_assignments() (부록 A.14)."""
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
    """ACTIVE Hold 없음 ∧ 열린 Case 없음 ∧ (Plan이 현재 Context보다 뒤처짐 ∨ Plan 밖 READY 작업
    있음)일 때만: 충돌이면 START_RUN 등록, 없으면 RECONFIRM 후보 + VALIDATE. 한 write 트랜잭션
    (부록 A.15·A.16). START_RUN 키에 plan을 넣는다(확정은 context를 바꾸지 않는다, A.21 4)."""
    site_id = pack.site_id
    cause = job["payload"].get("cause") or {}
    with db.write() as tx:
        site = get_site(tx, site_id)
        plan = get_current_plan(tx, site_id)
        assert site is not None and plan is not None
        ctx, rev = site.context_version, site.plan_revision
        start_key = f"START_RUN:REPLANNING:ctx{ctx}:plan{rev}"
        in_plan = {a.task_id for a in plan.assignments}
        pending_requests = any(
            t.lifecycle == "READY" and t.task_id not in in_plan
            for t in list_current_tasks(tx, site_id, pack)
        )
        if (
            list_active_holds(tx, site_id)
            or has_open_case(tx, site_id)
            or (plan.committed_context_version == ctx and not pending_requests)
        ):
            pass  # 재계획하지 않는다
        elif (existing := find_reconfirm_candidate(tx, site_id, ctx, rev)) is not None:
            _register_validate(tx, site_id, existing.candidate_id)
        elif not job_exists(tx, site_id, start_key):
            snapshot = create_snapshot(tx, site_id, pack)
            facts = snapshot.facts()
            conflicts = detect_conflicts(snapshot, facts.check_assignments(), pack)
            if conflicts:
                unit, primary = choose_acting(facts, conflicts, cause)
                payload = {
                    "agent_type": "REPLANNING",
                    "acting_unit_id": unit,
                    "acting_actor_id": _acting_actor(tx, site_id, unit, cause),
                    "snapshot_id": snapshot.snapshot_id,
                    "conflict": {"rule_id": primary.rule_id, "task_ids": list(primary.task_ids)},
                    "context_version": ctx,
                    "plan_revision": rev,
                    "cause": cause,
                }
                register_job(tx, site_id, "START_RUN", start_key, payload)
            else:
                candidate = _reconfirm_candidate(
                    snapshot.snapshot_id, snapshot.snapshot_hash, facts
                )
                insert_candidate(tx, site_id, candidate)
                _register_validate(tx, site_id, candidate.candidate_id)
        mark_done(tx, job["job_id"])


def validate_candidate(pack: LoadedPack, job: Job) -> None:
    """Validator는 트랜잭션 밖에서 돌리고, 등록·후속 job·DONE은 한 write 트랜잭션 (부록 A.15).

    이미 validation이 있는 후보는 다시 검증하지 않는다. STALE 후보도 검증한다(A.13).
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
        # Solver 후보가 C01–C10 FAIL이면 모델·검증 불일치로 Run ERROR (§8, A.13·A.16).
        # C11만 걸린 INCOMPLETE(비PASS)는 Run을 깨운다 (§11.5, A.21 3).
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
            elif run_id is not None and not any(v.status == "PASS" for v in stored):
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
    """Consultation 생성. 설정이 켜졌고 Run이 만든 후보에 동의 대기(PENDING) 항목이 있으면 같은 tx에서
    Coordination 협의 Run을 등록한다(기본안 A, A.24 2). 꺼져 있으면 검토 대기(기본안 B)."""
    candidate_id = job["payload"]["candidate_id"]
    with db.write() as tx:
        build_consultation(tx, pack.site_id, candidate_id)
        if get_settings().coordination_enabled:
            _register_consult(tx, pack, candidate_id)
        mark_done(tx, job["job_id"])


def _register_consult(tx: sqlite3.Connection, pack: LoadedPack, candidate_id: str) -> None:
    candidate = get_candidate(tx, pack.site_id, candidate_id)
    if candidate is None or candidate.kind != "REPLAN" or candidate.solver_result_id is None:
        return
    run_id = run_for_solver_result(tx, candidate.solver_result_id)
    run = get_run(tx, run_id) if run_id else None
    view = consultation_view(tx, pack.site_id, candidate_id)
    if run is None or view is None or "PENDING" not in view.item_status.values():
        return
    key = f"START_RUN:COORDINATION:CONSULT:{candidate_id}"
    register_coordination(tx, pack, "CONSULT", key, candidate_id, run.case_id)


def _start_allowed(tx: sqlite3.Connection, pack: LoadedPack, payload: dict[str, Any]) -> bool:
    """START_RUN 처리 시점 재확인 (§11.5, A.16·A.24 2). agent_type별로 다르다."""
    site = get_site(tx, pack.site_id)
    assert site is not None
    if payload["agent_type"] == "INTAKE":
        # 요청한 task_id가 아직 없을 때만 (A.26 1). 열린 Case·Hold와 무관하다(폼과 같다).
        return not tx.execute(
            "SELECT 1 FROM task WHERE site_id = ? AND task_id = ?",
            (pack.site_id, payload.get("task_id")),
        ).fetchone()
    if payload["agent_type"] == "EVENT_RESPONSE":
        # 그 Event의 Hold가 아직 걸려 있을 때만 (A.25 1). 열린 Case와 무관하다.
        hold = get_hold(tx, pack.site_id, payload.get("hold_id") or "")
        return hold is not None and hold["status"] == "ACTIVE"
    if payload["agent_type"] != "COORDINATION":
        return (
            not list_active_holds(tx, pack.site_id)
            and not has_open_case(tx, pack.site_id)
            and (payload.get("context_version"), payload.get("plan_revision"))
            == (site.context_version, site.plan_revision)
        )
    if payload.get("phase") == "NOTICE":
        return payload.get("plan_revision") == site.plan_revision
    candidate = get_candidate(tx, pack.site_id, payload["candidate_id"])
    if candidate is None or list_active_holds(tx, pack.site_id):
        return False
    state = candidate_state(tx, pack.site_id, candidate)
    return not (state.stale or state.rejected or state.committed)


def start_run(pack: LoadedPack, job: Job, model_factory: runtime.ModelFactory) -> str | None:
    """처리 시점에 Hold·열린 Case·(context, plan)을 다시 확인하고 맞으면 Run을 만든다 (A.16).

    Run 생성·job.run_id·DONE은 tx 하나. 그래프는 커밋 후 tx 밖에서 부른다. 만든 run_id를 반환한다.
    """
    site_id = pack.site_id
    payload = job["payload"]
    with db.write() as tx:
        site = get_site(tx, site_id)
        assert site is not None
        run_id = None
        if _start_allowed(tx, pack, payload):
            run_id = new_id("run")
            insert_run(
                tx,
                site_id,
                AgentRun(
                    run_id=run_id,
                    agent_type=payload["agent_type"],
                    # Coordination은 후보 Run의 Case를 잇는다 (A.24 3)
                    case_id=payload.get("case_id") or new_id("case"),
                    acting_actor_id=payload.get("acting_actor_id"),
                    acting_unit_id=payload["acting_unit_id"],
                    input_ref={**payload, "job_id": job["job_id"]},
                    exec_contract_version=runtime.exec_contract_version(payload["agent_type"]),
                    status="RUNNING",
                ),
            )
            set_job_run(tx, job["job_id"], run_id)
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

    0행이면 무효(이미 재개됨·세대 불일치·종료됨, §11.3(3)·I-17). 1행이면 tx 밖에서 같은 run_id로
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


HANDLERS = {
    "RECHECK": recheck,
    "VALIDATE": validate_candidate,
    "BUILD_CONSULTATION": build_consultation_job,
}
