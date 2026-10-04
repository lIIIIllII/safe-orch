"""일정 넣기 (ST-24).

넣는 사람은 자기 Unit의 작업만 넣는다. 문서의 시각은 Soft다(희망 영역이 되고 시간창은 Horizon 전체).
새 작업의 기준 배정은 문서의 배정이다. 넣을 수 없는 작업이 하나라도 있으면 전체를 거절하고, 사람이
빼기로 고른 작업만 뺀다. 넣기 하나는 사건 하나다. Pack 그대로의 R0(고정 없음)에서 본다.
"""

import copy
import uuid

from conftest import add_run, take_snapshot
from fastapi.testclient import TestClient
from scripted import Router

from app.commands.schedule import (
    ImportRequest,
    export_schedule,
    import_schedule,
    judge,
    preview_import,
)
from app.commands.task_request import (
    TaskRequestForm,
    TaskWithdraw,
    submit_task_request,
    withdraw_task_request,
)
from app.coordinator.dispatcher import run_until_idle
from app.domain.calendar import site_time
from app.main import app
from app.store import db
from app.store.repos.case_events import list_case_events
from app.store.repos.cases import end_case_run, queued_task_ids
from app.store.repos.dispatch import list_jobs
from app.store.repos.pins import preferred_windows
from app.store.repos.plans import get_current_plan
from app.store.repos.schedules import get_schedule, list_task_bases
from app.store.repos.site import get_actor, get_site
from app.store.repos.tasks import list_current_tasks

SITE = "YARD-01"
OTHERS = ("C", "M", "Q")  # UA의 작업. planner_b(UB)는 넣을 수 없다


def _key():
    return uuid.uuid4().hex


def _exported(pack, actor="planner_b"):
    out = export_schedule(pack, actor, _key())
    with db.read() as conn:
        return copy.deepcopy(get_schedule(conn, pack.site_id, out.result_refs["schedule_id"]))[
            "document"
        ]


def _at(pack, minute):
    return site_time(pack.horizon_start_utc, pack.timezone, minute)


def _add(pack, doc, task_id, start, end, **values):
    """문서에 새 작업(UB의 화기 작업, 자원 없음)과 배정을 더한다."""
    task = {
        "task_id": task_id,
        "unit_id": "UB",
        "owner_actor_id": "planner_b",
        "work_type": "HOT_WORK",
        "zone_id": "G",
        "duration": end - start,
        **values,
    }
    doc["tasks"].append(task)
    doc["assignments"].append(
        {"task_id": task_id, "start": _at(pack, start), "end": _at(pack, end), "resource_id": None}
    )
    return doc


def _import(pack, doc, exclude=OTHERS, actor="planner_b", key=None):
    body = ImportRequest(document=doc, exclude=tuple(exclude))
    return import_schedule(pack, actor, key or _key(), body)


def _preview(pack, doc, exclude=OTHERS, actor="planner_b"):
    with db.read() as conn:
        view = preview_import(conn, pack, get_actor(conn, pack.site_id, actor), doc, tuple(exclude))
    return view, {t["task_id"]: t for t in view["tasks"]}


def _task(pack, task_id):
    with db.read() as conn:
        return next(t for t in list_current_tasks(conn, pack.site_id, pack) if t.task_id == task_id)


def _site(pack):
    with db.read() as conn:
        return get_site(conn, pack.site_id)


def _events(pack, kind="SCHEDULE_IMPORTED"):
    with db.read() as conn:
        return [e for e in list_case_events(conn, pack.site_id) if e["kind"] == kind]


def _imports():
    with db.read() as conn:
        return conn.execute("SELECT COUNT(*) FROM schedule WHERE kind = 'IMPORT'").fetchone()[0]


def _move(pack, doc, task_id, start, end):
    a = next(a for a in doc["assignments"] if a["task_id"] == task_id)
    a["start"], a["end"] = _at(pack, start), _at(pack, end)


# ── 꺼낸 문서를 그대로 넣기 ────────────────────────────────────


