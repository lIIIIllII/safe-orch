"""일정 검토 Agent (AG-36).

일정 Case에 충돌이 있을 때 메인이 부른다. 서버는 최소 묶음(작업을 공유하는 충돌)과 그 사이의 관계, 사람만 풀
수 있는 묶음만 계산하고, 묶음을 어떻게 합칠지는 Agent가 정한다. 서버는 그 묶음이 최소 묶음의 합인지만 검사하고
묶음안을 불변 기록으로 남긴다. 재계획은 지금처럼 현장 충돌 전체를 한 번에 푼다. Pack 그대로의 R0에서 본다.
"""

import sqlite3
import uuid

import pytest
from conftest import add_task, make_task, take_snapshot
from scripted import Router, blocked, call, done, main_call, main_escalate, solve
from test_intake import _complete, _intake
from test_schedule_import import _add, _exported, _import

from app.agents import casefacts
from app.agents.observers import schedule_review as observer
from app.agents.prompts import schedule_review as prompt
from app.agents.specs import schedule_review as spec
from app.api.state import build_state
from app.commands.pins import TaskRef, pin_task
from app.commands.task_edit import EditRequest, edit_task
from app.commands.task_request import TaskRequestForm, submit_task_request
from app.coordinator.dispatcher import run_until_idle
from app.domain.bundles import check_bundles, group_relations, group_span, human_only_reason
from app.domain.groups import conflict_groups
from app.rules.engine import detect_conflicts
from app.store import db
from app.store.repos.bundles import list_bundle_plans
from app.store.repos.runs import get_run, list_steps
from app.store.repos.schedules import base_range_changes
from app.store.repos.site import get_site

GANTRY = {"zone_id": "F", "required_resource_type": "GANTRY", "requested_resource_id": "SITE-GC-01"}


def _key():
    return uuid.uuid4().hex


def _runs(agent_type):
    with db.read() as conn:
        ids = [
            r[0]
            for r in conn.execute(
                "SELECT run_id FROM agent_run WHERE agent_type = ? ORDER BY rowid", (agent_type,)
            )
        ]
        return [get_run(conn, i) for i in ids]


def _steps(run_id):
    with db.read() as conn:
        return list_steps(conn, run_id)


def _groups(pack):
    snap = take_snapshot(pack)
    facts = snap.facts()
    return facts, conflict_groups(detect_conflicts(snap, facts.check_assignments(), pack))


def _submit(*bundles, opinion="도장 구역 옆 화기 작업이 이틀째에 몰려 있다"):
    """SUBMIT_BUNDLES. bundles는 최소 묶음 ID 목록들이다."""
    return call(
        "SUBMIT_BUNDLES",
        "이유: 묶음을 정했다/다음: 종료",
        bundles=[{"group_ids": list(ids), "note": "도장과 화기의 간격"} for ids in bundles],
        opinion=opinion,
    )


def _import_two_conflicts(pack):
    """UB의 화기 작업 둘을 넣는다. 둘째 날 도장(P 오전, W 오후) 옆이라 충돌이 두 묶음 생긴다."""
    doc = _add(pack, _exported(pack), "S1", 1500, 1560)
    doc = _add(pack, doc, "S2", 1740, 1800)
    out = _import(pack, doc)
    assert out.status == "APPLIED" and out.result_refs["new_task_ids"] == ["S1", "S2"]
    return out


# ── 서버 검사: 묶음은 최소 묶음의 합만 ─────────────────────────


def test_bundles_must_be_a_union_of_minimal_groups():
    groups = ["g1", "g2", "g3"]
    # 합친 묶음과 그대로 둔 묶음은 통과한다
    assert check_bundles(groups, [["g1", "g2"], ["g3"]]) == []
    assert check_bundles(groups, [["g1"], ["g2"], ["g3"]]) == []
    assert check_bundles(groups, [["g3", "g2", "g1"]]) == []
    # 빠진 최소 묶음
    assert check_bundles(groups, [["g1", "g2"]]) == [{"code": "GROUP_MISSING", "group_ids": ["g3"]}]
    # 쪼갠 묶음: 한 최소 묶음을 두 묶음에 넣으면 같은 작업을 따로 움직이게 된다
    assert check_bundles(groups, [["g1", "g2"], ["g2", "g3"]]) == [
        {"code": "GROUP_REPEATED", "group_ids": ["g2"]}
    ]
    # 없는 ID
    assert check_bundles(groups, [["g1", "g2", "g3"], ["g9"]]) == [
        {"code": "UNKNOWN_GROUP", "group_ids": ["g9"]}
    ]
    found = check_bundles(groups, [["g1", "g1"], ["zz"]])
    assert [x["code"] for x in found] == ["UNKNOWN_GROUP", "GROUP_REPEATED", "GROUP_MISSING"]


