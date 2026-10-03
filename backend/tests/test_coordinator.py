"""dispatch 워커와 Coordinator 결정론 핸들러. 게이트 경로, T34."""

import time
import uuid

import pytest
from conftest import take_snapshot

from app.commands.approval import ApproveRequest, WaiveRequest, approve_and_commit, waive
from app.commands.events import EventReport, HoldRelease, receive_event, release_hold_command
from app.commands.task_request import TaskRequestForm, submit_task_request
from app.coordinator import transitions
from app.coordinator.dispatcher import (
    DispatchWorker,
    process_next,
    requeue_claimed_jobs,
    run_until_idle,
)
from app.domain.models import Conflict
from app.rules.engine import detect_conflicts
from app.solver import cpsat
from app.solver.candidate import build_candidate
from app.solver.search_spec import build_search_spec
from app.store import db
from app.store.repos.consultations import consultation_view, list_review_queue
from app.store.repos.dispatch import list_jobs, register_job
from app.store.repos.plans import get_current_plan
from app.store.repos.records import insert_search_spec, list_validations, register_solver_outcome
from app.store.repos.site import get_site

# ── 도우미 ─────────────────────────────────────────────────────


def _key():
    return uuid.uuid4().hex


def _submit_a(pack):
    data = pack.new_task.model_dump(exclude={"requested", "unit_id", "owner_actor_id", "movable"})
    out = submit_task_request(pack, "planner_a", _key(), TaskRequestForm(**data))
    assert out.status == "APPLIED"


def _site(pack):
    with db.read() as conn:
        return get_site(conn, pack.site_id)


def _jobs(pack, kind=None):
    with db.read() as conn:
        return [j for j in list_jobs(conn, pack.site_id) if kind is None or j["kind"] == kind]


def _count(table):
    with db.read() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _candidates(kind):
    with db.read() as conn:
        return [
            r[0] for r in conn.execute("SELECT candidate_id FROM candidate WHERE kind = ?", (kind,))
        ]


def _validations(pack, cid):
    with db.read() as conn:
        return list_validations(conn, pack.site_id, cid)


def _view(pack, cid):
    with db.read() as conn:
        return consultation_view(conn, pack.site_id, cid)


def _queue(pack):
    with db.read() as conn:
        return list_review_queue(conn, pack.site_id)


def _solve(pack, snap, level):
    conflict = detect_conflicts(snap, snap.facts().check_assignments(), pack)[0]
    spec = build_search_spec(snap, conflict, "UA", level)
    with db.write() as tx:
        insert_search_spec(tx, pack.site_id, spec)
    result = cpsat.solve(snap, spec, pack)
    cand = build_candidate(snap, spec, result)
    with db.write() as tx:
        register_solver_outcome(tx, snap, result, cand)
    return result, cand


def _approve(pack, cid, validation_id):
    body = ApproveRequest(
        candidate_id=cid,
        validation_id=validation_id,
        expected_context_version=_site(pack).context_version,
    )
    return approve_and_commit(pack, "supervisor", _key(), body)


def _event(pack, source="rep-1"):
    body = EventReport(source_event_id=source, event_type="DELAY", text="도장 준비 지연")
    out = receive_event(pack, "reporter", _key(), body)
    assert out.status == "APPLIED"
    return out.result_refs["hold_id"]


def _release(pack, hold_id):
    body = HoldRelease(
        hold_id=hold_id,
        resolution="NO_CHANGE",
        expected_context_version=_site(pack).context_version,
    )
    out = release_hold_command(pack, "supervisor", _key(), body)
    assert out.status == "APPLIED"


def _statuses(pack):
    return [(j["kind"], j["status"], j["attempts"]) for j in _jobs(pack)]