def test_exported_document_changes_nothing(seeded_real):
    pack = seeded_real
    doc = _exported(pack)
    before = _site(pack)
    view, _ = _preview(pack, doc)
    assert view["acceptable"] and not view["pack_mismatch"]
    assert {t["verdict"] for t in view["tasks"] if not t["excluded"]} == {"UNCHANGED"}

    out = _import(pack, doc)
    assert out.status == "APPLIED"
    assert (out.result_refs["new_task_ids"], out.result_refs["changed_task_ids"]) == ([], [])
    assert (out.result_refs["unchanged"], out.result_refs["excluded"]) == (6, sorted(OTHERS))
    # 사건도 현장 버전 변화도 없다. 기록만 남는다
    after = _site(pack)
    assert (after.context_version, after.plan_revision) == (
        before.context_version,
        before.plan_revision,
    )
    assert _events(pack) == [] and _imports() == 1
    with db.read() as conn:
        stored = get_schedule(conn, pack.site_id, out.result_refs["schedule_id"])
        assert list_task_bases(conn, pack.site_id) == []
    assert (stored["kind"], stored["document"]) == ("IMPORT", doc)


def test_other_units_tasks_reject_the_whole_document_until_excluded(seeded_real):
    pack = seeded_real
    doc = _add(pack, _exported(pack), "S1", 240, 300)
    view, tasks = _preview(pack, doc, exclude=())
    assert not view["acceptable"]
    assert {tid: tasks[tid]["reasons"] for tid in OTHERS} == {
        "C": ["OTHER_UNIT_TASK"],
        "M": ["OTHER_UNIT_TASK"],
        "Q": ["OTHER_UNIT_TASK"],
    }
    assert tasks["S1"]["verdict"] == "NEW"

    out = _import(pack, doc, exclude=())
    assert out.reason_codes == ("TASKS_NOT_IMPORTABLE",)
    assert sorted(t["task_id"] for t in out.result_refs["tasks"]) == sorted(OTHERS)
    # 거절이면 아무것도 남지 않는다
    with db.read() as conn:
        assert "S1" not in {t.task_id for t in list_current_tasks(conn, pack.site_id, pack)}
    assert _imports() == 0
    # 사람이 그 작업들을 빼면 나머지가 들어간다
    assert _import(pack, doc).status == "APPLIED"
    assert _task(pack, "S1").lifecycle == "READY"


def test_task_outside_the_horizon_and_same_id_of_another_owner(seeded_real):
    pack = seeded_real
    doc = _add(pack, _exported(pack), "S1", 240, 300)
    doc = _add(pack, doc, "S2", 300, 360)
    next(a for a in doc["assignments"] if a["task_id"] == "S2")["start"] = "2026-10-20 09:00"
    view, tasks = _preview(pack, doc)
    assert not view["acceptable"]
    assert (tasks["S2"]["verdict"], tasks["S2"]["reasons"]) == ("REJECTED", ["OUTSIDE_HORIZON"])
    assert _import(pack, doc).reason_codes == ("TASKS_NOT_IMPORTABLE",)
    assert _import(pack, doc, exclude=(*OTHERS, "S2")).status == "APPLIED"

    # 같은 Unit이어도 담당자가 아니면 그 작업은 넣을 수 없다 (C의 담당자는 foreman_a2)
    mine = _exported(pack, "planner_a")
    _, seen = _preview(pack, mine, exclude=(), actor="planner_a")
    assert seen["C"]["reasons"] == ["NOT_OWNER"] and seen["B"]["reasons"] == ["OTHER_UNIT_TASK"]
    # 넣는 권한은 폼과 같다
    assert _import(pack, mine, actor="supervisor").reason_codes == ("NOT_AUTHORIZED",)


