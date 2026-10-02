"""Replanning Run을 이관까지 닫는 테스트 도우미 (부록 A.30).

A.30부터 ESCALATE_NO_SOLUTION은 다른 Action이 모두 닫혔을 때만 열린다. 이관으로 끝나야 하는 기존
테스트는 이 도우미로 "닫힐 때까지 진행"한다: 바인딩된 도구 중 조회·질문을 고르고(사람 역할은 거절),
이관만 남으면 이관한다. SOLVE·TRY는 고르지 않는다(새 후보 대기가 생기면 테스트가 직접 다룬다).
"""

import uuid
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage
from scripted import ScriptedChatModel, call, escalate

from app.commands.messages import ReplyRequest, reply_message
from app.coordinator.dispatcher import run_until_idle
from app.store import db
from app.store.repos.messages import get_message
from app.store.repos.runs import get_run

MAX_TURNS = 8


def _enum(tool: dict[str, Any], arg: str) -> list[Any]:
    prop = tool["function"]["parameters"]["properties"][arg]
    return list((prop.get("items") or prop)["enum"])


class ClosingModel(ScriptedChatModel):
    """바인딩된 도구를 보고 고른다: 이관만 남으면 이관, 아니면 조회 → 자원 질문 → 시간창 질문."""

    def __init__(self, first: Sequence[AIMessage] = ()) -> None:
        super().__init__(list(first))  # 먼저 쓸 스크립트 응답, 다 쓰면 도구를 보고 고른다

    def invoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        if self.replies:
            return super().invoke(messages)
        tools, kwargs = self._bound
        self.calls.append({"tools": tools, "kwargs": kwargs, "messages": list(messages)})
        by_name = {t["function"]["name"]: t for t in tools}
        if "LIST_ASSIGNABLE_RESOURCES" in by_name:
            task = _enum(by_name["LIST_ASSIGNABLE_RESOURCES"], "task_id")[0]
            return call("LIST_ASSIGNABLE_RESOURCES", "자원 조회", task_id=task)
        if "ASK_TASK_OWNER" in by_name:
            tool = by_name["ASK_TASK_OWNER"]
            return call(
                "ASK_TASK_OWNER",
                "자원 확인",
                task_id=_enum(tool, "task_id")[0],
                axis="RESOURCE",
                allowed_values=_enum(tool, "allowed_values"),
                question="이 자원을 써도 되나요?",
            )
        if "ASK_WINDOW_CHANGE" in by_name:
            task = _enum(by_name["ASK_WINDOW_CHANGE"], "task_id")[0]
            return call("ASK_WINDOW_CHANGE", "시간창 확인", task_id=task, question="넓혀도 되나요?")
        if list(by_name) == ["ESCALATE_NO_SOLUTION"]:
            return escalate("열린 조회·확인이 모두 닫혔다")
        raise AssertionError(f"ClosingModel이 고를 수 없는 도구: {sorted(by_name)}")


def close_to_escalation(pack: Any, run_id: str, model: ClosingModel | None = None) -> list[str]:
    """run_id를 이관까지 진행한다. 질문이 오면 수신자로 거절한다. 거절한 메시지 ID 목록을 돌려준다."""
    model = model or ClosingModel()
    declined = []
    for _ in range(MAX_TURNS):
        run_until_idle(pack, model_factory=lambda: model)
        with db.read() as conn:
            run = get_run(conn, run_id)
            message = (
                get_message(conn, pack.site_id, run.wait_ref)
                if run.status == "WAITING_HUMAN" and run.wait_kind == "MESSAGE"
                else None
            )
        if message is None or message["status"] != "OPEN":
            return declined
        body = ReplyRequest(message_id=message["message_id"], decision="DECLINE", comment="거절")
        reply_message(pack, message["to_actor_id"], uuid.uuid4().hex, body)
        declined.append(message["message_id"])
    raise AssertionError("close_to_escalation: 사람 응답 반복 상한")


def cancel_running(pack: Any, reply: AIMessage | None = None):
    """모델 호출 중 Supervisor가 RUNNING Replanning Run을 취소한다(스크립트 응답 자리에 쓴다).

    이관이 Run을 끝내는 수단일 뿐이고 종료 상태를 확인하지 않는 테스트용이다(A.30). 그 step은
    취소 명령이 ABORTED(abort_reason CANCELLED)로 두고 Run은 CANCELLED다.
    """
    from app.commands.runs import CancelRun, cancel_run

    def act() -> AIMessage:
        with db.read() as conn:
            [run_id] = [
                r[0]
                for r in conn.execute(
                    "SELECT run_id FROM agent_run WHERE status = 'RUNNING'"
                    " AND agent_type = 'REPLANNING'"
                )
            ]
        out = cancel_run(pack, "supervisor", uuid.uuid4().hex, CancelRun(run_id=run_id))
        assert out.status == "APPLIED", out.reason_codes
        return reply or escalate("취소된 Run")

    return act
