"""Main AgentSpec. 순수 데이터: Goal, Action 스키마, Budget, 사용 조건.

store·commands·solver를 import하지 않는다. 사용 조건은 관찰 데이터만 보고 계산한다.
Action: CALL_AGENT, WAIT, ESCALATE, CLOSE. 승인·확정·Hold 해제·제안 확인 도구는 없다.
전문 Agent에게는 Agent 종류와 참조만 넘긴다(AG-24). 재계획에는 접근(목록 값)과 짧은 문장(인용)을 더한다(AG-28). 순서는 지침에 있고 여기 조건은
사실·유효성·Budget뿐이다 (AG-01).
"""

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agents import skills
from app.agents.types import AgentSpec
from app.config import get_settings
from app.domain.needs import MAX_NEEDS, SUMMARY_MAX, Need

AGENT_TYPE = "MAIN"
GOAL = (
    "맡은 사건을 끝까지 처리한다. 어떤 전문 Agent를 어떤 참조로 부를지, 돌아온 결과로 다음에 무엇을 할지, "
    "언제 기다리고 언제 끝낼지 판단한다. 승인·확정·Hold 해제는 사람만 한다."
)

# 설정값(MAIN_MAX_STEPS·MAIN_MAX_AGENT_CALLS). 기동 때 한 번 읽는다
MAX_STEPS = get_settings().main_max_steps
MAX_LLM_ATTEMPTS = MAX_STEPS * 2  # step × 2 (다른 Agent와 같은 규칙)
MAX_AGENT_CALLS = get_settings().main_max_agent_calls
# 사람이 이 Case에서 새 일을 만들 때마다 한도가 한 바퀴분씩 늘어난다 (AG-30)
ROUND_STEPS = 8  # 재계획 3(접근별) + 고르기 기다림 + 협의 + 승인 기다림 + 통지 + 여유 1
ROUND_AGENT_CALLS = 5  # 재계획 3 + 협의 + 통지
MAX_EXTRA_ROUNDS = 4  # 늘어나는 바퀴 수의 상한
RECURSION_LIMIT = (MAX_STEPS + MAX_EXTRA_ROUNDS * ROUND_STEPS) * 5 + 10
SUMMARY_MAX_DECISION = 200

CALL_ARGS = (
    "group_id",
    "acting_unit_id",
    "approach",
    "phase",
    "candidate_id",
    "event_id",
)
NOTE_MAX = 100  # 접근에 덧붙이는 문장 길이


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


class CallAgent(Action):
    """전문 Agent Run을 요청하고 결과를 기다린다. Agent 종류와 참조만 넘긴다. 재계획(REPLANNING)은 충돌 그룹과 그 그룹에 작업을 가진 Unit과 접근(무엇을 우선할지), 협의(COORDINATION)는 Supervisor가 고른 후보, 통지는 확정된 후보, 신고 대응(EVENT_RESPONSE)은 신고를 가리킨다."""

    OPENS = (
        "지금 받아들여지는 호출(calls)이 있고 전문 Agent 호출 수가 남아 있을 때. 열린 하위 Run이 있거나, "
        "ACTIVE Hold 중의 재계획·협의이거나, 같은 호출(재계획은 같은 접근)의 마지막 결과 뒤로 관련 사실이 "
        "바뀌지 않았거나, Supervisor가 고르지 않은 후보의 협의면 받아들여지지 않는다"
    )

    agent: Literal["REPLANNING", "COORDINATION", "EVENT_RESPONSE"] = Field(
        description="부를 전문 Agent"
    )
    group_id: str | None = Field(default=None, description="재계획할 충돌 그룹 ID (REPLANNING)")
    acting_unit_id: str | None = Field(
        default=None, description="재계획의 주체 Unit. 그 그룹에 작업을 가진 Unit (REPLANNING)"
    )
    approach: Literal["MIN_CHANGE", "PREFER_WINDOW"] | None = Field(
        default=None,
        description=(
            "재계획이 우선할 것 (REPLANNING). MIN_CHANGE 변경 작업 수를 줄인다, PREFER_WINDOW "
            "담당자의 희망에서 가장 덜 벗어나게 한다"
        ),
    )
    approach_note: str | None = Field(
        default=None,
        max_length=NOTE_MAX,
        description="접근에 덧붙이는 짧은 문장 (REPLANNING). 재계획 Agent에게 인용으로 전해진다",
    )
    phase: Literal["CONSULT", "NOTICE"] | None = Field(
        default=None,
        description="CONSULT 후보의 협의, NOTICE 확정 뒤 통지 (COORDINATION)",
    )
    candidate_id: str | None = Field(
        default=None, description="협의(Supervisor가 고른 후보)·통지할 후보 ID (COORDINATION)"
    )
    event_id: str | None = Field(default=None, description="대응할 신고 ID (EVENT_RESPONSE)")


class Wait(Action):
    """사람의 결정(안 고르기, 후보 승인·거절, Hold 해제)을 기다린다. 이 Case에 사건이 생기면 다시 관찰한다."""

    OPENS = "검토 대기 후보가 있거나 ACTIVE Hold가 있을 때"


