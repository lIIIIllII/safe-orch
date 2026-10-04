"""담당자의 직접 이동과 작업 없애기 (AG-31·CV-28).

담당자가 자기 작업의 시각을 옮기면 사람이 만든 후보(MOVE) + 검증 + Plan이 한 번에 남고 바로 확정된다.
놓을 수 있는 시작 구간과 확정은 같은 판정이다. Pack 파일 그대로(seeded_real)의 장면을 쓴다:
D(UB, 0분 시작)와 E(UB, 45분 시작)는 안전 규칙으로 엮여 있다.
"""

import uuid

import pytest
from conftest import add_run, take_snapshot
from fastapi.testclient import TestClient
from scripted import Router, cond, done, solve, solve_with

from app.agents import casefacts
from app.agents.observers import replanning as replanning_observer
from app.api.state import build_state
from app.commands.events import EventReport, receive_event
from app.commands.moves import (
    SNAP_MIN,
    MoveRequest,
    RemoveRequest,
    ResourceRequest,
    _judge,
    _probe,
    _remove_verdict,
    change_resource,
    move_check,
    move_range,
    remove_check,
    resource_check,
)
from app.commands.moves import move_task as move_command
from app.commands.moves import remove_task as remove_command
from app.commands.pins import TaskRef, pin_task
from app.commands.task_edit import EditRequest, edit_task
from app.commands.task_request import (
    TaskRequestForm,
    TaskWithdraw,
    submit_task_request,
    withdraw_task_request,
)
from app.coordinator.dispatcher import run_until_idle
from app.domain.hashes import candidate_hash
from app.main import app
from app.store import db
from app.store.repos.case_events import list_case_events
from app.store.repos.consultations import candidate_state, list_review_queue
from app.store.repos.plans import get_current_plan
from app.store.repos.records import get_candidate, list_validations
from app.store.repos.runs import get_run, list_steps
from app.store.repos.seed import seed_pack
from app.store.repos.tasks import list_current_tasks
from app.validator.validator import validate


def _key():
    return uuid.uuid4().hex


def _move(pack, actor, task_id, start):
    return move_command(pack, actor, _key(), MoveRequest(task_id=task_id, start=start))


def _range(pack, actor, task_id):
    with db.read() as conn:
        return move_range(conn, pack, actor, task_id)


def _check(pack, actor, task_id, start):
    with db.read() as conn:
        return move_check(conn, pack, actor, task_id, start)


def _plan(pack):
    with db.read() as conn:
        plan = get_current_plan(conn, pack.site_id)
    return plan, {a.task_id: a for a in plan.assignments}


def _count(table):
    with db.read() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _in_ranges(ranges, start):
    return any(r["start_min"] <= start <= r["start_max"] for r in ranges)