def test_document_level_rejections(seeded_real):
    pack = seeded_real
    doc = _exported(pack)
    other_site = {**doc, "site_id": "YARD-99"}
    assert _import(pack, other_site).reason_codes == ("SITE_MISMATCH",)
    broken = {k: v for k, v in doc.items() if k != "assignments"}
    out = _import(pack, broken)
    assert out.reason_codes == ("SCHEDULE_MALFORMED",) and out.result_refs["details"]
    # Pack이 다르면 거절하지 않고 표시만 한다. 값은 지금 Pack으로 검증한다
    old_pack = {**doc, "pack_hash": "other"}
    view, _ = _preview(pack, old_pack)
    assert view["pack_mismatch"] and view["acceptable"]
    assert _import(pack, old_pack).status == "APPLIED"


# ── 새 작업 ────────────────────────────────────────────────────


def test_new_task_time_is_soft_and_base_is_the_documents_assignment(seeded_real, main_on):
    pack = seeded_real
    doc = _add(pack, _exported(pack), "S1", 240, 300)  # 10/12 13:00–14:00
    # S2는 배정(14:00–15:00)과 다른 희망 영역(15:00–17:00)을 문서에 적었다
    doc = _add(
        pack,
        doc,
        "S2",
        300,
        360,
        zone_id="H",
        preferred_window={"start": _at(pack, 360), "end": _at(pack, 480), "origin": "DECIDED"},
        origins={"duration": "DECIDED"},
        pinned=True,
    )
    before = _site(pack)
    out = _import(pack, doc)
    assert out.status == "APPLIED" and out.result_refs["new_task_ids"] == ["S1", "S2"]
    horizon = pack.horizon_minutes
    s1, s2 = _task(pack, "S1"), _task(pack, "S2")
    # 담당자는 넣는 사람이고, 시간창은 Horizon 전체다(문서의 시각은 Hard가 아니다)
    assert (s1.unit_id, s1.owner_actor_id, s1.lifecycle, s1.revision) == (
        "UB",
        "planner_b",
        "READY",
        1,
    )
    assert (s1.earliest_start, s1.latest_start, s1.latest_end) == (0, horizon - 60, horizon)
    # 넣은 값은 담당자가 말한 값이다(문서의 출처는 쓰지 않는다)
    assert s2.decided_values == () and s1.fields["zone_id"].source_ref.startswith("schedule:")
    with db.read() as conn:
        hopes = preferred_windows(conn, pack.site_id)
        bases = {b.task_id: b for b in list_task_bases(conn, pack.site_id)}
        pins = conn.execute("SELECT COUNT(*) FROM task_pin").fetchone()[0]
        consents = conn.execute("SELECT axis FROM consent WHERE task_id = 'S1'").fetchall()
    # 희망 영역: 문서에 있으면 그것, 없으면 배정 구간. 말한 희망이고 만든 주체는 담당자다
    assert (hopes["S1"]["start"], hopes["S1"]["end"]) == (240, 300)
    assert (hopes["S2"]["start"], hopes["S2"]["end"]) == (360, 480)
    assert {(h["origin"], h["made_by"]) for h in (hopes["S1"], hopes["S2"])} == {
        ("STATED", "OWNER")
    }
    assert consents == []  # 시각의 동의 범위는 희망 영역이다(시작 범위 동의를 따로 만들지 않는다)
    assert pins == 0  # 문서의 고정은 살리지 않는다
    # 기준 배정은 문서의 배정이다. 희망 영역이 따로 있어도 그렇다
    assert (bases["S1"].start, bases["S2"].start) == (240, 300)
    base = take_snapshot(pack).facts().base_assignments()
    assert (base["S1"].start, base["S2"].start, base["S2"].end) == (240, 300, 360)

    # 넣기 하나는 사건 하나, 현장 버전 한 번, 재확인 한 번이다
    assert _site(pack).context_version == before.context_version + 1
    [event] = _events(pack)
    assert (event["ref"]["schedule_id"], event["ref"]["task_ids"]) == (
        out.result_refs["schedule_id"],
        ["S1", "S2"],
    )
    with db.read() as conn:
        causes = [
            j["payload"]["cause"] for j in list_jobs(conn, pack.site_id) if j["kind"] == "RECHECK"
        ]
    assert [c for c in causes if c.get("kind") == "SCHEDULE"] == [
        {"kind": "SCHEDULE", "schedule_id": out.result_refs["schedule_id"]}
    ]
    # 충돌이 없으면 지금처럼 재확인 후보가 된다: 문서의 배정 그대로 계획에 올린다
    run_until_idle(pack, model_factory=Router().factory())
    with db.read() as conn:
        [assignments] = conn.execute(
            "SELECT assignments FROM candidate WHERE kind = 'RECONFIRM'"
        ).fetchone()
        [main_case] = conn.execute("SELECT case_id FROM agent_run WHERE agent_type = 'MAIN'")
    assert '"start": 240, "task_id": "S1"' in assignments
    assert '"start": 300, "task_id": "S2"' in assignments
    assert event["case_id"] == main_case[0]


