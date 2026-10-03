"""스크립트 LLM.

llm.ChatModel 프로토콜(bind_tools + invoke)을 따른다. 응답은 AIMessage 또는 AIMessage를 돌려주는
함수(호출 순간의 부수 효과를 주입할 때)다. 응답이 모자라면 예외를 내고, 그 Run은 ERROR가 된다.

판단을 시험하지 않는 자리에는 기본 응답(auto)이 있다. 스크립트를 주지 않은 Agent에만 쓴다:
- 메인: 통지 → 신고 대응 → 협의 → 사전 확인(요청 작업의 담당자 확인만) → 재계획(요청 작업을 가진
  Unit) 순으로 받아들여지는 호출을 부르고, 없으면 기다릴 것이 있을 때 WAIT, 열린 일이 없으면 CLOSE,
  그것도 아니면 ESCALATE.
- Replanning: 검증을 통과한 살아 있는 후보가 있으면 RETURN_RESULT(DONE). 스크립트보다 먼저 본다
  (Run이 깨어날 때마다 모델이 새로 만들어져 스크립트가 처음부터 다시 시작되기 때문이다).
- Coordination: 협의는 요청할 항목마다 변경 요청 → 답 대기 → 결과, 통지는 대상마다 통지 → 결과,
  사전 확인은 확인마다 질문 → 답 대기 → 결과.
"""

import json
import uuid
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

from app.agents import skills
from app.agents.registry import BINDINGS
from app.domain.calendar import site_time
from app.packs.loader import load_pack, pack_dir
from app.store import db

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