def test_droppable_range_and_commit_are_the_same_verdict(seeded_real):
    """놓을 수 있는 구간 안의 시작은 확정되고, 밖의 시작은 놓기 판정도 확정도 거절된다."""
    pack = seeded_real
    found = _range(pack, "planner_b", "D")
    assert (found["reason_codes"], found["snap"], found["invalidates"]) == ([], SNAP_MIN, [])
    ranges = found["ranges"]
    # 지금 자리(0분)는 놓을 수 있는 자리지만 바뀌는 것이 없다. E(45–75분)와 겹치는 자리는 놓을 수 없다
    assert ranges[0] == {"start_min": 0, "start_max": 0} and ranges[1]["start_min"] == 90
    for start in range(SNAP_MIN, 200, SNAP_MIN):
        assert _check(pack, "planner_b", "D", start)["ok"] is _in_ranges(ranges, start)
    assert _check(pack, "planner_b", "D", 0)["reason_codes"] == ["NO_CHANGE"]

    before = (_count("candidate"), _count("snapshot"), _plan(pack)[0].plan_revision)
    blocked = _check(pack, "planner_b", "D", 45)
    out = _move(pack, "planner_b", "D", 45)
    assert out.status == "REJECTED" and list(out.reason_codes) == blocked["reason_codes"]
    assert out.reason_codes[0] == "MOVE_NOT_VALID" and len(out.reason_codes) > 1
    # 거절이면 아무것도 남지 않는다 (ST-04)
    assert (_count("candidate"), _count("snapshot"), _plan(pack)[0].plan_revision) == before

    out = _move(pack, "planner_b", "D", 90)
    assert out.status == "APPLIED"
    plan, placed = _plan(pack)
    assert plan.plan_revision == before[2] + 1 == out.result_refs["plan_revision"]
    assert (placed["D"].start, placed["D"].end, placed["E"].start) == (90, 120, 45)
    with db.read() as conn:
        candidate = get_candidate(conn, pack.site_id, plan.candidate_id)
        [validation] = list_validations(conn, pack.site_id, candidate.candidate_id)
        assert candidate_state(conn, pack.site_id, candidate).committed
        # 검토 대기열에 오르지 않고, 옮긴 작업은 고정되지 않는다
        assert list_review_queue(conn, pack.site_id) == []
        assert conn.execute("SELECT COUNT(*) FROM task_pin").fetchone()[0] == 0
        jobs = [r[0] for r in conn.execute("SELECT kind FROM dispatch_job")]
    assert (candidate.kind, candidate.made_by, validation.status) == ("MOVE", "planner_b", "PASS")
    assert "RECHECK" in jobs
    # 열린 메인이 없으면 사건을 만들지 않는다
    assert _count("case_event") == 0
    # 옮긴 뒤에는 새 자리가 기준이다
    assert _check(pack, "planner_b", "D", 90)["reason_codes"] == ["NO_CHANGE"]


def test_only_owner_moves_and_pin_window_hold_refuse(seeded_real):
    """담당자만 옮긴다. 고정된 작업, 시간창 밖, ACTIVE Hold 중에는 옮길 수 없다."""
    pack = seeded_real
    for actor in ("supervisor", "foreman_a2"):
        assert _move(pack, actor, "D", 90).reason_codes == ("NOT_AUTHORIZED",)
        assert _range(pack, actor, "D")["reason_codes"] == ["NOT_AUTHORIZED"]
    assert _move(pack, "planner_b", "ZZ", 90).reason_codes == ("TASK_NOT_FOUND",)

    task = next(t for t in pack.tasks if t.task_id == "D")
    outside = _move(pack, "planner_b", "D", task.latest_start + SNAP_MIN)
    assert outside.reason_codes[0] == "MOVE_NOT_VALID" and "WINDOW" in outside.reason_codes
    assert not _in_ranges(_range(pack, "planner_b", "D")["ranges"], task.latest_start + SNAP_MIN)

    assert pin_task(pack, "planner_b", _key(), TaskRef(task_id="E")).status == "APPLIED"
    assert _move(pack, "planner_b", "E", 200).reason_codes == ("TASK_PINNED",)
    assert _range(pack, "planner_b", "E") == {
        **_range(pack, "planner_b", "E"),
        "reason_codes": ["TASK_PINNED"],
        "ranges": [],
    }

    body = EventReport(source_event_id=_key(), event_type="OTHER", text="확인 필요")
    assert receive_event(pack, "reporter", _key(), body).status == "APPLIED"
    assert _move(pack, "planner_b", "D", 90).reason_codes == ("HOLD_ACTIVE",)
    assert _range(pack, "planner_b", "D")["reason_codes"] == ["HOLD_ACTIVE"]
    assert _plan(pack)[0].plan_revision == 0


