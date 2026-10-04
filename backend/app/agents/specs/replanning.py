"""Replanning AgentSpec. 순수 데이터: Goal, Action 스키마, Budget, 사용 조건.

store·commands·solver를 import하지 않는다. 사용 조건은 관찰 데이터(untried_levels 등)만 보고 계산한다.
Action: SOLVE_WITH_SCOPE, SOLVE_WITH_CONDITIONS, LIST_ASSIGNABLE_RESOURCES,
RETURN_RESULT.
사람 도구는 없다: 담당자 확인은 Coordination 한 창구로만 한다 (AG-09).
순서는 스킬 지침에 있고, 여기 조건은 사실·유효성·Budget뿐이다 (AG-01).
모듈 이름(GOAL, ACTIONS, tool_schemas 등)은 그대로 두고, 그 값으로 SPEC(AgentSpec)을 만든다.
"""

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agents import skills
from app.agents.types import AgentSpec
from app.domain.needs import ResultFields

AGENT_TYPE = "REPLANNING"
GOAL = (
    "Hard 제약과 확인된 조건을 지키면서 충돌을 해소하는 검증 가능한 대안을 찾는다. "
    "변경 작업 수를 먼저, 희망에서 벗어난 정도(지연)를 그다음으로 최소화한다."
)

MAX_STEPS = 15
MAX_LLM_ATTEMPTS = 30  # step × 2 (전송 재시도 1회 계상)
MAX_SOLVER_CALLS = 6
RECURSION_LIMIT = MAX_STEPS * 5 + 10
SUMMARY_MAX = 200

LEVELS = ("L0", "L1", "L2")


class Action(BaseModel):
    """모든 Action의 공통 인자. decision_summary는 저장할 때 200자로 자른다."""

    model_config = ConfigDict(extra="forbid")
    # 열리는 조건 한 줄. prompt의 "도구 전체와 열리는 조건" 절이 이것으로 만든다.
    # 실제 실행 가능 여부는 available_actions가 정한다(이 문장은 안내일 뿐이다).
    OPENS: ClassVar[str] = ""

    skill: str = Field(
        description="이번 행동에 쓰는 스킬 ID. 열린 스킬(open_skills) 중 이 도구를 가진 것"
    )
    decision_summary: str = Field(
        description="이유: …/다음: … 형식. 이 행동을 고른 이유와 다음 예정 단계 (200자 이내)"
    )


class SolveWithScope(Action):
    """현재 사실에서 탐색 범위를 정해 CP-SAT로 대안을 계산한다. 해가 있으면 후보가 등록되고 검증을
    기다린다. 해가 없으면(INFEASIBLE·UNKNOWN) 결과를 관찰하고 다음 전략을 고른다."""

    OPENS = "맡은 충돌이 있고 아직 시도하지 않은 탐색 범위와 Solver 호출이 남아 있을 때"

    level: Literal["L0", "L1", "L2"] = Field(
        description="탐색 범위. L0 충돌 당사자만, L1 같은 구역·같은 자원 작업까지, L2 acting_unit 작업 전부"
    )


class TaskCondition(BaseModel):
    """작업 하나에 거는 조건. 시각은 현장 날짜·시각 문자열이다 (AG-21)."""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(
        description="조건을 걸 작업 (탐색 범위 안의 고정되지 않은 acting_unit 작업)"
    )
    start_from: str | None = Field(
        default=None, description='이 시각 이후에 시작. 현장 날짜·시각 "YYYY-MM-DD HH:MM"'
    )
    start_until: str | None = Field(
        default=None, description='이 시각 이전에 시작. 현장 날짜·시각 "YYYY-MM-DD HH:MM"'
    )
    start_at: str | None = Field(
        default=None,
        description="이 시각에 시작(시작 지정). start_from·start_until과 같이 쓰지 않는다",
    )
    resource_id: str | None = Field(
        default=None,
        description="이 자원을 쓴다(자원 지정). 현재 자원이거나 그 작업이 쓸 수 있는 적격 자원",
    )
    use_preferred_window: bool = Field(
        default=False,
        description=(
            "그 작업의 희망 영역 안에서 시작하고 끝나게 한다(이 계산에서는 희망을 반드시 지킨다). "
            "다른 시각 조건과 같이 쓰지 않는다"
        ),
    )


