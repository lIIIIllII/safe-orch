"""스크립트 LLM.

llm.ChatModel 프로토콜(bind_tools + invoke)을 따른다. 응답은 AIMessage 또는 AIMessage를 돌려주는
함수(호출 순간의 부수 효과를 주입할 때)다. 응답이 모자라면 예외를 내고, 그 Run은 ERROR가 된다.
"""

import uuid
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

from app.agents import skills
from app.agents.registry import BINDINGS
from app.domain.calendar import site_time
from app.packs.loader import load_pack, pack_dir

Reply = AIMessage | Callable[[], AIMessage]

# 도구별 기본 스킬: 그 도구를 가진 Agent의 스킬 목록에서 첫 번째. 다른 스킬로 부르려면 skill 인자를 준다
DEFAULT_SKILL = {
    name: skills.default_skill(b.spec.skills, name)
    for b in BINDINGS.values()
    for name in b.spec.actions
}


_PACK = load_pack(pack_dir("shipyard"))
TIME_FIELDS = ("earliest_start", "latest_start", "latest_end", "new_earliest_start")


def site(minute: int) -> str:
    """Horizon 원점 기준 분 → 도구의 시각 인자 형식(현장 날짜·시각 문자열, AG-21)."""
    return site_time(_PACK.horizon_start_utc, _PACK.timezone, minute)


def _times(args: dict[str, Any]) -> dict[str, Any]:
    """테스트는 시각을 분으로 쓴다. 모델이 보내는 모양(문자열)으로 바꾼다."""
    out = {k: site(v) if k in TIME_FIELDS and isinstance(v, int) else v for k, v in args.items()}
    if isinstance(out.get("values"), dict):
        out["values"] = _times(out["values"])
    return out


JUDGED_FIELDS = ("work_type", "zone_id", "duration", "window", "resource")


def field_judgments(*open_fields: str, ambiguous: Sequence[str] = (), **values: Any) -> dict:
    """ASK_CLARIFICATION의 필드별 판단. open_fields는 빠짐, ambiguous는 모호, 나머지는 받음."""
    return {
        f: {
            "status": "MISSING"
            if f in open_fields
            else "AMBIGUOUS"
            if f in ambiguous
            else "RECEIVED",
            "value": values.get(f),
        }
        for f in JUDGED_FIELDS
    }


def call(name: str, summary: str = "다음 전략을 시도한다", **args: Any) -> AIMessage:
    args = _times(args)
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": name,
                "args": {
                    "decision_summary": summary,
                    "skill": DEFAULT_SKILL.get(name, "WRAP_UP"),
                    **args,
                },
                "id": uuid.uuid4().hex,
            }
        ],
    )


def solve(level: str, summary: str = "범위를 정해 계산한다") -> AIMessage:
    return call("SOLVE_WITH_SCOPE", summary, level=level)


def _result(decision: str, status: str, summary: str, paths: Sequence[dict]) -> AIMessage:
    message = call("RETURN_RESULT", decision, status=status, paths=list(paths))
    message.tool_calls[0]["args"]["summary"] = summary  # call의 summary는 decision_summary다
    return message


def blocked(summary: str = "더 시도할 전략이 없다", paths: Sequence[dict] = ()) -> AIMessage:
    """RETURN_RESULT(BLOCKED). paths는 [{"needs": [{"kind": ..., ...}]}]."""
    return _result("막힌 결과를 돌려준다", "BLOCKED", summary, paths)


def done(summary: str = "맡은 일을 마쳤다") -> AIMessage:
    """RETURN_RESULT(DONE)."""
    return _result("결과를 돌려준다", "DONE", summary, ())


def main_call(agent: str, **refs: Any) -> AIMessage:
    """메인의 CALL_AGENT. 참조만 넘긴다."""
    return call("CALL_AGENT", "이유: 부른다/다음: 결과를 본다", agent=agent, **refs)


def main_wait() -> AIMessage:
    return call("WAIT", "이유: 사람의 결정을 기다린다/다음: 다시 본다")


def main_close(text: str = "맡은 일을 마쳤다") -> AIMessage:
    message = call("CLOSE", "이유: 열린 일이 없다/다음: 종료")
    message.tool_calls[0]["args"]["summary"] = text
    return message


def main_escalate(text: str = "풀 수 없다", needs: Sequence[dict] = ()) -> AIMessage:
    message = call("ESCALATE", "이유: 풀 수 없다/다음: 이관", needs=list(needs))
    message.tool_calls[0]["args"]["summary"] = text
    return message


def escalate(reason: str = "더 시도할 전략이 없다") -> AIMessage:
    """부모 없는 Replanning Run의 막힌 결과. 임시 연결로 Run은 ESCALATED가 된다."""
    return blocked(reason)


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


AGENT_TITLES = {
    "MAIN": "Main Agent",
    "INTAKE": "Work Intake Agent",
    "COORDINATION": "Coordination Agent",
    "EVENT_RESPONSE": "Event Response Agent",
    "REPLANNING": "Replanning Agent",
}


class Router:
    """agent_type별 응답 큐. 한 model_factory로 Replanning·Coordination Run을 함께 돌린다.

    모델은 System 첫 문장("너는 SAFE-ORCH의 … Agent다")으로 어느 Agent인지 안다.
    """

    def __init__(
        self,
        replanning: Sequence[Reply] = (),
        coordination: Sequence[Reply] = (),
        event_response: Sequence[Reply] = (),
        intake: Sequence[Reply] = (),
        main: Sequence[Reply] = (),
    ):
        self.queues = {
            "MAIN": list(main),
            "REPLANNING": list(replanning),
            "COORDINATION": list(coordination),
            "EVENT_RESPONSE": list(event_response),
            "INTAKE": list(intake),
        }
        self.models: list[RoutedChatModel] = []

    def factory(self) -> Callable[[], "RoutedChatModel"]:
        def make() -> RoutedChatModel:
            model = RoutedChatModel(self)
            self.models.append(model)
            return model

        return make

    def left(self) -> dict[str, int]:
        return {k: len(v) for k, v in self.queues.items() if k != "MAIN" or v}


class RoutedChatModel(ScriptedChatModel):
    def __init__(self, router: Router):
        super().__init__([])
        self.router = router

    def invoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        tools, kwargs = self._bound
        self.calls.append({"tools": tools, "kwargs": kwargs, "messages": list(messages)})
        head = str(messages[0].content)[:60]
        kind = next(k for k, title in AGENT_TITLES.items() if title in head)
        queue = self.router.queues[kind]
        if not queue:
            raise RuntimeError("script exhausted")
        reply = queue.pop(0)
        return reply() if callable(reply) else reply
