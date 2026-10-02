"""시간창 넓히기 선택지 스캔 (부록 A.29 5). 독립 근거는 scripts/verify_demo_values.py(CP-SAT + 전수 열거)다.

스캔은 대상 작업 하나만 옮기고 나머지는 기준 배정에 고정한다. 선택지 창에서는 Solver(L0)가 해를 찾고,
시작 한도나 종료 한도를 1분이라도 줄이면 해가 없어야 한다(가장 좁은 창).
"""

from dataclasses import replace

from conftest import add_task, make_task, take_snapshot

from app.rules.window import widen_option
from scripts import verify_demo_values as verify


def _option(pack, task_id):
    return widen_option(take_snapshot(pack).facts(), pack, task_id)


def _n5(pack):
    d = next(x for x in pack.demo_requests if x.task_id == "N5")
    return make_task(
        pack,
        task_id="N5",
        unit_id="UB",
        owner_actor_id=d.requester,
        work_type=d.work_type,
        zone_id=d.zone_id,
        duration=d.duration,
        earliest_start=d.earliest_start,
        latest_start=d.latest_start,
        latest_end=d.latest_end,
        required_resource_type=None,
        requested_resource_id=None,
        source_ref="scenario:N5",
    )


def _l0(world, others, req, window):
    ls, le = window
    return verify.expected(world, others, replace(req, ls=ls, le=le), levels=("L0",))["L0"]


def test_a_option_is_narrowest_window_cpsat_agrees(with_a):
    """A(B구역 인양): 10:30 시작이 첫 자리 → 시작 한도 10:00 → 10:30, 종료 한도 10:30 → 11:00."""
    assert _option(with_a, "A") == {
        "task_id": "A",
        "fit_start": 90,
        "resource_id": "A-CR-01",
        "current": {"latest_start": 60, "latest_end": 90},
        "proposed": {"latest_start": 90, "latest_end": 120},
    }
    # 기본안 B(C 고정, A 자원 축 미확인)에서 Solver L0: 선택지 창이면 해, 1분 줄이면 해 없음
    world, fixture, a, _ = verify.load()
    fixed = [replace(t, mt=False, mr=False) if t.id == "C" else t for t in fixture]
    found = _l0(world, fixed, a, (90, 120))
    assert (found["status"], found["changed"], found["delay"], found["moved"]) == (
        "OPTIMAL",
        1,
        90,
        {"A": [90, "A-CR-01"]},
    )
    for window in ((89, 120), (90, 119), (60, 90)):
        assert _l0(world, fixed, a, window)["status"] == "INFEASIBLE"


def test_n5_option_cpsat_agrees(seeded):
    """N5(F구역 하부 작업): K(10/13 09:00–11:00) 뒤 11:00 시작 → 시작 한도 11:00, 종료 한도 12:00."""
    add_task(seeded, _n5(seeded))
    opt = _option(seeded, "N5")
    assert (opt["fit_start"], opt["proposed"], opt["resource_id"]) == (
        1560,
        {"latest_start": 1560, "latest_end": 1620},
        None,
    )
    world, fixture, _, demos = verify.load()
    n5 = next(d for d in demos if d.id == "N5")
    assert _l0(world, fixture, n5, (1560, 1620))["status"] == "OPTIMAL"
    for window in ((1559, 1620), (1560, 1619)):
        assert _l0(world, fixture, n5, window)["status"] == "INFEASIBLE"


def test_no_option_when_current_window_has_a_slot(seeded):
    """N1은 지금 창 안에서(옮기면) 풀린다 → 넓힐 필요가 없어 선택지가 없다."""
    d = next(x for x in seeded.demo_requests if x.task_id == "N1")
    n1 = make_task(
        seeded,
        task_id="N1",
        work_type=d.work_type,
        zone_id=d.zone_id,
        duration=d.duration,
        earliest_start=d.earliest_start,
        latest_start=d.latest_start,
        latest_end=d.latest_end,
        required_resource_type=d.required_resource_type,
        requested_resource_id=d.requested_resource_id,
        source_ref="scenario:N1",
    )
    add_task(seeded, n1)
    assert _option(seeded, "N1") is None


def test_no_option_when_other_tasks_already_conflict(with_a):
    """C를 옮겨 봐도 A(기준 09:00)와 B의 충돌이 남는다 → 이 작업만 옮겨서는 풀 수 없다."""
    assert _option(with_a, "C") is None
