"""Replanning AgentSpec. 순수 데이터: Goal, Action 스키마, Budget, 사용 조건.

store·commands·solver를 import하지 않는다. 사용 조건은 관찰 데이터(untried_levels 등)만 보고 계산한다.
Action: SOLVE_WITH_SCOPE, LIST_ASSIGNABLE_RESOURCES, TRY_ALTERNATIVE_RESOURCE, ASK_TASK_OWNER,
RETURN_RESULT. 순서는 스킬 지침에 있고, 여기 조건은 사실·유효성·Budget뿐이다 (AG-01).
모듈 이름(GOAL, ACTIONS, tool_schemas 등)은 그대로 두고, 그 값으로 SPEC(AgentSpec)을 만든다.
"""

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agents import skills
from app.agents.types import AgentSpec
from app.domain.needs import ResultFields

AGENT_TYPE = "REPLANNING"
GOAL = (
    "Hard 제약과 확인된 조건을 지키면서 충돌을 해소하는 검증 가능한 대안을 찾는다. "
    "변경 작업 수를 먼저, 총 지연을 그다음으로 최소화한다."
)

MAX_STEPS = 15
MAX_LLM_ATTEMPTS = 30  # step × 2 (전송 재시도 1회 계상)
MAX_HUMAN_ROUNDS = 2
MAX_SOLVER_CALLS = 6
RECURSION_LIMIT = MAX_STEPS * 5 + 10
SUMMARY_MAX = 200

LEVELS = ("L0", "L1", "L2")


class Action(BaseModel):
    """모든 Action의 공통 인자. decision_summary는 저장할 때 200자로 자른다."""

    model_config = ConfigDict(extra="forbid")
    # 열리는 조건 한 줄. prompt의 "도구 전체와 열리는 조건" 절이 이것으로 만든다.
    # 실제 실행 가능 여부는 available_actions가 정한다(이 문장은 안내일 뿐이다).
    OPENS: ClassVar[str] = ""

    skill: str = Field(
        description="이번 행동에 쓰는 스킬 ID. 열린 스킬(open_skills) 중 이 도구를 가진 것"
    )
    decision_summary: str = Field(
        description="이유: …/다음: … 형식. 이 행동을 고른 이유와 다음 예정 단계 (200자 이내)"
    )


class SolveWithScope(Action):
    """현재 사실에서 탐색 범위를 정해 CP-SAT로 대안을 계산한다. 해가 있으면 후보가 등록되고 검증을
    기다린다. 해가 없으면(INFEASIBLE·UNKNOWN) 결과를 관찰하고 다음 전략을 고른다."""

    OPENS = "맡은 충돌이 있고 아직 시도하지 않은 탐색 범위와 Solver 호출이 남아 있을 때"

    level: Literal["L0", "L1", "L2"] = Field(
        description="탐색 범위. L0 충돌 당사자만, L1 같은 구역·같은 자원 작업까지, L2 acting_unit 작업 전부"
    )


class ListAssignableResources(Action):
    """작업에 쓸 수 있는 자원을 조회한다(유형·사용 권한·가용 구간 기준). 결과는 자원 사실이 같은 동안
    유효하다. 대체 자원 시도와 담당자 확인 요청은 서버가 같은 기준으로 쓸 수 있는 자원만 받는다."""

    OPENS = (
        "필요한 자원이 있고 자원 축이 확인된 제약으로 고정되지 않은 작업을 현재 자원 사실에서 "
        "아직 조회하지 않았을 때"
    )

    task_id: str = Field(description="자원을 조회할 작업 (자원이 필요한 acting_unit 작업)")


class TryAlternativeResource(Action):
    """충돌 당사자 범위(L0)에 대체 자원 하나를 더해 CP-SAT로 계산한다. 자원 축이 확인된 작업에만 쓸 수
    있다. 해가 있으면 후보가 등록되고 검증을 기다린다."""

    OPENS = (
        "자원 축이 확인된(담당자 동의) 작업에 그 작업이 쓸 수 있는 아직 시도하지 않은 대체 자원이 있고 "
        "Solver 호출이 남아 있을 때"
    )

    task_id: str = Field(description="자원을 바꿔 볼 작업")
    resource_id: str = Field(description="시도할 대체 자원 (그 작업이 쓸 수 있는 자원)")


