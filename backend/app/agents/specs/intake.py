"""Work Intake AgentSpec. 순수 데이터: Goal, Action 스키마, Budget.

store·commands·solver를 import하지 않는다. 사용 조건은 관찰 데이터만 보고 계산한다.
완료가 내는 시각은 요청자의 희망이다: 서버가 희망 영역으로 기록하고 시간창은 Horizon 전체로 둔다 (ST-22).
Action: LOOKUP_RESOURCE, COMPLETE_TASKSPEC, RETURN_RESULT(막힘: 접수 미완으로 끝나고 요청자에게
알린다, AG-06). 요청자에게 묻는 도구는 없다: 문장에서 읽은 값과 스스로 정한 값으로 바로 완료하고, 값마다
출처(말함·정함)를 적는다. 서버는 출처를 검사하지 않는다 (AG-32). 위험 태그는 받지 않는다.
"""

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agents import skills
from app.agents.types import AgentSpec
from app.domain.needs import ResultFields

AGENT_TYPE = "INTAKE"
GOAL = (
    "자연어 작업 요청을 요청자에게 되묻지 않고 작업 요청(TaskSpec)으로 만든다. 문장에 없는 값은 스스로 "
    "정하고, 값마다 사람이 말한 값인지 스스로 정한 값인지 적는다."
)

MAX_STEPS = 12
MAX_LLM_ATTEMPTS = 24  # step × 2 (Replanning과 같은 규칙)
RECURSION_LIMIT = MAX_STEPS * 5 + 10
SUMMARY_MAX = 200
# 현장 날짜·시각 문자열 (AG-21). 희망 시작 범위의 처음·끝과 희망 종료 한도다 (ST-22)
TIME_FIELDS = ("earliest_start", "latest_start", "latest_end")


class Action(BaseModel):
    """모든 Action의 공통 인자. decision_summary는 저장할 때 200자로 자른다."""

    model_config = ConfigDict(extra="forbid")
    # 열리는 조건 한 줄. prompt의 "도구 전체와 열리는 조건" 절이 이것으로 만든다.
    OPENS: ClassVar[str] = ""

    skill: str = Field(
        description="이번 행동에 쓰는 스킬 ID. 열린 스킬(open_skills) 중 이 도구를 가진 것"
    )
    decision_summary: str = Field(
        description="이유: …/다음: … 형식. 이 행동을 고른 이유와 다음 예정 단계 (200자 이내)"
    )


class RequirementValue(BaseModel):
    """자원 요구 조건 하나: 자원의 속성 값이 맞춰야 하는 비교."""

    model_config = ConfigDict(extra="forbid")

    attribute: str = Field(description="자원 속성 이름(관찰의 resource_attributes)")
    op: Literal["GTE", "LTE", "CONTAINS"] = Field(
        description="GTE 수치 이상, LTE 수치 이하(수치 속성), CONTAINS 목록 포함(목록 속성)"
    )
    value: int | float | str = Field(description="비교 값. 수치 속성은 숫자, 목록 속성은 문자열")