def test_validator_checks_move_is_one_task_by_its_owner(seeded_real):
    """MOVE 후보의 C06: 바뀐 작업은 하나, 옮긴 사람이 그 작업의 담당자, 시각만."""
    pack = seeded_real
    with db.read() as conn:
        snapshot, facts = _probe(conn, pack)
    candidate, validation = _judge(snapshot, facts, pack, "D", 90, "planner_b")
    assert validation.status == "PASS"

    def reasons(assignments, made_by):
        changed = candidate.model_copy(
            update={
                "assignments": assignments,
                "made_by": made_by,
                "candidate_hash": candidate_hash(
                    assignments,
                    candidate.base_plan_revision,
                    candidate.context_version,
                    snapshot.snapshot_hash,
                    None,
                    candidate.pack_hash,
                ),
            }
        )
        checks = validate(snapshot, changed, None, pack).checks
        return {c.reason_code for c in checks if c.check_id == "C06"}

    assert reasons(candidate.assignments, "foreman_a2") == {"MOVE_NOT_OWNER"}
    two = tuple(
        a.model_copy(update={"start": 2880, "end": 2880 + a.end - a.start})
        if a.task_id == "B"
        else a
        for a in candidate.assignments
    )
    assert reasons(two, "planner_b") == {"MOVE_NOT_SINGLE_TASK"}
    other = tuple(
        a.model_copy(update={"resource_id": "SITE-CR-01"}) if a.task_id == "D" else a
        for a in candidate.assignments
    )
    assert "MOVE_TIME_AND_RESOURCE" in reasons(other, "planner_b")  # 시각만 또는 자원만 바꾼다
    # Plan 밖 작업을 넣은 MOVE 후보는 받지 않는다
    assert validate(
        snapshot, candidate.model_copy(update={"assignments": candidate.assignments[1:]}), None, pack
    ).status == "FAIL"  # fmt: skip


def test_linked_owner_is_notified_by_the_server(temp_db, real_pack):
    """옮긴 작업과 안전 규칙으로 엮인 작업의 담당자에게 서버가 통지한다. 옮긴 사람에게는 보내지 않는다."""
    tasks = tuple(
        t.model_copy(update={"owner_actor_id": "foreman_a2"}) if t.task_id == "E" else t
        for t in real_pack.tasks
    )
    pack = real_pack.model_copy(update={"tasks": tasks})
    with db.write() as tx:
        seed_pack(tx, pack)
    out = _move(pack, "planner_b", "D", 90)
    assert out.status == "APPLIED" and out.result_refs["notified"] == ["foreman_a2"]
    with db.read() as conn:
        [notice] = conn.execute(
            "SELECT run_id, step_no, to_actor_id, type, body, candidate_id FROM message"
        ).fetchall()
    assert tuple(notice[:4]) == (None, None, "foreman_a2", "NOTICE")
    assert "D" in notice[4] and "E(안전 규칙" in notice[4] and "R1" in notice[4]
    assert notice[5] == out.result_refs["candidate_id"]
    # 엮인 작업이 없으면 통지도 없다
    assert _move(pack, "planner_b", "B", 2880).result_refs["notified"] == []