# ── 서버 계산: 묶음 사이 관계, 사람만 풀 수 있는 묶음 ──────────


def test_relations_between_minimal_groups(seeded_real):
    """같은 자원(기준 자원과 적격 대안), 같은 구역·관계 구역, 충돌 시간 사이의 간격."""
    pack = seeded_real
    # X는 K(둘째 날 골리앗)와, Y는 Q(셋째 날 골리앗)와 겹친다: 묶음 둘이 같은 자원을 쓴다
    x = make_task(
        pack,
        task_id="X",
        duration=60,
        earliest_start=1440,
        latest_start=1440,
        latest_end=1500,
        **GANTRY,
    )
    y = make_task(
        pack,
        task_id="Y",
        duration=60,
        earliest_start=2910,
        latest_start=2910,
        latest_end=2970,
        **GANTRY,
    )
    add_task(pack, x)
    add_task(pack, y)
    facts, groups = _groups(pack)
    # Q는 같은 시간의 M과도 엮여 있어 한 최소 묶음이다(작업을 공유하는 충돌)
    assert [g.task_ids for g in groups] == [("K", "X"), ("M", "Q", "Y")]
    [relation] = group_relations(facts, groups)
    assert (relation["group_a"], relation["group_b"]) == (groups[0].group_id, groups[1].group_id)
    assert "SITE-GC-01" in relation["shared_resource_ids"]
    assert {"zone_a": "F", "zone_b": "F", "relation": "SAME"} in relation["zone_links"]
    (_, end_a), (start_b, _) = group_span(groups[0]), group_span(groups[1])
    assert relation["gap_minutes"] == start_b - end_a > 0
    # 묶음이 하나면 관계가 없다
    assert group_relations(facts, groups[:1]) == []


def test_group_with_only_pinned_tasks_is_human_only(seeded_real):
    pack = seeded_real
    _import_two_conflicts(pack)
    facts, groups = _groups(pack)
    by_tasks = {g.task_ids: g for g in groups}
    assert sorted(by_tasks) == [("P", "S1"), ("S2", "W")]
    assert [human_only_reason(facts, g) for g in groups] == [None, None]
    # 걸린 작업이 모두 고정되면 움직일 작업이 없다: 사람만 풀 수 있다
    for task_id in ("P", "S1"):
        assert pin_task(pack, "supervisor", _key(), TaskRef(task_id=task_id)).status == "APPLIED"
    facts, groups = _groups(pack)
    reasons = {g.task_ids: human_only_reason(facts, g) for g in groups}
    assert reasons == {("P", "S1"): "ALL_PINNED", ("S2", "W"): None}
    views = {tuple(v["task_ids"]): v for v in observer.group_views(pack, facts, groups)}
    assert (views[("P", "S1")]["human_only"], views[("S2", "W")]["human_only"]) == (True, False)


# ── 메인이 부르는 조건과 묶음안 ────────────────────────────────


