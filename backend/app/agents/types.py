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


# 메인이 재계획에 주는 접근(무엇을 우선할지). 방식은 재계획 Agent가 고른다 (AG-28)
# 변경 최소 / 희망 우선(희망에서 벗어난 정도를 먼저 줄인다)
APPROACHES = ("MIN_CHANGE", "PREFER_WINDOW")
