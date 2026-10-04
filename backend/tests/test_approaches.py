"""여러 안 제안: 접근을 달리한 재계획, 목적 순서, 같은 안 표시, Supervisor가 고른 안만 협의 (AG-28·AG-29·CV-27).

Pack 파일 그대로(seeded_real)에서 요청 A를 넣은 장면을 쓴다.
"""

import uuid

from conftest import (
    add_run,
    add_task,
    choose,
    make_task,
    pin_tasks,
    reply_request,
    take_snapshot,
)
from scripted import (
    Router,
    ScriptedChatModel,
    call,
    cond,
    done,
    main_call,
    main_wait,
    solve,
    solve_with,
)

from app.agents import casefacts, runtime
from app.api.state import build_state
from app.commands.approval import (
    ApproveRequest,
    ChooseRequest,
    RejectAllRequest,
    RejectRequest,
    WaiveRequest,
    approve_and_commit,
    choose_candidate,
    reject_all,
    reject_candidate,
    waive,
)
from app.commands.task_request import TaskRequestForm, submit_task_request
from app.coordinator.dispatcher import run_until_idle
from app.domain.models import Condition
from app.rules.engine import detect_conflicts
from app.solver import cpsat
from app.solver.candidate import build_candidate
from app.solver.search_spec import build_search_spec
from app.store import db
from app.store.repos.calls import call_key
from app.store.repos.consultations import case_objections, consultation_view
from app.store.repos.decisions import case_rejection_reasons
from app.store.repos.records import get_candidate, list_validations
from app.store.repos.runs import get_run, list_steps
from app.store.repos.site import get_site
from app.validator.validator import validate

CONFLICT = {"conflict": {"rule_id": "SEP-LIFT-BELOW", "task_ids": ["A", "B"]}}


def _key():
    return uuid.uuid4().hex


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


def _steps(run_id):
    with db.read() as conn:
        return list_steps(conn, run_id)


def _guards(run_id):
    return [(s["action"]["name"], s["guard"]["reason_code"]) for s in _steps(run_id)]


def _candidate_ids():
    with db.read() as conn:
        return [r[0] for r in conn.execute("SELECT candidate_id FROM candidate ORDER BY rowid")]


def _choose(pack, actor, candidate_id):
    with db.read() as conn:
        v = list_validations(conn, pack.site_id, candidate_id)[-1]
    body = ChooseRequest(candidate_id=candidate_id, validation_id=v.validation_id)
    return choose_candidate(pack, actor, _key(), body)


def _events(kind):
    with db.read() as conn:
        return conn.execute(
            "SELECT ref, case_id FROM case_event WHERE kind = ? ORDER BY seq", (kind,)
        ).fetchall()


# ── 목적 순서 ──────────────────────────────────────────────────


def _first_day_busy():
    """목적 순서의 효과를 보려고 대체 자원(공용 크레인)은 첫날에 쓸 수 없게 둔다: 고정되지 않은 작업은
    자원도 움직이므로 그대로 두면 자원을 바꿔 풀린다 (AG-34)."""
    with db.write() as tx:
        tx.execute(
            "UPDATE resource SET available_intervals = '[[1440, 3360]]'"
            " WHERE resource_id = 'SITE-CR-01'"
        )