def test_main_calls_schedule_review_and_gets_an_immutable_bundle_plan(seeded_real, main_on):
    """일정 Case에 충돌이 있으면 일정 검토를 부를 수 있다. Agent가 낸 묶음은 서버가 최소 묶음의 합인지만
    검사한다. 걸리면 거절되고 Run은 계속되며, 통과하면 묶음안을 불변 기록으로 남기고 그 한 번으로 Run이
    끝나 메인에 결과가 간다. 같은 사실로 다시 부르는 것은 거절한다."""
    pack = seeded_real
    _import_two_conflicts(pack)
    _, groups = _groups(pack)
    first, second = (g.group_id for g in groups)
    router = Router(
        main=[main_call("SCHEDULE_REVIEW"), main_call("SCHEDULE_REVIEW"), main_escalate()],
        schedule_review=[
            _submit([first]),  # 빠진 최소 묶음
            _submit([first, second], [second]),  # 한 최소 묶음을 두 묶음에
            _submit([first, "grp_none"], [second]),  # 없는 ID
            _submit([first, second]),  # 합친 묶음은 통과: 여기서 끝난다
            _submit([first, second]),  # 쓰이지 않는다
        ],
    )
    run_until_idle(pack, model_factory=router.factory())
    [main] = _runs("MAIN")
    [review] = _runs("SCHEDULE_REVIEW")
    assert (review.status, review.parent_run_id, review.case_id) == (
        "SUCCEEDED",
        main.run_id,
        main.case_id,
    )
    assert router.left()["SCHEDULE_REVIEW"] == 1  # 묶음안을 낸 뒤에는 LLM을 더 부르지 않는다
    assert (review.acting_unit_id, review.solver_calls_used) == (None, 0)  # 계산하지 않는다

    steps = _steps(review.run_id)
    assert [(s["guard"]["verdict"], s["guard"]["reason_code"]) for s in steps] == [
        ("REJECTED", "BUNDLES_INVALID"),
        ("REJECTED", "BUNDLES_INVALID"),
        ("REJECTED", "BUNDLES_INVALID"),
        ("ACCEPTED", None),
    ]
    assert [s["result_kind"] for s in steps] == ["REJECTED", "REJECTED", "REJECTED", "DONE"]
    assert [s["tool_result"]["violations"][0]["code"] for s in steps[:3]] == [
        "GROUP_MISSING",
        "GROUP_REPEATED",
        "UNKNOWN_GROUP",
    ]
    # 관찰은 서버 사실이다: 최소 묶음, 관계, 일정에서 온 작업과 기존 작업의 구분
    obs = steps[0]["observation"]
    assert tuple(sorted(obs)) == prompt.OBSERVATION_KEYS
    assert [g["task_ids"] for g in obs["groups"]] == [["P", "S1"], ["S2", "W"]]
    assert [(r["group_a"], r["group_b"]) for r in obs["relations"]] == [(first, second)]
    origin = {t["task_id"]: (t["from_schedule"], t["in_plan"]) for t in obs["tasks"]}
    assert origin == {
        "P": (False, True),
        "S1": (True, False),
        "S2": (True, False),
        "W": (False, True),
    }
    assert obs["schedule"][0]["task_ids"] == ["S1", "S2"]
    # 거절된 뒤에도 같은 도구가 열려 있다. RETURN_RESULT는 막힘뿐이다
    for s in steps:
        assert spec.available_actions(s["observation"])["RETURN_RESULT"]["status"] == ["BLOCKED"]
        assert "SUBMIT_BUNDLES" in spec.available_actions(s["observation"])

    # 묶음안: 그때의 Snapshot에 묶인 불변 기록. 묶음 구조는 서버 값, 메모와 의견은 모델 문장이다
    with db.read() as conn:
        [plan] = list_bundle_plans(conn, pack.site_id, case_id=main.case_id)
        snapshots = [r[0] for r in conn.execute("SELECT snapshot_id FROM snapshot")]
    assert (plan["run_id"], plan["step_no"], plan["snapshot_id"] in snapshots) == (
        review.run_id,
        4,
        True,
    )
    [bundle] = plan["bundles"]
    assert (bundle["bundle_id"], bundle["group_ids"], bundle["task_ids"]) == (
        "B1",
        [first, second],
        ["P", "S1", "S2", "W"],
    )
    assert (bundle["human_only"], bundle["quoted_note"]) == (False, "도장과 화기의 간격")
    assert plan["quoted_opinion"] == "도장 구역 옆 화기 작업이 이틀째에 몰려 있다"
    assert review.end_reason == f"BUNDLES_SUBMITTED:{plan['bundle_plan_id']}"
    # 메인에게 가는 결과는 그 묶음안이다: ID와 묶음 구조(서버 값). 메모와 의견은 넣지 않는다
    structure = {
        "bundle_id": "B1",
        "group_ids": [first, second],
        "task_ids": ["P", "S1", "S2", "W"],
        "human_only_group_ids": [],
        "human_only": False,
    }
    assert steps[3]["tool_result"] == {
        "status": "DONE",
        "paths": [],
        "bundle_plan": {"bundle_plan_id": plan["bundle_plan_id"], "bundles": [structure]},
    }
    for sql in ("UPDATE bundle_plan SET case_id = 'x'", "DELETE FROM bundle_plan"):
        with (
            pytest.raises(sqlite3.IntegrityError, match="immutable: bundle_plan"),
            db.write() as tx,
        ):
            tx.execute(sql)

    # 메인: 처음에는 일정 검토를 부를 수 있었고, 돌아온 뒤에는 묶음안 요약이 보이며 같은 사실의
    # 재호출은 거절된다. 재계획은 그대로 부를 수 있다
    main_steps = _steps(main.run_id)
    before, after = main_steps[0]["observation"], main_steps[1]["observation"]
    [child] = after["child_results"]
    assert (child["run_id"], child["run_status"]) == (review.run_id, "SUCCEEDED")
    assert child["result"] == {
        "by": "AGENT",
        "status": "DONE",
        "paths": [],
        "bundle_plan": {"bundle_plan_id": plan["bundle_plan_id"], "bundles": [structure]},
        "quoted_summary": None,
    }
    assert {"agent": "SCHEDULE_REVIEW"} in before["calls"]
    assert before["schedule_review"] == {"schedule_case": True, "bundle_plan": None}
    assert {"agent": "SCHEDULE_REVIEW"} not in after["calls"]
    assert {c["agent"] for c in after["calls"]} == {"REPLANNING"}
    assert after["schedule_review"]["bundle_plan"] == {
        "bundle_plan_id": plan["bundle_plan_id"],
        "current": True,
        "bundles": [
            {
                "bundle_id": "B1",
                "group_ids": [first, second],
                "task_ids": ["P", "S1", "S2", "W"],
                "human_only": False,
            }
        ],
        "human_only_bundle_ids": [],
    }
    assert "quoted_opinion" not in str(after["schedule_review"])  # 모델 문장은 넣지 않는다
    assert [(s["guard"]["verdict"], s["guard"]["reason_code"]) for s in main_steps[:2]] == [
        ("ACCEPTED", None),
        ("REJECTED", "SAME_FACTS"),
    ]

    # 사실이 바뀌면(고정) 다시 부를 수 있고, 앞 묶음안은 지금 사실의 것이 아니다
    assert pin_task(pack, "planner_b", _key(), TaskRef(task_id="S1")).status == "APPLIED"
    with db.read() as conn:
        facts = casefacts.build(conn, pack, main)
        state = build_state(conn, pack, "supervisor")
    assert {"agent": "SCHEDULE_REVIEW"} in facts["calls"]
    assert facts["schedule_review"]["bundle_plan"]["current"] is False
    [shown] = state["bundle_plans"]
    assert (shown["bundle_plan_id"], shown["current"], shown["case_id"]) == (
        plan["bundle_plan_id"],
        False,
        main.case_id,
    )
    assert shown["quoted_opinion"] and shown["groups"] and shown["relations"]


