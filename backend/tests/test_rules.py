"""Rule Engine 충돌 탐지. T07·T08·T30과 기본 제약."""

import json

import pytest
from conftest import LEGACY_PINNED, add_task, make_task, take_snapshot, with_facts

from app.domain.canonical import canonical_hash
from app.domain.models import Assignment, Predecessor, Requirement, ZoneRelation
from app.rules.engine import detect_conflicts
from app.store import db
from app.store.repos.site import get_site


def _asg(task_id, start, end, resource_id=None):
    return Assignment(task_id=task_id, start=start, end=end, resource_id=resource_id)


def _replace(assignments, *new):
    by_id = {a.task_id: a for a in assignments}
    by_id.update({a.task_id: a for a in new})
    return tuple(by_id[k] for k in sorted(by_id))


def _ids(conflicts):
    return [(c.rule_id, c.task_ids) for c in conflicts]


def _retask(snapshot, task_id, **changes):
    tasks = tuple(
        t.model_copy(update=changes) if t.task_id == task_id else t for t in snapshot.facts().tasks
    )
    return with_facts(snapshot, tasks=tasks)


# ── 확인 기준 ─────────────────────────────────────────────


def test_r0_has_no_conflict(seeded):
    snap = take_snapshot(seeded)
    assert detect_conflicts(snap, snap.facts().check_assignments(), seeded) == []


def test_r0_plus_a_base_is_single_sep_lift_below(with_a):
    snap = take_snapshot(with_a)
    check = snap.facts().check_assignments()
    assert _asg("A", 0, 30, "A-CR-01") in check
    conflicts = detect_conflicts(snap, check, with_a)
    assert [c.model_dump() for c in conflicts] == [
        {
            "rule_id": "SEP-LIFT-BELOW",
            "task_ids": ("A", "B"),
            "resource_id": None,
            "zone_ids": ("B",),
            "interval": (0, 60),
        }
    ]


# ── 작업 revision·Snapshot ────────────────────────────────


def test_add_task_inserts_revision_and_bumps_context(seeded):
    assert add_task(seeded, make_task(seeded)) == 1
    with db.read() as conn:
        assert get_site(conn, seeded.site_id).context_version == 1
        fields = json.loads(
            conn.execute("SELECT fields FROM task WHERE task_id = 'A'").fetchone()[0]
        )
    assert set(fields) == {"zone_id", "duration", "window", "resource"}
    assert {f["status"] for f in fields.values()} == {"CONFIRMED"}
    assert {f["source_ref"] for f in fields.values()} == {"scenario:new_task"}


def test_insert_task_revision_requires_next_revision(with_a):
    with pytest.raises(ValueError, match="revision 3 != expected 2"):
        add_task(with_a, make_task(with_a, revision=3))
    with pytest.raises(ValueError, match="revision 1 != expected 2"):
        add_task(with_a, make_task(with_a, revision=1))


def test_snapshot_content(with_a):
    snap = take_snapshot(with_a)
    content = snap.content
    assert snap.snapshot_id.startswith("snap_")
    assert snap.snapshot_hash == canonical_hash(content)
    assert set(content) == {
        "site_id", "pack_hash", "horizon_minutes", "work_intervals", "context_version",
        "plan_revision", "tasks", "resources", "pools", "zones", "zone_relations", "plan",
        "holds", "pins", "consents", "preferred_windows",
    }  # fmt: skip
    assert (content["site_id"], content["context_version"], content["plan_revision"]) == (
        "YARD-01",
        1,
        0,
    )
    assert content["pack_hash"] == with_a.pack_hash
    assert content["work_intervals"] == [[0, 480], [1440, 1920], [2880, 3360]]
    assert [t["task_id"] for t in content["tasks"]] == [
        "A", "B", "C", "D", "E", "K", "M", "P", "Q", "W",
    ]  # fmt: skip
    assert next(t for t in content["tasks"] if t["task_id"] == "E")["hazard_tags"] == ["FLAMMABLE"]
    assert content["zones"] == ["B", "C", "D", "D2", "F", "G", "G2", "H"]
    assert [(r["zone_a"], r["zone_b"]) for r in content["zone_relations"]] == [
        ("D", "D2"),
        ("D2", "D"),
        ("G", "G2"),
        ("G2", "G"),
    ]
    assert content["plan"]["plan_revision"] == 0
    assert (content["holds"], content["consents"]) == ([], [])
    assert sorted(p["task_id"] for p in content["pins"]) == sorted(LEGACY_PINNED)
    with db.read() as conn:
        stored = conn.execute(
            "SELECT snapshot_hash, content FROM snapshot WHERE snapshot_id = ?",
            (snap.snapshot_id,),
        ).fetchone()
    assert stored[0] == snap.snapshot_hash
    assert canonical_hash(json.loads(stored[1])) == snap.snapshot_hash


