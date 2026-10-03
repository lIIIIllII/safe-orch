"""스킬 층 (AG-01·AG-02·AG-19, CV-15). 열림 조건·도구 계산·Gateway 스킬 검사·자원 적격성."""

from conftest import add_run, take_snapshot, with_facts
from langchain_core.messages import AIMessage
from scripted import ScriptedChatModel, call, escalate

from app.agents import runtime, skills
from app.agents.needs import ask_refusals
from app.agents.observers.replanning import assignable_resources, build_observation
from app.agents.registry import BINDINGS
from app.agents.specs import coordination, event_response, intake, replanning
from app.domain.models import Requirement
from app.domain.needs import Need
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
    assert obs.data["open_skills"] == ["ASSESS", "BUILD_CANDIDATE", "WRAP_UP"]
    assert "eligible" not in obs.data  # 자원 적격성은 모델에 보이지 않는다
    assert obs.available["SOLVE_WITH_SCOPE"]["skill"] == ["BUILD_CANDIDATE"]


# ── 열림 조건(사실)과 도구 계산 ────────────────────────────────


def test_skills_open_on_facts_only():
    facts = {"has_conflict": True, "has_rejection": False}
    assert skills.open_skills(replanning.SKILLS, facts) == ["ASSESS", "BUILD_CANDIDATE", "WRAP_UP"]
    facts = {"has_conflict": True, "has_rejection": True}
    assert skills.open_skills(replanning.SKILLS, facts) == list(replanning.SKILLS)
    assert skills.open_skills(coordination.SKILLS, {"has_ask_need": True}) == [
        "PRE_CONFIRM",
        "WRAP_UP",
    ]
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
        "RETURN_RESULT": {},
    }
    facts = {"has_conflict": True, "has_rejection": True}
    out = skills.available(replanning.SKILLS, facts, valid)
    # 유효한 인자 값이 없는 도구(TRY)는 빠진다
    assert list(out) == ["SOLVE_WITH_SCOPE", "LIST_ASSIGNABLE_RESOURCES", "RETURN_RESULT"]
    # 열리지 않은 스킬의 도구(사전 확인이 없을 때의 ASK_OWNER)도 빠진다
    asking = {"ASK_OWNER": {"need_id": ["n1"]}, "RETURN_RESULT": {}}
    assert list(skills.available(coordination.SKILLS, {}, asking)) == ["RETURN_RESULT"]
    assert skills.available(coordination.SKILLS, {"has_ask_need": True}, asking)["ASK_OWNER"] == {
        "need_id": ["n1"],
        "skill": ["PRE_CONFIRM"],
    }
    assert out["SOLVE_WITH_SCOPE"] == {
        "level": ["L1"],
        "skill": ["BUILD_CANDIDATE", "APPLY_REJECTION"],
    }
    assert out["RETURN_RESULT"] == {"skill": ["WRAP_UP"]}


