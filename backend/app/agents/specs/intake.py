"""Work Intake AgentSpec (설계서 §18.2.1·§18.2.4, 부록 A.26). 순수 데이터: Goal, Action 스키마, Budget.

store·commands·solver를 import하지 않는다. 사용 조건은 관찰 데이터만 보고 계산한다.
Action(최소 경로): LOOKUP_RESOURCE, ASK_CLARIFICATION, REQUEST_CONFIRMATION, COMPLETE_TASKSPEC, ESCALATE.
모델이 추출·조회한 값은 PROPOSED이고, 요청자가 확인한 값만 CONFIRMED가 된다(D01). 위험 태그는 받지 않는다(I-14).
"""

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agents.types import AgentSpec

AGENT_TYPE = "INTAKE"
GOAL = (
    "자연어 작업 요청에서 critical field가 모두 요청자에게 확인된 작업 요청(TaskSpec)을 만든다. "
    "추정한 값을 확인된 값으로 다루지 않는다."
)

MAX_STEPS = 12  # §18.2.1 표
MAX_LLM_ATTEMPTS = 24  # step × 2 (Replanning과 같은 규칙, A.26)
MAX_HUMAN_ROUNDS = 3  # §18.2.1 표. 확인 질문과 값 확인 요청을 모두 센다
RECURSION_LIMIT = MAX_STEPS * 5 + 10
SUMMARY_MAX = 200
TEXT_MAX = 300
FIELD_IDS = ("zone_id", "duration", "window", "resource")


class Action(BaseModel):
    """모든 Action의 공통 인자. decision_summary는 저장할 때 200자로 자른다."""

    model_config = ConfigDict(extra="forbid")
    # 열리는 조건 한 줄. prompt의 "도구 전체와 열리는 조건" 절이 이것으로 만든다.
    OPENS: ClassVar[str] = ""

    decision_summary: str = Field(
        description="이유: …/다음: … 형식. 이 행동을 고른 이유와 다음 예정 단계 (200자 이내)"
    )


class TaskValues(BaseModel):
    """작업 요청 값. 시간은 Horizon 원점 기준 정수 분이다. 위험 태그 칸은 없다(I-14)."""

    model_config = ConfigDict(extra="forbid")

    work_type: str = Field(description="작업 유형 코드(관찰의 work_types)")
    zone_id: str = Field(description="구역 ID")
    duration: int = Field(gt=0, description="작업 시간(분)")
    earliest_start: int = Field(description="가장 이른 시작(분)")
    latest_start: int = Field(description="가장 늦은 시작(분)")
    latest_end: int = Field(description="종료 한도(분)")
    required_resource_type: str | None = Field(default=None, description="필요 자원 유형")
    requested_resource_id: str | None = Field(default=None, description="요청 자원 ID")


class LookupResource(Action):
    """자원을 유형별로 찾고, 요청자 Unit이 쓸 수 있는지와 가용 구간을 돌려준다."""

    OPENS = "언제나 열려 있다"

    resource_type: str | None = Field(default=None, description="자원 유형(없으면 전체)")


class AskClarification(Action):
    """빠지거나 모호한 값을 요청자에게 묻는다. 답은 자유 텍스트로 온다."""

    OPENS = "사람 확인 라운드가 남았고 답을 기다리는 질문·확인 요청이 없을 때"

    field_ids: list[Literal["zone_id", "duration", "window", "resource"]] = Field(
        min_length=1, description="물을 critical field"
    )
    question: str = Field(min_length=1, max_length=TEXT_MAX, description="요청자에게 보이는 질문")


class RequestConfirmation(Action):
    """작업 요청 값을 요청자에게 확인받는다. 서버가 폼과 같은 검증을 하고, 통과해야 확인 요청이 나간다."""

    OPENS = "사람 확인 라운드가 남았고 답을 기다리는 질문·확인 요청이 없을 때"

    values: TaskValues
    message: str = Field(
        min_length=1, max_length=TEXT_MAX, description="요청자에게 보이는 설명(Agent 설명으로 표시)"
    )


class CompleteTaskspec(Action):
    """요청자가 확인한 값으로 작업 요청을 완료한다. 제출 값은 확인받은 값과 같아야 한다."""

    OPENS = "이 Run의 마지막 확인 요청에 요청자가 확인(ACCEPT)했고 답을 기다리는 요청이 없을 때"

    values: TaskValues


class Escalate(Action):
    """확인된 작업 요청을 만들 수 없을 때(요청자가 거절, 값이 검증을 통과하지 못함 등) 사유와 함께 이관한다."""

    OPENS = "언제나 열려 있다"

    reason: str = Field(min_length=1, description="이관 사유")


ACTIONS: dict[str, type[Action]] = {
    "LOOKUP_RESOURCE": LookupResource,
    "ASK_CLARIFICATION": AskClarification,
    "REQUEST_CONFIRMATION": RequestConfirmation,
    "COMPLETE_TASKSPEC": CompleteTaskspec,
    "ESCALATE": Escalate,
}
FLOW = {
    "LOOKUP_RESOURCE": "CONTINUE",
    "ASK_CLARIFICATION": "WAIT",
    "REQUEST_CONFIRMATION": "WAIT",
    "COMPLETE_TASKSPEC": "DONE",
    "ESCALATE": "DONE",
}


def choices(obs: dict[str, Any]) -> dict[str, Any]:
    """Action별 열림 (A.26 2). 답을 기다리는 메시지가 있으면 묻거나 확인을 요청하지 않는다."""
    waiting = any(q["status"] == "OPEN" for q in obs["questions"]) or any(
        c["status"] == "OPEN" for c in obs["confirmations"]
    )
    rounds = obs["budget_remaining"].get("human_rounds", 0) > 0
    last = obs["confirmations"][-1] if obs["confirmations"] else None
    return {
        "ASK": rounds and not waiting,
        "REQUEST": rounds and not waiting,
        "COMPLETE": not waiting
        and last is not None
        and last["status"] == "ANSWERED"
        and last["decision"] == "ACCEPT",
    }


def available_actions(obs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    c = choices(obs)
    out: dict[str, dict[str, Any]] = {"LOOKUP_RESOURCE": {}}
    if c["ASK"]:
        out["ASK_CLARIFICATION"] = {}
    if c["REQUEST"]:
        out["REQUEST_CONFIRMATION"] = {}
    if c["COMPLETE"]:
        out["COMPLETE_TASKSPEC"] = {}
    out["ESCALATE"] = {}
    return out


def tool_schemas(available: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """bind_tools에 넘길 OpenAI 함수 스키마."""
    tools = []
    for name in available:
        model = ACTIONS[name]
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": (model.__doc__ or "").strip(),
                    "parameters": model.model_json_schema(),
                },
            }
        )
    return tools


SPEC = AgentSpec(
    agent_type=AGENT_TYPE,
    goal=GOAL,
    budget={
        "steps": MAX_STEPS,
        "llm_attempts": MAX_LLM_ATTEMPTS,
        "human_rounds": MAX_HUMAN_ROUNDS,
    },
    recursion_limit=RECURSION_LIMIT,
    summary_max=SUMMARY_MAX,
    actions=ACTIONS,
    available_actions=available_actions,
    tool_schemas=tool_schemas,
)