@pytest.fixture
def gate_r1(seeded):
    """게이트 경로를 R1까지 (워커 포함)."""
    pack = seeded
    _submit_a(pack)
    run_until_idle(pack)
    snap = take_snapshot(pack)
    _, none = _solve(pack, snap, "L0")
    _, alpha = _solve(pack, snap, "L1")
    assert none is None
    run_until_idle(pack)
    [v] = _validations(pack, alpha.candidate_id)
    body = WaiveRequest(candidate_id=alpha.candidate_id, task_ids=("C",), comment="확인")
    assert waive(pack, "supervisor", _key(), body).status == "APPLIED"
    assert _approve(pack, alpha.candidate_id, v.validation_id).status == "APPLIED"
    return pack, alpha


# ── 게이트 경로·T34 ────────────────────────────────────────────


def test_gate_path_with_worker(seeded, main_on):
    pack = seeded
    _submit_a(pack)
    run_until_idle(pack)
    # 사건이 메인을 띄운다. 모델이 없으면 메인은 아직 돌지 않는다(CONTINUE_RUN 대기)
    [start] = _jobs(pack, "CONTINUE_RUN")
    assert start["status"] == "PENDING" and start["attempts"] == 0
    with db.read() as conn:
        [main] = conn.execute("SELECT agent_type, status, run_id FROM agent_run").fetchall()
    assert tuple(main) == ("MAIN", "RUNNING", start["run_id"])
    assert _jobs(pack, "START_RUN") == []  # 재계획은 메인이 부른다

    snap = take_snapshot(pack)
    l0, none = _solve(pack, snap, "L0")
    assert l0.stage1["status"] == "INFEASIBLE" and none is None
    _, alpha = _solve(pack, snap, "L1")
    run_until_idle(pack)

    [v] = _validations(pack, alpha.candidate_id)
    assert v.status == "PASS"
    view = _view(pack, alpha.candidate_id)
    assert {i.task_id: i.base_status for i in view.items} == {"A": "COVERED", "C": "PENDING"}
    assert _queue(pack) == [alpha.candidate_id]
    assert {(j["kind"], j["status"]) for j in _jobs(pack) if j["kind"] != "CONTINUE_RUN"} == {
        ("RECHECK", "DONE"),
        ("VALIDATE", "DONE"),
        ("BUILD_CONSULTATION", "DONE"),
    }

    body = WaiveRequest(candidate_id=alpha.candidate_id, task_ids=("C",), comment="확인")
    assert waive(pack, "supervisor", _key(), body).status == "APPLIED"
    out = _approve(pack, alpha.candidate_id, v.validation_id)
    assert out.status == "APPLIED" and out.plan_revision == 1


def test_t34_hold_release_without_change_reconfirms(gate_r1):
    pack, alpha = gate_r1
    _release(pack, _event(pack))
    run_until_idle(pack)
    [rc] = _candidates("RECONFIRM")
    with db.read() as conn:
        plan = get_current_plan(conn, pack.site_id)
    [v] = _validations(pack, rc)
    assert v.status == "PASS"
    view = _view(pack, rc)
    assert view.items == () and view.status == "COMPLETE"
    assert _queue(pack) == [rc]
    out = _approve(pack, rc, v.validation_id)
    assert out.status == "APPLIED" and out.plan_revision == 2
    with db.read() as conn:
        r2 = get_current_plan(conn, pack.site_id)
    assert r2.candidate_id == rc and r2.assignments == plan.assignments == alpha.assignments


# ── RECHECK ────────────────────────────────────────────────────


def test_recheck_skips_while_hold_active(seeded):
    _submit_a(seeded)
    _event(seeded)
    snapshots = _count("snapshot")
    run_until_idle(seeded)
    assert _statuses(seeded) == [("RECHECK", "DONE", 1)]
    assert _count("snapshot") == snapshots and _count("candidate") == 0


