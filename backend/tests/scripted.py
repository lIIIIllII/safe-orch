"""스크립트 LLM (설계서 §16 "decide의 모델을 스크립트 응답으로", 부록 A.16).

llm.ChatModel 프로토콜(bind_tools + invoke)을 따른다. 응답은 AIMessage 또는 AIMessage를 돌려주는
함수(호출 순간의 부수 효과를 주입할 때)다. 응답이 모자라면 예외를 내고, 그 Run은 ERROR가 된다.
"""

import uuid
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

Reply = AIMessage | Callable[[], AIMessage]


def call(name: str, summary: str = "다음 전략을 시도한다", **args: Any) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": name, "args": {"decision_summary": summary, **args}, "id": uuid.uuid4().hex}
        ],
    )


def solve(level: str, summary: str = "범위를 정해 계산한다") -> AIMessage:
    return call("SOLVE_WITH_SCOPE", summary, level=level)


def escalate(reason: str = "더 시도할 전략이 없다") -> AIMessage:
    return call("ESCALATE_NO_SOLUTION", "이관한다", reason=reason)


class ScriptedChatModel:
    model_name = "scripted"

    def __init__(self, replies: Sequence[Reply]):
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []
        self._bound: tuple[list[dict[str, Any]], dict[str, Any]] = ([], {})

    def bind_tools(self, tools: Sequence[dict[str, Any]], **kwargs: Any) -> "ScriptedChatModel":
        self._bound = (list(tools), kwargs)
        return self

    def invoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        tools, kwargs = self._bound
        self.calls.append({"tools": tools, "kwargs": kwargs, "messages": list(messages)})
        if not self.replies:
            raise RuntimeError("script exhausted")
        reply = self.replies.pop(0)
        return reply() if callable(reply) else reply

    def tool_names(self, i: int) -> list[str]:
        return [t["function"]["name"] for t in self.calls[i]["tools"]]
