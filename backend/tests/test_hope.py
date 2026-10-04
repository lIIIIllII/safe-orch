"""희망 영역(Soft)과 가능 범위(Hard) (ST-22).

희망 영역은 계산에 들어가는 사실이다: 지연(희망에서 벗어난 정도)의 기준이고, 계획에 없는 작업의 기준
위치이며, 말한 희망의 범위 안이면 담당자에게 묻지 않는다. 시간창은 사람이 구조화된 입력으로 넣은 가능
범위다. 기준 장면(with_a)의 A는 폼 작업이라, 자연어 작업처럼 보려고 시간창을 Horizon 전체로 넓혀 쓴다.
"""

import uuid

from conftest import take_snapshot, with_facts
from scripted import Router

from app.api.state import build_state, off_hope, work_deviation
from app.commands.pins import PreferredWindow as WindowBody
from app.commands.pins import TaskRef, pin_task, set_preferred_window
from app.commands.task_edit import EditRequest, edit_task
from app.coordinator.dispatcher import run_until_idle
from app.domain.consultation import build_items
from app.domain.factdiff import fact_changes
from app.domain.models import Candidate, Condition, Conflict, PreferredWindow
from app.rules.engine import detect_conflicts
from app.solver import cpsat
from app.solver.search_spec import build_search_spec
from app.store import db
from app.store.repos.plans import get_current_plan
from app.store.repos.tasks import list_current_tasks


def _key():
    return uuid.uuid4().hex


def _hoped(snapshot, task_id, start, end, origin="STATED"):
    """그 작업에 희망 영역을 둔 Snapshot (메모리)."""
    window = PreferredWindow(task_id=task_id, start=start, end=end, origin=origin, made_by="INTAKE")
    kept = tuple(w for w in snapshot.facts().preferred_windows if w.task_id != task_id)
    return with_facts(snapshot, preferred_windows=(*kept, window))


def _wide(snapshot, task_id="A"):
    """그 작업의 시간창을 Horizon 전체로 넓힌 Snapshot (자연어로 접수된 작업의 모양)."""
    facts = snapshot.facts()
    horizon = facts.horizon_minutes

    def widen(t):
        window = {
            "earliest_start": 0,
            "latest_start": horizon - t.duration,
            "latest_end": horizon,
        }
        fields = dict(t.fields)
        fields["window"] = fields["window"].model_copy(update={"value": window})
        return t.model_copy(update={**window, "fields": fields})

    return with_facts(
        snapshot, tasks=tuple(widen(t) if t.task_id == task_id else t for t in facts.tasks)
    )


def _first_conflict(pack, snapshot):
    return detect_conflicts(snapshot, snapshot.facts().check_assignments(), pack)[0]


def _start(result, task_id):
    return next(a["start"] for a in result.solution if a["task_id"] == task_id)


# ── 희망에서 벗어난 정도 ───────────────────────────────────────


def test_deviation_is_the_distance_outside_the_hope_range_both_ways():
    """희망 시작 범위 밖으로 벗어난 거리다. 앞으로 벗어나도 뒤로 벗어나도 같고, 안이면 0이다."""
    hope = PreferredWindow(task_id="A", start=60, end=150)
    assert hope.start_range(30) == (60, 120)  # 이 안에서 시작하면 희망 영역 안에서 끝난다
    assert [hope.deviation(s, 30) for s in (60, 90, 120)] == [0, 0, 0]
    assert (hope.deviation(30, 30), hope.deviation(150, 30)) == (30, 30)
    # 영역이 작업 시간보다 짧으면 희망 시작 한 점이 기준이다
    short = PreferredWindow(task_id="A", start=60, end=70)
    assert short.start_range(30) == (60, 60)
    assert (short.deviation(60, 30), short.deviation(75, 30)) == (0, 15)


def test_task_without_hope_keeps_the_old_delay(with_a):
    """희망 영역이 없는 작업은 지금처럼 기준 시작보다 늦어진 만큼이다(앞당기면 0)."""
    facts = take_snapshot(with_a).facts()
    base = facts.base_assignments()
    assert base["A"].start == 0  # 계획에 없는 폼 작업의 기준 시작은 가장 이른 시작
    assert (facts.deviation("C", base["C"].start + 30), facts.deviation("C", 0)) == (30, 0)


