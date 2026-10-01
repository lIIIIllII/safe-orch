"""공통 StateGraph (설계서 §11.2, 부록 A.16): observe → reserve_step → decide → gateway → finish.

그래프는 DB를 직접 만지지 않는다. 읽기·쓰기는 주입된 port(RunPort)로만 하고, 도구 실행은
port.execute(= Tool Gateway)뿐이다. store·commands를 import하지 않는다. checkpointer 없이 compile한다.
그래프 상태는 호출 동안만 존재한다. edge는 Gateway 결과 종류와 Run 활성·Budget으로만 분기한다.
"""

from types import ModuleType
from typing import Any, Protocol, TypedDict

from langchain_core.messages import AIMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from app.agents.llm import ChatModel, bind, invoke_with_retry, model_id
from app.agents.types import AgentSpec, GatewayResult, StepMeta


class ObservationLike(Protocol):
    data: dict[str, Any]
    available: dict[str, dict[str, Any]]
    versions: tuple[int, int, int]

    @property
    def active(self) -> bool: ...

    @property
    def budget_exhausted(self) -> bool: ...


class RunPort(Protocol):
    def observe(self, run_id: str) -> ObservationLike: ...

    def reserve_step(self, run_id: str, obs: ObservationLike) -> int | None: ...

    def execute(
        self, run_id: str, step_no: int, message: AIMessage | None, meta: StepMeta
    ) -> GatewayResult: ...

    def finish(self, run_id: str, status: str, reason: str) -> None: ...


class GraphInput(TypedDict):
    run_id: str


class State(TypedDict, total=False):
    run_id: str
    obs: Any
    step_no: int | None
    message: Any
    meta: Any
    result: Any
    end: tuple[str, str] | None


def build_graph(port: RunPort, model: ChatModel, spec: AgentSpec, prompt: ModuleType) -> Any:
    """spec: AgentSpec(goal, tool_schemas), prompt: SYSTEM·PROMPT_VERSION·render_observation (A.23)."""
    system = SystemMessage(prompt.SYSTEM.format(goal=spec.goal))

    def observe(state: State) -> State:
        obs = port.observe(state["run_id"])
        end = (
            ("BUDGET_EXHAUSTED", "BUDGET_EXHAUSTED")
            if obs.active and obs.budget_exhausted
            else None
        )
        return {"obs": obs, "end": end, "step_no": None, "message": None, "result": None}

    def after_observe(state: State) -> str:
        return "reserve_step" if state["obs"].active and state["end"] is None else "finish"

    def reserve_step(state: State) -> State:
        return {"step_no": port.reserve_step(state["run_id"], state["obs"])}

    def after_reserve(state: State) -> str:
        return "finish" if state["step_no"] is None else "decide"

    def decide(state: State) -> State:
        """모델 1회 호출(전송 재시도 1회 포함). 실패도 step 결과로 Gateway에 넘긴다."""
        obs = state["obs"]
        human = prompt.render_observation(obs.data)
        call = invoke_with_retry(bind(model, spec.tool_schemas(obs.available)), [system, human])
        meta = StepMeta(
            model_id=model_id(model, call.message),
            prompt_version=prompt.PROMPT_VERSION,
            llm_attempts=call.attempts,
            error_kind=call.error_kind,
            error=call.error,
        )
        return {"message": call.message, "meta": meta}

    def gateway(state: State) -> State:
        result = port.execute(state["run_id"], state["step_no"], state["message"], state["meta"])
        end = (result.end_status, result.end_reason) if result.kind == "DONE" else None
        return {"result": result, "end": end}

    def after_gateway(state: State) -> str:
        kind = state["result"].kind
        if kind in ("CONTINUE", "REJECTED"):
            return "observe"
        if kind == "WAIT":
            return END
        return "finish"

    def finish(state: State) -> State:
        if state.get("end"):
            status, reason = state["end"]
            port.finish(state["run_id"], status, reason)
        return {}

    g = StateGraph(State, input_schema=GraphInput)
    g.add_node("observe", observe)
    g.add_node("reserve_step", reserve_step)
    g.add_node("decide", decide)
    g.add_node("gateway", gateway)
    g.add_node("finish", finish)
    g.add_edge(START, "observe")
    g.add_conditional_edges("observe", after_observe, ["reserve_step", "finish"])
    g.add_conditional_edges("reserve_step", after_reserve, ["decide", "finish"])
    g.add_edge("decide", "gateway")
    g.add_conditional_edges("gateway", after_gateway, ["observe", "finish", END])
    g.add_edge("finish", END)
    return g.compile()