def test_recheck_skips_when_plan_is_current(gate_r1):
    pack, _ = gate_r1
    with db.write() as tx:  # R1은 현재 context에서 확정됐다
        register_job(tx, pack.site_id, "RECHECK", "RECHECK:manual", {})
    snapshots, candidates = _count("snapshot"), _count("candidate")
    run_until_idle(pack)
    assert (_count("snapshot"), _count("candidate")) == (snapshots, candidates)
    assert _jobs(pack, "RECHECK")[-1]["status"] == "DONE"


def test_recheck_twice_has_one_effect(seeded):
    _submit_a(seeded)
    run_until_idle(seeded)
    snapshots = _count("snapshot")
    with db.write() as tx:  # 같은 job이 다시 배달된 경우
        tx.execute("UPDATE dispatch_job SET status = 'PENDING' WHERE kind = 'RECHECK'")
    run_until_idle(seeded)
    # 충돌이 있으면 RECHECK는 아무것도 만들지 않는다(재계획은 메인이 부른다)
    assert _jobs(seeded, "START_RUN") == []
    assert (_count("snapshot"), _count("candidate")) == (snapshots, 0)


def test_recheck_reuses_reconfirm_candidate(gate_r1):
    pack, _ = gate_r1
    hold_id = _event(pack)
    _release(pack, hold_id)
    with db.write() as tx:  # 같은 (context, plan)에 RECHECK가 하나 더
        register_job(tx, pack.site_id, "RECHECK", "RECHECK:again", {})
    run_until_idle(pack)
    [rc] = _candidates("RECONFIRM")
    assert len(_jobs(pack, "VALIDATE")) == 2  # Alpha + RECONFIRM 1개
    assert len(_validations(pack, rc)) == 1


def test_conflict_groups_share_tasks_and_list_units():
    """충돌 그룹 = 작업을 공유하는 충돌의 묶음. 그룹마다 작업을 가진 Unit이 나온다."""
    from app.domain.groups import conflict_groups
    from app.domain.models import SnapshotContent

    def task(tid, unit):
        return {
            "task_id": tid,
            "revision": 1,
            "unit_id": unit,
            "owner_actor_id": "x",
            "work_type": "LIFTING",
            "hazard_tags": ["LIFTING"],
            "zone_id": "B",
            "duration": 10,
            "earliest_start": 0,
            "latest_start": 0,
            "latest_end": 10,
            "movable": {"time": True, "resource": False},
            "fields": {},
            "lifecycle": "READY",
        }

    facts = SnapshotContent.model_validate(
        {
            "site_id": "S",
            "pack_hash": "h",
            "horizon_minutes": 60,
            "work_intervals": [[0, 60]],
            "context_version": 1,
            "plan_revision": 0,
            "tasks": [task("A", "UA"), task("B", "UB"), task("E", "UB")],
            "resources": [],
            "zones": ["B"],
            "zone_relations": [],
            "plan": {"plan_revision": 0, "assignments": []},
        }
    )
    tasks = facts.task_map()
    c_ab = Conflict(rule_id="R1", task_ids=("A", "B"), zone_ids=("B",), interval=(0, 10))
    c_e = Conflict(rule_id="R2", task_ids=("E",), zone_ids=("B",), interval=(0, 10))
    c_be = Conflict(rule_id="R3", task_ids=("B", "E"), zone_ids=("B",), interval=(0, 10))
    first, second = conflict_groups([c_ab, c_e], tasks)
    assert (first.task_ids, first.units) == (("A", "B"), {"UA": ("A",), "UB": ("B",)})
    assert (second.task_ids, second.units) == (("E",), {"UB": ("E",)})
    assert first.group_id != second.group_id
    # 작업을 공유하는 충돌은 한 그룹이다. 그룹 ID는 작업 집합에서 나온다(탐지 순서와 무관)
    [merged] = conflict_groups([c_ab, c_e, c_be], tasks)
    assert (merged.task_ids, merged.units) == (("A", "B", "E"), {"UA": ("A",), "UB": ("B", "E")})
    assert conflict_groups([c_be, c_e, c_ab], tasks)[0].group_id == merged.group_id


