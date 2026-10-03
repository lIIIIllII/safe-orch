"""Agent 실행 진입점. invoke(run_id)와 RunPort 구현.

그래프 입력은 run_id뿐이다(추가 키는 거절). Run의 agent_type으로 registry에서 binding(spec·prompt·
observer·executor)을 고른다. 등록되지 않은 agent_type이면 그래프를 부르지 않고 Run ERROR
(`AGENT_TYPE_NOT_REGISTERED: <agent_type>`)로 끝낸다. recursion_limit 초과나 예상하지 못한 예외는 Run
ERROR로 기록하고, 남은 RESERVED step·SolverJob은 ABORTED로 둔다. 기동 때 RUNNING으로 남은 Run은
Coordinator가 예약만 된 step을 정리하고 CONTINUE_RUN으로 이 진입점을 다시 부른다(관찰부터, ST-19).
"""

import logging
from typing import Any

from langchain_core.messages import AIMessage
from langgraph.errors import GraphRecursionError

from app.agents import graph as graph_module
from app.agents.llm import ChatModel, ModelFactory
from app.agents.observe import Observation
from app.agents.registry import BINDINGS
from app.agents.tool_gateway import ToolGateway
from app.agents.types import AgentBinding, GatewayResult, StepMeta
from app.domain.models import AgentRun
from app.packs.loader import LoadedPack
from app.store import db
from app.store.repos.cases import end_case_run
from app.store.repos.runs import abort_reserved, get_run, reserve_step

log = logging.getLogger(__name__)

__all__ = ["ModelFactory", "StoreRunPort", "exec_contract_version", "invoke"]
GRAPH_INPUT_KEYS = frozenset({"run_id"})
NOT_REGISTERED = "AGENT_TYPE_NOT_REGISTERED"


def exec_contract_version(agent_type: str) -> str:
    """Run에 기록할 실행 계약 버전 (기록만). 등록되지 않았으면 NOT_REGISTERED."""
    binding = BINDINGS.get(agent_type)
    return NOT_REGISTERED if binding is None else binding.exec_contract_version


class StoreRunPort:
    """graph.RunPort 구현. observe는 읽기 전용, 나머지는 짧은 write 트랜잭션."""

    def __init__(self, pack: LoadedPack, binding: AgentBinding):
        self.pack = pack
        self.binding = binding
        self.gateway = ToolGateway(pack, binding)

    def observe(self, run_id: str) -> Observation:
        with db.read() as conn:
            return self.binding.observer.build_observation(conn, self.pack, run_id)

    def reserve_step(self, run_id: str, obs: Observation) -> int | None:
        with db.write() as tx:
            return reserve_step(
                tx,
                run_id,
                obs.versions,
                self.binding.spec.goal,
                obs.data,
                self.binding.spec.tool_schemas(obs.available),
                getattr(obs, "seen_event_seq", 0),
            )

    def execute(
        self, run_id: str, step_no: int, message: AIMessage | None, meta: StepMeta
    ) -> GatewayResult:
        return self.gateway.execute(run_id, step_no, message, meta)

    def finish(self, run_id: str, status: str, reason: str) -> None:
        """관찰 단계의 종료(Budget 소진). 행동에 의한 종료는 Gateway가 step과 같은 tx에서 한다."""
        with db.write() as tx:
            end_case_run(tx, self.pack, run_id, status, reason, ("RUNNING",))


def _fail(pack: LoadedPack, run_id: str, reason: str) -> None:
    with db.write() as tx:
        abort_reserved(tx, run_id, reason)
        end_case_run(tx, pack, run_id, "ERROR", reason, ("RUNNING",))


def invoke(pack: LoadedPack, graph_input: dict[str, Any], model: ChatModel) -> AgentRun:
    """Run 1개를 observe부터 호출한다. WAIT·DONE·Budget 소진·비활성 중 하나로 끝난다."""
    if set(graph_input) != GRAPH_INPUT_KEYS:
        raise ValueError(f"graph input accepts only {sorted(GRAPH_INPUT_KEYS)}")
    run_id = graph_input["run_id"]
    with db.read() as conn:
        run = get_run(conn, run_id)
    if run is None:
        raise LookupError(f"run {run_id} not found")
    binding = BINDINGS.get(run.agent_type)
    if binding is None:
        # 그래프를 부르지 않는다. RUNNING으로 남아 열린 Case가 되지 않게 ERROR로 끝낸다
        _fail(pack, run_id, f"{NOT_REGISTERED}: {run.agent_type}")
        return _reload(run_id)
    port = StoreRunPort(pack, binding)
    system_text = binding.prompt.render_system(pack)
    graph = graph_module.build_graph(port, model, binding.spec, binding.prompt, system_text)
    try:
        graph.invoke({"run_id": run_id}, {"recursion_limit": binding.spec.recursion_limit})
    except GraphRecursionError:
        _fail(pack, run_id, "RECURSION_LIMIT")
    except Exception as e:  # 모델·도구 예외는 Run ERROR로 드러낸다
        log.exception("run %s failed", run_id)
        _fail(pack, run_id, f"EXCEPTION: {type(e).__name__}")
    return _reload(run_id)


def _reload(run_id: str) -> AgentRun:
    with db.read() as conn:
        run = get_run(conn, run_id)
    assert run is not None
    return run
