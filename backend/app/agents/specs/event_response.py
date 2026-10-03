"""Event Response AgentSpec. 순수 데이터: Goal, Action 스키마, Budget.

store·commands·solver를 import하지 않는다. 사용 조건은 관찰 데이터만 보고 계산한다.
Action: LOOKUP_TASKS, ANALYZE_IMPACT, PROPOSE_FACT_UPDATE, ASK_REPORTER, RETURN_RESULT.
Hold 해제·사실 직접 적용은 할 수 없다. 사실 수정은 Supervisor가 확인해야 효력이 생긴다.
"""

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from app.agents import skills
from app.agents.types import AgentSpec
from app.domain.needs import ResultFields

AGENT_TYPE = "EVENT_RESPONSE"
GOAL = (
    "지연 신고의 대상 작업과 새 시작 가능 시각을 파악해, Supervisor가 확인할 사실 수정안을 만든다. "
    "Hold를 해제하거나 사실을 직접 바꾸지 않는다."
)

MAX_STEPS = 10
MAX_LLM_ATTEMPTS = 20  # step × 2 (Replanning과 같은 규칙)
MAX_HUMAN_ROUNDS = 2  # ASK_REPORTER용
RECURSION_LIMIT = MAX_STEPS * 5 + 10
SUMMARY_MAX = 200
TEXT_MAX = 300


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


class LookupTasks(Action):
    """현재 계산 대상(READY) 작업을 작업 유형·구역으로 찾는다. 담당·시간창·현재 배정과 그 날짜·시각을 돌려준다."""

    OPENS = "Supervisor 확인을 기다리는 사실 수정안이 없을 때"

    work_type: str | None = Field(default=None, description="작업 유형 코드(관찰의 work_types)")
    zone_id: str | None = Field(default=None, description="구역 ID")


class AnalyzeImpact(Action):
    """작업의 시작 가능 시각을 새 값으로 늦추면 무엇이 어긋나는지 서버가 계산한다(시간창·근무시간·현재 배정·연결 작업)."""

    OPENS = "현재 계산 대상(READY) 작업이고 Supervisor 확인을 기다리는 사실 수정안이 없을 때"

    task_id: str = Field(description="분석할 작업 ID(현재 계산 대상 작업)")
    new_earliest_start: str = Field(
        description='새 시작 가능 시각. 현장 날짜·시각 "YYYY-MM-DD HH:MM"(서버가 분으로 바꾼다)'
    )


class ProposeFactUpdate(Action):
    """(작업, 새 시작 가능 시각)으로 사실 수정안을 만들고 Supervisor 확인을 기다린다. 서버가 영향 분석을
    다시 돌려 통과해야 받는다."""

    OPENS = "이 Run에서 폐기된 값이 아니고 확인을 기다리는 사실 수정안이 없을 때"

    task_id: str = Field(description="사실을 수정할 작업 ID")
    new_earliest_start: str = Field(
        description='새 시작 가능 시각. 현장 날짜·시각 "YYYY-MM-DD HH:MM"(서버가 분으로 바꾼다)'
    )
    evidence: str = Field(
        min_length=1,
        max_length=TEXT_MAX,
        description="Supervisor에게 보이는 근거 설명(신고 인용 포함)",
    )


class AskReporter(Action):
    """신고 내용이 모호할 때(대상·새 시작 가능 시각 등) 신고자에게 되묻는다. 답은 자유 텍스트로 온다."""

    OPENS = "사람 확인 라운드가 남았고 답을 기다리는 질문이나 확인을 기다리는 사실 수정안이 없을 때"

    question: str = Field(min_length=1, max_length=TEXT_MAX, description="신고자에게 보이는 질문")


class ReturnResult(Action, ResultFields):
    """조회·분석·신고자 확인으로 열 수 있는 대안이 남아 있지 않거나 신고가 시작 지연이 아닐 때만 막힌 결과를 돌려주고 Run을 끝낸다. 한 것과 막힌 이유를 요약에 적고, 필요한 것을 알면 길에 적는다."""

    # Replanning과 같은 조건 문구
    OPENS = "언제나 열려 있다. 단 조회·확인으로 열 수 있는 대안이 남아 있지 않거나 Budget이 부족할 때만 쓴다"