def test_predecessor_may_point_to_a_task_in_the_same_document(seeded_real):
    pack = seeded_real
    doc = _add(pack, _exported(pack), "S1", 240, 300)
    doc = _add(pack, doc, "S2", 300, 360, predecessors=[{"task_id": "S1", "min_lag": 0}])
    doc = _add(pack, doc, "S3", 360, 420, predecessors=[{"task_id": "NOPE", "min_lag": 0}])
    _, tasks = _preview(pack, doc)
    assert (tasks["S2"]["verdict"], tasks["S3"]["reasons"]) == ("NEW", ["PREDECESSOR_NOT_FOUND"])
    # 뺀 작업을 선행으로 가진 작업은 넣을 수 없다
    _, tasks = _preview(pack, doc, exclude=(*OTHERS, "S1", "S3"))
    assert tasks["S2"]["reasons"] == ["PREDECESSOR_NOT_FOUND"]
    assert _import(pack, doc, exclude=(*OTHERS, "S3")).status == "APPLIED"
    assert [p.task_id for p in _task(pack, "S2").predecessors] == ["S1"]


def test_missing_values_and_retired_ids_are_rejected(seeded_real):
    pack = seeded_real
    doc = _add(pack, _exported(pack), "S1", 240, 300)
    del doc["tasks"][-1]["zone_id"]
    doc = _add(pack, doc, "S2", 300, 360, zone_id="NOPE")
    doc = _add(pack, doc, "S3", 360, 420, work_type="NOPE")
    doc = _add(pack, doc, "S4", 420, 480, hazard_tags=["HOT_WORK"])  # 위험 태그는 받으면 버린다
    del doc["tasks"][-1]["duration"]  # 작업 시간은 배정의 끝 − 시작으로 채운다
    # S9: 폼으로 넣었다가 철회한 ID
    form = TaskRequestForm(
        task_id="S9",
        work_type="HOT_WORK",
        zone_id="G",
        duration=60,
        earliest_start=0,
        latest_start=420,
        latest_end=480,
    )
    assert submit_task_request(pack, "planner_b", _key(), form).status == "APPLIED"
    assert (
        withdraw_task_request(pack, "planner_b", _key(), TaskWithdraw(task_id="S9")).status
        == "APPLIED"
    )
    doc = _add(pack, doc, "S9", 240, 300)
    _, tasks = _preview(pack, doc)
    assert {tid: tasks[tid]["reasons"] for tid in ("S1", "S2", "S3", "S9")} == {
        "S1": ["FIELD_MISSING"],
        "S2": ["UNKNOWN_ZONE"],
        "S3": ["UNKNOWN_WORK_TYPE"],
        "S9": ["TASK_ID_RETIRED"],
    }
    assert tasks["S4"]["verdict"] == "NEW"
    assert _import(pack, doc, exclude=(*OTHERS, "S1", "S2", "S3", "S9")).status == "APPLIED"
    assert (_task(pack, "S4").duration, _task(pack, "S4").hazard_tags) == (60, ("HOT_WORK",))


# ── 이미 있는 내 작업 ──────────────────────────────────────────


