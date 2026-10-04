"""작업 고정·고정 해제 (AG-27).

고정은 사람만 한다(담당자는 자기 작업, Supervisor는 모든 작업). 고정된 작업은 Solver가 움직이지 않고
Validator가 검사한다. 고정·해제는 현장 버전을 올리고 열린 메인에 사건으로 간다 (ST-22).
"""

import uuid

import pytest
from conftest import LEGACY_PINNED, add_run, take_snapshot
from fastapi.testclient import TestClient
from scripted import Router, solve

from app.agents.observers import replanning as replanning_observer
from app.commands.pins import (
    TaskRef,
    pin_task,
    unpin_task,
)
from app.commands.task_request import TaskRequestForm, submit_task_request
from app.coordinator.dispatcher import run_until_idle
from app.domain.models import Condition
from app.main import app
from app.rules.engine import detect_conflicts
from app.solver import cpsat
from app.solver.candidate import build_candidate
from app.solver.search_spec import build_search_spec
from app.store import db
from app.store.repos.consultations import candidate_state
from app.store.repos.records import get_candidate
from app.store.repos.runs import get_run, list_steps
from app.store.repos.site import get_site
from app.validator.validator import validate

FORM_FIELDS = (
    "task_id",
    "work_type",
    "zone_id",
    "duration",
    "earliest_start",
    "latest_start",
    "latest_end",
    "required_resource_type",
    "requested_resource_id",
    "resource_requirements",
)


def _key():
    return uuid.uuid4().hex


def _pin(pack, actor, task_id):
    return pin_task(pack, actor, _key(), TaskRef(task_id=task_id))


def _unpin(pack, actor, task_id):
    return unpin_task(pack, actor, _key(), TaskRef(task_id=task_id))


def _context(pack):
    with db.read() as conn:
        return get_site(conn, pack.site_id).context_version


def _rows(sql, *params):
    with db.read() as conn:
        cur = conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def _events(kind=None):
    found = _rows("SELECT kind, ref, case_id FROM case_event ORDER BY seq")
    return [e for e in found if kind is None or e["kind"] == kind]


def _runs(agent_type):
    with db.read() as conn:
        ids = [
            r[0]
            for r in conn.execute(
                "SELECT run_id FROM agent_run WHERE agent_type = ? ORDER BY rowid", (agent_type,)
            )
        ]
        return [get_run(conn, rid) for rid in ids]


def _submit(pack, task_id):
    d = next(x for x in pack.demo_requests if x.task_id == task_id)
    form = TaskRequestForm(**{k: getattr(d, k) for k in FORM_FIELDS})
    return submit_task_request(pack, d.requester, _key(), form)


# ── 권한 ───────────────────────────────────────────────────────


def test_owner_pins_and_unpins_own_task(seeded):
    """담당자는 자기 작업만 고정·해제한다. 고정·해제마다 현장 버전이 오른다."""
    pack = seeded
    ctx = _context(pack)
    # C의 담당자는 foreman_a2다. 같은 Unit의 다른 사람(planner_a)도, 다른 Unit(planner_b)도 못 한다
    assert _pin(pack, "planner_a", "C").reason_codes == ("NOT_AUTHORIZED",)
    assert _pin(pack, "planner_b", "C").reason_codes == ("NOT_AUTHORIZED",)
    assert _context(pack) == ctx
    out = _pin(pack, "foreman_a2", "C")
    assert (out.status, out.result_refs["by_role"], _context(pack)) == ("APPLIED", "OWNER", ctx + 1)
    assert _pin(pack, "foreman_a2", "C").reason_codes == ("ALREADY_PINNED",)
    [row] = _rows("SELECT * FROM task_pin WHERE task_id = 'C'")
    assert (row["status"], row["pinned_by"], row["created_context_version"]) == (
        "ACTIVE",
        "foreman_a2",
        ctx + 1,
    )
    assert row["pinned_at"]  # 건 시각

    assert _unpin(pack, "planner_a", "C").reason_codes == ("NOT_AUTHORIZED",)
    assert _unpin(pack, "foreman_a2", "C").status == "APPLIED"
    assert _context(pack) == ctx + 2
    [row] = _rows("SELECT * FROM task_pin WHERE task_id = 'C'")
    assert (row["status"], row["released_by"], row["released_context_version"]) == (
        "RELEASED",
        "foreman_a2",
        ctx + 2,
    )
    assert _unpin(pack, "foreman_a2", "C").reason_codes == ("PIN_NOT_FOUND",)
    assert _pin(pack, "foreman_a2", "Z9").reason_codes == ("TASK_NOT_FOUND",)
    # 다시 고정하면 새 기록이 생긴다(앞의 것은 RELEASED로 남는다)
    assert _pin(pack, "foreman_a2", "C").status == "APPLIED"
    assert len(_rows("SELECT * FROM task_pin WHERE task_id = 'C'")) == 2


