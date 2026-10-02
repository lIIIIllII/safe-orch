"""Replanning 이관 정책 (부록 A.30): ESCALATE_NO_SOLUTION은 다른 Action이 모두 닫혔을 때만 열린다.

시연값에 기대지 않는 합성 관찰로 Available Actions를 확인한다(작업 X·자원 R1/R2는 가상의 값). 모든
테스트가 만든 실제 관찰의 불변식은 conftest의 replanning_invariants가 따로 확인한다.
"""

from closing import ClosingModel, close_to_escalation
from scripted import escalate
from test_resume import _alpha_waiting as alpha_waiting
from test_resume import _ask_waiting, _names, _reject_demo, _run, _steps

from app.agents.specs import replanning as spec

ESCALATE = "ESCALATE_NO_SOLUTION"


def _obs(**changes):
    """주 충돌(X, Y)의 acting 작업 X(자원 필요, 자원 축 미확인)가 있는 합성 관찰. 기본은 계산할 범위가 남음."""
    base = {
        "conflicts": [{"rule_id": "RULE", "task_ids": ["X", "Y"]}],
        "primary_conflict": {"rule_id": "RULE", "task_ids": ["X", "Y"]},
        "acting_tasks": [
            {
                "task_id": "X",
                "required_resource_type": "TYPE",
                "movable": {"time": True, "resource": False},
            }
        ],
        "constraints": [],
        "assignable_resources": [],
        "human_replies": [],
        "untried_levels": ["L1", "L2"],
        "window_options": [],
        "budget_remaining": {"steps": 10, "llm_attempts": 20, "human_rounds": 2, "solver_calls": 4},
    }
    return {**base, **changes}


def _listing(*alternatives):
    return {
        "task_id": "X",
        "current": "R1",
        "assignable": [{"resource_id": "R1"}, *({"resource_id": r} for r in alternatives)],
        "excluded": [],
        "untried_alternatives": [],
    }


def _available(obs):
    out = spec.available_actions(obs)
    names = list(out)
    # 불변식 (A.30 4)
    assert ESCALATE not in names or names == [ESCALATE], names
    if obs["budget_remaining"]["solver_calls"] <= 0:
        assert names == [ESCALATE], names
    return out


def test_solver_zero_leaves_only_escalate():
    """Solver 0: 미시도 범위·조회 대상·질문 값이 있어도 이관만 열린다(조회·질문은 Solver를 여는 수단)."""
    obs = _obs(
        untried_levels=["L1"],
        budget_remaining={"steps": 10, "llm_attempts": 20, "human_rounds": 2, "solver_calls": 0},
    )
    assert list(_available(obs)) == [ESCALATE]
    listed = {**obs, "untried_levels": [], "assignable_resources": [_listing("R2")]}
    assert list(_available(listed)) == [ESCALATE]


def test_after_reject_only_list_is_open():
    """미시도 범위가 없고 조회하지 않은 작업이 있으면 조회만 열린다. 이관은 닫혀 있다."""
    assert _available(_obs(untried_levels=[])) == {"LIST_ASSIGNABLE_RESOURCES": {"task_id": ["X"]}}


def test_only_open_question_left_allows_escalate():
    """답을 기다리는 질문만 남으면(다시 관찰한 경우) 같은 작업·축 질문과 시간창 질문이 닫혀 이관만 열린다.

    한계(A.30): 이미 보낸 질문을 다시 기다리는 Action은 없다.
    """
    question = {
        "task_id": "X",
        "axis": "RESOURCE",
        "allowed_values": ["R2"],
        "status": "OPEN",
        "decision": None,
    }
    obs = _obs(untried_levels=[], assignable_resources=[_listing("R2")], human_replies=[question])
    assert list(_available(obs)) == [ESCALATE]