def test_work_deviation_counts_work_minutes_on_both_sides(with_a):
    """근무 분도 같은 정의다: 희망 범위 밖으로 벗어난 구간의 근무 분."""
    facts = _hoped(_wide(take_snapshot(with_a)), "A", 420, 480).facts()  # 첫날 16:00–17:00
    assert facts.preferred_map()["A"].start_range(30) == (420, 450)
    assert work_deviation(facts, "A", 430) == 0
    assert (facts.deviation("A", 360), work_deviation(facts, "A", 360)) == (60, 60)
    # 다음 근무일 09:00으로 밀리면 달력 분은 밤을 지나지만 근무 분은 첫날 남은 30분뿐이다
    assert (facts.deviation("A", 1440), work_deviation(facts, "A", 1440)) == (990, 30)


# ── 기준 위치와 탐색 키 ────────────────────────────────────────


def test_unplanned_task_base_is_the_hope_start_and_hope_is_in_the_search_key(with_a):
    pack = with_a
    snap = take_snapshot(pack)
    hoped = _hoped(_wide(snap), "A", 30, 120)
    assert snap.facts().base_assignments()["A"].start == 0
    assert hoped.facts().base_assignments()["A"].start == 30
    # 시간창 밖의 희망은 시간창 안으로 맞춘 자리가 기준이다(폼 작업 A의 시작 한도는 10:00)
    assert _hoped(snap, "A", 200, 260).facts().base_assignments()["A"].start == 60
    # 계획에 있는 작업의 기준은 희망과 관계없이 지금 배치다
    c = snap.facts().base_assignments()["C"]
    assert _hoped(snap, "C", 200, 260).facts().base_assignments()["C"] == c

    # 희망 영역은 Solver 입력이다: 탐색 키가 달라진다. 출처는 입력이 아니라 키가 같다
    wide = _wide(snap)
    conflict = _first_conflict(pack, wide)
    keys = [
        build_search_spec(s, [conflict], "L1").search_key
        for s in (
            wide,
            _hoped(wide, "A", 0, 90),
            _hoped(wide, "A", 0, 90, "DECIDED"),
            _hoped(wide, "A", 0, 150),
        )
    ]
    assert keys[0] != keys[1] and keys[1] == keys[2] and keys[1] != keys[3]
    stated, decided = _hoped(wide, "A", 0, 90), _hoped(wide, "A", 0, 90, "DECIDED")
    assert stated.snapshot_hash != decided.snapshot_hash  # Snapshot에는 출처까지 들어간다


# ── Solver ─────────────────────────────────────────────────────


def test_unplanned_task_inside_its_hope_is_not_a_change(with_a):
    """계획에 없는 작업은 희망 시작 범위 안이면 어디에 놓여도 변경이 아니고 지연도 0이다. 범위 밖이면
    변경 하나이고 벗어난 만큼이 지연이다. A만 움직이는 범위(L0)에서 A가 갈 수 있는 첫 자리는 10:30이다."""
    pack = with_a
    wide = _wide(take_snapshot(pack))

    narrow = _hoped(wide, "A", 0, 90)  # 희망 시작 09:00–10:00
    spec = build_search_spec(narrow, [_first_conflict(pack, narrow)], "L0")
    assert [t for t, ax in spec.axes.items() if ax.time] == ["A"]
    result = cpsat.solve(narrow, spec, pack)
    assert (_start(result, "A"), result.stage1["changed"], result.stage2["delay"]) == (90, 1, 30)

    roomy = _hoped(wide, "A", 0, 150)  # 희망 시작 09:00–11:00
    spec = build_search_spec(roomy, [_first_conflict(pack, roomy)], "L0")
    result = cpsat.solve(roomy, spec, pack)
    assert (result.stage1["changed"], result.stage2["delay"]) == (0, 0)
    assert 90 <= _start(result, "A") <= 120


def test_placing_a_task_before_its_hope_costs_as_much_as_after(with_a):
    """희망보다 앞에 놓아도 벗어난 만큼이 지연이다: 지연 최소가 희망과 반대로 당기지 않는다."""
    pack = with_a
    snap = _hoped(_wide(take_snapshot(pack)), "A", 120, 180)  # 희망 시작 11:00–11:30
    conflict = Conflict(rule_id="TEST", task_ids=("A", "B"), zone_ids=(), interval=(0, 1))
    free = cpsat.solve(snap, build_search_spec(snap, [conflict], "L0"), pack)
    assert (free.stage2["delay"], _start(free, "A")) == (0, 120)  # 기준 위치가 희망 시작이다
    early = build_search_spec(snap, [conflict], "L0", {"A": Condition(start_max=90)})
    result = cpsat.solve(snap, early, pack)
    assert (_start(result, "A"), result.stage2["delay"]) == (90, 30)