def test_move_during_review_stales_candidates_and_tells_the_open_main(seeded_real, main_on):
    """검토 중에 옮기면 살아 있는 후보는 기존 판정으로 무효가 되고, 열린 메인에 사건이 가며 사람이 만든
    일로 센다 (AG-30). Plan 밖 요청(A)이 있어도 Plan 안 작업은 옮길 수 있다."""
    pack = seeded_real
    a = pack.new_task.model_dump(exclude={"requested", "unit_id", "owner_actor_id", "movable"})
    assert submit_task_request(pack, "planner_a", _key(), TaskRequestForm(**a)).status == "APPLIED"
    replies = [solve("L1"), solve_with("L2", cond("M", start_at=2940)), done()]
    run_until_idle(pack, model_factory=Router(replanning=replies, auto_done=False).factory())
    with db.read() as conn:
        queue = list_review_queue(conn, pack.site_id)
        [main_id] = [
            r[0] for r in conn.execute("SELECT run_id FROM agent_run WHERE agent_type = 'MAIN'")
        ]
        main = get_run(conn, main_id)
        assert casefacts.human_work(conn, pack.site_id, main.case_id) == 0
    assert queue and main.status == "WAITING_HUMAN"

    # [확정] 앞에 무효가 될 검토 중인 안을 보여 준다
    assert _range(pack, "planner_b", "D")["invalidates"] == queue
    assert _check(pack, "planner_b", "D", 90) == {
        "task_id": "D",
        "start": 90,
        "ok": True,
        "reason_codes": [],
        "invalidates": queue,
    }
    out = _move(pack, "planner_b", "D", 90)
    assert out.status == "APPLIED"
    with db.read() as conn:
        assert list_review_queue(conn, pack.site_id) == []
        for cid in queue:
            state = candidate_state(conn, pack.site_id, get_candidate(conn, pack.site_id, cid))
            assert state.stale_plan and not state.stale_context
        [event] = [e for e in list_case_events(conn, pack.site_id) if e["kind"] == "TASK_MOVED"]
        woke = get_run(conn, main_id)
        assert casefacts.human_work(conn, pack.site_id, main.case_id) == 1
        placed = {a.task_id for a in get_current_plan(conn, pack.site_id).assignments}
    assert event["case_id"] == main.case_id
    assert event["ref"] == {
        "task_id": "D",
        "actor_id": "planner_b",
        "candidate_id": out.result_refs["candidate_id"],
        "plan_revision": out.result_refs["plan_revision"],
    }
    assert woke.wake_seq == main.wake_seq + 1
    assert "A" not in placed  # Plan 밖 요청은 직접 이동으로 Plan에 들어가지 않는다

    # 메인이 깨어나 사건을 보고 이어 간다
    run_until_idle(pack, model_factory=Router().factory())
    with db.read() as conn:
        assert get_run(conn, main_id).status != "ERROR"


@pytest.fixture
def client(seeded_real):
    with TestClient(app) as c:
        yield c


def test_move_api(client, seeded_real):
    def get(url, actor):
        return client.get(f"/api/{url}", headers={"X-Actor": actor})

    def post(actor, body):
        headers = {"X-Actor": actor, "Idempotency-Key": _key()}
        return client.post("/api/tasks/D/move", json=body, headers=headers)

    found = get("tasks/D/move-range", "planner_b").json()
    assert found["ranges"][1]["start_min"] == 90 and found["reason_codes"] == []
    assert get("tasks/D/move-range", "supervisor").json()["reason_codes"] == ["NOT_AUTHORIZED"]
    assert get("tasks/D/move-check?start=45", "planner_b").json()["ok"] is False
    assert get("tasks/D/move-check?start=90", "planner_b").json()["ok"] is True
    assert post("supervisor", {"start": 90}).status_code == 403
    assert post("planner_b", {"start": 45}).status_code == 409
    res = post("planner_b", {"start": 90})
    assert res.status_code == 200 and res.json()["plan_revision"] == 1
    state = client.get(f"/api/sites/{seeded_real.site_id}/state", headers={"X-Actor": "planner_b"})
    placed = {a["task_id"]: a["start"] for a in state.json()["plan"]["assignments"]}
    assert placed["D"] == 90


# ── 작업 없애기 ────────────────────────────────────────────────


def _remove(pack, actor, task_id):
    return remove_command(pack, actor, _key(), RemoveRequest(task_id=task_id))


def _remove_check(pack, actor, task_id):
    with db.read() as conn:
        return remove_check(conn, pack, actor, task_id)


def _submit_a(pack, **more):
    a = pack.new_task.model_dump(exclude={"requested", "unit_id", "owner_actor_id", "movable"})
    out = submit_task_request(pack, "planner_a", _key(), TaskRequestForm(**{**a, **more}))
    assert out.status == "APPLIED"


def _task(pack, task_id):
    with db.read() as conn:
        return next(t for t in list_current_tasks(conn, pack.site_id, pack) if t.task_id == task_id)