def test_delay_first_swaps_stages_and_is_a_different_search(seeded_real):
    pack = seeded_real
    _first_day_busy()
    add_task(pack, make_task(pack))
    pin_tasks(pack, ["B"])  # 목적 순서의 차이를 A·C에서 보려고 충돌 상대 B는 고정해 둔다
    snap = take_snapshot(pack)
    conflict = detect_conflicts(snap, snap.facts().check_assignments(), pack)[0]
    conds = {"C": Condition(start_min=60)}
    plain = build_search_spec(snap, [conflict], "L1", conds)
    spec = build_search_spec(snap, [conflict], "L1", conds, "DELAY_FIRST")
    assert (spec.objective, plain.objective) == ("DELAY_FIRST", "CHANGE_FIRST")
    assert spec.search_key != plain.search_key and spec.hash != plain.hash

    result = cpsat.solve(snap, spec, pack)
    # 1단계가 총 지연, 2단계가 그 지연 안에서 변경 작업 수다. 계획 밖이고 기준 위치가 없는 A는 어느
    # 쪽에도 세지 않는다: 계획에 있던 C(10:00→10:30)만 변경 하나에 지연 30분이다 (CV-29)
    assert (result.stage1["status"], result.stage1["delay"]) == ("OPTIMAL", 30)
    assert (result.stage2["delay"], result.stage2["changed"]) == (30, 1)
    change_first = cpsat.solve(snap, plain, pack)
    assert (change_first.stage1["changed"], change_first.stage2["delay"]) == (1, 30)
    assert "delay" not in change_first.stage1  # 변경 먼저의 결과 모양은 그대로다
    candidate = build_candidate(snap, spec, result)
    assert validate(snap, candidate, spec, pack).status == "PASS"


def test_objective_comes_from_the_approach(seeded_real):
    """목적 순서는 메인이 준 접근에서 서버가 채운다. 덜 옮기기 호출의 계산은 범위 계산이든 조건 계산이든
    지연 먼저로 풀리고, 조건 없이 조건 도구를 쓰는 것은 범위 계산과 같아 받지 않는다 (CV-27)."""
    pack = seeded_real
    _first_day_busy()
    add_task(pack, make_task(pack))
    pin_tasks(pack, ["B"])  # 목적 순서의 차이를 A·C에서 보려고 충돌 상대 B는 고정해 둔다
    add_run(pack, "run_1", input_ref={**CONFLICT, "approach": "MIN_DELAY"})
    replies = [
        solve_with("L1"),  # 조건 없이 푸는 것은 범위 계산과 같다
        solve("L0"),  # L0은 해가 없다
        solve("L0"),
        solve_with("L1", cond("C", start_from=60)),
    ]
    run = runtime.invoke(pack, {"run_id": "run_1"}, ScriptedChatModel(replies))
    steps = _steps("run_1")
    assert [(s["result_kind"], s["guard"]["reason_code"]) for s in steps] == [
        ("REJECTED", "CONDITION_INVALID"),
        ("CONTINUE", None),
        ("REJECTED", "ALREADY_TRIED"),
        ("WAIT", None),
    ]
    assert steps[0]["observation"]["approach"] == {
        "approach": "MIN_DELAY",
        "quoted_note": None,
        "objective": "DELAY_FIRST",
        "scope_level": None,
    }
    result = steps[3]["tool_result"]
    assert (result["objective"], result["stage1"]["delay"], result["stage2"]["changed"]) == (
        "DELAY_FIRST",
        30,
        1,
    )
    assert run.solver_calls_used == 2
    # 이전 계산에 목적 순서와, 건 조건이 도구 인자와 같은 모양(현장 날짜·시각 문자열)으로 보인다
    add_run(pack, "run_2", case_id="case_run_1", input_ref={**CONFLICT, "approach": "MIN_DELAY"})
    with db.read() as conn:
        from app.agents.observers import replanning as observer

        attempts = observer.build_observation(conn, pack, "run_2").data["attempts"]
    assert [(a["objective"], a["conditions_as_args"]) for a in attempts] == [
        ("DELAY_FIRST", []),
        ("DELAY_FIRST", [{"task_id": "C", "start_from": "2026-10-12(월) 10:00"}]),
    ]
    assert attempts[1]["stage1"] == {"status": "OPTIMAL", "changed": None, "delay": 30}


