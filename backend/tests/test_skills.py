"""스킬 층 (AG-01~04·AG-18·AG-19, CV-15). 열림 조건·도구 계산·Gateway 스킬 검사·자원 적격성."""

from conftest import add_run, take_snapshot, with_facts
from langchain_core.messages import AIMessage
from scripted import ScriptedChatModel, call, escalate

from app.agents import runtime, skills
from app.agents.observers.replanning import assignable_resources, build_observation
from app.agents.registry import BINDINGS
from app.agents.specs import coordination, event_response, intake, replanning
from app.store import db
from app.store.repos.runs import list_steps

CONFLICT = {"conflict": {"rule_id": "SEP-LIFT-BELOW", "task_ids": ["A", "B"]}}


def _run(pack, replies, run_id="run_1"):
    add_run(pack, run_id, input_ref=CONFLICT)
    run = runtime.invoke(pack, {"run_id": run_id}, ScriptedChatModel(replies))
    with db.read() as conn:
        return run, list_steps(conn, run_id)


def _reasons(steps):
    return [(s["result_kind"], s["guard"]["reason_code"]) for s in steps]


# ── 정의 ───────────────────────────────────────────────────────


def test_every_agent_tool_belongs_to_one_of_its_skills():
    for binding in BINDINGS.values():
        spec = binding.spec
        assert all(s in skills.SKILLS for s in spec.skills)
        for tool in spec.actions:
            assert any(tool in skills.SKILLS[s].tools for s in spec.skills), tool


def test_skill_guides_have_no_pack_values(pack):
    """지침은 일반 규칙만 쓴다. 작업·자원·Actor·구역 ID와 작업 유형 이름이 없다."""
    text = "\n".join(" ".join((s.title, s.opens_text, *s.guide)) for s in skills.SKILLS.values())
    ids = [r.resource_id for r in pack.resources] + [a.actor_id for a in pack.actors]
    ids += [w.display_name for w in pack.work_types.values()] + list(pack.work_types)
    assert [i for i in ids if i in text] == []


def test_system_lists_skill_guides_and_observation_has_open_skills(with_a):
    for binding in BINDINGS.values():
        system = binding.prompt.render_system(with_a)
        for sid in binding.spec.skills:
            assert f"- {sid}({skills.SKILLS[sid].title})" in system
            assert skills.SKILLS[sid].guide[0] in system
    add_run(with_a, "run_o", input_ref=CONFLICT)
    with db.read() as conn:
        obs = build_observation(conn, with_a, "run_o")
    assert obs.data["open_skills"] == ["ASSESS", "BUILD_CANDIDATE", "ASK_OWNER_TEMP", "WRAP_UP"]
    assert "eligible" not in obs.data  # 자원 적격성은 모델에 보이지 않는다
    assert obs.available["SOLVE_WITH_SCOPE"]["skill"] == ["BUILD_CANDIDATE"]


# ── 열림 조건(사실)과 도구 계산 ────────────────────────────────


def test_skills_open_on_facts_only():
    facts = {"has_conflict": True, "has_rejection": False}
    assert skills.open_skills(replanning.SKILLS, facts) == ["ASSESS", "BUILD_CANDIDATE", "WRAP_UP"]
    facts = {"has_conflict": True, "has_rejection": True, "has_unconfirmed_axis": True}
    assert skills.open_skills(replanning.SKILLS, facts) == list(replanning.SKILLS)
    assert skills.open_skills(coordination.SKILLS, {}) == ["WRAP_UP"]
    assert skills.open_skills(coordination.SKILLS, {"has_unsent_notice": True}) == [
        "NOTIFY",
        "WRAP_UP",
    ]
    assert skills.open_skills(event_response.SKILLS, {"has_change": True, "has_hold": True}) == [
        "ASSESS",
        "IMPACT",
        "ASK_REPORTER",
        "FACT_UPDATE",
        "WRAP_UP",
    ]
    assert skills.open_skills(intake.SKILLS, {}) == ["ASSESS", "WRAP_UP"]


def test_available_is_open_skill_tools_with_valid_arguments():
    valid = {
        "SOLVE_WITH_SCOPE": {"level": ["L1"]},
        "LIST_ASSIGNABLE_RESOURCES": {"task_id": ["A"]},
        "ASK_TASK_OWNER": {"task_id": ["A"]},
        "ESCALATE_NO_SOLUTION": {},
    }
    facts = {"has_conflict": True, "has_rejection": True, "has_unconfirmed_axis": False}
    out = skills.available(replanning.SKILLS, facts, valid)
    # 유효한 인자 값이 없는 도구(TRY)와 열리지 않은 스킬의 도구(ASK)는 빠진다
    assert list(out) == ["SOLVE_WITH_SCOPE", "LIST_ASSIGNABLE_RESOURCES", "ESCALATE_NO_SOLUTION"]
    assert out["SOLVE_WITH_SCOPE"] == {
        "level": ["L1"],
        "skill": ["BUILD_CANDIDATE", "APPLY_REJECTION"],
    }
    assert out["ESCALATE_NO_SOLUTION"] == {"skill": ["WRAP_UP"]}


def test_replanning_skill_facts(with_a):
    add_run(with_a, "run_f", input_ref=CONFLICT)
    with db.read() as conn:
        data = build_observation(conn, with_a, "run_f").data
    assert replanning.skill_facts(data) == {
        "has_conflict": True,
        "has_rejection": False,
        "has_unconfirmed_axis": True,
    }
    assert "APPLY_REJECTION" in replanning.open_skills({**data, "rejections": [{"x": 1}]})
    assert "BUILD_CANDIDATE" not in replanning.open_skills({**data, "conflicts": []})