def test_schedule_review_is_only_for_a_schedule_case_with_conflicts(seeded_real, main_on):
    """일정 넣기 사건이 없는 Case에서는 일정 검토를 부를 수 없다. 충돌이 없는 일정 Case도 부르지 않는다
    (지금처럼 재확인 후보가 된다)."""
    pack = seeded_real
    a = pack.new_task.model_dump(exclude={"requested", "unit_id", "owner_actor_id", "movable"})
    assert submit_task_request(pack, "planner_a", _key(), TaskRequestForm(**a)).status == "APPLIED"
    router = Router(main=[main_call("SCHEDULE_REVIEW"), main_escalate()])
    run_until_idle(pack, model_factory=router.factory())
    [main] = _runs("MAIN")
    first = _steps(main.run_id)[0]
    assert first["observation"]["groups"]  # 충돌은 있다
    assert first["observation"]["schedule_review"]["schedule_case"] is False
    assert {"agent": "SCHEDULE_REVIEW"} not in first["observation"]["calls"]
    assert (first["guard"]["verdict"], first["guard"]["reason_code"]) == (
        "REJECTED",
        "NOT_SCHEDULE_CASE",
    )
    assert _runs("SCHEDULE_REVIEW") == []


def test_schedule_case_without_conflicts_does_not_open_the_review(seeded_real, main_on):
    pack = seeded_real
    doc = _add(pack, _exported(pack), "S1", 240, 300)  # 첫날 오후: 부딪히는 것이 없다
    assert _import(pack, doc).status == "APPLIED"
    run_until_idle(pack, model_factory=Router().factory())
    [main] = _runs("MAIN")
    first = _steps(main.run_id)[0]["observation"]
    assert first["schedule_review"]["schedule_case"] is True and first["groups"] == []
    assert {"agent": "SCHEDULE_REVIEW"} not in first["calls"]
    assert _runs("SCHEDULE_REVIEW") == []
    with db.read() as conn:
        kinds = [r[0] for r in conn.execute("SELECT kind FROM candidate")]
    assert kinds == ["RECONFIRM"]


def test_human_only_bundle_is_marked_in_the_plan(seeded_real, main_on):
    """사람만 풀 수 있는 최소 묶음만 든 묶음은 묶음안에 표시되고 메인 관찰에 모여 보인다."""
    pack = seeded_real
    _import_two_conflicts(pack)
    for task_id in ("P", "S1"):
        assert pin_task(pack, "supervisor", _key(), TaskRef(task_id=task_id)).status == "APPLIED"
    _, groups = _groups(pack)
    pinned, free = (g.group_id for g in groups)
    router = Router(
        main=[main_call("SCHEDULE_REVIEW"), main_escalate()],
        schedule_review=[_submit([pinned], [free])],
    )
    run_until_idle(pack, model_factory=router.factory())
    [main] = _runs("MAIN")
    seen = _steps(main.run_id)[1]["observation"]["schedule_review"]["bundle_plan"]
    assert [(b["bundle_id"], b["human_only"]) for b in seen["bundles"]] == [
        ("B1", True),
        ("B2", False),
    ]
    assert seen["human_only_bundle_ids"] == ["B1"]


