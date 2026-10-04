"""Schedule Review AgentSpec. 순수 데이터: Goal, Action 스키마, Budget.

store·commands·solver를 import하지 않는다. 사용 조건은 관찰 데이터만 보고 계산한다.
Action: SUBMIT_BUNDLES, RETURN_RESULT.
일정 Case의 충돌을 묶음으로 정리하고 검토 의견을 낸다. 서버가 계산한 최소 묶음(작업을 공유하는 충돌)을
어떻게 합칠지만 정한다: 배치·점수 계산, 고정·값 고치기, 사람에게 묻는 도구는 없다 (AG-10·AG-36).
묶음안을 내는 것이 끝내는 행동이다. RETURN_RESULT는 묶음안을 낼 수 없을 때(BLOCKED)만 쓴다.
"""

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from app.agents import skills
from app.agents.types import AgentSpec
from app.domain.needs import ResultFields

AGENT_TYPE = "SCHEDULE_REVIEW"
GOAL = (
    "일정으로 들어온 작업이 만든 충돌을 묶음으로 정리하고 검토 의견을 낸다. 서버가 계산한 최소 묶음을 따로 "
    "풀면 다시 부딪힐 것끼리, 원인이 같은 것끼리 합친다. 배치나 점수를 계산하지 않는다."
)

MAX_STEPS = 6
MAX_LLM_ATTEMPTS = 12  # step × 2 (다른 Agent와 같은 규칙)
RECURSION_LIMIT = MAX_STEPS * 5 + 10
SUMMARY_MAX = 200
NOTE_MAX = 200
OPINION_MAX = 600


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


class Bundle(BaseModel):
    """묶음 하나: 함께 풀 최소 묶음들."""

    model_config = ConfigDict(extra="forbid")

    group_ids: list[str] = Field(
        min_length=1, description="이 묶음에 넣는 최소 묶음 ID(관찰의 groups). 하나만 넣어도 된다"
    )
    note: str = Field(
        max_length=NOTE_MAX,
        description="이 묶음의 짧은 메모: 원인과 풀 때 주의할 사유. 읽는 쪽은 인용으로만 다룬다",
    )


class SubmitBundles(Action):
    """최소 묶음을 합쳐 정한 묶음과 일정 전체의 검토 의견을 내고 검토를 끝낸다. 서버는 모든 최소 묶음이 정확히 한 번 들어갔는지만 검사하고, 통과하면 묶음안으로 기록하고 Run이 끝난다(메인에게는 그 묶음안이 결과로 간다). 내기 전에 묶음을 정한다."""

    OPENS = (
        "충돌이 있을 때. 최소 묶음을 빠뜨리거나, 같은 최소 묶음을 두 묶음에 넣거나, 없는 ID를 쓰면 "
        "받아들여지지 않고 Run은 계속된다"
    )

    bundles: list[Bundle] = Field(min_length=1, description="묶음 목록")
    opinion: str = Field(
        min_length=1,
        max_length=OPINION_MAX,
        description="일정 전체에 대한 검토 의견. 읽는 쪽은 인용으로만 다룬다",
    )


class ReturnResult(Action, ResultFields):
    """묶음안을 낼 수 없을 때 막힌 결과(BLOCKED)를 돌려주고 Run을 끝낸다. 이유를 요약에 적는다. 묶음안을 낼 수 있으면 쓰지 않는다."""

    OPENS = "언제나 열려 있다. 상태는 BLOCKED뿐이다"


ACTIONS: dict[str, type[Action]] = {
    "SUBMIT_BUNDLES": SubmitBundles,
    "RETURN_RESULT": ReturnResult,
}
FLOW = {"SUBMIT_BUNDLES": "DONE", "RETURN_RESULT": "DONE"}

SKILLS = ("REVIEW_SCHEDULE", "WRAP_UP")


def skill_facts(obs: dict[str, Any]) -> dict[str, bool]:
    """스킬이 열리는 사실. 순서 조건은 없다."""
    return {"has_conflict": bool(obs["groups"])}


def open_skills(obs: dict[str, Any]) -> list[str]:
    return skills.open_skills(SKILLS, skill_facts(obs))


def valid_actions(obs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """유효한 도구와 인자 제한. 사실·유효성만 본다(순서 조건 없음)."""
    out: dict[str, dict[str, Any]] = {}
    if obs["groups"]:
        out["SUBMIT_BUNDLES"] = {}
    # 마치는 것은 SUBMIT_BUNDLES로 한다. 스스로 끝내는 결과는 막힘뿐이다
    out["RETURN_RESULT"] = {"status": ["BLOCKED"]}
    return out


def available_actions(obs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """{action 이름: 허용 인자 제한}. 열린 스킬의 도구 ∩ 유효한 도구. skill 인자의 허용 값도 넣는다."""
    return skills.available(SKILLS, skill_facts(obs), valid_actions(obs))


def tool_schemas(available: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """bind_tools에 넘길 OpenAI 함수 스키마. 인자 enum은 현재 허용 값만."""
    tools = []
    for name, limits in available.items():
        model = ACTIONS[name]
        params = model.model_json_schema()
        for arg, allowed in limits.items():
            prop = params["properties"][arg]
            (prop["items"] if prop.get("type") == "array" else prop)["enum"] = list(allowed)
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
