"""조건을 걸고 푸는 재계획, 사람이 남긴 사유, 같은 배치 묶기, 거절·이견된 변경 표시 (CV-24·25·26).

Pack 파일 그대로(seeded_real)에서 요청 A를 넣은 장면을 쓴다: A는 B(09:00–10:00) 뒤 10:00에만 들어가고
같은 크레인의 C(10:00–10:30)가 비켜야 한다. 조건이 없으면 C는 09:00으로 당겨진다(지연이 없으므로).
"""

import uuid

import pytest
from conftest import add_run, add_task, free_alternative, make_task, take_snapshot
from scripted import (
    Router,
    ScriptedChatModel,
    blocked,
    cond,
    done,
    main_call,
    main_wait,
    solve,
    solve_with,
)
from test_coordination import _alpha_consulting, _objected

from app.agents import casefacts, runtime
from app.agents.observers import replanning as replanning_observer
from app.api.state import build_state
from app.commands.approval import RejectRequest, reject_candidate
from app.commands.pins import PreferredWindow, TaskRef, pin_task, set_preferred_window
from app.commands.task_request import TaskRequestForm, submit_task_request
from app.coordinator.dispatcher import run_until_idle
from app.domain.models import Condition
from app.rules.engine import detect_conflicts
from app.solver import cpsat
from app.solver.candidate import build_candidate
from app.solver.search_spec import SearchSpecError, build_search_spec
from app.store import db
from app.store.repos.calls import fingerprint
from app.store.repos.consultations import contested_changes
from app.store.repos.records import get_candidate, list_validations
from app.store.repos.runs import get_run, list_steps
from app.validator.validator import validate

CONFLICT = {"conflict": {"rule_id": "SEP-LIFT-BELOW", "task_ids": ["A", "B"]}}


def _key():
    return uuid.uuid4().hex


@pytest.fixture
def real_a(seeded_real):
    """Pack 그대로의 R0 + 신규 작업 A. 조건의 효과를 보려고 대체 자원(공용 크레인)은 첫날에 쓸 수
    없게 둔다: 고정되지 않은 작업은 자원도 움직이므로 그대로 두면 자원을 바꿔 풀린다 (AG-34)."""
    add_task(seeded_real, make_task(seeded_real))
    with db.write() as tx:
        tx.execute(
            "UPDATE resource SET available_intervals = '[[1440, 3360]]'"
            " WHERE resource_id = 'SITE-CR-01'"
        )
    return seeded_real


def _spec(pack, snap, level, conditions=None):
    conflict = detect_conflicts(snap, snap.facts().check_assignments(), pack)[0]
    assert (conflict.rule_id, conflict.task_ids) == ("SEP-LIFT-BELOW", ("A", "B"))
    return build_search_spec(snap, conflict, "UA", level, conditions)


def _starts(result, *task_ids):
    by_id = {a["task_id"]: a["start"] for a in result.solution}
    return [by_id[t] for t in task_ids]


def _run(pack, replies, run_id="run_1"):
    add_run(pack, run_id, input_ref=CONFLICT)
    run = runtime.invoke(pack, {"run_id": run_id}, ScriptedChatModel(replies))
    with db.read() as conn:
        return run, list_steps(conn, run_id)


def _guards(steps):
    return [(s["result_kind"], (s["guard"] or {}).get("reason_code")) for s in steps]


# ── 조건은 좁히기만 한다 (Solver·SearchSpec) ───────────────────


def test_condition_moves_c_after_ten_instead_of_before(real_a):
    """목표 장면: 조건이 없으면 C는 09:00으로 가고, "C 시작 ≥ 10:00"을 걸면 A 10:00, C 10:30이 된다."""
    pack = real_a
    snap = take_snapshot(pack)
    plain = _spec(pack, snap, "L1")
    assert _starts(cpsat.solve(snap, plain, pack), "A", "C") == [60, 0]

    spec = _spec(pack, snap, "L1", {"C": Condition(start_min=60)})
    result = cpsat.solve(snap, spec, pack)
    assert _starts(result, "A", "C") == [60, 90]
    # 조건은 Solver 입력이다: 같은 범위라도 탐색 키와 hash가 다르고, 같은 조건이면 같다
    assert (spec.search_key, spec.hash) != (plain.search_key, plain.hash)
    again = _spec(pack, snap, "L1", {"C": Condition(start_min=60)})
    assert again.search_key == spec.search_key
    # 후보는 같은 Validator 경로를 탄다(SearchSpec hash에 조건이 들어간다)
    candidate = build_candidate(snap, spec, result)
    assert validate(snap, candidate, spec, pack).status == "PASS"
    tampered = spec.model_copy(update={"conditions": {}})
    codes = [c.reason_code for c in validate(snap, candidate, tampered, pack).checks]
    assert "SEARCH_SPEC_HASH_MISMATCH" in codes