def test_existing_task_with_another_assignment_gets_a_hope(seeded_real):
    """배정이 지금 계획과 다르면 그 구간이 희망 영역이 된다(타임라인에서 희망을 그린 것과 같다).
    계획은 그대로이고 현장 버전이 한 번 오른다."""
    pack = seeded_real
    doc = _exported(pack)
    _move(pack, doc, "K", 1560, 1680)  # 10/13 09:00–11:00 → 11:00–13:00
    _, tasks = _preview(pack, doc)
    assert (tasks["K"]["verdict"], tasks["K"]["hope_changed"]) == ("HOPE_CHANGED", True)
    before = _site(pack)
    out = _import(pack, doc)
    assert out.result_refs["changed_task_ids"] == ["K"]
    with db.read() as conn:
        hope = preferred_windows(conn, pack.site_id)["K"]
        plan = {a.task_id: a.start for a in get_current_plan(conn, pack.site_id).assignments}
    assert (hope["start"], hope["end"], hope["origin"], hope["made_by"]) == (
        1560,
        1680,
        "STATED",
        "OWNER",
    )
    assert plan["K"] == 1440 and _task(pack, "K").revision == 1
    assert _site(pack).context_version == before.context_version + 1
    # 같은 문서를 다시 넣으면 더 바뀌는 것이 없다
    again = _import(pack, doc)
    assert again.result_refs["changed_task_ids"] == []
    assert _site(pack).context_version == before.context_version + 1


def test_existing_task_with_other_values_is_edited_like_the_card(seeded_real):
    pack = seeded_real
    doc = _exported(pack)
    k = next(t for t in doc["tasks"] if t["task_id"] == "K")
    k["zone_id"] = "H"  # 골리앗은 F·H에서 쓸 수 있다
    _, tasks = _preview(pack, doc)
    assert (tasks["K"]["verdict"], tasks["K"]["changed"]) == ("VALUE_CHANGED", ["zone_id"])
    assert _import(pack, doc).status == "APPLIED"
    edited = _task(pack, "K")
    assert (edited.revision, edited.zone_id) == (2, "H")
    assert edited.fields["zone_id"].source_ref.startswith("schedule:")

    # 카드에서 고칠 수 없는 값은 문서로도 고칠 수 없다. 폼과 같은 검증에 걸리는 값도 그렇다
    fixed = _exported(pack)
    next(t for t in fixed["tasks"] if t["task_id"] == "K")["work_type"] = "PAINTING"
    next(t for t in fixed["tasks"] if t["task_id"] == "P")["zone_id"] = "NOPE"
    _, tasks = _preview(pack, fixed)
    assert (tasks["K"]["reasons"], tasks["P"]["reasons"]) == (
        ["VALUE_NOT_EDITABLE"],
        ["UNKNOWN_ZONE"],
    )


# ── 열린 메인과 대기열 ─────────────────────────────────────────


