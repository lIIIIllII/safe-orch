"""작업 카드에서 값 고치기 (AG-33)와 재계획이 보는 정한 값 (AG-32).

Work Intake가 정한 값은 작업 기록에 남는다. 요청자(담당자)가 카드에서 고치면 새 revision이고 그 값은
사람이 말한 값이 된다. 그대로 확인하는 길은 없다: 고치지 않은 정한 값은 정한 값으로 남는다. 재계획은 정한
값을 바꾸지 못하고, 막히면 결과의 열 수 있는 것에 올린다.
"""

from conftest import add_run, take_snapshot
from fastapi.testclient import TestClient
from scripted import Router, blocked, solve
from test_intake import (
    _base,
    _complete,
    _intake,
    _key,
    _run_intake,
    _runs,
    _site,
    _steps,
    _task,
)

from app.agents import casefacts
from app.api.state import build_state
from app.commands.task_edit import EditRequest, edit_task
from app.commands.task_request import TaskRequestForm, submit_task_request
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


def test_owner_edits_decided_values_on_card(seeded):
    """고치면 새 revision이고 고친 값만 말한 값이 된다. 검증은 폼과 같다. 고치지 않은 정한 값은 그대로
    정한 값으로 남고, 그대로 확인하는 길은 없다."""
    pack = seeded
    run = _run_intake(pack, [_complete(decided=DECIDED)])
    intake_source = f"intake:{run.input_ref['intake_id']}"
    ctx = _site(pack).context_version

    for actor in ("planner_b", "supervisor"):
        assert _edit(pack, actor, duration=45).reason_codes == ("NOT_AUTHORIZED",)
    assert _edit(pack, "planner_a", "ZZ", duration=45).reason_codes == ("TASK_NOT_FOUND",)
    assert _edit(pack, "planner_a", duration=30).reason_codes == ("NO_CHANGE",)
    assert _edit(pack, "planner_a").reason_codes == ("NO_CHANGE",)  # 고친 값이 없다
    # 폼과 같은 검증
    bad = _edit(pack, "planner_a", requested_resource_id="B-CR-01")
    assert bad.reason_codes == ("RESOURCE_NOT_AUTHORIZED",)
    assert _edit(pack, "planner_a", latest_end=10).reason_codes == ("INVALID_WINDOW",)
    assert (_task(pack, "A").revision, _site(pack).context_version) == (1, ctx)

    out = _edit(pack, "planner_a", duration=45, latest_end=105)
    assert out.status == "APPLIED" and out.result_refs["changed"] == ["duration", "latest_end"]
    assert "confirmed" not in out.result_refs
    a = _task(pack, "A")
    assert (a.revision, a.duration, a.latest_end) == (2, 45, 105)
    assert _origins(a) == {
        "zone_id": {},
        "duration": {},
        "window": {},
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
    # 요청 자원은 정한 값으로 남고, 기준 위치(정한 범위)는 카드에서 고치지 않는다
    assert a.decided_values == ("requested_resource_id",)
    assert (_base("A")["origin"], _base("A")["start_min"], _base("A")["start_max"]) == (
        "DECIDED",
        0,
        60,
    )
    # A는 아직 계획 밖 요청이라 사건이 남는다(자동 시작이 꺼져 있어 메인은 뜨지 않는다)
    with db.read() as conn:
        [event] = [e for e in list_case_events(conn, pack.site_id) if e["kind"] == "TASK_EDITED"]
    assert event["ref"]["changed"] == ["duration", "latest_end"]

    # 정한 값을 고치면 말한 값이 된다
    out = _edit(pack, "planner_a", requested_resource_id="SITE-CR-01")
    assert out.status == "APPLIED"
    a = _task(pack, "A")
    assert (a.revision, a.decided_values) == (3, ())
    assert all(f.origins == {} for f in a.fields.values())


def test_card_has_no_confirm(seeded):
    """카드의 [확인]은 없다: 본문에 confirm을 보내면 형식 오류다."""
    pack = seeded
    _run_intake(pack, [_complete(decided=("duration",))])
    with TestClient(app) as client:
        headers = {"X-Actor": "planner_a", "Idempotency-Key": _key()}
        res = client.post("/api/tasks/A/edit", json={"confirm": True}, headers=headers)
    assert res.status_code == 422
    assert _task(pack, "A").decided_values == ("duration",)


def test_edit_that_breaks_the_placement_is_accepted_and_starts_a_main(seeded_real, main_on):
    """계획에 있는 작업을 지금 배치가 깨지는 값으로 고쳐도 받는다. 서버의 충돌 검사에 걸리므로 사건이
    작업 준비됨처럼 전해져 열린 메인이 없으면 메인이 뜨고, 재계획이 그 작업을 다시 놓는다."""
    pack = seeded_real
    before = _task(pack, "C")
    assert _runs("MAIN") == []
    out = _edit(pack, "foreman_a2", "C", duration=before.duration + 90)
    assert out.status == "APPLIED"
    assert (_task(pack, "C").revision, _site(pack).plan_revision) == (before.revision + 1, 0)
    with db.read() as conn:
        state = build_state(conn, pack, "foreman_a2")
        [event] = [e for e in list_case_events(conn, pack.site_id) if e["kind"] == "TASK_EDITED"]
    # 고친 값과 어긋난 기존 배정은 충돌 검사에 잡힌다
    assert [(c["rule_id"], c["task_ids"]) for c in state["conflicts"]] == [("DURATION", ["C"])]
    [main] = _runs("MAIN")
    assert (main.status, event["case_id"]) == ("RUNNING", main.case_id)

    run_until_idle(pack, model_factory=Router(replanning=[solve("L0"), solve("L1")]).factory())
    [main] = _runs("MAIN")
    [rp] = _runs("REPLANNING")
    assert (main.status, rp.status, rp.case_id) == ("WAITING_HUMAN", "SUCCEEDED", main.case_id)
    with db.read() as conn:
        [cid] = list_review_queue(conn, pack.site_id)
        placed = next(
            a for a in get_candidate(conn, pack.site_id, cid).assignments if a.task_id == "C"
        )
    assert placed.end - placed.start == before.duration + 90


def test_edit_without_conflict_goes_only_to_an_open_main(seeded_real, main_on):
    """고친 뒤 충돌도 계획 밖 요청도 없으면 고정·직접 이동처럼 열린 메인에만 사건을 보낸다. 열린 메인이
    없으면 사건도 메인도 생기지 않는다."""
    pack = seeded_real
    before = _task(pack, "C")
    out = _edit(pack, "foreman_a2", "C", latest_end=before.latest_end - 30)
    assert out.status == "APPLIED" and _task(pack, "C").revision == before.revision + 1
    with db.read() as conn:
        assert list_case_events(conn, pack.site_id) == []
    assert _runs("MAIN") == []


def test_edit_during_review_tells_the_open_main_and_counts_as_human_work(seeded, main_on):
    """카드에서 고치면 살아 있는 후보는 무효가 되고, 열린 메인에 사건이 가며 사람이 만든 일로 센다
    (AG-30)."""
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

    assert _edit(pack, "planner_a", duration=45).status == "APPLIED"
    with db.read() as conn:
        for cid in queue:
            state = candidate_state(conn, pack.site_id, get_candidate(conn, pack.site_id, cid))
            assert state.stale_context
        [event] = [e for e in list_case_events(conn, pack.site_id) if e["kind"] == "TASK_EDITED"]
        assert casefacts.human_work(conn, pack.site_id, main.case_id) == 1
    assert event["case_id"] == main.case_id
    assert (event["ref"]["task_id"], event["ref"]["changed"]) == ("A", ["duration"])
    # 열린 메인이 있으면 그 메인이 받는다(새 메인을 띄우지 않는다)
    [woke] = _runs("MAIN")
    assert woke.wake_seq == main.wake_seq + 1


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
    acting = {t["task_id"]: t for t in step["observation"]["tasks"]}
    assert acting["A"]["decided_values"] == ["duration"]
    assert acting["C"]["decided_values"] == []
    # 정한 시각은 기준 위치의 범위다(정함 표시). Hard가 아니라 해를 막지 않으므로 열 수 있는 것에 오르지 않는다
    assert (acting["A"]["base"]["source"], acting["A"]["base"]["decided"]) == ("REQUEST", True)
    decided = [o for o in step["observation"]["openers"] if o.get("decided")]
    assert decided == [
        {"kind": "FACT_CHANGE", "field": "DURATION", "task_id": "A", "decided": True},
    ]
    result = [o for o in step["tool_result"]["openers"] if o.get("decided")]
    assert [(o["field"], o["task_id"]) for o in result] == [("DURATION", "A")]
    assert all("need_id" in o for o in result)
    a = _task(pack, "A")
    assert (a.revision, a.duration, a.decided_values) == (1, 30, ("duration",))
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


# ── 요청 시작 범위 고치기 (AG-33) ──────────────────────────────


def _facts(pack):
    return take_snapshot(pack).facts()


def _range(task_id):
    found = _base(task_id)
    return found["start_min"], found["start_max"], found["origin"]


def test_owner_edits_the_requested_start_range_on_the_card(seeded):
    """계획에 아직 없는 자기 작업의 요청 시작 범위를 담당자가 고친다. 고친 범위는 말한 범위가 되고, 지연·
    변경의 기준이 바뀐다. 작업 값은 그대로라 revision은 그대로이고 현장 버전은 오른다."""
    pack = seeded
    _run_intake(pack, [_complete(decided=("latest_start",))])
    assert _range("A") == (0, 60, "DECIDED")
    before = _facts(pack)
    assert (before.base_range("A"), before.deviation("A", 90)) == ((0, 60), 30)
    ctx = _site(pack).context_version

    # 담당자만 고친다. 바뀐 것이 없거나 범위 모양이 틀리면 거절한다
    assert _edit(pack, "planner_b", base_start_max=120).reason_codes == ("NOT_AUTHORIZED",)
    assert _edit(pack, "supervisor", base_start_max=120).reason_codes == ("NOT_AUTHORIZED",)
    assert _edit(pack, "planner_a", base_start=0, base_start_max=60).reason_codes == ("NO_CHANGE",)
    bad = _edit(pack, "planner_a", base_start=90, base_start_max=30)
    assert bad.reason_codes == ("INVALID_BASE_RANGE",)
    over = _edit(pack, "planner_a", base_start_max=pack.horizon_minutes)
    assert over.reason_codes == ("INVALID_BASE_RANGE",)
    assert (_range("A"), _site(pack).context_version) == ((0, 60, "DECIDED"), ctx)

    out = _edit(pack, "planner_a", base_start_max=120)
    assert out.status == "APPLIED" and out.result_refs["changed"] == ["base_range"]
    assert (_task(pack, "A").revision, out.result_refs["revision"]) == (1, 1)
    assert _site(pack).context_version == ctx + 1
    assert _range("A") == (0, 120, "STATED")
    after = _facts(pack)
    assert (after.base_range("A"), after.deviation("A", 90)) == ((0, 120), 0)
    assert after.base_info("A")["origin"] == "STATED"
    # 계획 밖 요청이 남아 있으므로 사건이 남는다(값 고치기와 같은 규칙)
    with db.read() as conn:
        [event] = [e for e in list_case_events(conn, pack.site_id) if e["kind"] == "TASK_EDITED"]
        state = build_state(conn, pack, "planner_a")
    assert event["ref"]["changed"] == ["base_range"]
    shown = next(t for t in state["tasks"] if t["task_id"] == "A")["base"]
    assert shown == {"source": "REQUEST", "start": 0, "start_max": 120, "origin": "STATED"}

    # 값과 범위를 같이 고치면 revision이 오르고, 범위는 한 점으로도 좁힐 수 있다
    out = _edit(pack, "planner_a", duration=45, base_start=30, base_start_max=30)
    assert out.status == "APPLIED" and out.result_refs["changed"] == ["base_range", "duration"]
    assert (_task(pack, "A").revision, _facts(pack).base_range("A")) == (2, (30, 30))


def test_requested_start_range_is_not_for_form_or_planned_tasks(seeded):
    """기준 위치가 없는 폼 작업은 고칠 범위가 없고, 계획에 있는 작업의 기준은 승인된 자리라 고치지 않는다."""
    pack = seeded
    form = TaskRequestForm(
        task_id="A2",
        work_type="LIFTING",
        zone_id="B",
        duration=30,
        earliest_start=0,
        latest_start=60,
        latest_end=90,
        required_resource_type="CRANE",
        requested_resource_id="A-CR-01",
    )
    assert submit_task_request(pack, "planner_a", _key(), form).status == "APPLIED"
    ctx = _site(pack).context_version
    out = _edit(pack, "planner_a", "A2", base_start=0, base_start_max=30)
    assert out.reason_codes == ("NO_BASE",)
    out = _edit(pack, "foreman_a2", "C", base_start=60, base_start_max=90)
    assert out.reason_codes == ("BASE_IN_PLAN",)
    assert (_base("A2"), _base("C"), _site(pack).context_version) == (None, None, ctx)


def test_queued_task_range_can_be_edited_without_touching_the_site(seeded, main_on):
    """대기 중인(QUEUED) 작업도 요청 시작 범위를 고친다. 아직 사실이 아니라 현장 버전은 그대로다."""
    pack = seeded
    add_run(pack, "main", agent_type="MAIN", acting_unit_id="SITE", acting_actor_id=None)
    assert _intake(pack).status == "APPLIED"
    run_until_idle(pack, model_factory=Router(intake=[_complete()]).factory())
    assert _task(pack, "A").lifecycle == "QUEUED"
    ctx = _site(pack).context_version
    out = _edit(pack, "planner_a", base_start=30)
    assert out.status == "APPLIED" and out.result_refs["queued"] is True
    assert _range("A") == (30, 60, "STATED") and _site(pack).context_version == ctx
    with db.read() as conn:
        tasks = build_state(conn, pack, "planner_a")["tasks"]
    shown = next(t for t in tasks if t["task_id"] == "A")["base"]
    assert shown == {"source": "REQUEST", "start": 30, "start_max": 60, "origin": "STATED"}