def test_start_at_and_resource_conditions(real_a):
    pack = real_a
    snap = take_snapshot(pack)
    fixed = _spec(pack, snap, "L1", {"C": Condition(start_min=120, start_max=120)})
    assert _starts(cpsat.solve(snap, fixed, pack), "A", "C") == [60, 120]
    # 자원 지정: 기준 자원은 그대로 받는다
    same = _spec(pack, snap, "L1", {"A": Condition(resource_id="A-CR-01")})
    assert cpsat.solve(snap, same, pack).solution is not None


@pytest.mark.parametrize(
    ("level", "conditions", "reason"),
    [
        ("L1", {"C": Condition()}, "CONDITION_INVALID"),
        ("L1", {"C": Condition(start_min=90, start_max=60)}, "CONDITION_INVALID"),
        ("L1", {"A": Condition(start_min=120)}, "CONDITION_OUTSIDE_WINDOW"),  # A 시작 한도 10:00
        ("L0", {"C": Condition(start_min=60)}, "CONDITION_TASK_NOT_IN_SCOPE"),  # L0은 A만
        ("L2", {"B": Condition(start_min=60)}, "CONDITION_TASK_NOT_IN_SCOPE"),  # 다른 Unit
    ],
)
def test_condition_validity(real_a, level, conditions, reason):
    snap = take_snapshot(real_a)
    with pytest.raises(SearchSpecError, match=reason):
        _spec(real_a, snap, level, conditions)


def test_condition_on_pinned_task_and_ineligible_resource(real_a):
    pack = real_a
    snap = take_snapshot(pack)
    with pytest.raises(SearchSpecError, match="RESOURCE_NOT_ELIGIBLE"):
        _spec(pack, snap, "L1", {"A": Condition(resource_id="B-CR-01")})  # UA에 허용되지 않음
    # 고정되지 않은 작업은 적격 자원이면 자원을 지정할 수 있다(담당자 확인이 필요 없다, AG-34)
    free = free_alternative(snap)
    ok = _spec(pack, free, "L0", {"A": Condition(resource_id="SITE-CR-01")})
    result = cpsat.solve(free, ok, pack)
    assert next(a for a in result.solution if a["task_id"] == "A")["resource_id"] == "SITE-CR-01"

    assert pin_task(pack, "foreman_a2", _key(), TaskRef(task_id="C")).status == "APPLIED"
    pinned = take_snapshot(pack)
    with pytest.raises(SearchSpecError, match="TASK_PINNED"):
        _spec(pack, pinned, "L1", {"C": Condition(start_min=60)})


# ── 도구: 인자 유효성, 재시도, 위반 목록 ───────────────────────


def test_tool_rejects_invalid_conditions_without_solver_budget(real_a):
    replies = [
        solve_with("L1", cond("C", start_from="내일 10시")),
        solve_with("L1", cond("C", start_at=60, start_from=60)),
        solve_with("L1", cond("C", start_from=60), cond("C", start_until=120)),
        solve_with("L1", cond("C", use_preferred_window=True)),
        solve_with("L1", cond("X9", start_from=60)),
        solve_with("L0", cond("C", start_from=60)),
        solve_with("L1", cond("A", start_from=120)),
        blocked(),
    ]
    run, steps = _run(real_a, replies)
    assert [g[1] for g in _guards(steps)[:-1]] == [
        "TIME_INVALID",
        "CONDITION_INVALID",
        "CONDITION_INVALID",
        "NO_PREFERRED_WINDOW",
        "CONDITION_TASK_NOT_IN_SCOPE",
        "CONDITION_TASK_NOT_IN_SCOPE",
        "CONDITION_OUTSIDE_WINDOW",
    ]
    assert run.solver_calls_used == 0