def test_snapshot_same_facts_same_hash_different_id(with_a):
    s1, s2 = take_snapshot(with_a), take_snapshot(with_a)
    assert s1.snapshot_id != s2.snapshot_id
    assert s1.snapshot_hash == s2.snapshot_hash


def test_snapshot_uses_current_ready_revision_only(with_a):
    e2 = make_task(
        with_a, revision=2, source_ref="fixture:plan_r0", **_e_values(with_a)
    ).model_copy(update={"lifecycle": "NEEDS_INFO"})
    add_task(with_a, e2)
    snap = take_snapshot(with_a)
    assert [t.task_id for t in snap.facts().tasks] == ["A", "B", "C", "D", "K", "M", "P", "Q", "W"]


def _e_values(pack):
    e = next(t for t in pack.tasks if t.task_id == "E")
    return e.model_dump(exclude={"revision", "lifecycle", "hazard_tags", "fields"}, mode="python")


# ── T07 자원 [start, end) ──────────────────────────────────────


def test_t07_resource_boundary_passes_overlap_fails(with_a):
    snap = take_snapshot(with_a)
    base = snap.facts().check_assignments()
    touching = _replace(base, _asg("A", 30, 60, "A-CR-01"), _asg("C", 60, 90, "A-CR-01"))
    assert "CAP-RESOURCE" not in [c.rule_id for c in detect_conflicts(snap, touching, with_a)]
    overlap = _replace(base, _asg("A", 31, 61, "A-CR-01"))
    cap = [c for c in detect_conflicts(snap, overlap, with_a) if c.rule_id == "CAP-RESOURCE"]
    assert [(c.task_ids, c.resource_id, c.interval) for c in cap] == [
        (("A", "C"), "A-CR-01", (31, 90))
    ]


# ── T08 SEPARATION ─────────────────────────────────────────────


@pytest.mark.parametrize(("e_start", "conflict"), [(44, True), (45, False)])
def test_t08_adjacent_gap_14_15(seeded, e_start, conflict):
    snap = take_snapshot(seeded)
    asg = _replace(snap.facts().check_assignments(), _asg("E", e_start, e_start + 30))
    found = [c for c in detect_conflicts(snap, asg, seeded) if c.rule_id == "SEP-HOT-FLAM"]
    assert bool(found) is conflict
    if conflict:
        assert (found[0].task_ids, found[0].zone_ids) == (("D", "E"), ("D", "D2"))


@pytest.mark.parametrize(("e_start", "conflict"), [(44, True), (45, False)])
def test_t08_same_zone_gap(seeded, e_start, conflict):
    snap = _retask(take_snapshot(seeded), "E", zone_id="D")
    asg = _replace(snap.facts().check_assignments(), _asg("E", e_start, e_start + 30))
    rules = [c.rule_id for c in detect_conflicts(snap, asg, seeded)]
    assert ("SEP-HOT-FLAM" in rules) is conflict


