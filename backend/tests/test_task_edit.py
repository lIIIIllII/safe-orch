"""작업 카드에서 값 고치기·정한 값 확인 (AG-33)과 재계획이 보는 정한 값 (AG-32).

Work Intake가 정한 값은 작업 기록에 남는다. 요청자(담당자)가 카드에서 고치면 새 revision이고 그 값은
사람이 말한 값이 된다. [확인]하면 남은 정한 값이 모두 말한 값이 되고 Consent가 생긴다. 재계획은 정한
값을 바꾸지 못하고, 막히면 결과의 열 수 있는 것에 올린다.
"""

from fastapi.testclient import TestClient
from scripted import Router, blocked, solve
from test_intake import (
    _complete,
    _consents,
    _intake,
    _key,
    _run_intake,
    _runs,
    _site,
    _steps,
    _task,
)

from app.agents import casefacts
from app.commands.task_edit import EditRequest, edit_task
from app.coordinator.dispatcher import run_until_idle
from app.main import app
from app.store import db
from app.store.repos.case_events import list_case_events
from app.store.repos.consultations import candidate_state, list_review_queue
from app.store.repos.records import get_candidate

DECIDED = ("duration", "latest_start", "latest_end", "requested_resource_id")


def _edit(pack, actor, task_id="A", **values):
    return edit_task(pack, actor, _key(), EditRequest(task_id=task_id, **values))


def _origins(task):
    return {name: f.origins for name, f in task.fields.items()}


def test_owner_edits_and_confirms_decided_values_on_card(seeded):
    """고치면 새 revision이고 고친 값만 말한 값이 된다. 검증은 폼과 같다. [확인]하면 남은 정한 값이 모두
    말한 값이 되고 시작 범위·요청 자원의 Consent가 생긴다."""
    pack = seeded
    run = _run_intake(pack, [_complete(decided=DECIDED)])
    intake_source = f"intake:{run.input_ref['intake_id']}"
    assert _consents("A") == []
    ctx = _site(pack).context_version

    for actor in ("planner_b", "supervisor"):
        assert _edit(pack, actor, duration=45).reason_codes == ("NOT_AUTHORIZED",)
    assert _edit(pack, "planner_a", "ZZ", duration=45).reason_codes == ("TASK_NOT_FOUND",)
    assert _edit(pack, "planner_a", duration=30).reason_codes == ("NO_CHANGE",)
    # 폼과 같은 검증
    bad = _edit(pack, "planner_a", requested_resource_id="B-CR-01")
    assert bad.reason_codes == ("RESOURCE_NOT_AUTHORIZED",)
    assert _edit(pack, "planner_a", latest_end=10).reason_codes == ("INVALID_WINDOW",)
    assert (_task(pack, "A").revision, _site(pack).context_version) == (1, ctx)

    out = _edit(pack, "planner_a", duration=45, latest_end=105)
    assert out.status == "APPLIED" and out.result_refs["changed"] == ["duration", "latest_end"]
    a = _task(pack, "A")
    assert (a.revision, a.duration, a.latest_end) == (2, 45, 105)
    assert _origins(a) == {
        "zone_id": {},
        "duration": {},
        "window": {"latest_start": "DECIDED"},
        "resource": {"requested_resource_id": "DECIDED"},
    }
    card = f"card:{out.result_refs['edit_id']}"
    sources = {name: f.source_ref for name, f in a.fields.items()}
    assert sources == {
        "zone_id": intake_source,
        "duration": card,
        "window": card,
        "resource": intake_source,
    }
    # 확인 기록의 값은 작업 값과 같다 (C11)
    assert a.fields["duration"].value == 45
    assert a.fields["window"].value["latest_end"] == 105
    assert _site(pack).context_version == ctx + 1
    # 시작 범위와 요청 자원에는 정한 값이 남아 있어 Consent가 없다
    assert _consents("A") == []
    with db.read() as conn:
        assert [e for e in list_case_events(conn, pack.site_id) if e["kind"] == "TASK_EDITED"] == []

    out = _edit(pack, "planner_a", confirm=True)
    assert out.status == "APPLIED" and out.result_refs["confirmed"] is True
    a = _task(pack, "A")
    assert (a.revision, a.decided_values) == (3, ())
    assert all(f.origins == {} for f in a.fields.values())
    confirm = f"card:{out.result_refs['edit_id']}"
    with db.read() as conn:
        current = conn.execute(
            "SELECT axis, scope, source_ref FROM consent WHERE task_id = 'A' AND task_revision = 3"
            " ORDER BY rowid"
        ).fetchall()
    assert [(c[0], c[2]) for c in current] == [("TIME", confirm), ("RESOURCE", confirm)]
    assert '"start_max":60' in current[0][1].replace(" ", "")
    # 더 확인할 것이 없다
    assert _edit(pack, "planner_a", confirm=True).reason_codes == ("NO_CHANGE",)