def test_supervisor_pin_is_released_only_by_supervisor(seeded):
    """Supervisor는 모든 작업을 고정·해제한다. Supervisor가 건 고정은 담당자가 풀지 못한다."""
    pack = seeded
    out = _pin(pack, "supervisor", "C")
    assert (out.status, out.result_refs["by_role"]) == ("APPLIED", "SUPERVISOR")
    assert _unpin(pack, "foreman_a2", "C").reason_codes == ("NOT_AUTHORIZED",)
    assert _unpin(pack, "supervisor", "C").status == "APPLIED"
    # 담당자가 건 고정은 Supervisor도 푼다
    assert _pin(pack, "foreman_a2", "C").status == "APPLIED"
    assert _unpin(pack, "supervisor", "C").status == "APPLIED"


def test_committed_task_is_not_pinned(seeded_real):
    """확정된 계획에 있다고 고정으로 보지 않는다: Pack의 R0 작업은 모두 고정 없이 움직일 수 있다."""
    pack = seeded_real
    assert _submit(pack, "N1").status == "APPLIED"  # K와 같은 장비·같은 시간
    snap = take_snapshot(pack)
    conflict = detect_conflicts(snap, snap.facts().check_assignments(), pack)[0]
    assert set(conflict.task_ids) == {"K", "N1"}
    spec = build_search_spec(snap, [conflict], "L0")
    assert (spec.axes["K"].time, spec.axes["K"].resource) == (True, True)


# ── Solver·Validator ───────────────────────────────────────────


def test_pinned_task_is_constant_for_solver_and_checked_by_validator(seeded_real):
    """K를 고정하면 K는 상수가 되고, K를 옮긴 후보는 Validator가 TASK_PINNED로 막는다."""
    pack = seeded_real
    assert _submit(pack, "N1").status == "APPLIED"
    before = take_snapshot(pack)
    conflict = detect_conflicts(before, before.facts().check_assignments(), pack)[0]
    # N1을 제자리에 두는 조건을 걸면 K가 움직여야 풀린다
    n1 = before.facts().base_assignments()["N1"].start
    hold_n1 = {"N1": Condition(start_min=n1, start_max=n1)}
    spec = build_search_spec(before, [conflict], "L0", hold_n1)
    result = cpsat.solve(before, spec, pack)
    moved_k = build_candidate(before, spec, result)  # 고정 전: K를 옮기는 해
    base = before.facts().base_assignments()["K"]
    assert next(a for a in moved_k.assignments if a.task_id == "K") != base

    assert _pin(pack, "planner_b", "K").status == "APPLIED"
    snap = take_snapshot(pack)
    pinned_spec = build_search_spec(snap, [conflict], "L0", hold_n1)
    assert (pinned_spec.axes["K"].time, pinned_spec.axes["K"].resource) == (False, False)
    pinned_result = cpsat.solve(snap, pinned_spec, pack)
    assert pinned_result.stage1["status"] == "INFEASIBLE"  # K가 상수라 N1을 제자리에 두고는 못 푼다
    # 고정 전 해를 고정 뒤 사실에서 검증하면 걸린다
    stale = moved_k.model_copy(update={"snapshot_id": snap.snapshot_id})
    checks = validate(snap, stale, pinned_spec, pack).checks
    assert ("C06", "FAIL", "TASK_PINNED", ("K",)) in [
        (c.check_id, c.status, c.reason_code, c.task_ids) for c in checks
    ]
    # 조건 없이 풀면 고정되지 않은 N1이 움직인다. K는 그대로다
    free = build_search_spec(snap, [conflict], "L0")
    solved = cpsat.solve(snap, free, pack)
    placed = {a["task_id"]: a["start"] for a in solved.solution}
    assert placed["K"] == base.start and placed["N1"] != snap.facts().base_assignments()["N1"].start


# ── 사건 ───────────────────────────────────────────────────────


def test_pin_without_open_main_makes_no_event(seeded):
    """열린 메인이 없으면 고정·해제는 사건을 만들지 않고 메인도 뜨지 않는다."""
    pack = seeded
    assert _pin(pack, "foreman_a2", "C").status == "APPLIED"
    assert _unpin(pack, "foreman_a2", "C").status == "APPLIED"
    assert _events() == [] and _runs("MAIN") == []


