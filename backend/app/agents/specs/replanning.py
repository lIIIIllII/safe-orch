"""Replanning AgentSpec (설계서 §11.7, 부록 A.16). 순수 데이터: Goal, Action 스키마, Budget, 사용 조건.

store·commands·solver를 import하지 않는다. 사용 조건은 관찰 데이터(untried_levels 등)만 보고 계산한다.
이번 Action은 SOLVE_WITH_SCOPE와 ESCALATE_NO_SOLUTION이다(LIST·TRY·ASK는 D5).
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

AGENT_TYPE = "REPLANNING"
GOAL = (
    "Hard 제약과 확인된 조건을 지키면서 충돌을 해소하는 검증 가능한 대안을 찾는다. "
    "변경 작업 수를 먼저, 총 지연을 그다음으로 최소화한다."
)

MAX_STEPS = 15
MAX_LLM_ATTEMPTS = 30  # step × 2 (전송 재시도 1회 계상, 블루프린트에 없는 값)
MAX_HUMAN_ROUNDS = 2
MAX_SOLVER_CALLS = 6
RECURSION_LIMIT = MAX_STEPS * 5 + 10
SUMMARY_MAX = 200

LEVELS = ("L0", "L1", "L2")


class Action(BaseModel):
    """모든 Action의 공통 인자. decision_summary는 저장할 때 200자로 자른다."""

    model_config = ConfigDict(extra="forbid")

    decision_summary: str = Field(
        description="이유: …/다음: … 형식. 이 행동을 고른 이유와 다음 예정 단계 (200자 이내)"
    )


class SolveWithScope(Action):
    """현재 사실에서 탐색 범위를 정해 CP-SAT로 대안을 계산한다. 해가 있으면 후보가 등록되고 검증을
    기다린다. 해가 없으면(INFEASIBLE·UNKNOWN) 결과를 관찰하고 다음 전략을 고른다."""

    level: Literal["L0", "L1", "L2"] = Field(
        description="탐색 범위. L0 충돌 당사자만, L1 같은 구역·같은 자원 작업까지, L2 acting_unit 작업 전부"
    )


class EscalateNoSolution(Action):
    """더 시도할 전략이 없을 때 사유를 붙여 사람에게 넘기고 Run을 끝낸다."""

    reason: str = Field(min_length=1, description="해가 없다고 판단한 근거(시도한 범위와 결과)")


ACTIONS: dict[str, type[Action]] = {
    "SOLVE_WITH_SCOPE": SolveWithScope,
    "ESCALATE_NO_SOLUTION": EscalateNoSolution,
}
FLOW = {"SOLVE_WITH_SCOPE": "CANDIDATE_OR_CONTINUE", "ESCALATE_NO_SOLUTION": "DONE"}


def available_actions(obs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """{action 이름: 허용 인자 제한}. obs에는 conflicts, untried_levels, budget_remaining이 있다."""
    out: dict[str, dict[str, Any]] = {}
    if obs["conflicts"] and obs["untried_levels"] and obs["budget_remaining"]["solver_calls"] > 0:
        out["SOLVE_WITH_SCOPE"] = {"level": list(obs["untried_levels"])}
    out["ESCALATE_NO_SOLUTION"] = {}
    return out


def tool_schemas(available: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """bind_tools에 넘길 OpenAI 함수 스키마. 인자 enum은 현재 허용 값만."""
    tools = []
    for name, limits in available.items():
        model = ACTIONS[name]
        params = model.model_json_schema()
        for arg, allowed in limits.items():
            params["properties"][arg]["enum"] = list(allowed)
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
