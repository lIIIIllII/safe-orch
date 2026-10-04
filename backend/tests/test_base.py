"""기준 위치와 지연·변경·협의 기준 (CV-29, AG-33).

기준 위치는 작업마다 하나다: 계획 작업은 승인된 자리(한 점), 일정의 새 작업은 문서의 배정(한 점), 자연어의
새 작업은 문장에서 말한 시작 범위, 폼의 새 작업은 없다. 변경과 지연은 이 범위에서 재고, Solver 목적·조회·
근무 분·협의 항목이 같은 정의다. 기준 장면(with_a)의 A는 폼 작업이라, 자연어 작업처럼 보려고 시간창을
Horizon 전체로 넓히고 기준 위치를 붙여 쓴다.
"""

import uuid

from conftest import add_task, make_task, take_snapshot, with_facts
from scripted import Router

from app.api.state import build_state, direction, plan_changes, work_deviation
from app.commands.pins import TaskRef, pin_task
from app.commands.task_edit import EditRequest, edit_task
from app.coordinator.dispatcher import run_until_idle
from app.domain.consultation import build_items
from app.domain.factdiff import fact_changes
from app.domain.models import Candidate, Condition, Conflict, TaskBase
from app.rules.engine import detect_conflicts
from app.solver import cpsat
from app.solver.candidate import build_candidate
from app.solver.search_spec import build_search_spec
from app.store import db
from app.store.repos.tasks import list_current_tasks


def _key():
    return uuid.uuid4().hex


def _based(snapshot, task_id, start, start_max=None, origin="STATED", resource_id=None):
    """그 작업에 기준 위치를 둔 Snapshot (메모리)."""
    base = TaskBase(
        task_id=task_id, start=start, start_max=start_max, origin=origin, resource_id=resource_id
    )
    kept = tuple(b for b in snapshot.facts().task_bases if b.task_id != task_id)
    return with_facts(snapshot, task_bases=(*kept, base))


def _wide(snapshot, task_id="A"):
    """그 작업의 시간창을 Horizon 전체로 넓힌 Snapshot (자연어·일정으로 접수된 작업의 모양)."""
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


# ── 기준 범위와 지연 ───────────────────────────────────────────


def test_planned_task_is_measured_from_its_approved_start_both_ways(with_a):
    """계획 작업: 승인된 시작 한 점에서 옮긴 거리(앞뒤 모두)가 지연이다. 근무 분도 같은 정의다."""
    facts = take_snapshot(with_a).facts()
    base = facts.base_assignments()
    assert base["C"].start == 60 and facts.base_range("C") == (60, 60)  # 승인된 시작 10:00
    assert [facts.deviation("C", s) for s in (90, 60, 0)] == [30, 0, 60]
    assert (work_deviation(facts, "C", 90), work_deviation(facts, "C", 0)) == (30, 60)
    assert [direction(facts, "C", s) for s in (90, 60, 0)] == ["LATE", None, "EARLY"]
    # 하루 앞이나 뒤로 옮기면 달력 분은 하루지만 근무 분은 그 사이의 근무 구간만이다
    k = base["K"].start
    assert (facts.deviation("K", k - 1440), work_deviation(facts, "K", k - 1440)) == (1440, 480)
    assert facts.base_info("C") == {"source": "PLAN", "start": 60, "start_max": 60, "origin": None}


def test_form_task_has_no_base_and_is_never_measured(with_a):
    """폼의 새 작업: 기준이 없다. 시간창 안 어디든 지연 0이고, 가장 이른 시작을 자리처럼 쓰지 않는다."""
    facts = take_snapshot(with_a).facts()
    assert "A" not in {a.task_id for a in facts.plan.assignments}
    a = facts.task_map()["A"]
    assert facts.base_range("A") is None
    assert [facts.deviation("A", s) for s in (a.earliest_start, 30, a.latest_start)] == [0, 0, 0]
    assert work_deviation(facts, "A", a.latest_start) == 0
    assert direction(facts, "A", a.latest_start) is None
    assert facts.base_info("A")["source"] == "NONE"
    # 검사 대상 배정의 자리는 가장 이른 시작·요청 자원이다(충돌 탐지용)
    placed = facts.base_assignments()["A"]
    assert (placed.start, placed.resource_id) == (a.earliest_start, a.requested_resource_id)