def test_replanning_cannot_choose_the_objective(seeded_real):
    """변경 최소 호출은 지연 먼저로 풀 수 없다: 도구에 목적 순서 인자가 없고, 계산은 변경 먼저다."""
    pack = seeded_real
    _first_day_busy()
    add_task(pack, make_task(pack))
    pin_tasks(pack, ["B"])
    add_run(pack, "run_1", input_ref={**CONFLICT, "approach": "MIN_CHANGE"})
    replies = [
        solve_with("L1", cond("C", start_from=60), objective="DELAY_FIRST"),
        solve_with("L1", cond("C", start_from=60)),
    ]
    runtime.invoke(pack, {"run_id": "run_1"}, ScriptedChatModel(replies))
    refused, solved = _steps("run_1")
    assert (refused["result_kind"], refused["guard"]["reason_code"]) == ("REJECTED", "MALFORMED")
    for tool in refused["available_actions"]:
        assert "objective" not in tool["function"]["parameters"]["properties"]
    assert refused["observation"]["approach"]["objective"] == "CHANGE_FIRST"
    result = solved["tool_result"]
    assert "objective" not in result and "delay" not in result["stage1"]  # 변경 먼저로 풀렸다
    assert (result["stage1"]["changed"], result["stage2"]["delay"]) == (1, 30)
    # 서버가 남기는 숫자: 기존 작업 변경 1(C), 추가 작업 변경 0(기준 없는 A는 세지 않는다), 옮긴 거리 30
    assert result["change_counts"] == {"existing": 1, "added": 0, "delay": 30}


# ── 접근을 달리한 재계획과 같은 안 ─────────────────────────────