def test_t08_unrelated_zone_no_separation(seeded):
    snap = _retask(take_snapshot(seeded), "E", zone_id="C")
    asg = _replace(snap.facts().check_assignments(), _asg("E", 45, 75))
    assert "SEP-HOT-FLAM" not in [c.rule_id for c in detect_conflicts(snap, asg, seeded)]
    asg = _replace(asg, _asg("E", 30, 60))  # 붙어 있어도 관계가 없으면 무관
    assert "SEP-HOT-FLAM" not in [c.rule_id for c in detect_conflicts(snap, asg, seeded)]


# ── T30 BELOW 방향 (Rule Engine) ───────────────────────────────


def _lift_c_over_b(pack, relation):
    """C(LIFTING, zone C)를 B(WORK_BELOW, zone B, 0–60)와 겹치게 둔다."""
    snap = with_facts(take_snapshot(pack), zone_relations=(relation,))
    asg = _replace(snap.facts().check_assignments(), _asg("C", 30, 60, "A-CR-01"))
    return [c for c in detect_conflicts(snap, asg, pack) if c.rule_id == "SEP-LIFT-BELOW"]


def test_t30_below_forward_applies(seeded):
    found = _lift_c_over_b(seeded, ZoneRelation(zone_a="C", zone_b="B", relation="BELOW"))
    assert [(c.task_ids, c.zone_ids) for c in found] == [(("B", "C"), ("B", "C"))]


def test_t30_below_reverse_does_not_apply(seeded):
    assert _lift_c_over_b(seeded, ZoneRelation(zone_a="B", zone_b="C", relation="BELOW")) == []


# ── 기본 제약 ──────────────────────────────────────────────────


def test_basic_duration_window_horizon(with_a):
    snap = take_snapshot(with_a)
    base = snap.facts().check_assignments()
    assert ("DURATION", ("A",)) in _ids(
        detect_conflicts(snap, _replace(base, _asg("A", 60, 75, "A-CR-01")), with_a)
    )
    # latest_start 60 초과
    assert ("WINDOW", ("A",)) in _ids(
        detect_conflicts(snap, _replace(base, _asg("A", 61, 91, "A-CR-01")), with_a)
    )
    # Horizon 3360 초과 (시간창은 안). CALENDAR도 따로 보고한다
    wide = _retask(snap, "E", latest_start=3350, latest_end=3400)
    asg = _replace(wide.facts().check_assignments(), _asg("E", 3340, 3370))
    found = _ids(detect_conflicts(wide, asg, with_a))
    assert ("WINDOW", ("E",)) in found and ("CALENDAR", ("E",)) in found
    asg = _replace(wide.facts().check_assignments(), _asg("E", 3330, 3360))
    found = _ids(detect_conflicts(wide, asg, with_a))
    assert ("WINDOW", ("E",)) not in found and ("CALENDAR", ("E",)) not in found


@pytest.mark.parametrize(
    ("start", "violates"),
    [
        (450, False),  # 16:30–17:00: 종료 = 근무 종료는 안 ([start, end))
        (451, True),  # 17:01 종료
        (480, True),  # 17:00–17:30 야간
        (1430, True),  # 다음 날 08:50–09:20
        (1440, False),  # 다음 날 09:00
        (470, True),  # 밤을 넘김 (1440 안으로 들어가지 않음)
    ],
)
def test_calendar_one_work_interval(with_a, start, violates):
    """작업 [start, end)는 근무 구간 하나 안 (기본 제약 CALENDAR)."""
    snap = _retask(take_snapshot(with_a), "E", latest_start=2000, latest_end=2100)
    asg = _replace(snap.facts().check_assignments(), _asg("E", start, start + 30))
    found = _ids(detect_conflicts(snap, asg, with_a))
    assert (("CALENDAR", ("E",)) in found) == violates
    assert ("WINDOW", ("E",)) not in found