class SolveWithConditions(Action):
    """탐색 범위에 작업별 조건을 걸어 CP-SAT로 계산한다. 조건은 시작 이후·이전, 시작 지정, 자원 지정,
    희망 영역을 시작 범위로 쓰기다. 조건은 좁히기만 하고, 걸지 않은 작업은 계산이 정한다. 범위 안
    작업을 모두 지정하면 그 배치 그대로를 검사한다. 목적 순서(objective)로 변경 작업 수와 희망에서 벗어난
    정도(지연) 가운데 무엇을 먼저 줄일지 고른다. 해가 있으면 후보가 등록되고 검증을 기다린다. 해가 없으면 계산 상태를
    돌려주고, 모두 지정한 배치였으면 그 배치가 어긴 규칙을 붙인다."""

    OPENS = (
        "맡은 충돌이 있고 Solver 호출이 남아 있을 때. 탐색 범위를 모두 시도한 뒤에도 쓸 수 있다. 같은 "
        "사실에서 같은 범위·같은 조건·같은 목적 순서는 받아들여지지 않는다. 조건 없이 변경 먼저로 푸는 "
        "것은 범위 계산과 같아 받아들여지지 않는다. 조건이 시간창 밖이거나, 고정된 작업이거나, "
        "범위 밖 작업이거나, 자원이 적격이 아니면 받아들여지지 않는다"
    )

    level: Literal["L0", "L1", "L2"] = Field(
        description="탐색 범위. L0 충돌 당사자만, L1 같은 구역·같은 자원 작업까지, L2 acting_unit 작업 전부"
    )
    conditions: list[TaskCondition] = Field(
        default_factory=list,
        description="작업별 조건 (작업마다 하나). 목적 순서만 바꿀 때는 비운다",
    )
    objective: Literal["CHANGE_FIRST", "DELAY_FIRST"] = Field(
        default="CHANGE_FIRST",
        description=(
            "목적 순서. CHANGE_FIRST 변경 작업 수를 먼저 줄이고 그 안에서 희망에서 벗어난 정도(지연)를 "
            "줄인다(기본). DELAY_FIRST 희망에서 벗어난 정도를 먼저 줄이고 그 안에서 변경 작업 수를 줄인다"
        ),
    )


class ListAssignableResources(Action):
    """작업에 쓸 수 있는 자원을 조회한다(유형·사용 권한·가용 구간 기준). 결과는 자원 사실이 같은 동안
    유효하다. 조건의 자원 지정은 서버가 같은 기준으로 쓸 수 있는 자원만 받는다."""

    OPENS = "필요한 자원이 있고 고정되지 않은 작업을 현재 자원 사실에서 아직 조회하지 않았을 때"

    task_id: str = Field(description="자원을 조회할 작업 (자원이 필요한 acting_unit 작업)")


class ReturnResult(Action, ResultFields):
    """결과를 돌려주고 Run을 끝낸다. 검증을 통과한 살아 있는 후보가 있으면 DONE으로 돌려준다(승인·거절은
    사람이 하고 그 결과는 이 Run이 받지 않는다). 탐색 범위 확대, 조건 걸기, 자원 조회로 열 수 있는
    대안이 남아 있지 않을 때만 BLOCKED로 돌려주고, 시도한 범위와 결과를 요약에, 무엇이 충족되면 해가
    열리는지를 길마다 필요한 것으로 적는다(다른 Unit, 사실 변경)."""

    OPENS = (
        "언제나 열려 있다. DONE은 검증을 통과한 살아 있는 후보가 있을 때만, BLOCKED는 계산·조회로 열 수 "
        "있는 대안이 남아 있지 않거나 Budget이 부족할 때만 쓴다"
    )