def test_approach_is_in_call_key_and_same_placement_joins_candidate(seeded_real, main_on):
    """같은 접근·같은 사실의 재호출은 거절되고 다른 접근은 받아들여진다. 다른 접근이 같은 배치를 내면
    새 후보 없이 그 후보에 접근이 더해진다. 고르기 전에는 협의가 나가지 않는다."""
    pack = seeded_real
    # 두 접근이 같은 배치에 닿는 장면: C를 고정해 두면 A가 10:00에 공용 크레인을 쓰는 해 하나뿐이다
    # (C가 풀려 있으면 A와 C가 크레인을 맞바꾸는 해가 변경 수·지연이 같아 접근마다 다른 해가 나올 수 있다)
    pin_tasks(pack, ["C"])
    _submit_a(pack)
    # 호출 키는 접근뿐이다: 재계획은 현장의 충돌 전체를 푼다 (AG-24)
    assert call_key("REPLANNING", {"approach": "MIN_DELAY"}) == "REPLANNING:MIN_DELAY"

    def replan(approach=None, note=None):
        if approach is None:  # 접근 없이
            return call("CALL_AGENT", "부른다", agent="REPLANNING")
        return main_call("REPLANNING", approach=approach, approach_note=note)

    def consult_unchosen():
        return main_call("COORDINATION", phase="CONSULT", candidate_id=_candidate_ids()[0])

    router = Router(
        main=[
            replan("MIN_CHANGE"),
            replan("MIN_CHANGE"),
            replan("MIN_DELAY", "늦어지는 작업이 없게"),
            replan("MIN_COST"),  # 목록에 없는 접근
            replan("KEEP_EXISTING"),  # 방향은 일정 넣기 사건이 있는 Case에서만 열린다 (AG-28)
            replan(),
            consult_unchosen,
            main_wait(),
        ],
        replanning=[
            solve("L1"),
            done(),
            solve("L1", "이유: 지연을 먼저 줄인다/다음: 검증"),
            done(),
        ],
        auto_done=False,
    )
    run_until_idle(pack, model_factory=router.factory())
    [main] = _runs("MAIN")
    assert _guards(main.run_id) == [
        ("CALL_AGENT", None),
        ("CALL_AGENT", "SAME_FACTS"),  # 같은 접근, 사실이 그대로
        ("CALL_AGENT", None),  # 접근이 다르면 받아들여진다
        ("CALL_AGENT", "MALFORMED"),
        ("CALL_AGENT", "APPROACH_NOT_OPEN"),
        ("CALL_AGENT", "APPROACH_REQUIRED"),
        ("CALL_AGENT", "CANDIDATE_NOT_CHOSEN"),
        ("WAIT", None),
    ]
    assert (main.status, main.wait_kind, _runs("COORDINATION")) == (
        "WAITING_HUMAN",
        "HUMAN_DECISION",
        [],
    )
    first, second = _runs("REPLANNING")
    assert [r.input_ref["call_key"].rsplit(":", 1)[1] for r in (first, second)] == [
        "MIN_CHANGE",
        "MIN_DELAY",
    ]
    [cid] = _candidate_ids()  # 두 접근이 같은 배치를 냈다
    s1 = _steps(second.run_id)[0]
    obs = s1["observation"]
    assert obs["approach"] == {
        "approach": "MIN_DELAY",
        "quoted_note": "늦어지는 작업이 없게",
        "objective": "DELAY_FIRST",  # 목적 순서는 접근에서 서버가 채운다 (CV-27)
        "scope_level": None,
    }
    assert obs["approach_candidates"] == [
        {"no": 1, "approach": "MIN_CHANGE", "candidate_id": cid, "same": False}
    ]
    assert s1["tool_result"]["same_as_candidate_id"] == cid

    # 메인 관찰과 화면 상태: 그 후보에 접근 둘, 고르지 않음, 협의 호출 없음
    waiting = _steps(main.run_id)[-1]["observation"]
    [seen] = waiting["candidates"]
    assert (seen["approaches"], seen["chosen"]) == (["MIN_CHANGE", "MIN_DELAY"], False)
    assert not [c for c in waiting["calls"] if c["agent"] == "COORDINATION"]
    with db.read() as conn:
        state = build_state(conn, pack, "supervisor")
    view = next(c for c in state["candidates"] if c["candidate_id"] == cid)
    assert [(a["no"], a["approach"], a["same"]) for a in view["approaches"]] == [
        (1, "MIN_CHANGE", False),
        (2, "MIN_DELAY", True),
    ]
    assert view["approaches"][1]["quoted_reason"] == "이유: 지연을 먼저 줄인다/다음: 검증"
    # 안 번호: 결과마다 번호 하나이고, 두 결과가 같은 배치면 번호를 이어 보인다
    assert view["plan_label"] == "1안 + 2안"
    assert (view["chosen"], view["case_id"], view["solver"]["objective"]) == (
        False,
        main.case_id,
        "CHANGE_FIRST",
    )

    # Supervisor만 고른다. 고르면 사건이 되어 메인이 깨어나고, 고른 안만 협의가 나간다
    assert _choose(pack, "planner_a", cid).reason_codes == ("NOT_AUTHORIZED",)
    out = _choose(pack, "supervisor", cid)
    assert out.status == "APPLIED" and out.result_refs["replaced"] == []
    assert _choose(pack, "supervisor", cid).reason_codes == ("ALREADY_CHOSEN",)
    [(ref, case_id)] = _events("CANDIDATE_CHOSEN")
    assert case_id == main.case_id and cid in ref
    assert _runs("MAIN")[0].wake_seq == main.wake_seq + 1
    run_until_idle(pack, model_factory=Router().factory())
    [consult] = _runs("COORDINATION")
    assert (consult.input_ref["phase"], consult.input_ref["candidate_id"], consult.status) == (
        "CONSULT",
        cid,
        "WAITING_HUMAN",
    )
    with db.read() as conn:
        state = build_state(conn, pack, "supervisor")
    assert next(c for c in state["candidates"] if c["candidate_id"] == cid)["chosen"] is True