def test_planned_task_counts_change_against_the_plan_and_delay_against_its_hope(with_a):
    """계획에 있는 작업은 변경을 지금 배치와 비교해 세고, 지연은 희망 영역과 비교해 잰다."""
    pack = with_a
    plain = take_snapshot(pack)
    spec = build_search_spec(plain, [_first_conflict(pack, plain)], "L1")
    before = cpsat.solve(plain, spec, pack)
    # 희망이 없으면 둘 다 늦어진 만큼이다: A 09:00→10:00, C 10:00→10:30
    assert (before.stage1["changed"], before.stage2["delay"]) == (2, 90)

    snap = _hoped(plain, "C", 90, 150)  # C의 희망 시작 10:30–11:00
    spec = build_search_spec(snap, [_first_conflict(pack, snap)], "L1")
    result = cpsat.solve(snap, spec, pack)
    # 같은 배치지만 C는 희망 범위 안이라 지연이 0이다. 지금 자리에서 옮겼으므로 변경으로는 센다
    assert (_start(result, "A"), _start(result, "C")) == (60, 90)
    assert (result.stage1["changed"], result.stage2["delay"]) == (2, 60)


# ── 협의: 말한 희망의 범위 안이면 묻지 않는다 ──────────────────


def _moved(snapshot, task_id, start):
    """기준 배정에서 그 작업의 시작만 옮긴 후보 (메모리)."""
    facts = snapshot.facts()
    base = facts.base_assignments()
    moved = base[task_id].model_copy(
        update={"start": start, "end": start + base[task_id].end - base[task_id].start}
    )
    return Candidate(
        candidate_id="cand_test",
        snapshot_id=snapshot.snapshot_id,
        search_spec_id=None,
        search_spec_hash=None,
        solver_result_id=None,
        base_plan_revision=facts.plan_revision,
        context_version=facts.context_version,
        pack_hash=facts.pack_hash,
        assignments=tuple(moved if a.task_id == task_id else a for a in base.values()),
        candidate_hash="h",
        kind="REPLAN",
    )


def _item_status(snapshot, task_id, start):
    items = build_items(snapshot.facts(), _moved(snapshot, task_id, start))
    return {i.task_id: i.base_status for i in items}.get(task_id)


def test_hope_range_decides_whether_the_owner_is_asked(with_a):
    wide = _wide(take_snapshot(with_a))
    # 말한 희망: 범위 안이면 묻지 않고(COVERED), 밖이면 묻는다(PENDING)
    stated = _hoped(wide, "A", 0, 150)
    assert (_item_status(stated, "A", 90), _item_status(stated, "A", 180)) == ("COVERED", "PENDING")
    # 정한 희망은 요청자가 확인하기 전까지 "묻지 않음"이 없다 (AG-33)
    assert _item_status(_hoped(wide, "A", 0, 150, "DECIDED"), "A", 90) == "PENDING"
    # 희망 영역도 시작 범위 Consent도 없으면 묻는다
    assert _item_status(wide, "A", 90) == "PENDING"


def test_form_task_without_hope_still_uses_its_start_range_consent(seeded_real):
    """희망 영역이 없는 폼 작업은 지금처럼 Hard 시작 범위가 동의 범위다. 희망을 그리면 그 범위가 된다."""
    from app.commands.task_request import TaskRequestForm, submit_task_request

    pack = seeded_real
    a = pack.new_task.model_dump(exclude={"requested", "unit_id", "owner_actor_id", "movable"})
    assert submit_task_request(pack, "planner_a", _key(), TaskRequestForm(**a)).status == "APPLIED"
    snap = take_snapshot(pack)
    assert (_item_status(snap, "A", 60), _item_status(snap, "A", 30)) == ("COVERED", "COVERED")
    hoped = _hoped(snap, "A", 0, 60)  # 희망 시작 09:00–09:30
    assert (_item_status(hoped, "A", 60), _item_status(hoped, "A", 30)) == ("PENDING", "COVERED")