def test_owner_removes_planned_task_and_it_leaves_facts(seeded_real):
    """담당자가 계획에 있는 자기 작업을 없애면 그 작업만 빠진 Plan이 바로 확정되고, 작업은 CANCELLED로
    남아 Snapshot·충돌·Agent 관찰에서 빠진다."""
    pack = seeded_real
    assert _remove_check(pack, "planner_b", "D") == {
        "task_id": "D",
        "path": "REMOVE",
        "ok": True,
        "reason_codes": [],
        "invalidates": [],
    }
    with db.read() as conn:
        before = build_state(conn, pack, "planner_b")["site"]
    out = _remove(pack, "planner_b", "D")
    assert out.status == "APPLIED"
    plan, placed = _plan(pack)
    assert plan.plan_revision == before["plan_revision"] + 1 and "D" not in placed
    assert len(placed) == len(pack.tasks) - 1
    task = _task(pack, "D")
    assert (task.lifecycle, task.revision) == ("CANCELLED", out.result_refs["revision"])
    add_run(pack, acting_unit_id="UB", acting_actor_id="planner_b")
    with db.read() as conn:
        candidate = get_candidate(conn, pack.site_id, plan.candidate_id)
        [validation] = list_validations(conn, pack.site_id, candidate.candidate_id)
        state = build_state(conn, pack, "planner_b")
        obs = replanning_observer.build_observation(conn, pack, "run_test").data
        jobs = [r[0] for r in conn.execute("SELECT kind FROM dispatch_job")]
        assert list_review_queue(conn, pack.site_id) == []
    assert (candidate.kind, candidate.made_by, validation.status) == ("REMOVE", "planner_b", "PASS")
    assert state["site"]["context_version"] == before["context_version"] + 1
    assert plan.committed_context_version == state["site"]["context_version"]
    assert "D" not in take_snapshot(pack).facts().task_map()
    assert not [c for c in state["conflicts"] if "D" in c["task_ids"]]
    assert "D" not in {t["task_id"] for t in obs["acting_tasks"]}
    assert "RECHECK" in jobs and _count("case_event") == 0
    # 없앤 작업은 다시 없애거나 옮길 수 없다
    assert _remove(pack, "planner_b", "D").reason_codes == ("TASK_NOT_FOUND",)
    assert _move(pack, "planner_b", "D", 90).reason_codes == ("TASK_NOT_FOUND",)


def test_remove_refusals_and_unplanned_request_goes_to_withdraw(seeded_real):
    """남의 작업, 고정된 작업, 뒤에 이어지는 작업이 있는 작업, ACTIVE Hold 중에는 없앨 수 없다. 계획에 없는
    요청은 이 명령이 아니라 요청 철회로 없앤다."""
    pack = seeded_real
    for actor in ("supervisor", "foreman_a2"):
        assert _remove(pack, actor, "D").reason_codes == ("NOT_AUTHORIZED",)
        assert _remove_check(pack, actor, "D")["reason_codes"] == ["NOT_AUTHORIZED"]

    # A는 계획 밖 요청이고 D 뒤에 이어진다
    _submit_a(pack, predecessors=({"task_id": "D"},))
    assert _remove(pack, "planner_b", "D").reason_codes == ("TASK_HAS_SUCCESSORS",)
    assert _remove_check(pack, "planner_b", "D")["reason_codes"] == ["TASK_HAS_SUCCESSORS"]
    found = _remove_check(pack, "planner_a", "A")
    assert (found["path"], found["ok"]) == ("WITHDRAW", True)
    assert _remove(pack, "planner_a", "A").reason_codes == ("TASK_NOT_IN_PLAN",)
    withdraw = TaskWithdraw(task_id="A")
    assert withdraw_task_request(pack, "planner_a", _key(), withdraw).status == "APPLIED"
    assert _task(pack, "A").lifecycle == "NEEDS_INFO"
    assert _remove_check(pack, "planner_b", "D")["ok"] is True

    assert pin_task(pack, "planner_b", _key(), TaskRef(task_id="D")).status == "APPLIED"
    assert _remove(pack, "planner_b", "D").reason_codes == ("TASK_PINNED",)
    assert _remove_check(pack, "planner_b", "D")["reason_codes"] == ["TASK_PINNED"]

    body = EventReport(source_event_id=_key(), event_type="OTHER", text="확인 필요")
    assert receive_event(pack, "reporter", _key(), body).status == "APPLIED"
    assert _remove(pack, "planner_b", "E").reason_codes == ("HOLD_ACTIVE",)
    assert _plan(pack)[0].plan_revision == 0 and _task(pack, "E").lifecycle == "READY"


