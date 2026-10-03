"""시작 상태 준비(LLM 없음). 스크립트 응답은 Action 이름·인자를 쓰므로 실행 계약 버전별 표로 둔다.

판정기와 달리 여기만 Action 이름에 묶인다. 계약이 바뀌면(skill 인자, 이름 변경) 표에 한 줄을 더한다.
"""

import uuid
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

from app.agents.registry import BINDINGS

PREP_MODEL = "eval-prep"


def _call(name: str, **args: Any) -> AIMessage:
    args = {"decision_summary": "평가 시작 상태 준비(스크립트)", **args}
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": uuid.uuid4().hex}])


def _consulting_v1(task: str) -> dict[str, list[AIMessage]]:
    """요청 A 폼 뒤: Replanning L0·L1 → 첫 후보, Coordination이 담당자에게 변경 요청 → 답 대기."""
    return {
        "REPLANNING": [
            _call("SOLVE_WITH_SCOPE", level="L0"),
            _call("SOLVE_WITH_SCOPE", level="L1"),
        ],
        "COORDINATION": [
            _call("SEND_CHANGE_REQUEST", task_id=task, message="후보의 변경을 확인해 주세요."),
            _call("WAIT_FOR_REPLIES"),
        ],
    }


# (Replanning 계약, Coordination 계약) → 준비 스크립트
CONSULTING: dict[tuple[str, str], Callable[[str], dict[str, list[AIMessage]]]] = {
    ("replanning-d5", "coordination-a24"): _consulting_v1,
}


class _PrepModel:
    """Agent별 응답 큐. bind된 도구가 그 Agent의 Action 집합인지로 어느 Agent인지 안다."""

    model_name = PREP_MODEL

    def __init__(self, queues: dict[str, list[AIMessage]]):
        self.queues = queues
        self.kind = ""

    def bind_tools(self, tools: Sequence[dict[str, Any]], **kwargs: Any) -> "_PrepModel":
        names = {t["function"]["name"] for t in tools}
        self.kind = next(
            agent for agent, b in BINDINGS.items() if names and names <= set(b.spec.actions)
        )
        return self

    def invoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        return self.queues[self.kind].pop(0)


def consulting_factory(task: str) -> Callable[[], _PrepModel] | None:
    """지금 계약 버전의 준비 스크립트. 표에 없으면 None(그 회차는 무효)."""
    key = (
        BINDINGS["REPLANNING"].exec_contract_version,
        BINDINGS["COORDINATION"].exec_contract_version,
    )
    make = CONSULTING.get(key)
    if make is None:
        return None
    queues = make(task)
    return lambda: _PrepModel(queues)