def test_stated_range_is_free_inside_and_measured_from_its_ends_outside(with_a):
    """자연어의 새 작업: 말한 시작 범위 안은 지연 0이고, 밖은 범위 끝에서 벗어난 거리다(앞뒤 모두)."""
    facts = _based(_wide(take_snapshot(with_a)), "A", 420, 450).facts()  # 첫날 16:00–16:30 시작
    assert facts.base_range("A") == (420, 450)
    assert facts.base_assignments()["A"].start == 420  # 가장 이른 시작이 기준 시작점이다
    assert [facts.deviation("A", s) for s in (420, 430, 450)] == [0, 0, 0]
    assert (facts.deviation("A", 360), work_deviation(facts, "A", 360)) == (60, 60)
    assert direction(facts, "A", 360) == "EARLY"
    # 다음 근무일 09:00으로 밀리면 달력 분은 밤을 지나지만 근무 분은 첫날 남은 30분뿐이다
    assert (facts.deviation("A", 1440), work_deviation(facts, "A", 1440)) == (990, 30)
    assert direction(facts, "A", 1440) == "LATE"
    assert facts.base_info("A") == {
        "source": "REQUEST",
        "start": 420,
        "start_max": 450,
        "origin": "STATED",
    }


def test_schedule_task_is_measured_from_the_document_point(with_a):
    """일정의 새 작업: 문서의 배정 한 점에서 잰다. 기준 자원은 문서 배정의 자원이다."""
    snap = _based(_wide(take_snapshot(with_a)), "A", 30, resource_id="SITE-CR-01")
    facts = snap.facts()
    assert facts.base_range("A") == (30, 30)
    assert [facts.deviation("A", s) for s in (0, 30, 90)] == [30, 0, 60]
    base = facts.base_assignments()["A"]
    assert (base.start, base.resource_id) == (30, "SITE-CR-01")
    # 문서의 자리 그대로면 변경이 아니고, 요청 자원으로 옮기면 기준 자원이 아니라 변경이다
    assert not facts.is_changed(base)
    assert facts.is_changed(base.model_copy(update={"resource_id": "A-CR-01"}))


def test_base_range_is_clamped_into_the_time_window(with_a):
    """시간창(Hard) 밖의 기준은 시간창 안으로 맞춘 범위가 기준이다(폼 작업 A의 시작 한도는 10:00)."""
    snap = take_snapshot(with_a)
    assert _based(snap, "A", 200, 260).facts().base_range("A") == (60, 60)
    assert _based(snap, "A", 30, 260).facts().base_range("A") == (30, 60)
    # 계획에 있는 작업의 기준은 기준 위치 기록과 관계없이 승인된 자리다
    c = snap.facts().base_assignments()["C"]
    assert _based(snap, "C", 200, 260).facts().base_assignments()["C"] == c
    assert _based(snap, "C", 200, 260).facts().base_range("C") == (c.start, c.start)


def test_base_range_is_in_the_search_key_and_its_origin_is_not(with_a):
    """기준 시작 범위는 Solver 입력이다: 탐색 키가 달라진다. 출처(말함·정함)는 입력이 아니라 키가 같다."""
    pack = with_a
    wide = _wide(take_snapshot(pack))
    conflict = _first_conflict(pack, wide)
    keys = [
        build_search_spec(s, [conflict], "L1").search_key
        for s in (
            wide,
            _based(wide, "A", 0, 60),
            _based(wide, "A", 0, 60, "DECIDED"),
            _based(wide, "A", 0, 120),
        )
    ]
    assert keys[0] != keys[1] and keys[1] == keys[2] and keys[1] != keys[3]
    stated, decided = _based(wide, "A", 0, 60), _based(wide, "A", 0, 60, "DECIDED")
    assert stated.snapshot_hash != decided.snapshot_hash  # Snapshot에는 출처까지 들어간다


