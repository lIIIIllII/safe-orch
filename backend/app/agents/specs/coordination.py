"""Coordination AgentSpec. 순수 데이터: Goal, Action 스키마, Budget.

store·commands·solver를 import하지 않는다. 사용 조건은 관찰 데이터만 보고 계산한다.
phase CONSULT(후보의 협의): SEND_CHANGE_REQUEST, WAIT_FOR_REPLIES, DRAFT_CONSTRAINT.
phase NOTICE(확정 뒤 통지): SEND_NOTICE. 두 phase 모두 RETURN_RESULT.
협의 완료는 서버가 계산하고, 이견은 담당자가 확인한 뒤에만 제약이 된다.
"""

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agents import skills
from app.agents.types import AgentSpec
from app.domain.needs import ResultFields

AGENT_TYPE = "COORDINATION"
GOAL = (
    "후보가 바꾸는 작업의 담당자와 협의해 동의 대기·이견 항목을 해소하고, 확정된 계획을 관련 담당자에게 "
    "알린다. 협의 완료를 선언하지 않고, 이견을 담당자 확인 없이 제약으로 만들지 않는다."
)

MAX_STEPS = 12
MAX_LLM_ATTEMPTS = 24  # step × 2 (Replanning과 같은 규칙)
RECURSION_LIMIT = MAX_STEPS * 5 + 10
SUMMARY_MAX = 200
TEXT_MAX = 300  # 모델이 쓰는 설명(agent_text) 길이

Axis = Literal["TIME", "RESOURCE"]


class Action(BaseModel):
    """모든 Action의 공통 인자. decision_summary는 저장할 때 200자로 자른다."""

    model_config = ConfigDict(extra="forbid")
    # 열리는 조건 한 줄. prompt의 "도구 전체와 열리는 조건" 절이 이것으로 만든다.
    OPENS: ClassVar[str] = ""

    skill: str = Field(
        description="이번 행동에 쓰는 스킬 ID. 열린 스킬(open_skills) 중 이 도구를 가진 것"
    )
    decision_summary: str = Field(
        description="이유: …/다음: … 형식. 이 행동을 고른 이유와 다음 예정 단계 (200자 이내)"
    )


class SendChangeRequest(Action):
    """작업 담당자에게 후보의 변경을 알리고 수락 또는 이견을 묻는다. 변경 내용(서버 문구)은 서버가 쓴다."""

    OPENS = (
        "후보가 살아 있고, 동의 대기(PENDING) 항목 중 이 Run이 아직 요청하지 않은 작업이 있을 때"
    )

    task_id: str = Field(description="변경 요청을 보낼 협의 항목의 작업 ID")
    message: str = Field(
        min_length=1, max_length=TEXT_MAX, description="담당자에게 보이는 설명(Agent 설명으로 표시)"
    )


class WaitForReplies(Action):
    """보낸 변경 요청·제약 초안의 답을 기다린다. 답이 오면 다시 관찰한다."""

    OPENS = "이 Run이 보낸 요청 중 답을 기다리는 변경 요청이나 확인 대기 중인 제약 초안이 있을 때"


class DraftConstraint(Action):
    """담당자의 이견을 작업·축 고정 제약 초안으로 만들어, 이견을 낸 담당자에게 확인을 요청한다.

    담당자가 확정해야만 제약이 생긴다. 축에는 그 항목에서 바뀐 축이 하나 이상 들어가야 한다.
    """

    OPENS = (
        "이견(DECLINE + 사유)으로 답한 변경 요청이 있고 그 요청에 확인 대기 중인 초안이 없을 때."
        " 이견이 작업 고정 요구가 아니면 쓰지 않아도 된다"
    )

    message_id: str = Field(description="이견으로 답한 변경 요청의 메시지 ID")
    reason_code: str = Field(description="제약 사유 코드")
    task_id: str = Field(description="고정할 작업 ID(그 변경 요청의 작업)")
    axes: list[Axis] = Field(min_length=1, description="고정할 축(TIME·RESOURCE)")
    message: str = Field(
        min_length=1, max_length=TEXT_MAX, description="담당자에게 보이는 설명(Agent 설명으로 표시)"
    )


class SendNotice(Action):
    """확정된 계획을 관련 담당자에게 알린다. 시간·구역·자원·안전 규칙 조치(서버 문구)는 서버가 쓴다."""

    OPENS = "확정 뒤 통지 단계에서, 통지 대상 중 아직 알리지 않은 담당자가 있을 때"

    actor_id: str = Field(description="통지할 담당자 actor_id")
    task_ids: list[str] = Field(min_length=1, description="이 담당자에게 알릴 작업 ID")
    message: str = Field(
        min_length=1, max_length=TEXT_MAX, description="담당자에게 보이는 설명(Agent 설명으로 표시)"
    )


class ReturnResult(Action, ResultFields):
    """협의·통지 결과를 돌려주고 Run을 끝낸다. 협의·후보의 상태는 바꾸지 않는다. 맡은 일을 마쳤으면
    DONE, 해결할 수 없는 상황(응답 불가, 해석할 수 없는 이견, 작업 고정 요구가 아닌 이견 등)이면
    BLOCKED로 돌려주고 필요한 것을 길에 적는다."""

    OPENS = "언제나 열려 있다"