ACTIONS: dict[str, type[Action]] = {
    "SOLVE_WITH_SCOPE": SolveWithScope,
    "SOLVE_WITH_CONDITIONS": SolveWithConditions,
    "LIST_ASSIGNABLE_RESOURCES": ListAssignableResources,
    "RETURN_RESULT": ReturnResult,
}
FLOW = {
    "SOLVE_WITH_SCOPE": "CANDIDATE_OR_CONTINUE",
    "SOLVE_WITH_CONDITIONS": "CANDIDATE_OR_CONTINUE",
    "LIST_ASSIGNABLE_RESOURCES": "CONTINUE",
    "RETURN_RESULT": "DONE",
}


SKILLS = ("ASSESS", "BUILD_CANDIDATE", "APPLY_REJECTION", "WRAP_UP")


def skill_facts(obs: dict[str, Any]) -> dict[str, bool]:
    """스킬이 열리는 사실. 순서 조건은 없다."""
    return {
        "has_conflict": bool(obs["conflicts"]),
        "has_rejection": bool(obs["rejections"]) or bool(obs["objections"]),
    }


def open_skills(obs: dict[str, Any]) -> list[str]:
    return skills.open_skills(SKILLS, skill_facts(obs))


def choices(obs: dict[str, Any], hidden: dict[str, Any] | None = None) -> dict[str, Any]:
    """작업별 허용 값. Available Actions와 Gateway의 인자 조합 검사가 같이 쓴다.

    LIST: 필요 자원이 있고 고정되지 않았으며 같은 자원 사실에서 아직 조회하지 않은 작업.
    """
    acting = {t["task_id"]: t for t in obs["acting_tasks"]}
    listed = {r["task_id"] for r in obs["assignable_resources"] if r["task_id"] in acting}
    return {
        "LIST": [
            tid
            for tid, t in acting.items()
            if t["required_resource_type"] and not t["pinned"] and tid not in listed
        ],
    }


def valid_actions(
    obs: dict[str, Any], hidden: dict[str, Any] | None = None
) -> dict[str, dict[str, Any]]:
    """유효한 인자 값이 있는 도구와 그 값. 사실·유효성·Budget만 본다(순서 조건 없음)."""
    out: dict[str, dict[str, Any]] = {}
    budget = obs["budget_remaining"]
    if obs["conflicts"] and obs["untried_levels"] and budget["solver_calls"] > 0:
        out["SOLVE_WITH_SCOPE"] = {"level": list(obs["untried_levels"])}
    # 조건을 걸어 풀기: 범위를 다 써도 열려 있다. 조건의 유효성은 실행 때 서버가 본다 (CV-24)
    movable = any(not t["pinned"] for t in obs["acting_tasks"])
    if obs["primary_conflict"] and movable and budget["solver_calls"] > 0:
        out["SOLVE_WITH_CONDITIONS"] = {"level": list(LEVELS)}
    c = choices(obs, hidden)
    if c["LIST"]:
        out["LIST_ASSIGNABLE_RESOURCES"] = {"task_id": c["LIST"]}
    # DONE은 검증을 통과한 살아 있는 후보가 있을 때만 유효하다(사실 조건)
    ready = bool((hidden or {}).get("done_ready"))
    out["RETURN_RESULT"] = {"status": ["DONE", "BLOCKED"] if ready else ["BLOCKED"]}
    return out


def available_actions(
    obs: dict[str, Any], hidden: dict[str, Any] | None = None
) -> dict[str, dict[str, Any]]:
    """{action 이름: 허용 인자 제한}. 열린 스킬의 도구 ∩ 유효한 도구. skill 인자의 허용 값도 넣는다."""
    return skills.available(SKILLS, skill_facts(obs), valid_actions(obs, hidden))


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
    budget={
        "steps": MAX_STEPS,
        "llm_attempts": MAX_LLM_ATTEMPTS,
        "solver_calls": MAX_SOLVER_CALLS,
    },
    recursion_limit=RECURSION_LIMIT,
    summary_max=SUMMARY_MAX,
    actions=ACTIONS,
    skills=SKILLS,
    available_actions=available_actions,
    tool_schemas=tool_schemas,
)