class AskTaskOwner(Action):
    """작업 담당자에게 이동 축(자원)을 열어 줄지 묻고 답을 기다린다. 담당자가 수락하면 그 자원이 동의
    범위에 들어가고 자원 축이 확인된다."""

    OPENS = (
        "자원 축이 미확인인 작업에 현재 자원 말고 담당자가 거절하지 않은 쓸 수 있는 대체 자원이 있고, "
        "같은 작업·축의 답을 기다리는 질문이 없으며, 사람 확인 라운드가 남아 있을 때"
    )

    task_id: str = Field(description="확인을 요청할 작업")
    axis: Literal["RESOURCE"] = Field(description="확인할 이동 축")
    allowed_values: list[str] = Field(
        min_length=1,
        description="허용을 요청할 자원 (그 작업이 쓸 수 있는 자원에서 현재 자원을 뺀 것)",
    )
    question: str = Field(min_length=1, description="담당자에게 보일 설명 (한국어)")


class ReturnResult(Action, ResultFields):
    """결과를 돌려주고 Run을 끝낸다. 검증을 통과한 살아 있는 후보가 있으면 DONE으로 돌려준다(승인·거절은
    사람이 하고 그 결과는 이 Run이 받지 않는다). 탐색 범위 확대, 자원 조회, 대체 자원 시도, 담당자
    확인으로 열 수 있는 대안이 남아 있지 않을 때만 BLOCKED로 돌려주고, 시도한 범위와 결과를 요약에,
    무엇이 풀리면 해가 열리는지 알면 길마다 필요한 것을 적는다."""

    OPENS = (
        "언제나 열려 있다. DONE은 검증을 통과한 살아 있는 후보가 있을 때만, BLOCKED는 조회·확인으로 열 수 "
        "있는 대안이 남아 있지 않거나 Budget이 부족할 때만 쓴다"
    )


ACTIONS: dict[str, type[Action]] = {
    "SOLVE_WITH_SCOPE": SolveWithScope,
    "LIST_ASSIGNABLE_RESOURCES": ListAssignableResources,
    "TRY_ALTERNATIVE_RESOURCE": TryAlternativeResource,
    "ASK_TASK_OWNER": AskTaskOwner,
    "RETURN_RESULT": ReturnResult,
}
FLOW = {
    "SOLVE_WITH_SCOPE": "CANDIDATE_OR_CONTINUE",
    "LIST_ASSIGNABLE_RESOURCES": "CONTINUE",
    "TRY_ALTERNATIVE_RESOURCE": "CANDIDATE_OR_CONTINUE",
    "ASK_TASK_OWNER": "WAIT",
    "RETURN_RESULT": "DONE",
}


SKILLS = ("ASSESS", "BUILD_CANDIDATE", "APPLY_REJECTION", "ASK_OWNER_TEMP", "WRAP_UP")


def skill_facts(obs: dict[str, Any]) -> dict[str, bool]:
    """스킬이 열리는 사실. 순서 조건은 없다."""
    return {
        "has_conflict": bool(obs["conflicts"]),
        "has_rejection": bool(obs["rejections"]) or bool(obs["constraints"]),
        "has_unconfirmed_axis": any(
            t["required_resource_type"] and not t["movable"]["resource"]
            for t in obs["acting_tasks"]
        ),
    }


def open_skills(obs: dict[str, Any]) -> list[str]:
    return skills.open_skills(SKILLS, skill_facts(obs))