# ── 사실 변경 표시 ─────────────────────────────────────────────


def test_fact_changes_between_two_snapshots(with_a):
    before = take_snapshot(with_a)
    facts = before.facts()
    c = facts.task_map()["C"]
    after = with_facts(
        _hoped(before, "C", 120, 180),
        tasks=tuple(
            t.model_copy(update={"duration": t.duration + 15}) if t.task_id == "C" else t
            for t in facts.tasks
            if t.task_id != "E"
        ),
        pins=tuple(p for p in facts.pins if p.task_id != "B"),
    ).facts()
    found = fact_changes(facts, after)
    assert {"kind": "TASK_REMOVED", "task_id": "E"} in found
    assert {
        "kind": "VALUE_CHANGED",
        "task_id": "C",
        "field": "duration",
        "before": c.duration,
        "after": c.duration + 15,
    } in found
    assert {"kind": "UNPINNED", "task_id": "B"} in found
    assert {
        "kind": "PREFERRED_WINDOW_CHANGED",
        "task_id": "C",
        "before": None,
        "after": {"start": 120, "end": 180, "origin": "STATED"},
    } in found
    assert len(found) == 4 and fact_changes(facts, facts) == []
    # 반대 방향: 새로 들어온 작업과 고정
    back = fact_changes(after, facts)
    assert {"kind": "TASK_ADDED", "task_id": "E"} in back
    assert next(x for x in back if x["kind"] == "PINNED")["task_id"] == "B"


def _reconfirm(pack):
    with db.read() as conn:
        state = build_state(conn, pack, "supervisor")
    return [c for c in state["candidates"] if c["kind"] == "RECONFIRM"]


def test_reconfirm_candidate_shows_what_changed_since_the_base_plan(seeded_real):
    """배치가 그대로인 재확정 후보에도 무엇 때문에 다시 확정하는지가 보인다: 기준 계획을 확정할 때의
    사실과 지금 사실의 차이(희망 영역, 고정, 카드에서 고친 값). 희망에서 벗어난 작업과 정도도 보인다."""
    pack = seeded_real
    with db.read() as conn:
        plan = get_current_plan(conn, pack.site_id)
        c = next(t for t in list_current_tasks(conn, pack.site_id, pack) if t.task_id == "C")
    placed = next(a for a in plan.assignments if a.task_id == "C")
    start, end = placed.start + 60, placed.start + 60 + 2 * c.duration
    body = WindowBody(task_id="C", start=start, end=end)
    assert set_preferred_window(pack, "foreman_a2", _key(), body).status == "APPLIED"
    run_until_idle(pack, model_factory=Router().factory())

    with db.read() as conn:
        tasks = {t["task_id"]: t for t in build_state(conn, pack, "foreman_a2")["tasks"]}
    assert tasks["C"]["base_start"] == placed.start  # 계획에 있는 작업의 기준은 지금 배치다
    [first] = _reconfirm(pack)
    hope = {"start": start, "end": end, "origin": "STATED"}
    assert first["changes"] == []  # 배치는 그대로다
    assert first["fact_changes"] == [
        {"kind": "PREFERRED_WINDOW_CHANGED", "task_id": "C", "before": None, "after": hope}
    ]
    # C는 지금 자리가 희망보다 이르다: 벗어난 정도는 희망 시작까지의 거리다
    assert first["off_hope"] == [
        {"task_id": "C", "delay": 60, "work_delay": 60, "direction": "EARLY"}
    ]

    assert pin_task(pack, "foreman_a2", _key(), TaskRef(task_id="C")).status == "APPLIED"
    edit = EditRequest(task_id="C", latest_end=c.latest_end - 30)
    assert edit_task(pack, "foreman_a2", _key(), edit).status == "APPLIED"
    run_until_idle(pack, model_factory=Router().factory())
    second = _reconfirm(pack)[0]  # 가장 최근 후보가 앞에 온다
    assert second["candidate_id"] != first["candidate_id"] and second["changes"] == []
    assert second["fact_changes"] == [
        {
            "kind": "VALUE_CHANGED",
            "task_id": "C",
            "field": "latest_end",
            "before": c.latest_end,
            "after": c.latest_end - 30,
        },
        {"kind": "PINNED", "task_id": "C", "pinned_by": "foreman_a2", "by_role": "OWNER"},
        {"kind": "PREFERRED_WINDOW_CHANGED", "task_id": "C", "before": None, "after": hope},
    ]