def test_validator_checks_remove_is_one_task_by_its_owner(seeded_real):
    """REMOVE 후보의 C06: 빠진 작업은 하나, 없앤 사람이 그 작업의 담당자, 남은 배정은 그대로."""
    pack = seeded_real
    with db.read() as conn:
        snapshot, facts = _probe(conn, pack)
        codes, candidate, validation = _remove_verdict(
            conn, snapshot, facts, pack, "D", "planner_b"
        )
    assert codes == [] and validation.status == "PASS"

    def reasons(assignments, made_by="planner_b"):
        changed = candidate.model_copy(
            update={
                "assignments": assignments,
                "made_by": made_by,
                "candidate_hash": candidate_hash(
                    assignments,
                    candidate.base_plan_revision,
                    candidate.context_version,
                    snapshot.snapshot_hash,
                    None,
                    candidate.pack_hash,
                ),
            }
        )
        checks = validate(snapshot, changed, None, pack).checks
        return {c.reason_code for c in checks if c.check_id in ("C02", "C06") and c.reason_code}

    assert reasons(candidate.assignments, "foreman_a2") == {"REMOVE_NOT_OWNER"}
    assert reasons(candidate.assignments[1:]) >= {"REMOVE_NOT_SINGLE_TASK", "TASK_MISSING"}
    assert reasons(facts.plan.assignments) == {"REMOVE_NOT_SINGLE_TASK"}
    shifted = tuple(
        a.model_copy(update={"start": a.start + 60, "end": a.end + 60}) if a.task_id == "E" else a
        for a in candidate.assignments
    )
    assert reasons(shifted) == {"TIME_AXIS_NOT_ALLOWED"}


def test_remove_notifies_linked_owner(temp_db, real_pack):
    """없앤 작업과 안전 규칙으로 엮여 있던 계획 작업의 담당자에게 서버가 통지한다(본인 제외)."""
    tasks = tuple(
        t.model_copy(update={"owner_actor_id": "foreman_a2"}) if t.task_id == "E" else t
        for t in real_pack.tasks
    )
    pack = real_pack.model_copy(update={"tasks": tasks})
    with db.write() as tx:
        seed_pack(tx, pack)
    out = _remove(pack, "planner_b", "D")
    assert out.status == "APPLIED" and out.result_refs["notified"] == ["foreman_a2"]
    with db.read() as conn:
        [notice] = conn.execute("SELECT run_id, to_actor_id, type, body FROM message").fetchall()
    assert tuple(notice[:3]) == (None, "foreman_a2", "NOTICE")
    assert "D" in notice[3] and "없애" in notice[3] and "E(안전 규칙" in notice[3]
    assert _remove(pack, "planner_b", "B").result_refs["notified"] == []