def test_basic_resource_missing_type_auth_availability(with_a):
    snap = take_snapshot(with_a)
    base = snap.facts().check_assignments()

    def rules_for(snapshot, *new):
        return {
            (c.rule_id, c.resource_id)
            for c in detect_conflicts(snapshot, _replace(base, *new), with_a)
        }

    assert ("RESOURCE_MISSING", None) in rules_for(snap, _asg("A", 60, 90))
    # B-CR-01: 유형은 맞지만 UA는 사용 불가
    found = rules_for(snap, _asg("A", 60, 90, "B-CR-01"))
    assert ("RESOURCE_AUTH", "B-CR-01") in found
    assert ("RESOURCE_TYPE", "B-CR-01") not in found
    trucks = _retask(snap, "A", required_resource_type="TRUCK")
    assert ("RESOURCE_TYPE", "SITE-CR-01") in rules_for(trucks, _asg("A", 60, 90, "SITE-CR-01"))
    short = with_facts(
        snap,
        resources=tuple(
            r.model_copy(update={"available_intervals": ((0, 60),)})
            if r.resource_id == "SITE-CR-01"
            else r
            for r in snap.facts().resources
        ),
    )
    assert ("AVAILABILITY", "SITE-CR-01") in rules_for(short, _asg("A", 60, 90, "SITE-CR-01"))
    assert ("AVAILABILITY", "SITE-CR-01") not in rules_for(short, _asg("A", 30, 60, "SITE-CR-01"))


def test_basic_precedence(seeded):
    snap = _retask(take_snapshot(seeded), "E", predecessors=(Predecessor(task_id="D", min_lag=20),))
    base = snap.facts().check_assignments()
    assert ("PRECEDENCE", ("D", "E")) in _ids(
        detect_conflicts(snap, _replace(base, _asg("E", 49, 79)), seeded)
    )
    assert ("PRECEDENCE", ("D", "E")) not in _ids(
        detect_conflicts(snap, _replace(base, _asg("E", 50, 80)), seeded)
    )


def test_basic_resource_zone_and_requirement(with_a):
    """구역·요구 조건은 적격성 함수 결과로 기본 제약이 된다 (CV-20)."""
    snap = take_snapshot(with_a)
    base = snap.facts().check_assignments()
    new = {"RESOURCE_ZONE", "RESOURCE_REQUIREMENT"}

    def rules_for(snapshot, *assignments):
        found = detect_conflicts(snapshot, _replace(base, *assignments), with_a)
        return {(c.rule_id, c.resource_id) for c in found if c.rule_id in new}

    # A는 B구역 인양(작업 유형 기본값 최대 하중 ≥ 20): 두 크레인 모두 맞는다
    assert rules_for(snap) == set()
    assert rules_for(snap, _asg("A", 60, 90, "SITE-CR-01")) == set()
    # 구역: A-CR-01은 B·C, SITE-CR-01은 B·C·D에서만 쓸 수 있다
    in_d = _retask(snap, "A", zone_id="D")
    assert rules_for(in_d) == {("RESOURCE_ZONE", "A-CR-01")}
    assert rules_for(in_d, _asg("A", 60, 90, "SITE-CR-01")) == set()
    everywhere = with_facts(
        in_d,
        resources=tuple(
            r.model_copy(update={"allowed_zone_ids": ("*",)}) for r in in_d.facts().resources
        ),
    )
    assert rules_for(everywhere) == set()
    # 요구 조건: 작업 값이 더해진다. A-CR-01은 25 t, SITE-CR-01은 50 t
    heavy = _retask(
        snap, "A", resource_requirements=(Requirement(attribute="max_load", op="GTE", value=40),)
    )
    assert rules_for(heavy) == {("RESOURCE_REQUIREMENT", "A-CR-01")}
    assert rules_for(heavy, _asg("A", 60, 90, "SITE-CR-01")) == set()
    # 작업 유형 기본값만으로도 걸린다. 조건이 여럿이어도 충돌은 작업·자원당 하나다
    strict = _retask(
        snap,
        "A",
        default_requirements=(
            Requirement(attribute="max_load", op="GTE", value=30),
            Requirement(attribute="usage", op="CONTAINS", value="블록"),
        ),
    )
    found = detect_conflicts(strict, base, with_a)
    assert [(c.rule_id, c.task_ids) for c in found if c.rule_id in new] == [
        ("RESOURCE_REQUIREMENT", ("A",))
    ]
