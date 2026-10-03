"""Event Response AgentSpec. 순수 데이터: Goal, Action 스키마, Budget.

store·commands·solver를 import하지 않는다. 사용 조건은 관찰 데이터만 보고 계산한다.
Action: LOOKUP_TASKS, ANALYZE_IMPACT, PROPOSE_FACT_UPDATE, ASK_REPORTER, ESCALATE.
Hold 해제·사실 직접 적용은 할 수 없다. 사실 수정은 Supervisor가 확인해야 효력이 생긴다.
"""

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from app.agents.types import AgentSpec

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

    OPENS = "조회 결과에 있는 작업이고 Supervisor 확인을 기다리는 사실 수정안이 없을 때"

    task_id: str = Field(description="분석할 작업 ID(조회 결과에 있는 작업)")
    new_earliest_start: int = Field(
        description="새 시작 가능 시각. Horizon 원점 기준 정수 분(결과에 날짜·시각이 함께 나온다)"
    )


class ProposeFactUpdate(Action):
    """분석이 통과한 (작업, 새 시작 가능 시각)으로 사실 수정안을 만들고 Supervisor 확인을 기다린다."""

    OPENS = (
        "같은 작업·값의 분석이 지금 현장 정보에서 통과(ok)했고, 이 Run에서 폐기된 값이 아니며,"
        " 확인을 기다리는 사실 수정안이 없을 때"
    )

    task_id: str = Field(description="사실을 수정할 작업 ID")
    new_earliest_start: int = Field(description="새 시작 가능 시각(정수 분, 분석한 값)")
    evidence: str = Field(
        min_length=1,
        max_length=TEXT_MAX,
        description="Supervisor에게 보이는 근거 설명(신고 인용 포함)",
    )


class AskReporter(Action):
    """신고 내용이 모호할 때(대상·새 시작 가능 시각 등) 신고자에게 되묻는다. 답은 자유 텍스트로 온다."""

    OPENS = (
        "이 Run에서 LOOKUP_TASKS를 한 번 이상 했고, 사람 확인 라운드가 남았고,"
        " 답을 기다리는 질문이나 확인을 기다리는 사실 수정안이 없을 때"
    )

    question: str = Field(min_length=1, max_length=TEXT_MAX, description="신고자에게 보이는 질문")


class Escalate(Action):
    """조회·분석·신고자 확인으로 열 수 있는 대안이 남아 있지 않거나 신고가 시작 지연이 아닐 때만 사유와 함께 이관한다."""

    # Replanning p7과 같은 조건 문구
    OPENS = "언제나 열려 있다. 단 조회·확인으로 열 수 있는 대안이 남아 있지 않거나 Budget이 부족할 때만 쓴다"

    reason: str = Field(min_length=1, description="이관 사유")


ACTIONS: dict[str, type[Action]] = {
    "LOOKUP_TASKS": LookupTasks,
    "ANALYZE_IMPACT": AnalyzeImpact,
    "PROPOSE_FACT_UPDATE": ProposeFactUpdate,
    "ASK_REPORTER": AskReporter,
    "ESCALATE": Escalate,
}
FLOW = {
    "LOOKUP_TASKS": "CONTINUE",
    "ANALYZE_IMPACT": "CONTINUE",
    "PROPOSE_FACT_UPDATE": "WAIT",
    "ASK_REPORTER": "WAIT",
    "ESCALATE": "DONE",
}


def choices(obs: dict[str, Any]) -> dict[str, Any]:
    """Action별 허용 값. Available Actions와 Gateway의 인자 조합 검사가 같이 쓴다.

    LOOKUP: 확인 대기 제안 없음. ANALYZE: 조회한 작업. PROPOSE: 지금 Context에서 통과한 분석의
    (작업, 값) 중 이 Run에서 폐기되지 않은 것.
    """
    pending = any(p["status"] == "PENDING" for p in obs["proposals"])
    looked = sorted({t["task_id"] for lk in obs["lookups"] for t in lk["tasks"]})
    declined = {
        (p["task_id"], p["new_value"]) for p in obs["proposals"] if p["status"] == "DISCARDED"
    }
    propose: dict[str, list[int]] = {}
    for a in obs["analyses"]:
        key = (a["task_id"], a["new_earliest_start"])
        if a["ok"] and a["current"] and key not in declined:
            values = propose.setdefault(a["task_id"], [])
            if a["new_earliest_start"] not in values:
                values.append(a["new_earliest_start"])
    asking = any(q["status"] == "OPEN" for q in obs["reporter_replies"])
    rounds = obs["budget_remaining"].get("human_rounds", 0) > 0
    # 조회로 알 수 있는 것(대상 작업)은 먼저 조회한다. 결과가 0건이어도 조회는 한 것이다
    looked_up = bool(obs["lookups"])
    return {
        "LOOKUP": not pending,
        "ANALYZE": [] if pending else looked,
        "PROPOSE": {} if pending else propose,
        # 신고자 되묻기: 조회 뒤, 답을 기다리는 질문·확인 대기 수정안이 없고 사람 라운드가 남을 때
        "ASK": looked_up and rounds and not pending and not asking,
    }


def available_actions(obs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """{action 이름: 허용 인자 제한}. 작업·값 조합은 choices로 Gateway가 다시 검사한다."""
    c = choices(obs)
    out: dict[str, dict[str, Any]] = {}
    if c["LOOKUP"]:
        out["LOOKUP_TASKS"] = {}
    if c["ANALYZE"]:
        out["ANALYZE_IMPACT"] = {"task_id": c["ANALYZE"]}
    if c["PROPOSE"]:
        out["PROPOSE_FACT_UPDATE"] = {
            "task_id": sorted(c["PROPOSE"]),
            "new_earliest_start": sorted({v for vs in c["PROPOSE"].values() for v in vs}),
        }
    if c["ASK"]:
        out["ASK_REPORTER"] = {}
    out["ESCALATE"] = {}
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
    budget={
        "steps": MAX_STEPS,
        "llm_attempts": MAX_LLM_ATTEMPTS,
        "human_rounds": MAX_HUMAN_ROUNDS,
    },
    recursion_limit=RECURSION_LIMIT,
    summary_max=SUMMARY_MAX,
    actions=ACTIONS,
    available_actions=available_actions,
    tool_schemas=tool_schemas,
)