def test_off_hope_lists_only_tasks_outside_their_hope(with_a):
    snap = _hoped(take_snapshot(with_a), "C", 0, 240)
    facts = snap.facts()
    assert off_hope(facts, facts.base_assignments().values()) == []  # C는 희망 범위 안에 있다
    late = _moved(_hoped(snap, "C", 0, 60), "C", 90)
    assert off_hope(_hoped(snap, "C", 0, 60).facts(), late.assignments) == [
        {"task_id": "C", "delay": 60, "work_delay": 60, "direction": "LATE"}
    ]


# ── 안이 바꾸는 것 (안 비교) ───────────────────────────────────


def test_plan_changes_list_time_resource_new_placement_and_off_request(with_a):
    """안 비교의 "바뀌는 것"은 서버가 계산한다: 시각 변경, 자원 변경, 계획 밖 작업의 새 배치, 요청 자원과
    다름. 계획 밖 작업은 기준 자리 그대로 놓여도 새 배치로 나온다."""
    from app.api.state import plan_changes

    snap = take_snapshot(with_a)
    facts = snap.facts()
    base = facts.base_assignments()
    c, a = base["C"], base["A"]
    assert a.resource_id == "A-CR-01" and facts.task_map()["A"].requested_resource_id == "A-CR-01"

    def placed(**moves):
        return tuple(x.model_copy(update=moves.get(tid, {})) for tid, x in base.items())

    # 기준 그대로: 계획 밖의 A만 새 배치로 나온다(요청 자원 그대로)
    [only] = plan_changes(facts, placed())
    assert (only["task_id"], only["kind"], only["before"]) == ("A", "NEW", None)
    assert (only["time_changed"], only["resource_changed"], only["off_request"]) == (
        False,
        False,
        False,
    )

    found = plan_changes(
        facts,
        placed(
            A={"start": 60, "end": 90, "resource_id": "SITE-CR-01"},
            C={"start": c.start + 30, "end": c.end + 30},
        ),
    )
    by_task = {x["task_id"]: x for x in found}
    assert sorted(by_task) == ["A", "C"]
    # 계획 밖 작업: 새 배치이고 요청 자원과 다른 자원에 놓였다
    assert (by_task["A"]["kind"], by_task["A"]["after"]["start"]) == ("NEW", 60)
    assert (by_task["A"]["resource_changed"], by_task["A"]["off_request"]) == (True, True)
    assert (by_task["A"]["requested_resource_id"], by_task["A"]["resource_type"]) == (
        "A-CR-01",
        "CRANE",
    )
    # 계획에 있던 작업: 시각만 바뀌었다(전·후가 있다)
    assert (by_task["C"]["kind"], by_task["C"]["before"]["start"]) == ("CHANGED", c.start)
    assert (by_task["C"]["time_changed"], by_task["C"]["resource_changed"]) == (True, False)

    # 계획에 있던 작업의 자원만 바뀌면 자원 변경이고, 요청 자원과도 달라진다
    [swap] = [
        x
        for x in plan_changes(facts, placed(C={"resource_id": "SITE-CR-01"}))
        if x["task_id"] == "C"
    ]
    assert (swap["time_changed"], swap["resource_changed"], swap["off_request"]) == (
        False,
        True,
        True,
    )
    assert (swap["before"]["resource_id"], swap["after"]["resource_id"]) == (
        c.resource_id,
        "SITE-CR-01",
    )


def test_change_request_text_has_resource_before_and_after(with_a):
    """담당자에게 가는 변경 요청 문장에 자원 전→후가 들어간다."""
    from app.agents.executors.coordination import change_request_text

    facts = take_snapshot(with_a).facts()
    c = facts.base_assignments()["C"]
    task = facts.task_map()["C"]
    swapped = c.model_copy(update={"resource_id": "SITE-CR-01"})
    text = change_request_text(with_a, task, c, swapped)
    assert f"자원 {c.resource_id} → SITE-CR-01" in text and "시작" not in text
    moved = swapped.model_copy(update={"start": c.start + 30, "end": c.end + 30})
    both = change_request_text(with_a, task, c, moved)
    assert "시작 " in both and f"자원 {c.resource_id} → SITE-CR-01" in both