def test_no_primary_conflict():
    """주 충돌이 없으면 계산·조회가 닫힌다. 조회 결과가 없으면 이관만 열린다."""
    obs = _obs(conflicts=[], primary_conflict=None, untried_levels=[])
    assert list(_available(obs)) == [ESCALATE]
    # 한계(A.30): 주 충돌이 없어도 조회 결과가 있으면 자원 질문이 열릴 수 있다. 그때 이관은 닫힌다
    listed = {**obs, "assignable_resources": [_listing("R2")]}
    assert list(_available(listed)) == ["ASK_TASK_OWNER"]


def test_list_without_alternatives_closes_then_escalate():
    """대체 자원이 없어도 조회는 열린다(조회해야 안다). 조회 결과에 대체 자원이 없으면 이관만 남는다."""
    assert list(_available(_obs(untried_levels=[]))) == ["LIST_ASSIGNABLE_RESOURCES"]
    obs = _obs(untried_levels=[], assignable_resources=[_listing()])
    assert list(_available(obs)) == [ESCALATE]


def test_last_step_has_no_escalate_exception():
    """남은 step이 1이어도 열린 Action이 있으면 이관은 닫혀 있다(끝나면 BUDGET_EXHAUSTED, A.30 2)."""
    obs = _obs(
        untried_levels=[],
        budget_remaining={"steps": 1, "llm_attempts": 2, "human_rounds": 2, "solver_calls": 4},
    )
    assert list(_available(obs)) == ["LIST_ASSIGNABLE_RESOURCES"]


def test_escalate_while_closed_is_action_not_available(seeded):
    """도구에 없는 이관을 호출해도 Gateway가 이 tx에서 다시 관찰해 거절하고 Run은 계속된다 (A.30 1)."""
    pack = seeded
    run = alpha_waiting(pack)
    _reject_demo(pack, run.wait_ref)
    close_to_escalation(pack, run.run_id, ClosingModel([escalate("열린 조회가 있어도 이관")]))
    steps = _steps(run.run_id)
    s_esc = steps[2]
    assert _names(s_esc) == ["LIST_ASSIGNABLE_RESOURCES"]
    assert (s_esc["action"]["name"], s_esc["guard"]) == (
        ESCALATE,
        {"verdict": "REJECTED", "reason_code": "ACTION_NOT_AVAILABLE"},
    )
    assert steps[3]["observation"]["last_guard"]["reason_code"] == "ACTION_NOT_AVAILABLE"
    assert steps[3]["action"]["name"] == "LIST_ASSIGNABLE_RESOURCES"
    ended = _run(run.run_id)
    assert (ended.status, _names(steps[-1])) == ("ESCALATED", [ESCALATE])


def test_window_closed_when_solver_zero(seeded, solver_limit):
    """Solver가 0이면 시간창 선택지도 계산하지 않는다(window_gate, A.30 3)."""
    pack = seeded
    solver_limit(3)  # Alpha 2회 + 1회 남음: 조회·자원 질문까지 열린다
    waiting = _ask_waiting(pack)
    obs = _steps(waiting.run_id)[-1]["observation"]
    closed = {**obs, "budget_remaining": {**obs["budget_remaining"], "solver_calls": 0}}
    assert spec.window_gate({**closed, "human_replies": []}) is False


def test_invariant_check_detects_old_contract(with_a, monkeypatch, use_db_path, tmp_path):
    """conftest의 불변식 검사가 비어 있지 않다: 옛 계약(이관 언제나)으로 만든 관찰을 위반으로 잡는다."""
    from conftest import add_run, replanning_invariant_violations
    from scripted import ScriptedChatModel, solve

    from app.agents import runtime

    original = spec.available_actions
    monkeypatch.setattr(spec, "available_actions", lambda obs: {**original(obs), ESCALATE: {}})
    add_run(with_a, "run_old", input_ref={"conflict": {"rule_id": "SEP-LIFT-BELOW",
                                                       "task_ids": ["A", "B"]}})  # fmt: skip
    runtime.invoke(with_a, {"run_id": "run_old"}, ScriptedChatModel([solve("L1")]))
    violations = replanning_invariant_violations()
    assert violations and "ESCALATE with others" in violations[0]
    use_db_path(tmp_path / "empty.db")  # 이 테스트가 만든 위반 DB는 autouse 검사 대상에서 뺀다
