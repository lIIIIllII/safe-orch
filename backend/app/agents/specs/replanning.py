"""Replanning AgentSpec (설계서 §11.7, 부록 A.16). 순수 데이터: Goal, Action 스키마, Budget, 사용 조건.

store·commands·solver를 import하지 않는다. 사용 조건은 관찰 데이터(untried_levels 등)만 보고 계산한다.
Action: SOLVE_WITH_SCOPE, LIST_ASSIGNABLE_RESOURCES, TRY_ALTERNATIVE_RESOURCE, ASK_TASK_OWNER,
ASK_WINDOW_CHANGE(A.29), ESCALATE_NO_SOLUTION (A.21 5). ASK는 계산으로 시도할 범위가 없을 때만 연다
(A.21 0-2, 서버 정책). 시간창 질문은 자원 경로까지 닫힌 뒤에만 연다(A.29 2).
모듈 이름(GOAL, ACTIONS, tool_schemas 등)은 그대로 두고, 그 값으로 SPEC(AgentSpec)을 만든다 (A.23).
"""

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agents.types import AgentSpec

AGENT_TYPE = "REPLANNING"
GOAL = (
    "Hard 제약과 확인된 조건을 지키면서 충돌을 해소하는 검증 가능한 대안을 찾는다. "
    "변경 작업 수를 먼저, 총 지연을 그다음으로 최소화한다."
)

MAX_STEPS = 15
MAX_LLM_ATTEMPTS = 30  # step × 2 (전송 재시도 1회 계상, 블루프린트에 없는 값)
MAX_HUMAN_ROUNDS = 2
MAX_SOLVER_CALLS = 6
RECURSION_LIMIT = MAX_STEPS * 5 + 10
SUMMARY_MAX = 200

LEVELS = ("L0", "L1", "L2")