def test_open_main_queues_new_tasks_applies_changes_and_promotes_all_at_once(seeded_real, main_on):
    pack = seeded_real
    add_run(pack, "main", agent_type="MAIN", acting_unit_id="SITE", acting_actor_id=None)
    doc = _add(pack, _exported(pack), "S1", 240, 300)
    doc = _add(pack, doc, "S2", 300, 360)
    _move(pack, doc, "K", 1560, 1680)
    before = _site(pack)
    out = _import(pack, doc)
    assert out.status == "APPLIED" and out.result_refs["queued"] is True
    # 새 작업은 대기열에 선다(아직 사실이 아니다). 기존 작업의 희망은 바로 적용되고 열린 메인에 전해진다
    assert (_task(pack, "S1").lifecycle, _task(pack, "S2").lifecycle) == ("QUEUED", "QUEUED")
    assert "S1" not in take_snapshot(pack).facts().task_map()
    with db.read() as conn:
        assert preferred_windows(conn, pack.site_id)["K"]["start"] == 1560
    assert _site(pack).context_version == before.context_version + 1
    [event] = _events(pack)
    assert (event["case_id"], event["ref"]["task_ids"], event["ref"]["changed_task_ids"]) == (
        "case_main",
        [],
        ["K"],
    )
    # 그동안 온 폼도 대기열에 선다
    form = TaskRequestForm(
        task_id="F1",
        work_type="HOT_WORK",
        zone_id="G",
        duration=60,
        earliest_start=0,
        latest_start=420,
        latest_end=480,
    )
    assert submit_task_request(pack, "planner_a", _key(), form).result_refs["queued"] is True

    # 메인이 끝나면 대기 중인 접수가 전부 한 번에 오른다: 현장 버전 한 번, 일정은 사건 하나
    with db.write() as tx:
        assert end_case_run(tx, pack, "main", "CANCELLED", "TEST")
    with db.read() as conn:
        assert queued_task_ids(conn, pack.site_id) == []
    assert {_task(pack, t).lifecycle for t in ("S1", "S2", "F1")} == {"READY"}
    assert _site(pack).context_version == before.context_version + 2
    promoted = _events(pack)[-1]
    assert promoted["ref"]["schedule_id"] == out.result_refs["schedule_id"]
    assert (promoted["ref"]["kind"], promoted["ref"]["task_ids"]) == ("QUEUE", ["S1", "S2"])
    assert [e["ref"]["task_id"] for e in _events(pack, "TASK_READY")] == ["F1"]
    assert promoted["case_id"] == _events(pack, "TASK_READY")[0]["case_id"]
    # 올라온 작업의 기준 배정은 문서의 배정이다
    assert take_snapshot(pack).facts().base_assignments()["S2"].start == 300


# ── 같은 명령 두 번, API ───────────────────────────────────────


def test_same_command_twice_has_one_effect(seeded_real):
    pack = seeded_real
    doc = _add(pack, _exported(pack), "S1", 240, 300)
    key = _key()
    first = _import(pack, doc, key=key)
    again = _import(pack, doc, key=key)
    assert (first.status, again.status) == ("APPLIED", "REPLAYED")
    assert again.result_refs == first.result_refs
    assert (_imports(), _task(pack, "S1").revision, len(_events(pack))) == (1, 1, 1)
    # 다른 키로 다시 넣으면 S1은 이미 있는 내 작업이고 바뀌는 것이 없다
    third = _import(pack, doc)
    assert (third.result_refs["new_task_ids"], third.result_refs["unchanged"]) == ([], 7)
    assert _task(pack, "S1").revision == 1 and len(_events(pack)) == 1


def test_judgement_is_the_same_for_preview_and_command(seeded_real):
    """미리보기는 읽기 전용이다: 기록도 작업도 남기지 않는다. 넣기 명령이 같은 판정을 다시 한다."""
    pack = seeded_real
    doc = _add(pack, _exported(pack), "S1", 240, 300)
    with TestClient(app) as client:
        body = {"document": doc, "exclude": list(OTHERS)}
        res = client.post(
            f"/api/sites/{SITE}/schedules/preview", json=body, headers={"X-Actor": "planner_b"}
        )
        assert res.status_code == 200, res.text
        view = res.json()
        assert view["acceptable"] and view["authorized"]
        assert _imports() == 0
        with db.read() as conn:
            assert "S1" not in {t.task_id for t in list_current_tasks(conn, pack.site_id, pack)}
            judged = judge(conn, pack, get_actor(conn, pack.site_id, "planner_b"), doc, OTHERS)
        assert [(t["task_id"], t["verdict"]) for t in view["tasks"]] == [
            (i.task_id, i.verdict) for i in judged.items
        ]
        headers = {"X-Actor": "planner_b", "Idempotency-Key": _key()}
        done = client.post(f"/api/sites/{SITE}/schedules/import", json=body, headers=headers)
        assert done.status_code == 200, done.text
        assert done.json()["result_refs"]["new_task_ids"] == ["S1"]
        refused = client.post(
            f"/api/sites/{SITE}/schedules/import",
            json={"document": doc, "exclude": []},
            headers={"X-Actor": "planner_b", "Idempotency-Key": _key()},
        )
        assert refused.status_code == 409
        assert refused.json()["reason_codes"] == ["TASKS_NOT_IMPORTABLE"]
