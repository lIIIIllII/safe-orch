"""Observation의 agent_type 공통 부분.

store를 import하지 않는다. agent_type별 관찰 계산은 observers/<agent_type>.py에 있다.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
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
    # 서버만 아는 사실(모델에 보이지 않는다): 유효성 판정에 쓴다
    hidden: dict[str, Any] = field(default_factory=dict)
    # 이 Run의 한도가 spec과 다를 때만 준다 (메인, AG-30)
    limits: Mapping[str, int] | None = None

    @property
    def active(self) -> bool:
        return self.run.status == "RUNNING"

    @property
    def budget_exhausted(self) -> bool:
        limits = self.limits or self.spec.budget
        return (
            self.run.steps_used >= limits["steps"]
            or self.run.llm_attempts_used >= limits["llm_attempts"]
        )


def budget_remaining(
    run: AgentRun, spec: AgentSpec, limits: Mapping[str, int] | None = None
) -> dict[str, float]:
    """한도를 둔 카운터마다 남은 양 (Replanning: steps·llm_attempts·human_rounds·solver_calls).

    limits를 주지 않으면 spec의 한도다."""
    used = run.budget_used
    return {name: limit - used[name] for name, limit in (limits or spec.budget).items()}


def recent_steps(steps: list[dict[str, Any]], n: int = 5) -> list[dict[str, Any]]:
    """완료된 step 중 최근 n개의 요약 (Coordination이 쓴다. Replanning 관찰은 같은 값을 직접 만든다)."""
    return [
        {
            "step_no": s["step_no"],
            "action": (s["action"] or {}).get("name"),
            "result_kind": s["result_kind"],
            "guard": s["guard"],
        }
        for s in steps[-n:]
    ]


def last_guard(steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    """직전 완료 step이 REJECTED면 그 guard."""
    return steps[-1]["guard"] if steps and steps[-1]["guard"]["verdict"] == "REJECTED" else None