def choices(obs: dict[str, Any], hidden: dict[str, Any] | None = None) -> dict[str, Any]:
    """작업별 허용 값. Available Actions와 Gateway의 인자 조합 검사가 같이 쓴다.

    hidden["eligible"]은 서버가 계산한 자원 적격성이다(유형·사용 권한·가용 구간·구역·요구 조건, CV-15):
    {작업: {"alternatives": 현재 자원 말고 쓸 수 있는 자원, "untried": 그중 아직 시도하지 않은 것}}.
    자원 조회를 했는지는 보지 않는다.
    LIST: 필요 자원이 있고 RESOURCE 축이 제약으로 막히지 않았으며 같은 자원 사실에서 아직 조회하지 않은 작업.
    TRY: 자원 축 허용(movable.resource ∧ RESOURCE 제약 없음) 작업의 미시도 대체 자원.
    ASK: 자원 축 미확인 ∧ RESOURCE 제약 없음 ∧ 같은 작업·축의 열린 질문 없음, 값은 대체 자원 −
    이 Case에서 담당자가 거절(DECLINE)한 값. 거절당한 질문을 같은 사람에게 다시 보내지 않는다.
    """
    eligible = (hidden or {}).get("eligible", {})
    acting = {t["task_id"]: t for t in obs["acting_tasks"]}
    frozen = {(c["task_id"], axis) for c in obs["constraints"] for axis in c["frozen_axes"]}
    listed = {r["task_id"] for r in obs["assignable_resources"] if r["task_id"] in acting}
    open_asks = {(h["task_id"], h["axis"]) for h in obs["human_replies"] if h["status"] == "OPEN"}
    declined = {
        (h["task_id"], h["axis"], v)
        for h in obs["human_replies"]
        if h["decision"] == "DECLINE"
        for v in h["allowed_values"]
    }
    try_: dict[str, list[str]] = {}
    ask: dict[str, list[str]] = {}
    for tid, e in eligible.items():
        if tid not in acting or (tid, "RESOURCE") in frozen:
            continue
        if acting[tid]["movable"]["resource"]:
            if e["untried"]:
                try_[tid] = list(e["untried"])
        elif (tid, "RESOURCE") not in open_asks:
            values = [v for v in e["alternatives"] if (tid, "RESOURCE", v) not in declined]
            if values:
                ask[tid] = values
    return {
        "LIST": [
            tid
            for tid, t in acting.items()
            if t["required_resource_type"] and (tid, "RESOURCE") not in frozen and tid not in listed
        ],
        "TRY": try_,
        "ASK": ask,
    }


def _union(groups: dict[str, list[str]]) -> list[str]:
    return sorted({v for vs in groups.values() for v in vs})


def valid_actions(
    obs: dict[str, Any], hidden: dict[str, Any] | None = None
) -> dict[str, dict[str, Any]]:
    """유효한 인자 값이 있는 도구와 그 값. 사실·유효성·Budget만 본다(순서 조건 없음)."""
    out: dict[str, dict[str, Any]] = {}
    budget = obs["budget_remaining"]
    if obs["conflicts"] and obs["untried_levels"] and budget["solver_calls"] > 0:
        out["SOLVE_WITH_SCOPE"] = {"level": list(obs["untried_levels"])}
    c = choices(obs, hidden)
    if c["LIST"]:
        out["LIST_ASSIGNABLE_RESOURCES"] = {"task_id": c["LIST"]}
    if c["TRY"] and obs["primary_conflict"] and budget["solver_calls"] > 0:
        out["TRY_ALTERNATIVE_RESOURCE"] = {
            "task_id": sorted(c["TRY"]),
            "resource_id": _union(c["TRY"]),
        }
    if c["ASK"] and budget["human_rounds"] > 0:
        out["ASK_TASK_OWNER"] = {
            "task_id": sorted(c["ASK"]),
            "axis": ["RESOURCE"],
            "allowed_values": _union(c["ASK"]),
        }
    # DONE은 검증을 통과한 살아 있는 후보가 있을 때만 유효하다(사실 조건)
    ready = bool((hidden or {}).get("done_ready"))
    out["RETURN_RESULT"] = {"status": ["DONE", "BLOCKED"] if ready else ["BLOCKED"]}
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
        "solver_calls": MAX_SOLVER_CALLS,
    },
    recursion_limit=RECURSION_LIMIT,
    summary_max=SUMMARY_MAX,
    actions=ACTIONS,
    skills=SKILLS,
    available_actions=available_actions,
    tool_schemas=tool_schemas,
)