def test_choosing_another_plan_ends_the_open_consultation(seeded_real, main_on):
    """고른 안을 바꾸면 앞 안의 열린 협의는 정리되고(요청 취소) 새로 고른 안만 협의한다."""
    pack = seeded_real
    _submit_a(pack)
    replies = [solve("L1"), solve_with("L2", cond("M", start_at=2940)), done()]
    run_until_idle(pack, model_factory=Router(replanning=replies, auto_done=False).factory())
    first, second = _candidate_ids()
    assert _runs("COORDINATION") == []
    choose(pack, first)
    run_until_idle(pack, model_factory=Router().factory())
    [consult] = _runs("COORDINATION")
    assert (consult.input_ref["candidate_id"], consult.status) == (first, "WAITING_HUMAN")

    out = _choose(pack, "supervisor", second)
    assert out.status == "APPLIED" and out.result_refs["replaced"] == [first]
    [ended] = _runs("COORDINATION")
    assert (ended.status, ended.end_reason) == ("STALE", f"CHOICE_CHANGED:{second}")
    with db.read() as conn:
        requests = conn.execute(
            "SELECT candidate_id, status FROM message WHERE type = 'CHANGE_REQUEST' ORDER BY rowid"
        ).fetchall()
        state = build_state(conn, pack, "supervisor")
    assert [tuple(r) for r in requests] == [(first, "CANCELLED")]
    chosen = {c["candidate_id"]: c["chosen"] for c in state["candidates"]}
    assert (chosen[first], chosen[second]) == (False, True)
    assert set(state["review_queue"]) == {first, second}  # 고르지 않은 안은 그대로 둔다
    # 안 번호는 결과가 나온 순서다: 한 호출이 낸 두 안도 각각 다음 번호를 받는다
    views = {c["candidate_id"]: c for c in state["candidates"]}
    assert (views[first]["plan_label"], views[second]["plan_label"]) == ("1안", "2안")
    assert [a["no"] for cid in (first, second) for a in views[cid]["approaches"]] == [1, 1]

    run_until_idle(pack, model_factory=Router().factory())
    again = _runs("COORDINATION")[-1]
    assert (again.input_ref["candidate_id"], again.status) == (second, "WAITING_HUMAN")
    assert _choose(pack, "supervisor", first).status == "APPLIED"  # 다시 바꿀 수 있다

    # 안 번호는 고정이다: 앞 안이 거절되어도 남은 안의 번호가 당겨지지 않는다
    with db.read() as conn:
        v = list_validations(conn, pack.site_id, first)[-1]
    body = RejectRequest(
        candidate_id=first, validation_id=v.validation_id, reason_code="PREFERENCE"
    )
    assert reject_candidate(pack, "supervisor", _key(), body).status == "APPLIED"
    with db.read() as conn:
        state = build_state(conn, pack, "supervisor")
    views = {c["candidate_id"]: c for c in state["candidates"]}
    assert (views[first]["display_status"], views[first]["plan_label"]) == ("REJECTED", "1안")
    assert views[second]["plan_label"] == "2안"


def test_choice_change_and_objection_count_as_human_work(seeded_real, main_on):
    """처음 고르기는 흐름의 일부라 세지 않는다. 고른 안을 바꾸면 1, 그 안에 담당자 이견이 나면 1이고,
    이견이 난 안을 떠나 다시 고르는 것은 그 이견에서 이미 셌다 (AG-30)."""
    pack = seeded_real
    _submit_a(pack)
    replies = [solve("L1"), solve_with("L2", cond("M", start_at=2940)), done()]
    run_until_idle(pack, model_factory=Router(replanning=replies, auto_done=False).factory())
    first, second = _candidate_ids()
    [main] = _runs("MAIN")

    def count():
        with db.read() as conn:
            return casefacts.human_work(conn, pack.site_id, main.case_id)

    choose(pack, first)
    run_until_idle(pack, model_factory=Router().factory())
    assert count() == 0
    choose(pack, second)
    run_until_idle(pack, model_factory=Router().factory())
    assert count() == 1

    with db.read() as conn:
        group, owner = conn.execute(
            "SELECT request_group_id, to_actor_id FROM message WHERE type = 'CHANGE_REQUEST'"
            " AND status = 'OPEN' AND candidate_id = ?",
            (second,),
        ).fetchone()
    out = reply_request(pack, owner, group, "DECLINE", "그 시각은 안 됩니다")
    assert out.status == "APPLIED"
    assert count() == 2
    choose(pack, first)
    assert count() == 2