class Escalate(Action):
    """풀 수 없는 일을 Supervisor에게 이관하고 Run을 끝낸다. 한 것과 막힌 이유를 요약에 적고, 풀리려면 필요한 것을 알면 적는다."""

    OPENS = "언제나 열려 있다. 단 부를 수 있는 호출과 기다릴 것으로 풀 수 없을 때만 쓴다"

    summary: str = Field(
        min_length=1,
        max_length=SUMMARY_MAX,
        description="Supervisor가 읽을 요약(한 것과 막힌 이유)",
    )
    needs: list[Need] = Field(
        default_factory=list,
        max_length=MAX_NEEDS,
        description="풀리려면 필요한 것. 종류와 그 종류의 참조만 쓴다. 모르면 비운다",
    )


class Close(Action):
    """맡은 일이 모두 닫혔을 때 Run을 끝낸다."""

    OPENS = "이 Case의 열린 일(open_work)이 없을 때"

    summary: str = Field(min_length=1, max_length=SUMMARY_MAX, description="무엇을 처리했는지 요약")


ACTIONS: dict[str, type[Action]] = {
    "CALL_AGENT": CallAgent,
    "WAIT": Wait,
    "ESCALATE": Escalate,
    "CLOSE": Close,
}
FLOW = {"CALL_AGENT": "WAIT", "WAIT": "WAIT", "ESCALATE": "DONE", "CLOSE": "DONE"}

SKILLS = ("ORCHESTRATE",)


def skill_facts(obs: dict[str, Any]) -> dict[str, bool]:
    """스킬이 열리는 사실. 순서 조건은 없다."""
    return {}


def open_skills(obs: dict[str, Any]) -> list[str]:
    return skills.open_skills(SKILLS, skill_facts(obs))


def call_refs(action: CallAgent) -> dict[str, Any]:
    """호출의 참조(관찰의 calls 항목과 같은 모양)."""
    out: dict[str, Any] = {"agent": action.agent}
    out.update({k: v for k in CALL_ARGS if (v := getattr(action, k)) not in (None, [])})
    return out


def valid_actions(obs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """유효한 인자 값이 있는 도구와 그 값. 사실·유효성·Budget만 본다(순서 조건 없음).

    CALL_AGENT의 인자 조합(어느 그룹에 어느 Unit 등)은 실행 때 서버가 다시 검사한다.
    """
    out: dict[str, dict[str, Any]] = {}
    calls = obs["calls"]
    if calls and obs["budget_remaining"]["agent_calls"] > 0:
        limits: dict[str, Any] = {"agent": sorted({c["agent"] for c in calls})}
        for arg in CALL_ARGS:
            values = sorted({c[arg] for c in calls if c.get(arg) is not None})
            if values:
                limits[arg] = values
        out["CALL_AGENT"] = limits
    waiting = obs["waiting_for"]
    if waiting["candidates"] or waiting["holds"]:
        out["WAIT"] = {}
    out["ESCALATE"] = {}
    if not obs["open_work"]:
        out["CLOSE"] = {}
    return out


def available_actions(obs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """{action 이름: 허용 인자 제한}. 열린 스킬의 도구 ∩ 유효한 도구. skill 인자의 허용 값도 넣는다."""
    return skills.available(SKILLS, skill_facts(obs), valid_actions(obs))


def _enum(prop: dict[str, Any], allowed: list[str]) -> None:
    """선택 인자(str | None)면 문자열 쪽에만, 목록 인자면 항목에 enum을 건다."""
    if prop.get("type") == "array":
        prop["items"]["enum"] = list(allowed)
    elif "anyOf" in prop:
        for branch in prop["anyOf"]:
            if branch.get("type") == "string":
                branch["enum"] = list(allowed)
    else:
        prop["enum"] = list(allowed)


def tool_schemas(available: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """bind_tools에 넘길 OpenAI 함수 스키마. 인자 enum은 현재 허용 값만."""
    tools = []
    for name, limits in available.items():
        model = ACTIONS[name]
        params = model.model_json_schema()
        for arg, allowed in limits.items():
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


def budget_limits(human_work: int) -> dict[str, int]:
    """사람이 만든 일의 횟수만큼 늘린 한도. Agent 자신의 행동으로는 늘지 않는다 (AG-30)."""
    rounds = min(human_work, MAX_EXTRA_ROUNDS)
    extra = {
        "steps": rounds * ROUND_STEPS,
        "llm_attempts": rounds * ROUND_STEPS * 2,
        "agent_calls": rounds * ROUND_AGENT_CALLS,
    }
    return {name: limit + extra[name] for name, limit in SPEC.budget.items()}


SPEC = AgentSpec(
    agent_type=AGENT_TYPE,
    goal=GOAL,
    budget={"steps": MAX_STEPS, "llm_attempts": MAX_LLM_ATTEMPTS, "agent_calls": MAX_AGENT_CALLS},
    recursion_limit=RECURSION_LIMIT,
    summary_max=SUMMARY_MAX_DECISION,
    actions=ACTIONS,
    skills=SKILLS,
    available_actions=available_actions,
    tool_schemas=tool_schemas,
)