def test_same_conditions_are_not_solved_twice_and_other_conditions_are(real_a):
    """같은 범위·같은 조건은 다시 풀지 않는다. 조건이 다르면 범위를 다 쓴 뒤에도 풀 수 있다."""
    both_at_ten = (cond("A", start_at=60), cond("C", start_at=60))
    replies = [
        solve("L0"),
        solve_with("L1", *both_at_ten),
        solve_with("L1", *both_at_ten),
        solve_with("L0", cond("A", start_at=60)),
        solve_with("L1", cond("C", start_from=60)),
    ]
    run, steps = _run(real_a, replies)
    assert _guards(steps) == [
        ("CONTINUE", None),  # L0 INFEASIBLE
        ("CONTINUE", None),  # A·C 모두 10:00: 같은 크레인이라 해가 없다
        ("REJECTED", "ALREADY_TRIED"),
        ("CONTINUE", None),  # L0을 이미 썼어도 조건이 있으면 다른 탐색이다
        ("WAIT", None),  # C 시작 ≥ 10:00 → 후보
    ]
    assert run.solver_calls_used == 4  # 거절된 재시도는 Budget을 쓰지 않는다
    # 같은 조건의 재시도는 그때의 결과를 돌려준다(어느 step이었는지, Solver 상태, 건 조건)
    again = steps[2]["tool_result"]
    assert again["first"] == {"run_id": run.run_id, "step_no": 2, "this_run": True}
    assert (again["scope_level"], again["stage1"]["status"]) == ("L1", "INFEASIBLE")
    assert sorted(again["conditions"]) == ["A", "C"]
    seen = steps[3]["observation"]["last_guard"]
    assert seen["reason_code"] == "ALREADY_TRIED"
    assert [c["task_id"] for c in seen["previous"]["conditions_as_args"]] == ["A", "C"]
    # 모두 지정한 배치였으면 그 배치가 어긴 규칙을 돌려준다
    s2 = steps[1]["tool_result"]
    assert s2["stage1"]["status"] == "INFEASIBLE"
    assert {"rule_id": "CAP-RESOURCE", "task_ids": ["A", "C"]} in s2["violations"]
    assert s2["conditions"]["C"] == {
        "start_min": 60,
        "start_max": 60,
        "resource_id": None,
        "preferred": False,
    }
    assert "violations" not in steps[4]["tool_result"]
    # 관찰의 이전 계산에 건 조건이 보인다
    last = steps[4]["observation"]["attempts"]
    assert [bool(a["conditions"]) for a in last] == [False, True, True]
    with db.read() as conn:
        cand = get_candidate(conn, real_a.site_id, steps[4]["tool_result"]["candidate_id"])
    placed = {a.task_id: a.start for a in cand.assignments}
    assert (placed["A"], placed["C"]) == (60, 90)


def test_preferred_window_becomes_start_range(real_a):
    """희망 영역은 Agent가 조건으로 넣을 때만 Solver 입력이 된다. 서버는 강제하지 않는다."""
    pack = real_a
    body = PreferredWindow(task_id="C", start=120, end=180)  # 11:00–12:00
    assert set_preferred_window(pack, "foreman_a2", _key(), body).status == "APPLIED"
    _, steps = _run(pack, [solve_with("L1", cond("C", use_preferred_window=True))])
    result = steps[0]["tool_result"]
    assert result["conditions"]["C"] == {
        "start_min": 120,
        "start_max": 150,  # 끝 − duration
        "resource_id": None,
        "preferred": True,
    }
    with db.read() as conn:
        cand = get_candidate(conn, pack.site_id, result["candidate_id"])
    assert {a.task_id: a.start for a in cand.assignments}["C"] == 120
    acting = {t["task_id"]: t for t in steps[0]["observation"]["acting_tasks"]}
    assert acting["C"]["clock"]["base_start"] == "2026-10-12(월) 10:00"


# ── 같은 배치는 하나로 묶는다 ─────────────────────────────────