# ── 승인·수용은 고른 안에만, 죽은 안의 답은 LATE (AG-29·ST-15) ──


def _approve(pack, candidate_id):
    with db.read() as conn:
        v = list_validations(conn, pack.site_id, candidate_id)[-1]
        ctx = get_site(conn, pack.site_id).context_version
    body = ApproveRequest(
        candidate_id=candidate_id, validation_id=v.validation_id, expected_context_version=ctx
    )
    return approve_and_commit(pack, "supervisor", _key(), body)


def _requests(candidate_id):
    """그 안의 변경 요청 한 통들 [(request_group_id, 담당자, 상태들)]."""
    with db.read() as conn:
        found = conn.execute(
            "SELECT request_group_id, to_actor_id, status FROM message"
            " WHERE type = 'CHANGE_REQUEST' AND candidate_id = ? ORDER BY rowid",
            (candidate_id,),
        ).fetchall()
    out: dict[str, tuple[str, set[str]]] = {}
    for group, owner, status in found:
        out.setdefault(group, (owner, set()))[1].add(status)
    return [(group, owner, statuses) for group, (owner, statuses) in out.items()]


def test_approve_and_waive_only_on_the_chosen_plan(seeded_real, main_on):
    """승인과 협의 항목 수용은 Supervisor가 지금 고른 안에만 된다. 다른 안으로 가려면 그 안을 고른다.
    앞 안의 요청에 온 답은 LATE로만 남고 이견으로 쌓이지 않는다."""
    pack = seeded_real
    _submit_a(pack)
    replies = [solve("L1"), solve_with("L2", cond("M", start_at=2940)), done()]
    run_until_idle(pack, model_factory=Router(replanning=replies, auto_done=False).factory())
    first, second = _candidate_ids()
    [main] = _runs("MAIN")
    choose(pack, first)
    run_until_idle(pack, model_factory=Router().factory())
    [(group, owner, statuses)] = _requests(first)
    assert statuses == {"OPEN"}

    # 고르지 않은 2안: 승인도 수용도 거절된다. 거절(모두 거절 포함)은 고르지 않아도 된다
    with db.read() as conn:
        pending = [i.task_id for i in consultation_view(conn, pack.site_id, second).items]
    assert "CANDIDATE_NOT_CHOSEN" in _approve(pack, second).reason_codes
    body = WaiveRequest(candidate_id=second, task_ids=tuple(pending), comment="급함")
    assert waive(pack, "supervisor", _key(), body).reason_codes == ("CANDIDATE_NOT_CHOSEN",)

    # 2안을 고르면 1안의 열린 협의는 바로 정리되고, 2안은 수용한 뒤 승인된다
    choose(pack, second)
    assert _requests(first) == [(group, owner, {"CANCELLED"})]
    assert waive(pack, "supervisor", _key(), body).status == "APPLIED"
    assert _approve(pack, second).status == "APPLIED"
    assert "CANDIDATE_NOT_CHOSEN" in _approve(pack, first).reason_codes

    # 무효가 된 1안의 요청에 온 답: 기록만 남는다(LATE). 유효한 답도 이견 사유도 아니다
    late = reply_request(pack, owner, group, "DECLINE", "그 시각은 안 됩니다")
    assert late.status == "APPLIED" and late.result_refs["late"] is True
    assert _requests(first) == [(group, owner, {"LATE"})]
    with db.read() as conn:
        assert case_objections(conn, pack.site_id, main.case_id) == []