def test_return_result_takes_only_blocked(seeded_real, main_on):
    """RETURN_RESULT는 묶음안을 낼 수 없을 때만 쓴다: DONE은 받지 않고 Run은 계속된다."""
    pack = seeded_real
    _import_two_conflicts(pack)
    router = Router(
        main=[main_call("SCHEDULE_REVIEW"), main_escalate()],
        schedule_review=[done(), blocked("묶음을 정할 수 없다")],
    )
    run_until_idle(pack, model_factory=router.factory())
    [review] = _runs("SCHEDULE_REVIEW")
    steps = _steps(review.run_id)
    assert [(s["guard"]["verdict"], s["result_kind"]) for s in steps] == [
        ("REJECTED", "REJECTED"),
        ("ACCEPTED", "DONE"),
    ]
    assert (review.status, review.end_reason) == ("BLOCKED", "RETURN_BLOCKED")
    with db.read() as conn:
        assert list_bundle_plans(conn, pack.site_id) == []
    [main] = _runs("MAIN")
    [child] = _steps(main.run_id)[1]["observation"]["child_results"]
    assert (child["result"]["by"], child["result"]["status"]) == ("AGENT", "BLOCKED")
    assert "bundle_plan" not in child["result"]


def test_schedule_review_prompt_fingerprint():
    assert prompt.fingerprint() == prompt.PROMPT_FINGERPRINTS[prompt.PROMPT_VERSION]
    assert sorted(spec.ACTIONS) == ["RETURN_RESULT", "SUBMIT_BUNDLES"]  # 계산·고정·질문 도구가 없다


# ── 바뀐 사실: 요청 시작 범위 ──────────────────────────────────


def test_edited_start_range_is_listed_as_a_changed_fact(seeded, main_on):
    """카드에서 요청 시작 범위를 고친 것도 "기준 계획 확정 뒤 바뀐 사실"에 보인다. 고치기 전에 계산된
    안에는 보이지 않는다."""
    pack = seeded
    assert _intake(pack).status == "APPLIED"
    router = Router(intake=[_complete()], replanning=[solve("L0"), solve("L1")])
    run_until_idle(pack, model_factory=router.factory())
    with db.read() as conn:
        ctx = get_site(conn, pack.site_id).context_version
        assert base_range_changes(conn, pack.site_id, 0, ctx, {"A"}) == []  # 고친 적이 없다
        before = build_state(conn, pack, "supervisor")["candidates"]
    kinds = {x["kind"] for c in before for x in c["fact_changes"]}
    assert before and "BASE_RANGE_CHANGED" not in kinds

    body = EditRequest(task_id="A", base_start_max=120)
    assert edit_task(pack, "planner_a", _key(), body).status == "APPLIED"
    changed = {
        "kind": "BASE_RANGE_CHANGED",
        "task_id": "A",
        "before": {"start": 0, "start_max": 60, "origin": "STATED"},
        "after": {"start": 0, "start_max": 120, "origin": "STATED"},
    }
    with db.read() as conn:
        now = get_site(conn, pack.site_id).context_version
        assert base_range_changes(conn, pack.site_id, 0, now, {"A", "C"}) == [changed]
        # 고치기 전 시점까지만 보면 없고, 고친 뒤를 기준으로 삼으면 더 바뀐 것이 없다
        assert base_range_changes(conn, pack.site_id, 0, ctx, {"A"}) == []
        assert base_range_changes(conn, pack.site_id, now, now, {"A"}) == []

    # 메인이 깨어나 다시 재계획한다. 고친 뒤 계산된 안의 바뀐 사실 목록에 보인다
    run_until_idle(pack, model_factory=Router(replanning=[solve("L0"), solve("L1")]).factory())
    with db.read() as conn:
        after = build_state(conn, pack, "supervisor")["candidates"]
    fresh = [c for c in after if c["context_version"] == now]
    assert fresh and all(changed in c["fact_changes"] for c in fresh)
    stale = [c for c in after if c["context_version"] < now]
    assert stale and not any(changed in c["fact_changes"] for c in stale)