def _submit_a(pack):
    a = pack.new_task.model_dump(exclude={"requested", "unit_id", "owner_actor_id", "movable"})
    assert submit_task_request(pack, "planner_a", _key(), TaskRequestForm(**a)).status == "APPLIED"


def _runs(agent_type):
    with db.read() as conn:
        ids = [
            r[0]
            for r in conn.execute(
                "SELECT run_id FROM agent_run WHERE agent_type = ? ORDER BY rowid", (agent_type,)
            )
        ]
        return [get_run(conn, rid) for rid in ids]


def _candidate_ids():
    with db.read() as conn:
        return [r[0] for r in conn.execute("SELECT candidate_id FROM candidate ORDER BY rowid")]


def test_same_placement_reaches_existing_candidate(seeded_real, main_on):
    """L1과 L2가 같은 배치를 내면 후보는 하나다. 둘째 시도는 기존 후보에 도달했다고 남는다."""
    pack = seeded_real
    _submit_a(pack)
    router = Router(replanning=[solve("L1"), solve("L2"), done()], auto_done=False)
    run_until_idle(pack, model_factory=router.factory())
    [rp] = _runs("REPLANNING")
    with db.read() as conn:
        s1, s2, s3 = list_steps(conn, rp.run_id)
        jobs = conn.execute(
            "SELECT step_no, same_candidate_id FROM solver_job WHERE run_id = ? ORDER BY step_no",
            (rp.run_id,),
        ).fetchall()
    [cid] = _candidate_ids()
    assert s1["tool_result"]["candidate_id"] == cid
    assert (s2["result_kind"], s2["guard"]["reason_code"]) == ("CONTINUE", "SAME_AS_EXISTING")
    assert (s2["tool_result"]["candidate_id"], s2["tool_result"]["same_as_candidate_id"]) == (
        None,
        cid,
    )
    assert [tuple(j) for j in jobs] == [(1, None), (2, cid)]
    attempts = s3["observation"]["attempts"]
    assert [(a["candidate_id"], a["same_as_candidate_id"]) for a in attempts] == [
        (cid, None),
        (None, cid),
    ]
    assert s3["observation"]["untried_levels"] == ["L0"]  # L2는 해 본 탐색이다
    assert (rp.status, s3["tool_result"]["candidate_ids"]) == ("SUCCEEDED", [cid])


# ── 사람이 남긴 사유와 거절·이견된 변경 ────────────────────────


def test_objection_is_in_observation_fingerprint_and_marks_candidate(seeded, main_on):
    """담당자 이견은 Case 동안 재계획 관찰에 남고, 재계획 재호출의 사실 지문을 바꾸며, 그 변경을 담은
    살아 있는 후보에 표시된다. 서버는 그 후보를 거절하지 않는다."""
    pack = seeded
    rp, _ = _alpha_consulting(pack)
    alpha = rp.wait_ref
    other_key = "REPLANNING:grp_x:UB"
    with db.read() as conn:
        before = fingerprint(conn, pack.site_id, other_key, None)
    _objected(pack)
    with db.read() as conn:
        after = fingerprint(conn, pack.site_id, other_key, None)
        cand = get_candidate(conn, pack.site_id, alpha)
        marks = contested_changes(conn, pack.site_id, cand)
        consult_key = fingerprint(conn, pack.site_id, "COORDINATION:CONSULT:none", None)
    assert before != after and consult_key != after
    assert marks == [{"task_id": "C", "by": "OBJECTION"}]

    add_run(pack, "run_next", case_id=rp.case_id, input_ref=CONFLICT)
    with db.read() as conn:
        obs = replanning_observer.build_observation(conn, pack, "run_next").data
        main_view = casefacts.candidate_view(conn, pack, alpha)
        state = build_state(conn, pack, "supervisor")
    [objection] = obs["objections"]
    assert (objection["candidate_id"], objection["task_id"], objection["owner_actor_id"]) == (
        alpha,
        "C",
        "foreman_a2",
    )
    assert (objection["before"]["start"], objection["after"]["start"]) == (60, 90)
    assert objection["quoted_comment"] == "작업발판 연계 공정 확정"
    assert "APPLY_REJECTION" in obs["open_skills"]  # 이견만 있어도 열린다
    assert obs["live_candidates"] == [{"candidate_id": alpha, "contested": marks}]
    assert (main_view["live"], main_view["contested"]) == (True, marks)
    view = next(c for c in state["candidates"] if c["candidate_id"] == alpha)
    assert (view["display_status"], view["contested"], view["conditions"]) == ("OPEN", marks, [])