def ask_owner(index: int = 0, message: str = "대체 자원을 써도 되는지 확인해 주세요.") -> Reply:
    """Coordination 사전 확인의 ASK_OWNER. need_id는 부를 때 정해지므로 가장 최근 사전 확인 Run에서 읽는다."""

    def make() -> AIMessage:
        with db.read() as conn:
            row = conn.execute(
                "SELECT input_ref FROM agent_run WHERE agent_type = 'COORDINATION'"
                " AND json_extract(input_ref, '$.phase') = 'ASK' ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        need_id = json.loads(row[0])["need_ids"][index]
        return call("ASK_OWNER", need_id=need_id, message=message)

    return make


def wait_answers() -> AIMessage:
    """사전 확인의 답 대기."""
    return call("WAIT_FOR_REPLIES", skill="PRE_CONFIRM")


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
    """Replanning의 막힌 결과(길 없음). 서버가 열 수 있는 것을 붙이고, 다음은 메인이 정한다."""
    return blocked(reason)


AGENT_TITLES = {
    "MAIN": "Main Agent",
    "INTAKE": "Work Intake Agent",
    "COORDINATION": "Coordination Agent",
    "EVENT_RESPONSE": "Event Response Agent",
    "REPLANNING": "Replanning Agent",
}


def agent_kind(messages: Sequence[BaseMessage]) -> str:
    """System 첫 문장("너는 SAFE-ORCH의 … Agent다")으로 어느 Agent인지 안다."""
    head = str(messages[0].content)[:60]
    return next((k for k, title in AGENT_TITLES.items() if title in head), "REPLANNING")


def _observation(messages: Sequence[BaseMessage]) -> dict[str, Any]:
    return json.loads(str(messages[1].content).split("\n", 1)[1])


def _limits(tools: Sequence[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for t in tools:
        if t["function"]["name"] == name:
            return t["function"]["parameters"]["properties"]
    return None


def _auto_ask(obs: dict[str, Any], ask: dict[str, Any] | None) -> dict[str, Any] | None:
    """기본 응답의 사전 확인: 물을 수 있는 확인 가운데 아직 계획에 없는 요청 작업의 것만 묻는다."""
    if ask is None:
        return None
    units = [u for g in obs["groups"] for u in g["units"]]
    requests = {t for u in units for t in u["request_task_ids"]}
    needs = {}
    for u in units:
        last = u["last_result"] or {}
        found = [n for p in last.get("paths", []) for n in p["needs"]] + last.get("openers", [])
        needs.update({n["need_id"]: n for n in found})
    ids, seen = [], []
    for i in ask["need_ids"]:  # 정렬돼 있다: Agent가 엮은 길의 need(p)가 서버 need(s)보다 앞이다
        what = (needs[i].get("task_id"), needs[i].get("values"))
        if what[0] in requests and what not in seen:
            ids.append(i)
            seen.append(what)
    return {**ask, "need_ids": ids} if ids else None


def auto_main(obs: dict[str, Any]) -> AIMessage:
    """메인의 기본 응답(판단을 시험하지 않는 자리)."""
    calls = obs["calls"]

    def first(**want: Any) -> dict[str, Any] | None:
        return next((c for c in calls if all(c.get(k) == v for k, v in want.items())), None)

    # 막힌 재계획은 다시 부르지 않는다. 그 뒤 사전 확인을 마쳤으면 사실이 바뀐 것이라 다시 부른다
    failed: set[tuple[Any, Any]] = set()
    for r in obs["child_results"]:
        if not r["call"]:
            continue
        if r["call"].get("phase") == "ASK" and r["run_status"] == "SUCCEEDED":
            failed.clear()
        elif r["run_status"] != "SUCCEEDED":
            failed.add((r["call"].get("group_id"), r["call"].get("acting_unit_id")))
    requesters = {
        (g["group_id"], u["unit_id"])
        for g in obs["groups"]
        for u in g["units"]
        if u["request_task_ids"] or not any(x["request_task_ids"] for x in g["units"])
    } - failed
    replanning = next(
        (
            c
            for c in calls
            if c["agent"] == "REPLANNING" and (c["group_id"], c["acting_unit_id"]) in requesters
        ),
        None,
    )
    chosen = (
        first(agent="COORDINATION", phase="NOTICE")
        or first(agent="EVENT_RESPONSE")
        or first(agent="COORDINATION", phase="CONSULT")
        or _auto_ask(obs, first(agent="COORDINATION", phase="ASK"))
        # 검토 대기 후보가 있으면 재계획을 더 부르지 않고 기다린다
        or (None if obs["waiting_for"]["candidates"] else replanning)
    )
    if chosen is not None and obs["budget_remaining"]["agent_calls"] > 0:
        return main_call(**chosen)
    if obs["waiting_for"]["candidates"] or obs["waiting_for"]["holds"]:
        return main_wait()
    return main_close() if not obs["open_work"] else main_escalate()


def auto_coordination(obs: dict[str, Any], tools: Sequence[dict[str, Any]]) -> AIMessage:
    """Coordination의 기본 응답: 요청·질문·통지를 빠짐없이 보내고, 기다릴 것이 있으면 기다리고, 끝낸다."""
    send, notice = _limits(tools, "SEND_CHANGE_REQUEST"), _limits(tools, "SEND_NOTICE")
    ask, wait = _limits(tools, "ASK_OWNER"), _limits(tools, "WAIT_FOR_REPLIES")
    if ask is not None:
        need_id = ask["need_id"]["enum"][0]
        return call("ASK_OWNER", need_id=need_id, message="대체 자원을 써도 되는지 확인해 주세요.")
    if send is not None:
        task_id = send["task_id"]["enum"][0]
        return call("SEND_CHANGE_REQUEST", task_id=task_id, message="후보의 변경을 확인해 주세요.")
    if notice is not None:
        target = next(t for t in obs["notice_targets"] if not t["sent"])
        return call(
            "SEND_NOTICE",
            actor_id=target["actor_id"],
            task_ids=target["task_ids"],
            message="확정된 계획을 알립니다.",
        )
    if wait is not None:
        return call("WAIT_FOR_REPLIES", skill=wait["skill"]["enum"][0])
    return done("협의·통지 결과를 돌려준다")


def auto_reply(
    kind: str, tools: Sequence[dict[str, Any]], messages: Sequence[BaseMessage]
) -> AIMessage | None:
    """그 Agent의 기본 응답. 없으면 None."""
    if kind == "MAIN":
        return auto_main(_observation(messages))
    if kind == "COORDINATION":
        return auto_coordination(_observation(messages), tools)
    if kind == "REPLANNING":
        result = _limits(tools, "RETURN_RESULT")
        if result is not None and "DONE" in result["status"]["enum"]:
            return done("검증을 통과한 후보가 있다")
    return None


class ScriptedChatModel:
    """스크립트는 Replanning(과 Intake·Event Response)의 응답이다. 메인과 Coordination은 기본 응답으로
    돈다(auto=False면 스크립트만 쓴다). Agent별 스크립트가 필요하면 Router를 쓴다."""

    model_name = "scripted"

    def __init__(self, replies: Sequence[Reply], auto: bool = True):
        self.replies = list(replies)
        self.auto = auto
        self.calls: list[dict[str, Any]] = []
        self._bound: tuple[list[dict[str, Any]], dict[str, Any]] = ([], {})

    def bind_tools(self, tools: Sequence[dict[str, Any]], **kwargs: Any) -> "ScriptedChatModel":
        self._bound = (list(tools), kwargs)
        return self

    def invoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        tools, kwargs = self._bound
        self.calls.append({"tools": tools, "kwargs": kwargs, "messages": list(messages)})
        if self.auto and (reply := auto_reply(agent_kind(messages), tools, messages)) is not None:
            return reply
        if not self.replies:
            raise RuntimeError("script exhausted")
        reply = self.replies.pop(0)
        return reply() if callable(reply) else reply

    def tool_names(self, i: int) -> list[str]:
        return [t["function"]["name"] for t in self.calls[i]["tools"]]


class Router:
    """agent_type별 응답 큐. 한 model_factory로 Replanning·Coordination Run을 함께 돌린다.

    모델은 System 첫 문장("너는 SAFE-ORCH의 … Agent다")으로 어느 Agent인지 안다.
    스크립트를 준 Agent는 스크립트대로, 주지 않은 메인·Coordination은 기본 응답으로 돈다.
    Replanning은 DONE을 낼 수 있으면 기본 응답으로 낸다(auto_done=False면 스크립트대로).
    """

    def __init__(
        self,
        replanning: Sequence[Reply] = (),
        coordination: Sequence[Reply] = (),
        event_response: Sequence[Reply] = (),
        intake: Sequence[Reply] = (),
        main: Sequence[Reply] = (),
        auto_done: bool = True,
    ):
        self.auto_done = auto_done
        self.scripted = {
            kind for kind, replies in (("MAIN", main), ("COORDINATION", coordination)) if replies
        }
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
        kind = agent_kind(messages)
        queue = self.router.queues[kind]
        use_auto = (
            self.router.auto_done if kind == "REPLANNING" else kind not in self.router.scripted
        )
        if use_auto and (auto := auto_reply(kind, tools, messages)) is not None:
            return auto
        if not queue:
            raise RuntimeError("script exhausted")
        reply = queue.pop(0)
        return reply() if callable(reply) else reply