def test_remove_during_review_stales_candidates_and_tells_the_open_main(seeded_real, main_on):
    """검토 중에 없애면 살아 있는 후보는 기존 판정으로 무효가 되고, 열린 메인에 사건이 가며 사람이 만든
    일로 센다 (AG-30). 메인은 없앤 작업 없이 이어 간다."""
    pack = seeded_real
    _submit_a(pack)
    replies = [solve("L1"), solve_with("L2", cond("M", start_at=2940)), done()]
    run_until_idle(pack, model_factory=Router(replanning=replies, auto_done=False).factory())
    with db.read() as conn:
        queue = list_review_queue(conn, pack.site_id)
        [main_id] = [
            r[0] for r in conn.execute("SELECT run_id FROM agent_run WHERE agent_type = 'MAIN'")
        ]
        main = get_run(conn, main_id)
    assert queue and _remove_check(pack, "planner_b", "D")["invalidates"] == queue
    assert _remove_check(pack, "planner_a", "A")["invalidates"] == queue  # 철회도 무효로 만든다

    out = _remove(pack, "planner_b", "D")
    assert out.status == "APPLIED"
    with db.read() as conn:
        assert list_review_queue(conn, pack.site_id) == []
        for cid in queue:
            state = candidate_state(conn, pack.site_id, get_candidate(conn, pack.site_id, cid))
            assert state.stale_plan and state.stale_context
        [event] = [e for e in list_case_events(conn, pack.site_id) if e["kind"] == "TASK_REMOVED"]
        woke = get_run(conn, main_id)
        assert casefacts.human_work(conn, pack.site_id, main.case_id) == 1
    assert event["case_id"] == main.case_id and event["ref"]["task_id"] == "D"
    assert woke.wake_seq == main.wake_seq + 1

    run_until_idle(pack, model_factory=Router().factory())
    with db.read() as conn:
        ended = get_run(conn, main_id)
        seen = list_steps(conn, main_id)[-1]["observation"]
    assert ended.status != "ERROR"
    assert "TASK_REMOVED" in [e["kind"] for e in seen["events"]]


def test_remove_api(client, seeded_real):
    def get(actor):
        return client.get("/api/tasks/D/remove-check", headers={"X-Actor": actor}).json()

    def post(actor):
        headers = {"X-Actor": actor, "Idempotency-Key": _key()}
        return client.post("/api/tasks/D/remove", headers=headers)

    assert get("supervisor")["reason_codes"] == ["NOT_AUTHORIZED"]
    assert (get("planner_b")["path"], get("planner_b")["ok"]) == ("REMOVE", True)
    assert post("supervisor").status_code == 403
    res = post("planner_b")
    assert res.status_code == 200 and res.json()["plan_revision"] == 1
    state = client.get(f"/api/sites/{seeded_real.site_id}/state", headers={"X-Actor": "planner_b"})
    body = state.json()
    assert "D" not in {a["task_id"] for a in body["plan"]["assignments"]}
    assert next(t for t in body["tasks"] if t["task_id"] == "D")["lifecycle"] == "CANCELLED"


# ── 작업 카드에서 자원 바꾸기 (AG-31·AG-34) ────────────────────


def _resource(pack, actor, task_id, resource_id):
    body = ResourceRequest(task_id=task_id, resource_id=resource_id)
    return change_resource(pack, actor, _key(), body)


def _resource_check(pack, actor, task_id, resource_id):
    with db.read() as conn:
        return resource_check(conn, pack, actor, task_id, resource_id)