# ── Solver ─────────────────────────────────────────────────────


def test_new_task_inside_its_stated_range_is_not_a_change(with_a):
    """새 작업은 말한 시작 범위 안이면 어디에 놓여도 변경이 아니고 지연도 0이다. 범위 밖이면 변경 하나이고
    범위 끝에서 벗어난 만큼이 지연이다. A만 움직이는 범위(L0)에서 A가 갈 수 있는 첫 자리는 10:30이다."""
    pack = with_a
    wide = _wide(take_snapshot(pack))

    narrow = _based(wide, "A", 0, 60)  # 09:00–10:00 사이 시작
    spec = build_search_spec(narrow, [_first_conflict(pack, narrow)], "L0")
    assert [t for t, ax in spec.axes.items() if ax.time] == ["A"]
    result = cpsat.solve(narrow, spec, pack)
    assert (_start(result, "A"), result.stage1["changed"], result.stage2["delay"]) == (90, 1, 30)

    roomy = _based(wide, "A", 0, 120)  # 09:00–11:00 사이 시작
    spec = build_search_spec(roomy, [_first_conflict(pack, roomy)], "L0")
    result = cpsat.solve(roomy, spec, pack)
    assert (result.stage1["changed"], result.stage2["delay"]) == (0, 0)
    assert 90 <= _start(result, "A") <= 120


def test_decided_range_is_used_exactly_like_a_stated_one(with_a):
    """접수 Agent가 정한 범위도 말한 범위와 똑같이 쓴다: 같은 해, 같은 변경 수와 지연."""
    pack = with_a
    wide = _wide(take_snapshot(pack))
    found = []
    for origin in ("STATED", "DECIDED"):
        snap = _based(wide, "A", 0, 120, origin)
        spec = build_search_spec(snap, [_first_conflict(pack, snap)], "L0")
        result = cpsat.solve(snap, spec, pack)
        found.append((result.solution, result.stage1["changed"], result.stage2["delay"]))
        assert build_items(snap.facts(), build_candidate(snap, spec, result)) == ()
    assert found[0] == found[1]


def test_placing_a_task_before_its_range_costs_as_much_as_after(with_a):
    """범위보다 앞에 놓아도 벗어난 만큼이 지연이다: 지연 최소가 요청과 반대로 당기지 않는다."""
    pack = with_a
    snap = _based(_wide(take_snapshot(pack)), "A", 120, 150)  # 11:00–11:30 사이 시작
    conflict = Conflict(rule_id="TEST", task_ids=("A", "B"), zone_ids=(), interval=(0, 1))
    free = cpsat.solve(snap, build_search_spec(snap, [conflict], "L0"), pack)
    assert free.stage2["delay"] == 0 and 120 <= _start(free, "A") <= 150
    early = build_search_spec(snap, [conflict], "L0", {"A": Condition(start_max=90)})
    result = cpsat.solve(snap, early, pack)
    assert (_start(result, "A"), result.stage2["delay"]) == (90, 30)


def test_form_task_gives_way_first_and_the_planned_task_counts_as_the_change(with_a):
    """폼의 새 작업 A는 변경도 지연도 아니고, 계획에 있던 C(10:00→10:30)만 변경 하나에 지연 30분이다."""
    pack = with_a
    plain = take_snapshot(pack)
    spec = build_search_spec(plain, [_first_conflict(pack, plain)], "L1")
    result = cpsat.solve(plain, spec, pack)
    assert (_start(result, "A"), _start(result, "C")) == (60, 90)
    assert (result.stage1["changed"], result.stage2["delay"]) == (1, 30)


