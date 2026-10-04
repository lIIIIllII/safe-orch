"""일정 문서와 꺼내기 (ST-23).

문서는 plan_r0 뼈대(tasks + assignments)에 머리말을 더한 것이고 시각은 현장 날짜·시각 문자열이다.
꺼내기는 지금 확정 계획을 문서로 만들어 불변 기록에 남긴다.
"""

import copy
import sqlite3
import uuid

import pytest
from conftest import LEGACY_PINNED, add_task, make_task
from fastapi.testclient import TestClient

from app.commands.schedule import export_schedule
from app.domain.schedule import ScheduleError, content_hash, from_document, to_document
from app.main import app
from app.store import db
from app.store.repos.schedules import get_schedule

SITE = "YARD-01"


def _key():
    return uuid.uuid4().hex


def _export(pack, actor="supervisor", key=None):
    outcome = export_schedule(pack, actor, key or _key())
    assert outcome.status in ("APPLIED", "REPLAYED"), outcome
    with db.read() as conn:
        return outcome, get_schedule(conn, pack.site_id, outcome.result_refs["schedule_id"])


def _read(pack, document):
    return from_document(document, pack.horizon_start_utc, pack.timezone, pack.horizon_minutes)


def _reasons(pack, document):
    with pytest.raises(ScheduleError) as e:
        _read(pack, document)
    return e.value.reasons


# ── 꺼내기 ─────────────────────────────────────────────────────


def test_export_writes_current_plan_as_document(seeded):
    pack = seeded
    outcome, stored = _export(pack)
    doc = stored["document"]

    assert outcome.status == "APPLIED"
    assert (stored["kind"], stored["plan_revision"], stored["actor_id"]) == (
        "EXPORT",
        0,
        "supervisor",
    )
    assert outcome.result_refs == {
        "schedule_id": doc["schedule_id"],
        "content_hash": stored["content_hash"],
        "plan_revision": 0,
        "task_count": 9,
    }
    assert (doc["site_id"], doc["pack_hash"], doc["plan_revision"], doc["exported_by"]) == (
        SITE,
        pack.pack_hash,
        0,
        "supervisor",
    )
    assert [t["task_id"] for t in doc["tasks"]] == sorted(a.task_id for a in pack.plan_r0)
    # 시각은 현장 날짜·시각 문자열이다 (AG-21)
    placed = {a["task_id"]: a for a in doc["assignments"]}
    assert placed["C"] == {
        "task_id": "C",
        "start": "2026-10-12(월) 10:00",
        "end": "2026-10-12(월) 10:30",
        "resource_id": "A-CR-01",
    }
    assert placed["K"]["start"] == "2026-10-13(화) 09:00"
    # 고정은 표시용으로 담는다. 위험 태그와 시간창은 담지 않는다
    assert sorted(t["task_id"] for t in doc["tasks"] if t["pinned"]) == sorted(LEGACY_PINNED)
    assert not {"hazard_tags", "earliest_start", "latest_start", "latest_end"} & set(
        doc["tasks"][0]
    )
    # 배치를 바꾸지 않는다
    assert (outcome.context_version, outcome.plan_revision) == (0, 0)


def test_export_leaves_out_requests_outside_the_plan_and_has_no_hope_column(seeded):
    pack = seeded
    add_task(pack, make_task(pack))  # 계획 밖 요청 A
    _, stored = _export(pack)
    tasks = {t["task_id"]: t for t in stored["document"]["tasks"]}

    assert pack.new_task.task_id not in tasks
    assert all("preferred_window" not in t for t in tasks.values())  # 희망 영역 칸은 없다


def test_same_key_replays_and_same_plan_gives_same_hash(seeded):
    pack = seeded
    key = _key()
    first, stored = _export(pack, key=key)
    again, _ = _export(pack, key=key)
    other, other_stored = _export(pack, actor="planner_b")

    assert again.status == "REPLAYED"
    assert again.result_refs == first.result_refs
    # 다시 꺼내면 기록은 새로 남지만 본문 hash는 같다
    assert other.result_refs["schedule_id"] != first.result_refs["schedule_id"]
    assert other_stored["content_hash"] == stored["content_hash"]
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM schedule").fetchone()[0] == 2


@pytest.mark.parametrize("sql", ["UPDATE schedule SET kind = 'IMPORT'", "DELETE FROM schedule"])
def test_schedule_is_immutable(seeded, sql):
    _export(seeded)
    with pytest.raises(sqlite3.IntegrityError, match="immutable: schedule"), db.write() as tx:
        tx.execute(sql)


def test_api_export_then_get_document(seeded):
    with TestClient(app) as client:
        headers = {"X-Actor": "planner_a", "Idempotency-Key": _key()}
        res = client.post(f"/api/sites/{SITE}/schedules/export", headers=headers)
        assert res.status_code == 200, res.text
        refs = res.json()["result_refs"]
        got = client.get(f"/api/schedules/{refs['schedule_id']}", headers={"X-Actor": "planner_a"})
        assert got.status_code == 200
        assert content_hash(got.json()) == refs["content_hash"]
        missing = client.get("/api/schedules/sch_none", headers={"X-Actor": "planner_a"})
        assert missing.status_code == 404


# ── 문서 읽기·쓰기 ─────────────────────────────────────────────


def test_document_round_trip(seeded):
    pack = seeded
    _, stored = _export(pack)
    doc = stored["document"]
    schedule = _read(pack, doc)

    assert {a.task_id: (a.start, a.end) for a in schedule.assignments}["K"] == (1440, 1560)
    assert to_document(schedule, pack.horizon_start_utc, pack.timezone) == doc


def test_read_fills_duration_and_drops_hazard_tags(seeded):
    pack = seeded
    doc = copy.deepcopy(_export(pack)[1]["document"])
    for t in doc["tasks"]:
        del t["duration"]
        t["hazard_tags"] = ["HOT_WORK"]
    # 요일을 빼고 써도 읽는다
    for a in doc["assignments"]:
        a["start"] = a["start"].replace("(월)", "").replace("(화)", "").replace("(수)", "")
    schedule = _read(pack, doc)

    assert {t.task_id: t.duration for t in schedule.tasks}["W"] == 240


def test_read_rejects_what_it_cannot_fill(seeded):
    pack = seeded
    base = _export(pack)[1]["document"]

    missing = copy.deepcopy(base)
    del missing["tasks"][0]["zone_id"]
    assert any("tasks[0].zone_id" in r for r in _reasons(pack, missing))

    outside = copy.deepcopy(base)
    outside["assignments"][0]["start"] = "2026-10-20 09:00"
    assert any("outside the horizon" in r for r in _reasons(pack, outside))

    mismatch = copy.deepcopy(base)
    mismatch["tasks"][0]["duration"] += 5
    assert any("!= duration" in r for r in _reasons(pack, mismatch))

    unplaced = copy.deepcopy(base)
    gone = unplaced["assignments"].pop(0)["task_id"]
    assert f"schedule.tasks[0]: no assignment for {gone!r}" in _reasons(pack, unplaced)

    stray = copy.deepcopy(base)
    stray["assignments"].append({**stray["assignments"][0], "task_id": "ZZ"})
    assert any("undefined task 'ZZ'" in r for r in _reasons(pack, stray))

    window = copy.deepcopy(base)
    window["tasks"][0]["earliest_start"] = "2026-10-12 09:00"  # 시간창은 문서의 칸이 아니다
    assert any("earliest_start" in r for r in _reasons(pack, window))

    assert _reasons(pack, []) == ["schedule: must be an object"]
