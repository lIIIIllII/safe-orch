"""시작 상태 준비(LLM 없음). 스크립트 응답은 Action 이름·인자를 쓰므로 실행 계약 버전별 표로 둔다.

판정기와 달리 여기만 Action 이름에 묶인다. 계약이 바뀌면(skill 인자, 이름 변경) 표에 한 줄을 더한다.
메인은 관찰의 "지금 받아들여지는 호출"에서 고른다(참조는 실행 중에 정해진다): 협의가 있으면 협의,
없으면 첫 호출.
"""

import json
import uuid
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

from app.agents.registry import BINDINGS

PREP_MODEL = "eval-prep"


def _call(name: str, skill: str | None = None, **args: Any) -> AIMessage:
    args = {"decision_summary": "평가 시작 상태 준비(스크립트)", **args}
    if skill is not None:
        args["skill"] = skill
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


def _consulting_v2(task: str) -> dict[str, list[AIMessage]]:
    """v1과 같은 행동. 스킬 층 계약(모든 Action에 skill 인자)."""
    return {
        "REPLANNING": [
            _call("SOLVE_WITH_SCOPE", "BUILD_CANDIDATE", level="L0"),
            _call("SOLVE_WITH_SCOPE", "BUILD_CANDIDATE", level="L1"),
        ],
        "COORDINATION": [
            _call(
                "SEND_CHANGE_REQUEST",
                "CONSULT",
                task_id=task,
                message="후보의 변경을 확인해 주세요.",
            ),
            _call("WAIT_FOR_REPLIES", "CONSULT"),
        ],
    }


def _consulting_v3(task: str) -> dict[str, list[AIMessage]]:
    """메인이 부른다: 메인 → Replanning L0·L1 → 검증 뒤 DONE → 메인 → Coordination 변경 요청 → 답 대기."""
    queues = _consulting_v2(task)
    queues["REPLANNING"].append(
        _call("RETURN_RESULT", "WRAP_UP", status="DONE", summary="평가 시작 상태 준비(스크립트)")
    )
    return queues


# (메인 계약, Replanning 계약, Coordination 계약) → 준비 스크립트. 메인이 없던 계약은 None
CONSULTING: dict[tuple[str | None, str, str], Callable[[str], dict[str, list[AIMessage]]]] = {
    (None, "replanning-d5", "coordination-a24"): _consulting_v1,
    (None, "replanning-c2", "coordination-c2"): _consulting_v2,
    (
        None,
        "replanning-c2",
        "coordination-c3",
    ): _consulting_v2,  # 스킬 상대별 분할(CONSULT는 그대로)
    (None, "replanning-c3", "coordination-c4"): _consulting_v2,  # 종료 도구만 RETURN_RESULT로 바뀜
    ("main-c1", "replanning-c4", "coordination-c5"): _consulting_v3,  # 메인이 부른다, 검증 뒤 DONE
}


def _main_call(messages: Sequence[BaseMessage]) -> AIMessage:
    """메인의 준비 응답: 받아들여지는 호출 중 협의가 있으면 협의, 없으면 첫 호출."""
    obs = json.loads(str(messages[1].content).split("\n", 1)[1])
    calls = obs["calls"]
    chosen = next((c for c in calls if c["agent"] == "COORDINATION"), calls[0])
    return _call("CALL_AGENT", "ORCHESTRATE", **chosen)


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
        if self.kind == "MAIN":
            return _main_call(messages)
        return self.queues[self.kind].pop(0)


def consulting_factory(task: str) -> Callable[[], _PrepModel] | None:
    """지금 계약 버전의 준비 스크립트. 표에 없으면 None(그 회차는 무효)."""
    main = BINDINGS.get("MAIN")
    key = (
        None if main is None else main.exec_contract_version,
        BINDINGS["REPLANNING"].exec_contract_version,
        BINDINGS["COORDINATION"].exec_contract_version,
    )
    make = CONSULTING.get(key)
    if make is None:
        return None
    queues = make(task)
    return lambda: _PrepModel(queues)
