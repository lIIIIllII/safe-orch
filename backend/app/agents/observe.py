"""Observation의 agent_type 공통 부분 (설계서 §11.2 observe, 부록 A.16·A.23).

store를 import하지 않는다. agent_type별 관찰 계산은 observers/<agent_type>.py에 있다.
"""

from dataclasses import dataclass
from typing import Any

from app.agents.types import AgentSpec
from app.domain.models import AgentRun


@dataclass(frozen=True)
class Observation:
    run: AgentRun
    versions: tuple[int, int, int]  # (context_version, plan_revision, wake_seq)
    data: dict[str, Any]
    available: dict[str, dict[str, Any]]
    spec: AgentSpec

    @property
    def active(self) -> bool:
        return self.run.status == "RUNNING"

    @property
    def budget_exhausted(self) -> bool:
        return (
            self.run.steps_used >= self.spec.budget["steps"]
            or self.run.llm_attempts_used >= self.spec.budget["llm_attempts"]
        )


def budget_remaining(run: AgentRun, spec: AgentSpec) -> dict[str, float]:
    """spec이 한도를 둔 카운터마다 남은 양 (Replanning: steps·llm_attempts·human_rounds·solver_calls)."""
    used = run.budget_used
    return {name: limit - used[name] for name, limit in spec.budget.items()}