class Action(BaseModel):
    """모든 Action의 공통 인자. decision_summary는 저장할 때 200자로 자른다."""

    model_config = ConfigDict(extra="forbid")
    # 열리는 조건 한 줄. prompt의 "도구 전체와 열리는 조건" 절이 이것으로 만든다 (A.21 p7).
    # 실제 실행 가능 여부는 available_actions가 정한다(이 문장은 안내일 뿐이다).
    OPENS: ClassVar[str] = ""

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
    유효하며, 대체 자원 시도와 담당자 확인 요청은 이 결과에 있는 자원만 쓸 수 있다."""

    OPENS = (
        "맡은 충돌의 당사자 작업(L0) 중 필요한 자원이 있고 자원 축이 확인된 제약으로 고정되지 않은 "
        "작업을 현재 자원 사실에서 아직 조회하지 않았을 때"
    )

    task_id: str = Field(
        description="자원을 조회할 작업 (맡은 충돌의 당사자 중 자원이 필요한 acting_unit 작업)"
    )


class TryAlternativeResource(Action):
    """충돌 당사자 범위(L0)에 대체 자원 하나를 더해 CP-SAT로 계산한다. 자원 축이 확인된 작업에만 쓸 수
    있다. 해가 있으면 후보가 등록되고 검증을 기다린다."""

    OPENS = (
        "자원 축이 확인된(담당자 동의) 작업에 자원 조회 결과의 아직 시도하지 않은 대체 자원이 있고 "
        "Solver 호출이 남아 있을 때"
    )

    task_id: str = Field(description="자원을 바꿔 볼 작업")
    resource_id: str = Field(description="시도할 대체 자원 (최근 자원 조회 결과의 쓸 수 있는 자원)")


class AskTaskOwner(Action):
    """작업 담당자에게 이동 축(자원)을 열어 줄지 묻고 답을 기다린다. 담당자가 수락하면 그 자원이 동의
    범위에 들어가고 자원 축이 확인된다. 계산으로 시도할 범위가 남아 있으면 쓸 수 없다."""

    OPENS = (
        "아직 시도하지 않은 탐색 범위가 없고, 자원 축이 미확인인 작업의 자원 조회 결과에 현재 자원 말고 "
        "담당자가 거절하지 않은 대체 자원이 있으며, 사람 확인 라운드가 남아 있을 때"
    )

    task_id: str = Field(description="확인을 요청할 작업")
    axis: Literal["RESOURCE"] = Field(description="확인할 이동 축")
    allowed_values: list[str] = Field(
        min_length=1, description="허용을 요청할 자원 (최근 자원 조회 결과에서 현재 자원을 뺀 것)"
    )
    question: str = Field(min_length=1, description="담당자에게 보일 설명 (한국어)")


class AskWindowChange(Action):
    """작업 담당자에게 시간창을 넓혀도 되는지 묻고 답을 기다린다. 넓힐 값은 서버가 계산한 선택지
    (window_options)이고, 담당자가 확인하면 시간창이 바뀌어 탐색 범위가 다시 열린다."""

    OPENS = (
        "아직 시도하지 않은 탐색 범위가 없고, 자원 조회·대체 자원 시도·자원 확인 질문으로 열 수 있는 것이 "
        "남아 있지 않으며, 시간창을 넓히면 자리가 생기는 작업(시간창 선택지)이 있고, 사람 확인 라운드가 "
        "남아 있을 때"
    )

    task_id: str = Field(description="시간창을 넓힐지 물을 작업 (시간창 선택지가 있는 작업)")
    question: str = Field(min_length=1, description="담당자에게 보일 설명 (한국어)")


class EscalateNoSolution(Action):
    """탐색 범위 확대, 자원 조회, 대체 자원 시도, 담당자 확인으로 열 수 있는 대안이 남아 있지 않을 때만
    사유를 붙여 사람에게 넘기고 Run을 끝낸다."""

    OPENS = "언제나 열려 있다. 단 조회·확인으로 열 수 있는 대안이 남아 있지 않거나 Budget이 부족할 때만 쓴다"

    reason: str = Field(min_length=1, description="해가 없다고 판단한 근거(시도한 범위와 결과)")


ACTIONS: dict[str, type[Action]] = {
    "SOLVE_WITH_SCOPE": SolveWithScope,
    "LIST_ASSIGNABLE_RESOURCES": ListAssignableResources,
    "TRY_ALTERNATIVE_RESOURCE": TryAlternativeResource,
    "ASK_TASK_OWNER": AskTaskOwner,
    "ASK_WINDOW_CHANGE": AskWindowChange,
    "ESCALATE_NO_SOLUTION": EscalateNoSolution,
}
FLOW = {
    "SOLVE_WITH_SCOPE": "CANDIDATE_OR_CONTINUE",
    "LIST_ASSIGNABLE_RESOURCES": "CONTINUE",
    "TRY_ALTERNATIVE_RESOURCE": "CANDIDATE_OR_CONTINUE",
    "ASK_TASK_OWNER": "WAIT",
    "ASK_WINDOW_CHANGE": "WAIT",
    "ESCALATE_NO_SOLUTION": "DONE",
}


def choices(obs: dict[str, Any]) -> dict[str, Any]:
    """작업별 허용 값 (A.21 5). Available Actions와 Gateway의 인자 조합 검사가 같이 쓴다.

    LIST: 주 충돌의 L0 작업(acting) 중 필요 자원이 있고 RESOURCE 축이 제약으로 막히지 않았으며 같은
    자원 사실에서 아직 조회하지 않은 것. TRY 범위(주 충돌 L0 + 대체 자원)와 맞춘다.
    TRY: 자원 축 허용(movable.resource ∧ RESOURCE 제약 없음) 작업의 유효 조회 결과 중 미시도 대체 자원.
    ASK: 자원 축 미확인 ∧ RESOURCE 제약 없음 ∧ 같은 작업·축의 열린 질문 없음, 값은 조회 결과 − 현재 자원
    − 이 Case에서 담당자가 거절(DECLINE)한 값. 거절당한 질문을 같은 사람에게 다시 보내지 않는다.
    WINDOW: 작업 → 시간창 선택지 값. 자원 경로가 닫힌 뒤에만 연다(window_gate, A.29 2).
    """
    acting = {t["task_id"]: t for t in obs["acting_tasks"]}
    frozen = {(c["task_id"], axis) for c in obs["constraints"] for axis in c["frozen_axes"]}
    listed = {r["task_id"]: r for r in obs["assignable_resources"] if r["task_id"] in acting}
    open_asks = {(h["task_id"], h["axis"]) for h in obs["human_replies"] if h["status"] == "OPEN"}
    declined = {
        (h["task_id"], h["axis"], v)
        for h in obs["human_replies"]
        if h["decision"] == "DECLINE"
        for v in h["allowed_values"]
    }
    try_: dict[str, list[str]] = {}
    ask: dict[str, list[str]] = {}
    for tid, r in listed.items():
        if (tid, "RESOURCE") in frozen:
            continue
        if acting[tid]["movable"]["resource"]:
            if r["untried_alternatives"]:
                try_[tid] = list(r["untried_alternatives"])
        elif (tid, "RESOURCE") not in open_asks:
            values = [
                a["resource_id"]
                for a in r["assignable"]
                if a["resource_id"] != r["current"]
                and (tid, "RESOURCE", a["resource_id"]) not in declined
            ]
            if values:
                ask[tid] = values
    l0 = set((obs["primary_conflict"] or {}).get("task_ids", []))
    out: dict[str, Any] = {
        "LIST": [
            tid
            for tid, t in acting.items()
            if tid in l0
            and t["required_resource_type"]
            and (tid, "RESOURCE") not in frozen
            and tid not in listed
        ],
        "TRY": try_,
        "ASK": ask,
    }
    # 시간창 질문: 자원 경로가 닫힌 뒤에만, 서버 선택지 중 이 Case에서 거절되지 않은 것 (A.29 2)
    out["WINDOW"] = (
        {
            o["task_id"]: window_key(o)
            for o in obs.get("window_options", [])
            if o["task_id"] in acting
            and o["task_id"] in l0
            and acting[o["task_id"]]["movable"]["time"]
            and (o["task_id"], "TIME") not in frozen
            and (o["task_id"], "TIME", window_key(o)) not in declined
        }
        if window_gate(obs, out)
        else {}
    )
    return out


def window_key(option: dict[str, Any]) -> str:
    """시간창 선택지의 값 표기 "<새 latest_start>/<새 latest_end>" (질문 allowed_values, A.29 3)."""
    p = option["proposed"]
    return f"{p['latest_start']}/{p['latest_end']}"


def window_gate(obs: dict[str, Any], c: dict[str, Any] | None = None) -> bool:
    """시간창 질문의 선택지 밖 조건 (A.29 2). 미시도 범위 없음 ∧ 사람 라운드 남음 ∧ 자원 경로가 닫힘
    (자원 조회 대상·미시도 대체 자원·RESOURCE 질문 값 없음) ∧ 답을 기다리는 질문 없음.

    observer는 이 조건이 갖춰졌을 때만 선택지를 계산한다.
    """
    if c is None:
        c = choices({**obs, "window_options": []})
    return (
        not obs["untried_levels"]
        and obs["budget_remaining"]["human_rounds"] > 0
        and not c["LIST"]
        and not c["TRY"]
        and not c["ASK"]
        and not any(h["status"] == "OPEN" for h in obs["human_replies"])
    )


def _union(groups: dict[str, list[str]]) -> list[str]:
    return sorted({v for vs in groups.values() for v in vs})


def available_actions(obs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """{action 이름: 허용 인자 제한}. 작업별 조합은 choices로 Gateway가 다시 검사한다."""
    out: dict[str, dict[str, Any]] = {}
    budget = obs["budget_remaining"]
    if obs["conflicts"] and obs["untried_levels"] and budget["solver_calls"] > 0:
        out["SOLVE_WITH_SCOPE"] = {"level": list(obs["untried_levels"])}
    c = choices(obs)
    if c["LIST"]:
        out["LIST_ASSIGNABLE_RESOURCES"] = {"task_id": c["LIST"]}
    if c["TRY"] and obs["primary_conflict"] and budget["solver_calls"] > 0:
        out["TRY_ALTERNATIVE_RESOURCE"] = {
            "task_id": sorted(c["TRY"]),
            "resource_id": _union(c["TRY"]),
        }
    # 사람에게는 계산으로 할 수 있는 탐색을 먼저 한 뒤에만 묻는다 (A.21 0-2)
    if c["ASK"] and not obs["untried_levels"] and budget["human_rounds"] > 0:
        out["ASK_TASK_OWNER"] = {
            "task_id": sorted(c["ASK"]),
            "axis": ["RESOURCE"],
            "allowed_values": _union(c["ASK"]),
        }
    if c["WINDOW"]:
        out["ASK_WINDOW_CHANGE"] = {"task_id": sorted(c["WINDOW"])}
    out["ESCALATE_NO_SOLUTION"] = {}
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
        "solver_calls": MAX_SOLVER_CALLS,
    },
    recursion_limit=RECURSION_LIMIT,
    summary_max=SUMMARY_MAX,
    actions=ACTIONS,
    available_actions=available_actions,
    tool_schemas=tool_schemas,
)