def test_moving_a_planned_task_earlier_is_not_free(seeded_real):
    """계획 작업은 승인된 시작에서 옮긴 거리가 지연이다(앞뒤 모두, CV-29).
    K(10/13 09:00–11:00)의 자리를 X가 차지하면 K는 하루 앞으로 가지 않고 가장 가까운 11:00으로 간다."""
    pack = seeded_real
    gantry = {
        "zone_id": "F",
        "required_resource_type": "GANTRY",
        "requested_resource_id": "SITE-GC-01",
    }
    # X: 10/13 09:00에만 시작할 수 있는 2시간 작업(계획 밖, 기준 없음)
    x = make_task(
        pack, task_id="X", duration=120, earliest_start=1440, latest_start=1440, latest_end=1560
    )
    add_task(pack, x.model_copy(update=gantry))
    snap = take_snapshot(pack)
    facts = snap.facts()
    conflicts = detect_conflicts(snap, facts.check_assignments(), pack)
    assert [c.task_ids for c in conflicts] == [("K", "X")]
    assert facts.task_map()["K"].earliest_start == 0  # K는 첫날로도 갈 수 있다
    for objective in ("CHANGE_FIRST", "DELAY_FIRST"):
        spec = build_search_spec(snap, conflicts, "L0", None, objective)
        result = cpsat.solve(snap, spec, pack)
        assert (_start(result, "X"), _start(result, "K")) == (1440, 1560), objective
        # X는 계획 밖이고 기준이 없어 세지 않는다. K는 변경 하나에 지연 120분이다
        assert (result.stage1["status"], result.stage2["status"]) == ("OPTIMAL", "OPTIMAL")
        assert (
            result.stage2["changed"] if objective == "DELAY_FIRST" else result.stage1["changed"]
        ) == 1
        assert result.stage2["delay"] == 120
    assert (facts.deviation("K", 1560), facts.deviation("K", 0), facts.deviation("X", 1440)) == (
        120,
        1440,
        0,
    )


# ── 협의: 기준에서 바뀐 작업만 묻는다 ──────────────────────────


def _moved(snapshot, task_id, start, resource_id=None):
    """기준 배정에서 그 작업의 시작(과 자원)만 옮긴 후보 (메모리)."""
    facts = snapshot.facts()
    base = facts.base_assignments()
    update = {"start": start, "end": start + base[task_id].end - base[task_id].start}
    if resource_id is not None:
        update["resource_id"] = resource_id
    moved = base[task_id].model_copy(update=update)
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


def _asked(snapshot, task_id, start, resource_id=None):
    """그 후보의 협의 항목이 가는 사람 (작업 → 담당자)."""
    items = build_items(snapshot.facts(), _moved(snapshot, task_id, start, resource_id))
    return {i.task_id: i.owner_actor_id for i in items}


def test_new_task_goes_to_its_requester_only_outside_the_stated_range(with_a):
    wide = _wide(take_snapshot(with_a))
    owner = wide.facts().task_map()["A"].owner_actor_id
    stated = _based(wide, "A", 0, 120)
    # 범위 안은 협의 항목이 아니고, 밖은 요청자에게 간다
    assert (_asked(stated, "A", 90), _asked(stated, "A", 180)) == ({}, {"A": owner})
    # 범위 밖 항목의 변경 전은 기준 시작점(범위의 가장 이른 시작)이다
    [item] = build_items(stated.facts(), _moved(stated, "A", 180))
    assert (item.before.start, item.after.start) == (0, 180)
    # 정한 범위도 똑같다
    decided = _based(wide, "A", 0, 120, "DECIDED")
    assert (_asked(decided, "A", 90), _asked(decided, "A", 180)) == ({}, {"A": owner})
    # 범위 안이어도 기준 자원(요청 자원)이 아니면 항목이다
    assert _asked(stated, "A", 90, "SITE-CR-01") == {"A": owner}


def test_form_task_is_asked_only_when_its_resource_changes(with_a):
    """폼의 새 작업: 시간창 안 어디든 협의 항목이 아니다. 자원이 요청 자원과 다를 때만 항목이 된다."""
    snap = take_snapshot(with_a)
    owner = snap.facts().task_map()["A"].owner_actor_id
    assert (_asked(snap, "A", 0), _asked(snap, "A", 30), _asked(snap, "A", 60)) == ({}, {}, {})
    assert _asked(snap, "A", 30, "SITE-CR-01") == {"A": owner}