class DemandValue(BaseModel):
    """수량 수요 하나: 작업이 그 종류의 풀에서 쓰는 수량(예: 인원 수)."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(description="수량 풀 종류 코드(관찰의 pool_kinds)")
    quantity: int = Field(gt=0, description="수량")


class TaskValues(BaseModel):
    """작업 요청 값. 시각은 현장 날짜·시각 문자열 "YYYY-MM-DD HH:MM"로 쓴다(서버가 분으로 바꾼다).
    시각 셋은 요청자의 희망이다: 서버가 희망 영역으로 기록하고 가능 범위(시간창)로 강제하지 않는다.
    위험 태그 칸은 없다."""

    model_config = ConfigDict(extra="forbid")

    work_type: str = Field(description="작업 유형 코드(관찰의 work_types)")
    zone_id: str = Field(description="구역 ID")
    duration: int = Field(gt=0, description="작업 시간(분)")
    earliest_start: str = Field(
        description='희망하는 가장 이른 시작. 현장 날짜·시각 "YYYY-MM-DD HH:MM"'
    )
    latest_start: str = Field(
        description='희망하는 가장 늦은 시작. 현장 날짜·시각 "YYYY-MM-DD HH:MM"'
    )
    latest_end: str = Field(description='희망하는 종료 한도. 현장 날짜·시각 "YYYY-MM-DD HH:MM"')
    required_resource_type: str | None = Field(default=None, description="필요 자원 유형")
    requested_resource_id: str | None = Field(default=None, description="요청 자원 ID")
    resource_requirements: list[RequirementValue] = Field(
        default_factory=list,
        description=(
            "자원 요구 조건(선택). 요청 문장에 있을 때만 넣는다. "
            "작업 유형의 기본 요구 조건은 서버가 붙이므로 넣지 않는다"
        ),
    )
    pool_demands: list[DemandValue] = Field(
        default_factory=list,
        description=(
            "인원 같은 수량 수요(선택). 요청 문장에 수량이 있을 때만 넣는다. "
            "작업 유형의 기본 수요는 서버가 붙이므로 넣지 않는다"
        ),
    )


class LookupResource(Action):
    """요청자 Unit이 쓸 수 있는 자원과 쓸 수 없는 자원·이유를 돌려준다. 구역과 작업 유형을 주면 그 구역에서 그 작업 유형의 기본 요구 조건까지 맞는 자원을 유형을 가리지 않고 가린다."""

    OPENS = "언제나 열려 있다"

    resource_type: str | None = Field(
        default=None, description="자원 유형으로 좁힌다(없으면 모든 유형)"
    )
    zone_id: str | None = Field(default=None, description="작업 구역 ID(알면 넣는다)")
    work_type: str | None = Field(default=None, description="작업 유형 코드(알면 넣는다)")


Origin = Literal["STATED", "DECIDED"]
ORIGIN_TEXT = (
    "STATED 요청 문장에서 그대로 읽힌 값, DECIDED 문장에 없거나 해석·추정이 들어가 스스로 정한 값"
)


class ValueOrigins(BaseModel):
    """값마다의 출처. 요청자가 작업 카드에서 스스로 정한 값(DECIDED)을 따로 보고 고치거나 확인한다."""

    model_config = ConfigDict(extra="forbid")

    work_type: Origin = Field(description=ORIGIN_TEXT)
    zone_id: Origin = Field(description=ORIGIN_TEXT)
    duration: Origin = Field(description=ORIGIN_TEXT)
    earliest_start: Origin = Field(description=ORIGIN_TEXT)
    latest_start: Origin = Field(description=ORIGIN_TEXT)
    latest_end: Origin = Field(description=ORIGIN_TEXT)
    required_resource_type: Origin | None = Field(
        default=None, description="필요 자원 유형의 출처(자원이 있으면 적는다)"
    )
    requested_resource_id: Origin | None = Field(
        default=None, description="요청 자원의 출처(자원이 있으면 적는다)"
    )


class CompleteTaskspec(Action):
    """작업 요청을 완료한다. 요청자에게 묻지 않고, 문장에서 읽은 값과 스스로 정한 값을 함께 내며 값마다 출처를 적는다. 서버가 폼과 같은 검증을 하고, 통과하면 작업이 만들어진다. 낸 시각은 희망 영역이 된다."""

    OPENS = "언제나 열려 있다"

    values: TaskValues
    origins: ValueOrigins = Field(description="값마다 사람이 말한 값인지 스스로 정한 값인지")


class ReturnResult(Action, ResultFields):
    """작업 요청을 만들 수 없을 때(값을 고쳐도 검증을 통과하지 못함 등) 접수 미완으로 끝낸다. 요청자에게 접수 미완과 사유가 통지된다. 한 것과 막힌 이유를 요약에 적고, 필요한 것을 알면 길에 적는다."""

    OPENS = "언제나 열려 있다"


ACTIONS: dict[str, type[Action]] = {
    "LOOKUP_RESOURCE": LookupResource,
    "COMPLETE_TASKSPEC": CompleteTaskspec,
    "RETURN_RESULT": ReturnResult,
}
FLOW = {
    "LOOKUP_RESOURCE": "CONTINUE",
    "COMPLETE_TASKSPEC": "DONE",
    "RETURN_RESULT": "DONE",
}


def code_values(obs: dict[str, Any]) -> dict[str, list[str]]:
    """코드 값을 받는 인자의 허용 값(관찰의 Pack 데이터). 정적 스키마에는 넣지 않는다."""
    types = obs["resource_types"]
    return {
        "work_type": [w["work_type"] for w in obs["work_types"]],
        "zone_id": list(obs["zones"]),
        "required_resource_type": [t["resource_type"] for t in types],
        "requested_resource_id": sorted({r for t in types for r in t["resource_ids"]}),
        "resource_requirements.attribute": [a["name"] for a in obs["resource_attributes"]],
        "pool_demands.kind": [k["kind"] for k in obs["pool_kinds"]],
    }


SKILLS = ("ASSESS", "TASK_INTAKE", "WRAP_UP")


def skill_facts(obs: dict[str, Any]) -> dict[str, bool]:
    """스킬이 열리는 사실. 순서 조건은 없다."""
    request = bool(obs.get("request"))
    return {"has_draft_request": request}


def open_skills(obs: dict[str, Any]) -> list[str]:
    return skills.open_skills(SKILLS, skill_facts(obs))


def available_actions(obs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """{action 이름: 인자 제한}. 열린 스킬의 도구 ∩ 유효한 도구. skill 인자의 허용 값도 넣는다."""
    return skills.available(SKILLS, skill_facts(obs), valid_actions(obs))


def valid_actions(obs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """유효한 도구와 인자 제한. 코드 인자에는 실행 시 Pack 값으로 enum을 건다(Replanning과 같은 방식)."""
    codes = code_values(obs)
    return {
        "LOOKUP_RESOURCE": {
            "resource_type": codes["required_resource_type"],
            "zone_id": codes["zone_id"],
            "work_type": codes["work_type"],
        },
        "COMPLETE_TASKSPEC": {"values": codes},
        # 완료는 COMPLETE_TASKSPEC으로 한다. 스스로 끝내는 결과는 막힘(접수 미완)뿐이다
        "RETURN_RESULT": {"status": ["BLOCKED"]},
    }


def _enum(prop: dict[str, Any], allowed: list[str]) -> None:
    """선택 인자(str | None)면 문자열 쪽에만 enum을 건다."""
    if "anyOf" in prop:
        for branch in prop["anyOf"]:
            if branch.get("type") == "string":
                branch["enum"] = list(allowed)
    else:
        prop["enum"] = list(allowed)


def tool_schemas(available: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """bind_tools에 넘길 OpenAI 함수 스키마. 코드 인자의 enum은 현재 허용 값만(values는 TaskValues 안)."""
    tools = []
    for name, limits in available.items():
        model = ACTIONS[name]
        params = model.model_json_schema()
        for arg, allowed in limits.items():
            if arg == "values":
                props = params["$defs"]["TaskValues"]["properties"]
                for field, field_allowed in allowed.items():
                    if field == "resource_requirements.attribute":
                        # 선언된 속성이 없으면 enum을 걸지 않는다(빈 enum은 스키마 오류)
                        if field_allowed:
                            requirement = params["$defs"]["RequirementValue"]["properties"]
                            _enum(requirement["attribute"], field_allowed)
                        continue
                    if field == "pool_demands.kind":
                        if field_allowed:
                            demand = params["$defs"]["DemandValue"]["properties"]
                            _enum(demand["kind"], field_allowed)
                        continue
                    _enum(props[field], field_allowed)
            else:
                _enum(params["properties"][arg], allowed)
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": (model.__doc__ or "").strip(),
                    "parameters": params,
                },
            }
        )
    return tools


SPEC = AgentSpec(
    agent_type=AGENT_TYPE,
    goal=GOAL,
    budget={"steps": MAX_STEPS, "llm_attempts": MAX_LLM_ATTEMPTS},
    recursion_limit=RECURSION_LIMIT,
    summary_max=SUMMARY_MAX,
    actions=ACTIONS,
    skills=SKILLS,
    available_actions=available_actions,
    tool_schemas=tool_schemas,
)