ACTIONS: dict[str, type[Action]] = {
    "SEND_CHANGE_REQUEST": SendChangeRequest,
    "WAIT_FOR_REPLIES": WaitForReplies,
    "DRAFT_CONSTRAINT": DraftConstraint,
    "SEND_NOTICE": SendNotice,
    "RETURN_RESULT": ReturnResult,
}
FLOW = {
    "SEND_CHANGE_REQUEST": "CONTINUE",
    "WAIT_FOR_REPLIES": "WAIT",
    "DRAFT_CONSTRAINT": "CONTINUE",
    "SEND_NOTICE": "CONTINUE",
    "RETURN_RESULT": "DONE",
}
REASON_CODES = ("TASK_IMMOVABLE",)  # 제약을 만드는 사유


def choices(obs: dict[str, Any]) -> dict[str, Any]:
    """Action별 허용 값. Available Actions와 Gateway의 인자 조합 검사가 같이 쓴다.

    REQUEST: 후보가 살아 있고 PENDING이며 이 Run의 변경 요청이 없는 항목.
    DRAFT: 이견(DECLINE + 사유)으로 답한 이 Run의 변경 요청 중 확인 대기 초안이 없는 것
           {message_id: {task_id, changed_axes}}.
    WAIT: 답을 기다리는 변경 요청 또는 확인 대기 초안이 있음.
    NOTICE: 아직 알리지 않은 통지 대상 {actor_id: task_ids}.
    """
    live = bool((obs.get("candidate") or {}).get("live"))
    request, draft, waiting = [], {}, False
    for item in obs["items"]:
        mine = item["requests"]
        if not live:
            continue
        if item["status"] == "PENDING" and not mine:
            request.append(item["task_id"])
        for req in mine:
            pending_draft = (req.get("draft") or {}).get("status") == "PENDING"
            if req["status"] == "OPEN" or pending_draft:
                waiting = True
            if (
                req["status"] == "ANSWERED"
                and req["decision"] == "DECLINE"
                and (req["quoted_comment"] or "").strip()
                and not pending_draft
            ):
                draft[req["message_id"]] = {
                    "task_id": item["task_id"],
                    "changed_axes": item["changed_axes"],
                }
    notice = {t["actor_id"]: list(t["task_ids"]) for t in obs["notice_targets"] if not t["sent"]}
    return {"REQUEST": request, "DRAFT": draft, "WAIT": waiting, "NOTICE": notice}


SKILLS = ("CONSULT", "NOTIFY", "WRAP_UP")
OPEN_ITEM = ("PENDING", "OBJECTED", "OBJECTION_DRAFT_PENDING")


def skill_facts(obs: dict[str, Any]) -> dict[str, bool]:
    """스킬이 열리는 사실. 순서 조건은 없다."""
    live = bool((obs.get("candidate") or {}).get("live"))
    consult = live and any(i["status"] in OPEN_ITEM for i in obs["items"])
    return {
        "has_consult_item": consult,
        "has_unsent_notice": any(not t["sent"] for t in obs["notice_targets"]),
    }


def open_skills(obs: dict[str, Any]) -> list[str]:
    return skills.open_skills(SKILLS, skill_facts(obs))


def available_actions(obs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """{action 이름: 허용 인자 제한}. 열린 스킬의 도구 ∩ 유효한 도구. skill 인자의 허용 값도 넣는다."""
    return skills.available(SKILLS, skill_facts(obs), valid_actions(obs))


def valid_actions(obs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """유효한 인자 값이 있는 도구와 그 값. 조합(메시지·작업, 담당자·작업)은 choices로 Gateway가 다시 검사한다."""
    c = choices(obs)
    out: dict[str, dict[str, Any]] = {}
    if c["REQUEST"]:
        out["SEND_CHANGE_REQUEST"] = {"task_id": sorted(c["REQUEST"])}
    if c["WAIT"]:
        out["WAIT_FOR_REPLIES"] = {}
    if c["DRAFT"]:
        out["DRAFT_CONSTRAINT"] = {
            "message_id": sorted(c["DRAFT"]),
            "reason_code": list(REASON_CODES),
            "task_id": sorted({d["task_id"] for d in c["DRAFT"].values()}),
        }
    if c["NOTICE"]:
        out["SEND_NOTICE"] = {
            "actor_id": sorted(c["NOTICE"]),
            "task_ids": sorted({t for ts in c["NOTICE"].values() for t in ts}),
        }
    out["RETURN_RESULT"] = {}
    return out


def tool_schemas(available: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """bind_tools에 넘길 OpenAI 함수 스키마. 인자 enum은 현재 허용 값만."""
    tools = []
    for name, limits in available.items():
        model = ACTIONS[name]
        params = model.model_json_schema()
        for arg, allowed in limits.items():
            prop = params["properties"][arg]
            (prop["items"] if prop.get("type") == "array" else prop)["enum"] = list(allowed)
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": (model.__doc__ or "").strip(),
                    "parameters": params,
                },
            }
        )
    return tools


SPEC = AgentSpec(
    agent_type=AGENT_TYPE,
    goal=GOAL,
    budget={"steps": MAX_STEPS, "llm_attempts": MAX_LLM_ATTEMPTS},
    recursion_limit=RECURSION_LIMIT,
    summary_max=SUMMARY_MAX,
    actions=ACTIONS,
    skills=SKILLS,
    available_actions=available_actions,
    tool_schemas=tool_schemas,
)