def test_planned_task_goes_to_its_owner_whenever_it_moves(with_a):
    """계획 작업: 승인된 자리에서 앞으로든 뒤로든 옮기면 그 담당자에게 간다."""
    snap = take_snapshot(with_a)
    facts = snap.facts()
    c, owner = facts.base_assignments()["C"], facts.task_map()["C"].owner_actor_id
    assert _asked(snap, "C", c.start) == {}
    assert _asked(snap, "C", c.start + 30) == {"C": owner}
    assert _asked(snap, "C", c.start - 30) == {"C": owner}
    assert _asked(snap, "C", c.start, "SITE-CR-01") == {"C": owner}


def test_consultation_items_and_solver_change_count_use_the_same_rule(with_a):
    """협의 항목과 Solver의 변경 수가 같은 기준이다: 해마다 항목 수 = 변경 작업 수."""
    pack = with_a
    plain = take_snapshot(pack)
    wide = _wide(plain)
    cases = [
        (plain, "L1"),  # 폼 작업 A는 세지 않고 계획 작업 C만 바뀐다
        (_based(wide, "A", 0, 60), "L0"),  # 말한 범위 밖으로 놓인다
        (_based(wide, "A", 0, 120), "L0"),  # 말한 범위 안에서 비킨다
        (_based(wide, "A", 30), "L0"),  # 문서의 배정 한 점에서 벗어난다
    ]
    counts = []
    for snap, level in cases:
        spec = build_search_spec(snap, [_first_conflict(pack, snap)], level)
        result = cpsat.solve(snap, spec, pack)
        candidate = build_candidate(snap, spec, result)
        items = build_items(snap.facts(), candidate)
        assert len(items) == result.stage1["changed"]
        assert sum(snap.facts().is_changed(a) for a in candidate.assignments) == len(items)
        counts.append(([i.task_id for i in items], result.stage2["delay"]))
    assert counts == [(["C"], 30), (["A"], 30), ([], 0), (["A"], 60)]


# ── 사실 변경 표시 ─────────────────────────────────────────────