# ── 안 번호 (AG-29) ────────────────────────────────────────────


def _labels(pack):
    with db.read() as conn:
        state = build_state(conn, pack, "supervisor")
    return {c["candidate_id"]: c["plan_label"] for c in state["candidates"]}


def test_plan_numbers_follow_result_order_and_join_the_same_placement(seeded_real, main_on):
    """결과가 나온 순서대로 1안, 2안, 3안이다. 해가 없는 계산은 번호를 받지 않고, 이미 있는 안과 같은 배치를
    낸 결과도 번호를 받아 그 안에 "1안 + 3안"으로 이어 보인다. 거절 뒤에도 번호는 그대로다."""
    pack = seeded_real
    _submit_a(pack)

    def same_as_first():
        # 1안이 A를 놓은 자리를 조건으로 걸어 다시 푼다: 1안과 같은 배치가 나온다
        with db.read() as conn:
            first = get_candidate(conn, pack.site_id, _candidate_ids()[0])
        start = next(a.start for a in first.assignments if a.task_id == "A")
        return solve_with("L0", cond("A", start_at=start))

    replies = [
        # A·B를 지금 자리에 못 박으면 해가 없다: 번호를 받지 않는다
        solve_with("L0", cond("A", start_at=0), cond("B", start_at=0)),
        solve("L0"),  # 1안
        solve("L1"),  # 2안
        same_as_first,  # 1안과 같은 배치: 3안
        done(),
    ]
    run_until_idle(pack, model_factory=Router(replanning=replies, auto_done=False).factory())
    [rp] = _runs("REPLANNING")
    results = [s["tool_result"] for s in _steps(rp.run_id)[:4]]
    first, second = _candidate_ids()
    assert results[0]["stage1"]["status"] == "INFEASIBLE"
    assert [r["candidate_id"] for r in results] == [None, first, second, None]
    assert results[3]["same_as_candidate_id"] == first
    assert _labels(pack) == {first: "1안 + 3안", second: "2안"}

    # 거절·무효가 생겨도 번호는 바뀌지 않는다
    with db.read() as conn:
        v = list_validations(conn, pack.site_id, first)[-1]
    body = RejectRequest(candidate_id=first, validation_id=v.validation_id, reason_code="OTHER")
    assert reject_candidate(pack, "supervisor", _key(), body).status == "APPLIED"
    assert _labels(pack) == {first: "1안 + 3안", second: "2안"}


# ── 모두 거절 (AG-29) ──────────────────────────────────────────

REASON = "두 안 모두 오전 인양이 겹친다"


def _reject_all(pack, actor, comment=REASON, key=None, case_id=None):
    body = RejectAllRequest(case_id=case_id or _runs("MAIN")[0].case_id, comment=comment)
    return reject_all(pack, actor, key or _key(), body)