def test_edit_keeps_consent_of_unchanged_axis_and_drops_changed_one(seeded):
    """값이 바뀌지 않은 축의 Consent는 새 revision으로 간다. 시작 범위를 고치면 새 범위의 Consent가 된다."""
    pack = seeded
    _run_intake(pack, [_complete(decided=("duration",))])
    assert [c[0] for c in _consents("A")] == ["TIME", "RESOURCE"]
    out = _edit(pack, "planner_a", latest_start=30, latest_end=60)
    assert out.status == "APPLIED"
    with db.read() as conn:
        current = conn.execute(
            "SELECT axis, scope, source_ref FROM consent WHERE task_id = 'A' AND task_revision = 2"
            " ORDER BY rowid"
        ).fetchall()
    by_axis = {c[0]: (c[1].replace(" ", ""), c[2]) for c in current}
    assert sorted(by_axis) == ["RESOURCE", "TIME"]
    assert '"start_max":30' in by_axis["TIME"][0] and by_axis["TIME"][1].startswith("card:")
    assert by_axis["RESOURCE"][1].startswith("intake:")  # 복사된 것
    assert _task(pack, "A").decided_values == ("duration",)  # 고치지 않은 정한 값은 남는다


def test_edit_of_planned_task_must_keep_its_placement(seeded):
    """계획에 있는 작업은 지금 배치가 깨지는 값으로는 고칠 수 없다. 깨지지 않는 값은 고칠 수 있다."""
    pack = seeded
    before = _task(pack, "C")
    out = _edit(pack, "foreman_a2", "C", duration=before.duration + 30)
    assert out.reason_codes == ("EDIT_BREAKS_PLAN",)
    assert _task(pack, "C").revision == before.revision
    out = _edit(pack, "foreman_a2", "C", latest_end=before.latest_end + 30)
    assert out.status == "APPLIED"
    assert (_task(pack, "C").revision, _site(pack).plan_revision) == (before.revision + 1, 0)


def test_edit_during_review_tells_the_open_main_and_counts_as_human_work(seeded, main_on):
    """카드에서 고치거나 확인하면 살아 있는 후보는 무효가 되고, 열린 메인에 사건이 가며 사람이 만든 일로
    센다 (AG-30)."""
    pack = seeded
    router = Router(
        intake=[_complete(decided=("duration",))], replanning=[solve("L0"), solve("L1")]
    )
    assert _intake(pack).status == "APPLIED"
    run_until_idle(pack, model_factory=router.factory())
    [main] = _runs("MAIN")
    with db.read() as conn:
        queue = list_review_queue(conn, pack.site_id)
        assert casefacts.human_work(conn, pack.site_id, main.case_id) == 0
    assert queue and main.status == "WAITING_HUMAN"

    assert _edit(pack, "planner_a", confirm=True).status == "APPLIED"
    with db.read() as conn:
        for cid in queue:
            state = candidate_state(conn, pack.site_id, get_candidate(conn, pack.site_id, cid))
            assert state.stale_context
        [event] = [e for e in list_case_events(conn, pack.site_id) if e["kind"] == "TASK_EDITED"]
        assert casefacts.human_work(conn, pack.site_id, main.case_id) == 1
    assert event["case_id"] == main.case_id
    assert (event["ref"]["task_id"], event["ref"]["confirmed"]) == ("A", True)
    assert _runs("MAIN")[0].wake_seq == main.wake_seq + 1


def test_replanning_sees_decided_values_and_cannot_change_them(seeded, main_on):
    """재계획 관찰에 정한 값이 보이고, 막힌 결과의 열 수 있는 것에 "요청자가 고치면 열림"으로 올라간다.
    재계획은 작업 값을 바꾸지 않는다 (CV-24)."""
    pack = seeded
    assert _intake(pack).status == "APPLIED"
    router = Router(
        intake=[_complete(decided=("duration", "latest_start"))],
        replanning=[blocked("범위를 다 써도 해가 없다")],
    )
    run_until_idle(pack, model_factory=router.factory())
    [rp] = _runs("REPLANNING")
    [step] = _steps(rp.run_id)
    acting = {t["task_id"]: t for t in step["observation"]["acting_tasks"]}
    assert acting["A"]["decided_values"] == ["duration", "latest_start"]
    assert acting["C"]["decided_values"] == []
    decided = [o for o in step["observation"]["openers"] if o.get("decided")]
    assert decided == [
        {"kind": "FACT_CHANGE", "field": "WINDOW", "task_id": "A", "decided": True},
        {"kind": "FACT_CHANGE", "field": "DURATION", "task_id": "A", "decided": True},
    ]
    result = [o for o in step["tool_result"]["openers"] if o.get("decided")]
    assert [(o["field"], o["task_id"]) for o in result] == [("WINDOW", "A"), ("DURATION", "A")]
    assert all("need_id" in o for o in result)
    a = _task(pack, "A")
    assert (a.revision, a.duration, a.decided_values) == (1, 30, ("duration", "latest_start"))
    # 메인은 정한 값 표시가 붙은 결과를 읽고 이어 간다
    assert _runs("MAIN")[0].status != "ERROR"


def test_edit_api(seeded):
    pack = seeded
    _run_intake(pack, [_complete(decided=("duration",))])
    with TestClient(app) as client:

        def post(actor, body):
            headers = {"X-Actor": actor, "Idempotency-Key": _key()}
            return client.post("/api/tasks/A/edit", json=body, headers=headers)

        assert post("planner_b", {"duration": 45}).status_code == 403
        assert post("planner_a", {"duration": 0}).status_code == 422
        res = post("planner_a", {"duration": 45, "latest_end": 105})
        assert res.status_code == 200 and res.json()["result_refs"]["revision"] == 2
        state = client.get(f"/api/sites/{pack.site_id}/state", headers={"X-Actor": "planner_a"})
    task = next(t for t in state.json()["tasks"] if t["task_id"] == "A")
    assert task["duration"] == 45 and task["fields"]["duration"]["origins"] == {}