def test_owner_changes_resource_on_card_and_it_commits(seeded_real):
    """계획에 있는 작업의 자원을 카드에서 바꾸면, 지금 시각 그대로 자원만 바꾼 배치를 직접 이동과 같은
    판정으로 보고 바로 확정한다. 요청 자원과 그 동의도 새 자원으로 바뀐다."""
    pack = seeded_real
    plan, placed = _plan(pack)
    before = placed["C"]
    assert before.resource_id == "A-CR-01"
    ok = _resource_check(pack, "foreman_a2", "C", "SITE-CR-01")
    assert (ok["path"], ok["ok"], ok["reason_codes"]) == ("MOVE", True, [])
    # 안 되는 자원: 확인과 확정이 같은 사유로 거절한다
    bad = _resource_check(pack, "foreman_a2", "C", "B-CR-01")
    out = _resource(pack, "foreman_a2", "C", "B-CR-01")
    assert bad["ok"] is False and list(out.reason_codes) == bad["reason_codes"]
    assert out.reason_codes[0] == "MOVE_NOT_VALID" and "RESOURCE_AUTH" in out.reason_codes
    for actor in ("supervisor", "planner_a"):
        assert _resource(pack, actor, "C", "SITE-CR-01").reason_codes == ("NOT_AUTHORIZED",)
    assert _resource(pack, "foreman_a2", "C", "A-CR-01").reason_codes == ("NO_CHANGE",)
    assert _resource(pack, "foreman_a2", "C", "NOPE").reason_codes == ("RESOURCE_NOT_FOUND",)
    assert _plan(pack)[0].plan_revision == plan.plan_revision and _task(pack, "C").revision == 1

    out = _resource(pack, "foreman_a2", "C", "SITE-CR-01")
    assert out.status == "APPLIED"
    new_plan, placed = _plan(pack)
    assert new_plan.plan_revision == plan.plan_revision + 1
    assert (placed["C"].start, placed["C"].resource_id) == (before.start, "SITE-CR-01")
    task = _task(pack, "C")
    assert (task.revision, task.requested_resource_id) == (2, "SITE-CR-01")
    assert task.fields["resource"].value["requested_resource_id"] == "SITE-CR-01"
    assert task.fields["resource"].source_ref.startswith("card:") and task.decided_values == ()
    with db.read() as conn:
        candidate = get_candidate(conn, pack.site_id, new_plan.candidate_id)
        [validation] = list_validations(conn, pack.site_id, candidate.candidate_id)
        consents = conn.execute(
            "SELECT axis, scope, source_ref FROM consent WHERE task_id = 'C' AND task_revision = 2"
            " ORDER BY rowid"
        ).fetchall()
        assert list_review_queue(conn, pack.site_id) == []
    assert (candidate.kind, candidate.made_by, validation.status) == ("MOVE", "foreman_a2", "PASS")
    by_axis = {c[0]: (c[1], c[2]) for c in consents}
    assert "RESOURCE" in by_axis  # 다른 축의 동의가 있으면 그대로 따라온다
    assert "SITE-CR-01" in by_axis["RESOURCE"][0] and "A-CR-01" not in by_axis["RESOURCE"][0]
    assert by_axis["RESOURCE"][1].startswith("card:")
    assert _count("case_event") == 0  # 열린 메인이 없으면 사건을 만들지 않는다


def test_unplanned_request_changes_its_requested_resource(seeded_real):
    """계획 밖 요청은 요청 자원을 고친다(값 고치기). 고친 자원이 그 작업의 기준 자원이 된다."""
    pack = seeded_real
    _submit_a(pack)
    found = _resource_check(pack, "planner_a", "A", "SITE-CR-01")
    assert (found["path"], found["ok"]) == ("EDIT", True)
    assert _resource(pack, "planner_a", "A", "SITE-CR-01").reason_codes == ("TASK_NOT_IN_PLAN",)
    out = edit_task(
        pack, "planner_a", _key(), EditRequest(task_id="A", requested_resource_id="SITE-CR-01")
    )
    assert out.status == "APPLIED"
    assert _task(pack, "A").requested_resource_id == "SITE-CR-01"
    base = take_snapshot(pack).facts().base_assignments()["A"]
    assert base.resource_id == "SITE-CR-01"


def test_resource_change_api(client, seeded_real):
    def get(actor, resource_id):
        url = f"/api/tasks/C/resource-check?resource_id={resource_id}"
        return client.get(url, headers={"X-Actor": actor}).json()

    def post(actor, resource_id):
        headers = {"X-Actor": actor, "Idempotency-Key": _key()}
        return client.post(
            "/api/tasks/C/resource", json={"resource_id": resource_id}, headers=headers
        )

    assert get("foreman_a2", "SITE-CR-01")["ok"] is True
    assert get("foreman_a2", "B-CR-01")["ok"] is False
    assert post("supervisor", "SITE-CR-01").status_code == 403
    assert post("foreman_a2", "B-CR-01").status_code == 409
    res = post("foreman_a2", "SITE-CR-01")
    assert res.status_code == 200 and res.json()["plan_revision"] == 1
