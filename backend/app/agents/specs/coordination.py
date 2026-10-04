"""Coordination AgentSpec. 순수 데이터: Goal, Action 스키마, Budget.

store·commands·solver를 import하지 않는다. 사용 조건은 관찰 데이터만 보고 계산한다.
phase CONSULT(후보의 협의): SEND_CHANGE_REQUEST, WAIT_FOR_REPLIES.
phase NOTICE(확정 뒤 통지): SEND_NOTICE.
모든 phase에 RETURN_RESULT.
협의 완료는 서버가 계산하고, 담당자의 이견은 결과에 담아 돌려준다(고정은 사람만 한다, AG-27).
"""

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from app.agents import skills
from app.agents.types import AgentSpec
from app.domain.needs import ResultFields

AGENT_TYPE = "COORDINATION"
GOAL = (
    "후보가 바꾸는 작업의 담당자와 협의해 확인 대기·이견 항목을 해소하고, 확정된 계획을 관련 담당자에게 "
    "알린다. 후보가 없는 사전 확인에서는 맡은 확인을 담당자에게 묻고 답을 그대로 돌려준다. "
    "협의 완료를 선언하지 않고, 담당자의 이견은 결과에 담아 돌려준다."
)

MAX_STEPS = 12
MAX_LLM_ATTEMPTS = 24  # step × 2 (Replanning과 같은 규칙)
RECURSION_LIMIT = MAX_STEPS * 5 + 10
SUMMARY_MAX = 200
TEXT_MAX = 300  # 모델이 쓰는 설명(agent_text) 길이


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
    """담당자 한 명에게 후보의 변경을 한 통으로 알리고 항목마다 수락 또는 이견을 묻는다. 항목은 서버가
    채운다: 그 담당자의 확인 대기 항목 가운데 이 Run이 아직 묻지 않은 것 전부다. 변경 내용(서버 문구)도
    서버가 쓴다."""

    OPENS = (
        "후보가 살아 있고, 확인 대기(PENDING) 항목 중 이 Run이 아직 요청하지 않은 것이 있는 담당자가 "
        "있을 때"
    )

    actor_id: str = Field(description="변경 요청을 보낼 담당자 actor_id")
    message: str = Field(
        min_length=1, max_length=TEXT_MAX, description="담당자에게 보이는 설명(Agent 설명으로 표시)"
    )


class WaitForReplies(Action):
    """보낸 변경 요청의 답을 기다린다. 답이 오면 다시 관찰한다."""

    OPENS = "이 Run이 보낸 요청 중 답을 기다리는 변경 요청이 있을 때"


class SendNotice(Action):
    """확정된 계획을 관련 담당자에게 알린다. 시간·구역·자원·안전 규칙 조치(서버 문구)는 서버가 쓴다."""

    OPENS = "확정 뒤 통지 단계에서, 통지 대상 중 아직 알리지 않은 담당자가 있을 때"

    actor_id: str = Field(description="통지할 담당자 actor_id")
    task_ids: list[str] = Field(min_length=1, description="이 담당자에게 알릴 작업 ID")
    message: str = Field(
        min_length=1, max_length=TEXT_MAX, description="담당자에게 보이는 설명(Agent 설명으로 표시)"
    )


class ReturnResult(Action, ResultFields):
    """협의·통지 결과를 돌려주고 Run을 끝낸다. 협의·후보의 상태는 바꾸지 않는다. 맡은 일을
    마쳤으면 DONE, 해결할 수 없는 상황(응답 불가, 담당자의 이견 등)이면
    BLOCKED로 돌려주고 필요한 것을 길에 적는다."""

    OPENS = "언제나 열려 있다"


ACTIONS: dict[str, type[Action]] = {
    "SEND_CHANGE_REQUEST": SendChangeRequest,
    "WAIT_FOR_REPLIES": WaitForReplies,
    "SEND_NOTICE": SendNotice,
    "RETURN_RESULT": ReturnResult,
}
FLOW = {
    "SEND_CHANGE_REQUEST": "CONTINUE",
    "WAIT_FOR_REPLIES": "WAIT",
    "SEND_NOTICE": "CONTINUE",
    "RETURN_RESULT": "DONE",
}


def choices(obs: dict[str, Any]) -> dict[str, Any]:
    """Action별 허용 값. Available Actions와 Gateway의 인자 조합 검사가 같이 쓴다.

    REQUEST: 보낼 항목이 남은 담당자 {actor_id: task_ids}. 항목 = 후보가 살아 있고 PENDING이며 이 Run의
    변경 요청이 없는 것. 한 통에 담는 순서(바뀐 뒤 시작 시각순)다 (ST-26).
    WAIT: 답을 기다리는 변경 요청이 있음.
    NOTICE: 아직 알리지 않은 통지 대상 {actor_id: task_ids}.
    """
    live = bool((obs.get("candidate") or {}).get("live"))
    request: dict[str, list[str]] = {}
    waiting = False
    for item in sorted(obs["items"], key=lambda i: (i["after"]["start"], i["task_id"])):
        mine = item["requests"]
        if not live:
            continue
        if item["status"] == "PENDING" and not mine:
            request.setdefault(item["owner_actor_id"], []).append(item["task_id"])
        if any(req["status"] == "OPEN" for req in mine):
            waiting = True
    notice = {t["actor_id"]: list(t["task_ids"]) for t in obs["notice_targets"] if not t["sent"]}
    return {"REQUEST": request, "WAIT": waiting, "NOTICE": notice}


SKILLS = ("CONSULT", "NOTIFY", "WRAP_UP")
OPEN_ITEM = ("PENDING", "OBJECTED")


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
    """유효한 인자 값이 있는 도구와 그 값. 조합(담당자·작업)은 choices로 Gateway가 다시 검사한다."""
    c = choices(obs)
    out: dict[str, dict[str, Any]] = {}
    if c["REQUEST"]:
        out["SEND_CHANGE_REQUEST"] = {"actor_id": sorted(c["REQUEST"])}
    if c["WAIT"]:
        out["WAIT_FOR_REPLIES"] = {}
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