def test_rejected_change_marks_other_live_candidate_and_conditions_solve_again(
    seeded_real, main_on
):
    """목표 장면: 거절된 후보의 C 변경과 같은 변경을 담은 다른 후보에 표시가 붙고, 재계획이 사유를 읽어
    "C 시작 ≥ 10:00"을 걸면 새 안(A 10:00, C 10:30)이 나온다."""
    pack = seeded_real
    # 이 장면은 C를 옮겨 푸는 안을 본다. 대체 자원은 첫날에 쓸 수 없게 둔다(자원을 바꿔 풀리지 않게)
    with db.write() as tx:
        tx.execute(
            "UPDATE resource SET available_intervals = '[[1440, 3360]]'"
            " WHERE resource_id = 'SITE-CR-01'"
        )
    _submit_a(pack)
    replies = [solve("L1"), solve_with("L2", cond("M", start_at=2940)), done()]
    router = Router(replanning=replies, auto_done=False)
    run_until_idle(pack, model_factory=router.factory())
    first, second = _candidate_ids()  # 배치가 다르다(M). 둘 다 C를 09:00으로 옮긴다
    with db.read() as conn:
        v = list_validations(conn, pack.site_id, first)[-1]
        assert (
            contested_changes(conn, pack.site_id, get_candidate(conn, pack.site_id, second)) == []
        )
    comment = "c작업은 오전 10시 이후로만 작업 가능"
    body = RejectRequest(
        candidate_id=first,
        validation_id=v.validation_id,
        reason_code="TIME_WINDOW_UNACCEPTABLE",
        target_task_ids=("C",),
        comment=comment,
    )
    assert reject_candidate(pack, "supervisor", _key(), body).status == "APPLIED"
    with db.read() as conn:
        other = get_candidate(conn, pack.site_id, second)
        assert contested_changes(conn, pack.site_id, other) == [{"task_id": "C", "by": "REJECTION"}]
        state = build_state(conn, pack, "supervisor")
    view = next(c for c in state["candidates"] if c["candidate_id"] == second)
    assert view["display_status"] == "OPEN"  # 서버가 같이 거절하지 않는다
    assert view["conditions"] == [
        {
            "task_id": "M",
            "start_min": 2940,
            "start_max": 2940,
            "resource_id": None,
            "preferred": False,
        }
    ]

    # 메인이 재계획을 다시 부르고, 재계획이 거절 사유를 보고 조건을 걸어 다시 푼다
    [main] = _runs("MAIN")
    with db.read() as conn:
        group = list_steps(conn, main.run_id)[0]["action"]["args"]["group_id"]
    router = Router(
        main=[main_call("REPLANNING", group_id=group, acting_unit_id="UA"), main_wait()],
        replanning=[solve_with("L1", cond("C", start_from=60)), done()],
        auto_done=False,
    )
    run_until_idle(pack, model_factory=router.factory())
    again = _runs("REPLANNING")[-1]
    with db.read() as conn:
        s1 = list_steps(conn, again.run_id)[0]
        cand = get_candidate(conn, pack.site_id, s1["tool_result"]["candidate_id"])
        fresh = contested_changes(conn, pack.site_id, cand)
    obs = s1["observation"]
    # 조건 없는 L2는 아직 풀지 않았다(앞의 L2는 M에 조건을 건 다른 탐색이다)
    assert obs["untried_levels"] == ["L0", "L2"]
    assert obs["rejections"][0]["quoted_comment"] == comment
    assert {"candidate_id": second, "contested": [{"task_id": "C", "by": "REJECTION"}]} in obs[
        "live_candidates"
    ]
    placed = {a.task_id: a.start for a in cand.assignments}
    assert (placed["A"], placed["C"]) == (60, 90)
    assert fresh == [] and again.status == "SUCCEEDED"