def test_fact_changes_between_two_snapshots(with_a):
    before = take_snapshot(with_a)
    facts = before.facts()
    c = facts.task_map()["C"]
    after = with_facts(
        before,
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
    assert len(found) == 3 and fact_changes(facts, facts) == []
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
    사실과 지금 사실의 차이(고정, 카드에서 고친 값)."""
    pack = seeded_real
    with db.read() as conn:
        c = next(t for t in list_current_tasks(conn, pack.site_id, pack) if t.task_id == "C")
    assert pin_task(pack, "foreman_a2", _key(), TaskRef(task_id="C")).status == "APPLIED"
    edit = EditRequest(task_id="C", latest_end=c.latest_end - 30)
    assert edit_task(pack, "foreman_a2", _key(), edit).status == "APPLIED"
    run_until_idle(pack, model_factory=Router().factory())

    with db.read() as conn:
        tasks = {t["task_id"]: t for t in build_state(conn, pack, "foreman_a2")["tasks"]}
    assert tasks["C"]["base"]["source"] == "PLAN"  # 계획에 있는 작업의 기준은 승인된 자리다
    assert tasks["C"]["base"]["start"] == tasks["C"]["base_start"]
    found = _reconfirm(pack)[0]  # 가장 최근 후보가 앞에 온다
    assert found["changes"] == [] and found["plan_changes"] == []  # 배치는 그대로다
    assert found["fact_changes"] == [
        {
            "kind": "VALUE_CHANGED",
            "task_id": "C",
            "field": "latest_end",
            "before": c.latest_end,
            "after": c.latest_end - 30,
        },
        {"kind": "PINNED", "task_id": "C", "pinned_by": "foreman_a2", "by_role": "OWNER"},
    ]


# ── 안이 바꾸는 것 (안 비교) ───────────────────────────────────


def test_plan_changes_list_time_resource_new_placement_and_off_request(with_a):
    """안 비교의 "바뀌는 것"은 서버가 계산한다: 시각 변경, 자원 변경, 계획 밖 작업의 새 배치, 요청 자원과
    다름, 기준에서 옮긴 거리와 방향. 계획 밖 작업은 기준 그대로 놓여도 새 배치로 나온다."""
    snap = take_snapshot(with_a)
    facts = snap.facts()
    base = facts.base_assignments()
    c, a = base["C"], base["A"]
    assert a.resource_id == "A-CR-01" and facts.task_map()["A"].requested_resource_id == "A-CR-01"

    def placed(**moves):
        return tuple(x.model_copy(update=moves.get(tid, {})) for tid, x in base.items())

    # 기준 그대로: 계획 밖의 A만 새 배치로 나온다(폼 작업이라 기준 위치가 없고 변경이 아니다)
    [only] = plan_changes(facts, placed())
    assert (only["task_id"], only["kind"], only["before"], only["base"]) == ("A", "NEW", None, None)
    assert (only["changed"], only["time_changed"], only["resource_changed"]) == (
        False,
        False,
        False,
    )
    assert (only["delay"], only["direction"], only["off_request"]) == (0, None, False)

    found = plan_changes(
        facts,
        placed(
            A={"start": 60, "end": 90, "resource_id": "SITE-CR-01"},
            C={"start": c.start + 30, "end": c.end + 30},
        ),
    )
    by_task = {x["task_id"]: x for x in found}
    assert sorted(by_task) == ["A", "C"]
    # 폼의 새 작업: 시각은 어디든 변경이 아니고, 요청 자원과 다른 자원에 놓인 것만 변경이다
    assert (by_task["A"]["kind"], by_task["A"]["after"]["start"]) == ("NEW", 60)
    assert (by_task["A"]["time_changed"], by_task["A"]["delay"]) == (False, 0)
    assert (by_task["A"]["changed"], by_task["A"]["resource_changed"]) == (True, True)
    assert by_task["A"]["off_request"] is True
    assert (by_task["A"]["requested_resource_id"], by_task["A"]["resource_type"]) == (
        "A-CR-01",
        "CRANE",
    )
    # 계획에 있던 작업: 시각만 바뀌었다(전·후와 옮긴 거리·방향이 있다)
    assert (by_task["C"]["kind"], by_task["C"]["before"]["start"]) == ("CHANGED", c.start)
    assert (by_task["C"]["time_changed"], by_task["C"]["resource_changed"]) == (True, False)
    assert (by_task["C"]["delay"], by_task["C"]["work_delay"], by_task["C"]["direction"]) == (
        30,
        30,
        "LATE",
    )
    [early] = [
        x
        for x in plan_changes(facts, placed(C={"start": c.start - 30, "end": c.end - 30}))
        if x["task_id"] == "C"
    ]
    assert (early["delay"], early["direction"]) == (30, "EARLY")

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
    assert (swap["delay"], swap["direction"]) == (0, None)


def test_plan_changes_show_the_base_of_a_new_task(with_a):
    """새 작업의 기준 위치(요청한 시작 범위·출처)가 새 배치에 붙는다. 범위 안이면 변경이 아니고, 밖이면
    범위 끝에서 옮긴 거리와 방향이 나온다."""
    snap = _based(_wide(take_snapshot(with_a)), "A", 30, 90, "DECIDED")
    facts = snap.facts()
    base = facts.base_assignments()

    def a_at(start):
        moved = base["A"].model_copy(update={"start": start, "end": start + 30})
        [found] = [x for x in plan_changes(facts, (moved,)) if x["task_id"] == "A"]
        return found

    inside = a_at(60)
    assert inside["base"] == {
        "start": 30,
        "start_max": 90,
        "resource_id": "A-CR-01",
        "origin": "DECIDED",
    }
    assert (inside["kind"], inside["changed"], inside["delay"], inside["direction"]) == (
        "NEW",
        False,
        0,
        None,
    )
    late, early = a_at(150), a_at(0)
    assert (late["changed"], late["delay"], late["direction"]) == (True, 60, "LATE")
    assert (early["changed"], early["delay"], early["direction"]) == (True, 30, "EARLY")


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