def test_pin_wakes_open_main_and_stales_review_candidate(seeded, main_on):
    """검토 대기 중에 고정하면 후보는 STALE이 되고, 사건이 열린 메인의 Case에 들어가 메인이 깨어난다."""
    pack = seeded
    assert _submit(pack, "N1").status == "APPLIED"
    run_until_idle(pack, model_factory=Router(replanning=[solve("L0")]).factory())
    [main] = _runs("MAIN")
    assert (main.status, main.wait_kind) == ("WAITING_HUMAN", "HUMAN_DECISION")
    [rp] = _runs("REPLANNING")
    with db.read() as conn:
        steps = list_steps(conn, rp.run_id)
    cand_id = next(s["tool_result"]["candidate_id"] for s in steps if s["tool_result"])
    with db.read() as conn:
        assert not candidate_state(
            conn, pack.site_id, get_candidate(conn, pack.site_id, cand_id)
        ).stale

    out = _pin(pack, "planner_a", "N1")
    assert out.status == "APPLIED"
    with db.read() as conn:
        assert candidate_state(conn, pack.site_id, get_candidate(conn, pack.site_id, cand_id)).stale
    [event] = _events("TASK_PINNED")
    assert event["case_id"] == main.case_id and '"task_id":"N1"' in event["ref"].replace(" ", "")
    [woke] = _runs("MAIN")
    assert woke.wake_seq == main.wake_seq + 1

    # 메인이 깨어나 다시 본다: N1도 K도 고정이라 움직일 작업이 없다
    run_until_idle(pack, model_factory=Router().factory())
    with db.read() as conn:
        seen = list_steps(conn, main.run_id)[-1]["observation"]
    assert [e["kind"] for e in seen["events"]][-1] == "TASK_PINNED"
    assert seen["replanning"]["movable_task_ids"] == []

    assert _unpin(pack, "planner_a", "N1").status == "APPLIED"
    assert len(_events("TASK_UNPINNED")) == (1 if _runs("MAIN")[0].status == "WAITING_HUMAN" else 0)


def test_replanning_observation_shows_who_pinned(with_a):
    pack = with_a
    add_run(pack)
    with db.read() as conn:
        obs = replanning_observer.build_observation(conn, pack, "run_test").data
    acting = {t["task_id"]: t for t in obs["tasks"]}
    assert "constraints" not in obs and "movable" not in acting["A"]
    # 작업 전체가 보인다(Unit을 가리지 않는다). 기준 상태의 고정이 그대로 보인다
    assert {tid for tid, t in acting.items() if t["pinned"]} == set(LEGACY_PINNED)
    assert {acting["A"]["unit_id"], acting["B"]["unit_id"]} == {"UA", "UB"}
    assert acting["Q"]["pinned"] == {"pinned_by": "foreman_a2", "by_role": "OWNER"}
    assert set(LEGACY_PINNED) >= {"M", "Q"}
    # 희망 영역과 동의 범위는 없다. 기준 배정에 출처가 붙는다: 계획 작업은 승인된 자리, 폼 요청은 없음
    assert "consents" not in obs and "preferred_window" not in acting["A"]
    assert (acting["C"]["base"]["source"], acting["A"]["base"]["source"]) == ("PLAN", "NONE")
    c = acting["C"]["base"]
    assert (c["start_range"], c["decided"]) == ([c["start"], c["start"]], False)
    assert (acting["A"]["base"]["start_range"], acting["A"]["clock"]["base_start_max"]) == (
        None,
        None,
    )


# ── API ────────────────────────────────────────────────────────


@pytest.fixture
def client(seeded):
    with TestClient(app) as c:
        yield c


def _post(client, url, actor, body=None):
    headers = {"X-Actor": actor, "Idempotency-Key": _key()}
    return client.post(f"/api/{url}", json=body, headers=headers)


def test_pin_api(client, seeded):
    assert _post(client, "tasks/C/pin", "planner_b").status_code == 403
    res = _post(client, "tasks/C/pin", "foreman_a2")
    assert (res.status_code, res.json()["status"]) == (200, "APPLIED"), res.text
    state = client.get(f"/api/sites/{seeded.site_id}/state", headers={"X-Actor": "foreman_a2"})
    c = next(t for t in state.json()["tasks"] if t["task_id"] == "C")
    assert (c["pin"]["pinned_by"], c["pin"]["by_role"]) == ("foreman_a2", "OWNER")
    assert "movable" not in c and "preferred_window" not in c
    # 희망 영역을 그리고 지우는 길은 없다
    assert _post(client, "tasks/C/preferred-window", "foreman_a2", {}).status_code == 404
    assert _post(client, "tasks/C/unpin", "planner_a").status_code == 403
    assert _post(client, "tasks/C/unpin", "foreman_a2").status_code == 200
    assert _post(client, "tasks/C/unpin", "foreman_a2").status_code == 404  # PIN_NOT_FOUND