def test_reject_all_turns_down_every_live_plan_with_one_reason(seeded_real, main_on):
    """[모두 거절]: 이 Case의 살아 있는 안이 한 번에 거절된다. 안마다 하나씩 거절과 같은 처리(결정 기록,
    보낸 협의 요청 정리)이고, 사유는 한 번만 쌓이며 메인에게는 사건 하나, 사람이 만든 일도 하나다."""
    pack = seeded_real
    _submit_a(pack)
    replies = [solve("L1"), solve_with("L2", cond("M", start_at=2940)), done()]
    run_until_idle(pack, model_factory=Router(replanning=replies, auto_done=False).factory())
    first, second = _candidate_ids()
    [main] = _runs("MAIN")
    choose(pack, first)
    run_until_idle(pack, model_factory=Router().factory())
    [consult] = _runs("COORDINATION")
    assert (consult.input_ref["candidate_id"], consult.status) == (first, "WAITING_HUMAN")
    before = len(_events("CANDIDATE_DECIDED"))
    with db.read() as conn:
        assert casefacts.human_work(conn, pack.site_id, main.case_id) == 0

    # Supervisor만 한다. 사유는 꼭 적는다. 거절은 아무것도 바꾸지 않는다
    assert _reject_all(pack, "planner_a").reason_codes == ("NOT_AUTHORIZED",)
    assert _reject_all(pack, "supervisor", "  ").reason_codes == ("COMMENT_REQUIRED",)
    assert _reject_all(pack, "supervisor", case_id="case_none").reason_codes == (
        "NO_LIVE_CANDIDATE",
    )
    with db.read() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM decision WHERE type = 'REJECT'").fetchone()[0] == 0
        )

    out = _reject_all(pack, "supervisor", key="k-reject-all")
    assert out.status == "APPLIED" and out.result_refs["candidate_ids"] == [first, second]
    # 같은 키로 다시 보내도 효과는 한 번이다
    again = _reject_all(pack, "supervisor", key="k-reject-all")
    assert again.status == "REPLAYED" and again.result_refs == out.result_refs
    with db.read() as conn:
        decided = conn.execute(
            "SELECT candidate_id, reason_code, comment, batch_id FROM decision"
            " WHERE type = 'REJECT' ORDER BY rowid"
        ).fetchall()
        requests = conn.execute(
            "SELECT candidate_id, status FROM message WHERE type = 'CHANGE_REQUEST'"
        ).fetchall()
        state = build_state(conn, pack, "supervisor")
        reasons = case_rejection_reasons(conn, main.case_id)
        facts = casefacts.rejection_facts(conn, main.case_id)
        work = casefacts.human_work(conn, pack.site_id, main.case_id)
    # 안마다 결정 기록이 남고 같은 묶음이다. 고른 안의 협의는 끝나고 보낸 요청은 정리된다
    assert [(d[0], d[1], d[2]) for d in decided] == [
        (first, "OTHER", REASON),
        (second, "OTHER", REASON),
    ]
    assert {d[3] for d in decided} == {out.result_refs["batch_id"]}
    assert [tuple(r) for r in requests] == [(first, "CANCELLED")]
    assert _runs("COORDINATION")[0].status == "STALE"
    views = {c["candidate_id"]: c["display_status"] for c in state["candidates"]}
    assert (views[first], views[second], state["review_queue"]) == ("REJECTED", "REJECTED", [])
    # 사유는 한 번만 쌓인다 (CV-26). 메인에게는 사건 하나, 사람이 만든 일도 하나다 (AG-30)
    assert [(r["quoted_comment"], r["candidate_ids"]) for r in reasons] == [
        (REASON, [first, second])
    ]
    assert (facts["count"], work) == (1, 1)
    events = _events("CANDIDATE_DECIDED")
    assert len(events) == before + 1
    ref, case_id = events[-1]
    assert case_id == main.case_id and first in ref and second in ref
    # 더 거절할 살아 있는 안이 없다
    assert _reject_all(pack, "supervisor").reason_codes == ("NO_LIVE_CANDIDATE",)


def test_reject_all_api(seeded_real, main_on):
    from fastapi.testclient import TestClient

    from app.main import app

    pack = seeded_real
    _submit_a(pack)
    run_until_idle(pack, model_factory=Router(replanning=[solve("L1"), done()]).factory())
    [main] = _runs("MAIN")
    [cid] = _candidate_ids()
    with TestClient(app) as client:

        def post(actor, body):
            headers = {"X-Actor": actor, "Idempotency-Key": _key()}
            return client.post(f"/api/cases/{main.case_id}/reject-all", json=body, headers=headers)

        assert post("planner_a", {"comment": "다시"}).status_code == 403
        assert post("supervisor", {}).status_code == 422
        res = post("supervisor", {"comment": "다시"})
    assert res.status_code == 200 and res.json()["result_refs"]["candidate_ids"] == [cid]