ACTIONS: dict[str, type[Action]] = {
    "LOOKUP_TASKS": LookupTasks,
    "ANALYZE_IMPACT": AnalyzeImpact,
    "PROPOSE_FACT_UPDATE": ProposeFactUpdate,
    "ASK_REPORTER": AskReporter,
    "RETURN_RESULT": ReturnResult,
}
FLOW = {
    "LOOKUP_TASKS": "CONTINUE",
    "ANALYZE_IMPACT": "CONTINUE",
    "PROPOSE_FACT_UPDATE": "WAIT",
    "ASK_REPORTER": "WAIT",
    "RETURN_RESULT": "DONE",
}


SKILLS = ("ASSESS", "IMPACT", "ASK_REPORTER", "FACT_UPDATE", "WRAP_UP")


def skill_facts(obs: dict[str, Any]) -> dict[str, bool]:
    """스킬이 열리는 사실. 순서 조건은 없다."""
    event = obs.get("event") or {}
    return {
        "has_change": bool(event),
        "has_hold": (event.get("hold") or {}).get("status") == "ACTIVE",
    }


def open_skills(obs: dict[str, Any]) -> list[str]:
    return skills.open_skills(SKILLS, skill_facts(obs))


def choices(obs: dict[str, Any], hidden: dict[str, Any] | None = None) -> dict[str, Any]:
    """Action별 허용 값. Available Actions와 Gateway의 인자 조합 검사가 같이 쓴다.

    hidden["ready"]는 현재 계산 대상(READY) 작업 ID다. 조회했는지는 보지 않는다.
    LOOKUP: 확인 대기 제안 없음. ANALYZE·PROPOSE: READY 작업. PROPOSE는 이 Run에서 폐기된
    (작업, 값)을 다시 낼 수 없고, 실행 때 서버가 영향 분석을 다시 돌려 통과해야 받는다 (CV-16).
    """
    pending = any(p["status"] == "PENDING" for p in obs["proposals"])
    ready = sorted((hidden or {}).get("ready", []))
    asking = any(q["status"] == "OPEN" for q in obs["reporter_replies"])
    rounds = obs["budget_remaining"].get("human_rounds", 0) > 0
    return {
        "LOOKUP": not pending,
        "ANALYZE": [] if pending else ready,
        "PROPOSE": [] if pending else ready,
        "DISCARDED": {
            (p["task_id"], p["new_value"]) for p in obs["proposals"] if p["status"] == "DISCARDED"
        },
        # 신고자 되묻기: 답을 기다리는 질문·확인 대기 수정안이 없고 사람 라운드가 남을 때
        "ASK": rounds and not pending and not asking,
    }


def valid_actions(
    obs: dict[str, Any], hidden: dict[str, Any] | None = None
) -> dict[str, dict[str, Any]]:
    """유효한 인자 값이 있는 도구와 그 값. 사실·유효성·Budget만 본다(순서 조건 없음)."""
    c = choices(obs, hidden)
    out: dict[str, dict[str, Any]] = {}
    if c["LOOKUP"]:
        out["LOOKUP_TASKS"] = {}
    if c["ANALYZE"]:
        out["ANALYZE_IMPACT"] = {"task_id": c["ANALYZE"]}
    if c["PROPOSE"]:
        out["PROPOSE_FACT_UPDATE"] = {"task_id": c["PROPOSE"]}
    if c["ASK"]:
        out["ASK_REPORTER"] = {}
    # 수정안은 Supervisor 확인으로 끝나므로(AG-14) 스스로 끝내는 결과는 막힘뿐이다
    out["RETURN_RESULT"] = {"status": ["BLOCKED"]}
    return out


def available_actions(
    obs: dict[str, Any], hidden: dict[str, Any] | None = None
) -> dict[str, dict[str, Any]]:
    """{action 이름: 허용 인자 제한}. 열린 스킬의 도구 ∩ 유효한 도구. skill 인자의 허용 값도 넣는다."""
    return skills.available(SKILLS, skill_facts(obs), valid_actions(obs, hidden))


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
    budget={
        "steps": MAX_STEPS,
        "llm_attempts": MAX_LLM_ATTEMPTS,
        "human_rounds": MAX_HUMAN_ROUNDS,
    },
    recursion_limit=RECURSION_LIMIT,
    summary_max=SUMMARY_MAX,
    actions=ACTIONS,
    skills=SKILLS,
    available_actions=available_actions,
    tool_schemas=tool_schemas,
)
