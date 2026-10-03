"""seed와 조회. 조회값이 Pack 값·fields 모양과 같은지 확인한다."""

import json

import pytest

from app.store import db
from app.store.repos.plans import get_current_plan
from app.store.repos.resources import list_resources
from app.store.repos.seed import SeedError, seed_pack
from app.store.repos.site import get_site, list_zone_relations
from app.store.repos.tasks import list_current_tasks

SRC = "fixture:plan_r0"


def _confirmed(value):
    return {"value": value, "status": "CONFIRMED", "source_ref": SRC}


def test_site(seeded):
    with db.read() as conn:
        site = get_site(conn, "YARD-01")
    assert site.model_dump() == {
        "site_id": "YARD-01",
        "pack_hash": seeded.pack_hash,
        "horizon_start_utc": "2026-10-12T00:00:00Z",
        "horizon_minutes": 3360,
        "context_version": 0,
        "plan_revision": 0,
    }


def test_units_actors_zones(seeded):
    with db.read() as conn:
        units = conn.execute(
            "SELECT unit_id, name, unit_type FROM work_unit ORDER BY unit_id"
        ).fetchall()
        actors = conn.execute(
            "SELECT actor_id, name, unit_id, roles FROM actor ORDER BY actor_id"
        ).fetchall()
        zones = [r[0] for r in conn.execute("SELECT zone_id FROM zone ORDER BY zone_id")]
    assert units == [
        ("SITE", "현장 운영", "SITE_OFFICE"),
        ("UA", "협력사 A", "SUBCONTRACTOR"),
        ("UB", "협력사 B", "SUBCONTRACTOR"),
    ]
    assert [(a, n, u, json.loads(r)) for a, n, u, r in actors] == [
        ("foreman_a2", "Foreman A2", "UA", []),
        ("planner_a", "Planner A", "UA", ["UNIT_PLANNER"]),
        ("planner_b", "Planner B", "UB", ["UNIT_PLANNER"]),
        ("reporter", "Reporter", "SITE", ["REPORTER"]),
        ("supervisor", "Supervisor", "SITE", ["SUPERVISOR"]),
    ]
    assert zones == ["B", "C", "D", "D2", "F", "G", "G2", "H"]


def test_zone_relations_adjacent_stored_both_ways(seeded):
    with db.read() as conn:
        rels = list_zone_relations(conn, "YARD-01")
    assert [(r.zone_a, r.zone_b, r.relation) for r in rels] == [
        ("D", "D2", "ADJACENT"),
        ("D2", "D", "ADJACENT"),
        ("G", "G2", "ADJACENT"),
        ("G2", "G", "ADJACENT"),
    ]


def test_resources(seeded):
    with db.read() as conn:
        res = list_resources(conn, "YARD-01")
    assert [r.model_dump() for r in res] == [
        {
            "resource_id": "A-CR-01",
            "resource_type": "CRANE",
            "owner_unit_id": "UA",
            "allowed_unit_ids": ("UA",),
            "capacity": 1,
            "available_intervals": ((0, 3360),),
        },
        {
            "resource_id": "B-CR-01",
            "resource_type": "CRANE",
            "owner_unit_id": "UB",
            "allowed_unit_ids": ("UB",),
            "capacity": 1,
            "available_intervals": ((0, 3360),),
        },
        {
            "resource_id": "SITE-CR-01",
            "resource_type": "CRANE",
            "owner_unit_id": "SITE",
            "allowed_unit_ids": ("UA",),
            "capacity": 1,
            "available_intervals": ((0, 3360),),
        },
        {
            "resource_id": "SITE-GC-01",
            "resource_type": "GANTRY",
            "owner_unit_id": "SITE",
            "allowed_unit_ids": ("UA", "UB"),
            "capacity": 1,
            "available_intervals": ((0, 3360),),
        },
    ]


# fmt: off
# (unit, 담당, work_type, hazard_tags, zone, duration, es, ls, le, 자원 유형, 자원, movable)
EXPECTED_TASKS = {
    "B": ("UB", "planner_b", "WORK_BELOW", ("WORK_BELOW",), "B", 60, 0, 0, 60, None, None,
          (False, False)),
    "C": ("UA", "foreman_a2", "LIFTING", ("LIFTING",), "C", 30, 60, 90, 120, "CRANE", "A-CR-01",
          (True, False)),
    "D": ("UB", "planner_b", "HOT_WORK", ("HOT_WORK",), "D", 30, 0, 0, 30, None, None,
          (False, False)),
    "E": ("UB", "planner_b", "PAINTING", ("FLAMMABLE",), "D2", 30, 45, 120, 150, None, None,
          (True, False)),
    # 시연 확장: 모두 고정
    "K": ("UB", "planner_b", "LIFTING", ("LIFTING",), "F", 120, 1440, 1440, 1560, "GANTRY",
          "SITE-GC-01", (False, False)),
    "M": ("UA", "foreman_a2", "WORK_BELOW", ("WORK_BELOW",), "H", 90, 2910, 2910, 3000, None,
          None, (False, False)),
    "P": ("UB", "planner_b", "PAINTING", ("FLAMMABLE",), "G2", 60, 1500, 1500, 1560, None, None,
          (False, False)),
    "Q": ("UA", "foreman_a2", "LIFTING", ("LIFTING",), "F", 60, 2910, 2910, 2970, "GANTRY",
          "SITE-GC-01", (False, False)),
    "W": ("UB", "planner_b", "PAINTING", ("FLAMMABLE",), "G2", 240, 1680, 1680, 1920, None, None,
          (False, False)),
}
# fmt: on


