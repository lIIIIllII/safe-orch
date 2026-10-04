"""그래프와 Gateway가 주고받는 값. store를 import하지 않는다."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Literal

GatewayKind = Literal["CONTINUE", "WAIT", "DONE", "REJECTED", "INACTIVE"]


@dataclass(frozen=True)
class StepMeta:
    """step마다 decide가 채운다. error_kind가 있으면 모델 응답이 없다(LLM_ERROR·LLM_CONFIG)."""

    model_id: str
    prompt_version: str
    llm_attempts: int = 1
    error_kind: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class GatewayResult:
    """그래프 edge는 kind로만 분기한다. DONE이면 end_status로 Run을 끝낸다."""

    kind: GatewayKind
    reason: str | None = None
    end_status: str | None = None
    end_reason: str | None = None


@dataclass(frozen=True)
class AgentSpec:
    """agent_type별 순수 데이터. graph는 이것만 받는다. store를 모른다.

    budget: 카운터 이름(steps·llm_attempts·human_rounds·solver_calls) → 한도.
    """

    agent_type: str
    goal: str
    budget: Mapping[str, int]
    recursion_limit: int
    summary_max: int
    actions: Mapping[str, Any]  # 허용 도구
    skills: tuple[str, ...]  # 이 Agent가 쓰는 스킬 (app.agents.skills)
    available_actions: Callable[[dict[str, Any]], dict[str, dict[str, Any]]]
    tool_schemas: Callable[[dict[str, dict[str, Any]]], list[dict[str, Any]]]


@dataclass(frozen=True)
class AgentBinding:
    """agent_type 하나의 묶음. registry가 만들고 runtime이 고른다.

    observer: build_observation(conn, pack, run_id)을 가진 모듈. executor: ToolGateway가 만들고
    ToolGateway.execute 안에서만 부르는 Action 실행기 클래스. ToolGateway는 registry를 import하지 않고
    runtime이 넘긴 이 값을 쓴다.
    """

    spec: AgentSpec
    prompt: ModuleType
    observer: ModuleType
    executor: type
    exec_contract_version: str


# 메인이 재계획에 주는 접근(무엇을 우선할지). 목적 순서는 접근이 정하고, 방식(범위·조건)은 재계획
# Agent가 고른다 (AG-28·CV-27). 접근 목록은 Case 종류로 나뉜다(사실 조건).
# 일정이 아닌 Case: 변경 최소 / 덜 옮기기(기준에서 옮긴 거리를 먼저 줄인다)
APPROACHES = ("MIN_CHANGE", "MIN_DELAY")
# 일정 넣기 사건이 있는 Case의 세 방향: 기존 위주(기존 작업을 지킨다) / 추가 위주(들어온 작업을 문서
# 자리에 둔다) / 적절하게(전체 변경 최소. 변경 최소와 목적은 같지만 값은 따로 둔다)
DIRECTIONS = ("KEEP_EXISTING", "KEEP_ADDED", "BALANCED")
# 접근 → 목적 순서. 서버가 채운다
APPROACH_OBJECTIVE = {
    "MIN_CHANGE": "CHANGE_FIRST",
    "MIN_DELAY": "DELAY_FIRST",
    "KEEP_EXISTING": "EXISTING_FIRST",
    "KEEP_ADDED": "ADDED_FIRST",
    "BALANCED": "CHANGE_FIRST",
}
# 방향 호출의 탐색 범위는 서버가 작업 전체로 채운다: 세 방향이 같은 범위에서 풀려야 숫자를 비교한다
DIRECTION_SCOPE = "L2"


def approaches_for(schedule_case: bool) -> tuple[str, ...]:
    """그 Case에서 열리는 접근 목록."""
    return DIRECTIONS if schedule_case else APPROACHES


def objective_of(approach: str | None) -> str:
    """접근이 정하는 목적 순서. 접근 없이 부른 재계획은 변경 먼저다."""
    return APPROACH_OBJECTIVE.get(approach or "", "CHANGE_FIRST")


def scope_of(approach: str | None) -> str | None:
    """서버가 채우는 탐색 범위. 방향 호출이 아니면 None(재계획이 고른다)."""
    return DIRECTION_SCOPE if approach in DIRECTIONS else None