def test_replanning_skill_facts(with_a):
    add_run(with_a, "run_f", input_ref=CONFLICT)
    with db.read() as conn:
        data = build_observation(conn, with_a, "run_f").data
    assert replanning.skill_facts(data) == {
        "has_conflict": True,
        "has_rejection": False,
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
        "BLOCKED",
        "RETURN_BLOCKED",
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
    assert (run.status, run.end_reason) == ("BLOCKED", "RETURN_BLOCKED")


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
    assert listed["excluded"] == [
        {"resource_id": "B-CR-01", "reasons": [{"reason": "NOT_ALLOWED"}]}  # 권한
    ]
    assert "SITE-GC-01" not in str(listed)  # 유형이 다르면 대상이 아니다
    closed = [
        r.model_copy(update={"available_intervals": ()}) if r.resource_id == "SITE-CR-01" else r
        for r in facts.resources
    ]
    no_slot = assignable_resources(with_facts(snapshot, resources=tuple(closed)).facts(), a, "UA")
    assert {"resource_id": "SITE-CR-01", "reasons": [{"reason": "NO_AVAILABILITY"}]} in no_slot[
        "excluded"
    ]  # 가용


def test_listing_reports_zone_and_requirement_reasons(with_a):
    """자원 조회의 제외 사유에 구역·요구 조건(어느 속성인지)이 나온다 (CV-20)."""
    snapshot = take_snapshot(with_a)

    def listed(**changes):
        tasks = tuple(
            t.model_copy(update=changes) if t.task_id == "A" else t for t in snapshot.facts().tasks
        )
        facts = with_facts(snapshot, tasks=tasks).facts()
        return assignable_resources(facts, facts.task_map()["A"], "UA")

    in_d = listed(zone_id="D")  # A-CR-01은 B·C, SITE-CR-01은 B·C·D, B-CR-01은 B·D
    assert in_d["assignable"] == [{"resource_id": "SITE-CR-01"}]
    assert in_d["excluded"] == [
        {"resource_id": "A-CR-01", "reasons": [{"reason": "ZONE_NOT_ALLOWED"}]},
        {"resource_id": "B-CR-01", "reasons": [{"reason": "NOT_ALLOWED"}]},
    ]
    need = (
        Requirement(attribute="max_load", op="GTE", value=40),
        Requirement(attribute="usage", op="CONTAINS", value="블록"),
    )
    heavy = listed(resource_requirements=need[:1])
    assert heavy["assignable"] == [{"resource_id": "SITE-CR-01"}]
    assert heavy["excluded"][0] == {
        "resource_id": "A-CR-01",
        "reasons": [{"reason": "REQUIREMENT_NOT_MET", "attribute": "max_load"}],
    }
    both = listed(zone_id="D", resource_requirements=need)
    assert both["assignable"] == []
    assert {x["resource_id"]: x["reasons"] for x in both["excluded"]} == {
        "A-CR-01": [
            {"reason": "ZONE_NOT_ALLOWED"},
            {"reason": "REQUIREMENT_NOT_MET", "attribute": "max_load"},
            {"reason": "REQUIREMENT_NOT_MET", "attribute": "usage"},
        ],
        "B-CR-01": [
            {"reason": "NOT_ALLOWED"},
            {"reason": "REQUIREMENT_NOT_MET", "attribute": "max_load"},
            {"reason": "REQUIREMENT_NOT_MET", "attribute": "usage"},
        ],
        "SITE-CR-01": [{"reason": "REQUIREMENT_NOT_MET", "attribute": "usage"}],
    }


def test_owner_ask_validity_is_checked_on_facts(with_a):
    """사전 확인으로 물을 수 있는 것은 지금 유효한 자원 축 담당자 확인뿐이다. 조회했는지는 보지 않는다."""

    def need(**refs):
        return Need(kind="OWNER_CONSENT", task_id="A", **refs)

    add_run(with_a, "run_q", input_ref=CONFLICT)
    with db.read() as conn:
        run = build_observation(conn, with_a, "run_q").run
        refusals = ask_refusals(
            conn,
            with_a,
            run,
            [
                need(axis="RESOURCE", values=["B-CR-01"]),  # 사용 권한 없음
                need(axis="RESOURCE", values=["SITE-GC-01"]),  # 유형이 다름
                need(axis="RESOURCE", values=["SITE-CR-01", "B-CR-01"]),  # 하나라도 쓸 수 없으면
                need(axis="RESOURCE", values=["SITE-CR-01"]),
                need(axis="TIME"),  # 시간 동의는 후보 협의에서 받는다
                need(axis="RESOURCE"),
                Need(kind="OTHER_UNIT", group_id="g", unit_id="UB"),
            ],
        )
    assert refusals == [
        "RESOURCE_NOT_ELIGIBLE",
        "RESOURCE_NOT_ELIGIBLE",
        "RESOURCE_NOT_ELIGIBLE",
        None,
        "TIME_AXIS_NOT_ASKABLE",
        "NO_VALUES",
        "NOT_OWNER_CONSENT",
    ]
