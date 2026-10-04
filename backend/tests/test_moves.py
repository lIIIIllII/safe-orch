"""담당자의 직접 이동 (AG-31·CV-28).

담당자가 자기 작업의 시각을 옮기면 사람이 만든 후보(MOVE) + 검증 + Plan이 한 번에 남고 바로 확정된다.
놓을 수 있는 시작 구간과 확정은 같은 판정이다. Pack 파일 그대로(seeded_real)의 장면을 쓴다:
D(UB, 0분 시작)와 E(UB, 45분 시작)는 안전 규칙으로 엮여 있다.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from scripted import Router, cond, done, solve, solve_with

from app.agents import casefacts
from app.commands.events import EventReport, receive_event
from app.commands.moves import SNAP_MIN, MoveRequest, _judge, _probe, move_check, move_range
from app.commands.moves import move_task as move_command
from app.commands.pins import TaskRef, pin_task
from app.commands.task_request import TaskRequestForm, submit_task_request
from app.coordinator.dispatcher import run_until_idle
from app.domain.hashes import candidate_hash
from app.main import app
from app.store import db
from app.store.repos.case_events import list_case_events
from app.store.repos.consultations import candidate_state, list_review_queue
from app.store.repos.plans import get_current_plan
from app.store.repos.records import get_candidate, list_validations
from app.store.repos.runs import get_run
from app.store.repos.seed import seed_pack
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
    assert (candidate.kind, candidate.moved_by, validation.status) == ("MOVE", "planner_b", "PASS")
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

    def reasons(assignments, moved_by):
        changed = candidate.model_copy(
            update={
                "assignments": assignments,
                "moved_by": moved_by,
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
    assert "RESOURCE_AXIS_NOT_ALLOWED" in reasons(other, "planner_b")
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