def test_tasks_match_a5_table(seeded):
    with db.read() as conn:
        tasks = list_current_tasks(conn, "YARD-01", seeded)
    assert [t.task_id for t in tasks] == ["B", "C", "D", "E", "K", "M", "P", "Q", "W"]
    for t in tasks:
        assert (
            t.unit_id,
            t.owner_actor_id,
            t.work_type,
            t.hazard_tags,
            t.zone_id,
            t.duration,
            t.earliest_start,
            t.latest_start,
            t.latest_end,
            t.required_resource_type,
            t.requested_resource_id,
            (t.movable.time, t.movable.resource),
        ) == EXPECTED_TASKS[t.task_id]
        assert t.revision == 1
        assert t.lifecycle == "READY"
        assert t.predecessors == ()


def test_task_fields_match_a8(seeded):
    with db.read() as conn:
        tasks = {t.task_id: t for t in list_current_tasks(conn, "YARD-01", seeded)}
    dumped = {k: {f: v.model_dump() for f, v in t.fields.items()} for k, t in tasks.items()}
    assert dumped["C"] == {
        "zone_id": _confirmed("C"),
        "duration": _confirmed(30),
        "window": _confirmed({"earliest_start": 60, "latest_start": 90, "latest_end": 120}),
        "resource": _confirmed(
            {"required_resource_type": "CRANE", "requested_resource_id": "A-CR-01"}
        ),
    }
    assert dumped["E"] == {
        "zone_id": _confirmed("D2"),
        "duration": _confirmed(30),
        "window": _confirmed({"earliest_start": 45, "latest_start": 120, "latest_end": 150}),
    }
    for task_id in ("B", "D"):
        assert set(dumped[task_id]) == {"zone_id", "duration", "window"}


def test_current_task_revision_is_max(seeded):
    with db.write() as tx:
        tx.execute(
            "INSERT INTO task SELECT site_id, task_id, 2, unit_id, owner_actor_id, work_type,"
            " zone_id, duration, 60, latest_start, latest_end, required_resource_type,"
            " requested_resource_id, predecessors, movable, fields, lifecycle"
            " FROM task WHERE task_id = 'E'"
        )
    with db.read() as conn:
        e = next(t for t in list_current_tasks(conn, "YARD-01", seeded) if t.task_id == "E")
    assert (e.revision, e.earliest_start) == (2, 60)


def test_plan_r0(seeded):
    with db.read() as conn:
        plan = get_current_plan(conn, "YARD-01")
    assert plan.plan_revision == 0
    assert plan.candidate_id is None
    assert plan.committed_context_version == 0
    assert [(a.task_id, a.start, a.end, a.resource_id) for a in plan.assignments] == [
        ("B", 0, 60, None),
        ("C", 60, 90, "A-CR-01"),
        ("D", 0, 30, None),
        ("E", 45, 75, None),
        ("K", 1440, 1560, "SITE-GC-01"),
        ("P", 1500, 1560, None),
        ("W", 1680, 1920, None),
        ("Q", 2910, 2970, "SITE-GC-01"),
        ("M", 2910, 3000, None),
    ]


def test_seed_audit_one_row(seeded):
    with db.read() as conn:
        audit = conn.execute("SELECT command, actor_id, payload FROM audit").fetchall()
    assert len(audit) == 1
    command, actor_id, payload = audit[0]
    assert (command, actor_id) == ("SEED", None)
    assert json.loads(payload) == {"pack": "shipyard", "pack_hash": seeded.pack_hash}


def test_scenario_task_a_not_seeded(seeded):
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM task WHERE task_id = 'A'").fetchone()[0] == 0
    a = seeded.new_task
    assert (a.unit_id, a.owner_actor_id, a.work_type, a.zone_id, a.duration) == (
        "UA",
        "planner_a",
        "LIFTING",
        "B",
        30,
    )
    assert (a.earliest_start, a.latest_start, a.latest_end) == (0, 60, 90)
    assert (a.required_resource_type, a.requested_resource_id) == ("CRANE", "A-CR-01")
    assert (a.requested.start, a.requested.end) == (0, 30)
    assert (a.movable.time, a.movable.resource) == (True, False)


def test_seed_twice_raises(seeded):
    with pytest.raises(SeedError), db.write() as tx:
        seed_pack(tx, seeded)
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 1