# ── VALIDATE ───────────────────────────────────────────────────


def test_validate_skips_existing_validation(gate_r1, monkeypatch):
    pack, alpha = gate_r1
    monkeypatch.setattr(transitions, "validate", lambda *a: pytest.fail("revalidated"))
    with db.write() as tx:
        tx.execute("UPDATE dispatch_job SET status = 'PENDING' WHERE kind = 'VALIDATE'")
    run_until_idle(pack)
    assert len(_validations(pack, alpha.candidate_id)) == 1


def test_validate_stale_candidate(seeded):
    _submit_a(seeded)
    snap = take_snapshot(seeded)
    _, alpha = _solve(seeded, snap, "L1")
    _event(seeded)  # Alpha STALE
    run_until_idle(seeded)
    [v] = _validations(seeded, alpha.candidate_id)
    assert v.status == "PASS"
    assert _view(seeded, alpha.candidate_id).status == "CANCELLED"


# ── 실패·재시도·claim ──────────────────────────────────────────


def test_handler_failure_retries_then_failed_and_next_job_runs(seeded, monkeypatch):
    _submit_a(seeded)  # RECHECK job
    snap = take_snapshot(seeded)
    _, alpha = _solve(seeded, snap, "L1")  # VALIDATE job
    snapshots = _count("snapshot")

    def boom(*args, **kwargs):
        raise RuntimeError("forced")

    monkeypatch.setattr(transitions, "detect_conflicts", boom)
    recheck_id = _jobs(seeded, "RECHECK")[0]["job_id"]
    assert process_next(seeded) == recheck_id
    [job] = _jobs(seeded, "RECHECK")
    assert (job["status"], job["attempts"]) == ("PENDING", 1) and "forced" in job["last_error"]
    assert _count("snapshot") == snapshots  # 롤백
    processed = run_until_idle(seeded)
    assert processed[:2] == [recheck_id, recheck_id]
    [job] = _jobs(seeded, "RECHECK")
    assert (job["status"], job["attempts"]) == ("FAILED", 3)
    assert _jobs(seeded, "VALIDATE")[0]["status"] == "DONE"
    assert _validations(seeded, alpha.candidate_id)[0].status == "PASS"


def test_requeue_claimed_on_start(seeded):
    _submit_a(seeded)
    with db.write() as tx:
        tx.execute("UPDATE dispatch_job SET status = 'CLAIMED'")
    assert process_next(seeded) is None
    assert requeue_claimed_jobs(seeded) == 1
    assert _jobs(seeded, "RECHECK")[0]["status"] == "PENDING"
    assert run_until_idle(seeded)


def test_unhandled_kinds_are_not_claimed_and_do_not_block(seeded):
    from conftest import add_run

    sid = seeded.site_id
    add_run(seeded, "r", status="ERROR")
    with db.write() as tx:
        register_job(tx, sid, "START_RUN", "START_RUN:x", {})
        register_job(tx, sid, "RESUME_RUN", "RESUME_RUN:r:1", run_id="r", wait_generation=1)
        register_job(tx, sid, "CONTINUE_RUN", "CONTINUE_RUN:r", run_id="r")
    _submit_a(seeded)
    run_until_idle(seeded)
    assert _statuses(seeded)[:3] == [
        ("START_RUN", "PENDING", 0),
        ("RESUME_RUN", "PENDING", 0),
        ("CONTINUE_RUN", "PENDING", 0),
    ]
    assert _jobs(seeded, "RECHECK")[0]["status"] == "DONE"


def test_worker_thread_smoke(seeded):
    _submit_a(seeded)
    worker = DispatchWorker(seeded, poll_s=0.05)
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while _jobs(seeded, "RECHECK")[0]["status"] != "DONE":
            assert time.monotonic() < deadline, "worker did not process RECHECK"
            time.sleep(0.05)
    finally:
        worker.stop()
    assert not worker._thread.is_alive()