# ── Gateway 스킬 검사 ──────────────────────────────────────────


def test_skill_not_open_and_tool_not_in_skill_are_rejected(with_a):
    run, steps = _run(
        with_a,
        [
            call("SOLVE_WITH_SCOPE", skill="APPLY_REJECTION", level="L0"),  # 거절이 없어 닫힌 스킬
            call("SOLVE_WITH_SCOPE", skill="NO_SUCH_SKILL", level="L0"),
            call("SOLVE_WITH_SCOPE", skill="WRAP_UP", level="L0"),  # 열려 있지만 이 도구가 없다
            escalate(),
        ],
    )
    assert _reasons(steps) == [
        ("REJECTED", "SKILL_NOT_OPEN"),
        ("REJECTED", "SKILL_NOT_OPEN"),
        ("REJECTED", "TOOL_NOT_IN_SKILL"),
        ("DONE", None),
    ]
    assert (
        steps[0]["action"]["args"]["skill"] == "APPLY_REJECTION"
    )  # 고른 스킬은 step JSON에 남는다
    assert steps[1]["observation"]["last_guard"]["reason_code"] == "SKILL_NOT_OPEN"
    assert (run.status, run.end_reason, run.solver_calls_used) == (
        "ESCALATED",
        "ESCALATE_NO_SOLUTION",
        0,
    )


def test_skill_rejections_do_not_count_as_malformed_twice(with_a):
    """형식 오류 사이에 스킬 선택 실수가 끼어도, 스킬 선택 실수가 이어져도 Run을 끝내지 않는다 (AG-19)."""
    text = AIMessage(content="L1로 하겠습니다")
    wrong = call("SOLVE_WITH_SCOPE", skill="APPLY_REJECTION", level="L0")
    run, steps = _run(with_a, [text, wrong, wrong, text, escalate()])
    assert [g for _, g in _reasons(steps)] == [
        "MALFORMED",
        "SKILL_NOT_OPEN",
        "SKILL_NOT_OPEN",
        "MALFORMED",
        None,
    ]
    assert (run.status, run.end_reason) == ("ESCALATED", "ESCALATE_NO_SOLUTION")


def test_missing_skill_argument_is_malformed(with_a):
    no_skill = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "SOLVE_WITH_SCOPE",
                "args": {"decision_summary": "계산", "level": "L0"},
                "id": "x",
            }
        ],
    )
    _, steps = _run(with_a, [no_skill, escalate()])
    assert _reasons(steps)[0] == ("REJECTED", "MALFORMED")


# ── 자원 적격성 (CV-15) ────────────────────────────────────────


def _ask(*values):
    return call(
        "ASK_TASK_OWNER", task_id="A", axis="RESOURCE", allowed_values=list(values), question="?"
    )


def test_resource_eligibility_type_permission_and_availability(with_a):
    add_run(with_a, "run_e", input_ref=CONFLICT)
    with db.read() as conn:
        obs = build_observation(conn, with_a, "run_e")
    # 조회하지 않아도 서버는 쓸 수 있는 자원을 안다: 유형이 같고 사용 권한이 있는 것
    assert obs.data["assignable_resources"] == []
    assert obs.hidden["eligible"]["A"]["alternatives"] == ["SITE-CR-01"]
    snapshot = take_snapshot(with_a)
    facts = snapshot.facts()
    a = facts.task_map()["A"]
    listed = assignable_resources(facts, a, "UA")
    assert [r["resource_id"] for r in listed["assignable"]] == ["A-CR-01", "SITE-CR-01"]
    assert listed["excluded"] == [{"resource_id": "B-CR-01", "reason": "NOT_ALLOWED"}]  # 권한
    assert "SITE-GC-01" not in str(listed)  # 유형이 다르면 대상이 아니다
    closed = [
        r.model_copy(update={"available_intervals": ()}) if r.resource_id == "SITE-CR-01" else r
        for r in facts.resources
    ]
    no_slot = assignable_resources(with_facts(snapshot, resources=tuple(closed)).facts(), a, "UA")
    assert {"resource_id": "SITE-CR-01", "reason": "NO_AVAILABILITY"} in no_slot["excluded"]  # 가용


def test_ineligible_resource_is_rejected_at_execution(with_a):
    """쓰거나 묻는 자원은 실행 때 적격성을 검사한다. 조회했는지는 보지 않는다."""
    run, steps = _run(
        with_a,
        [_ask("B-CR-01"), _ask("SITE-GC-01"), _ask("SITE-CR-01", "B-CR-01"), _ask("SITE-CR-01")],
    )
    assert _reasons(steps) == [
        ("REJECTED", "RESOURCE_NOT_ELIGIBLE"),  # 사용 권한 없음
        ("REJECTED", "RESOURCE_NOT_ELIGIBLE"),  # 유형이 다름
        ("REJECTED", "RESOURCE_NOT_ELIGIBLE"),  # 하나라도 쓸 수 없으면 거절
        ("WAIT", None),  # 조회 없이도 쓸 수 있는 자원이면 받는다
    ]
    assert (run.status, run.human_rounds_used) == ("WAITING_HUMAN", 1)
