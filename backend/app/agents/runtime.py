"""Agent 실행 진입점 (설계서 §11.2·§11.3(7), 부록 A.16). invoke(run_id)와 RunPort 구현.

그래프 입력은 run_id뿐이다(추가 키는 거절). recursion_limit 초과나 예상하지 못한 예외는 Run ERROR로
기록하고, 남은 RESERVED step·SolverJob은 ABORTED로 둔다. 재시작 복구(§11.4)는 이번 범위가 아니다.
"""

import logging
from typing import Any

from langchain_core.messages import AIMessage
from langgraph.errors import GraphRecursionError

from app.agents import graph as graph_module
from app.agents.llm import ChatModel, ModelFactory
from app.agents.observe import Observation, build_observation
from app.agents.prompts import replanning as replanning_prompt
from app.agents.specs import replanning as replanning_spec
from app.agents.tool_gateway import ToolGateway
from app.agents.types import GatewayResult, StepMeta
from app.domain.models import AgentRun
from app.packs.loader import LoadedPack
from app.store import db
from app.store.repos.runs import end_run, get_run, reserve_step

log = logging.getLogger(__name__)

EXEC_CONTRACT_VERSION = "replanning-3a"
__all__ = ["EXEC_CONTRACT_VERSION", "ModelFactory", "StoreRunPort", "invoke"]
GRAPH_INPUT_KEYS = frozenset({"run_id"})


class StoreRunPort:
    """graph.RunPort 구현. observe는 읽기 전용, 나머지는 짧은 write 트랜잭션."""

    def __init__(self, pack: LoadedPack):
        self.pack = pack
        self.gateway = ToolGateway(pack)

    def observe(self, run_id: str) -> Observation:
        with db.read() as conn:
            return build_observation(conn, self.pack, run_id)

    def reserve_step(self, run_id: str, obs: Observation) -> int | None:
        with db.write() as tx:
            return reserve_step(
                tx,
                run_id,
                obs.versions,
                replanning_spec.GOAL,
                obs.data,
                replanning_spec.tool_schemas(obs.available),
            )

    def execute(
        self, run_id: str, step_no: int, message: AIMessage | None, meta: StepMeta
    ) -> GatewayResult:
        return self.gateway.execute(run_id, step_no, message, meta)

    def finish(self, run_id: str, status: str, reason: str) -> None:
        with db.write() as tx:
            end_run(tx, run_id, status, reason)


def _fail(run_id: str, reason: str) -> None:
    with db.write() as tx:
        tx.execute(
            "UPDATE agent_step SET status = 'ABORTED', abort_reason = ?"
            " WHERE run_id = ? AND status = 'RESERVED'",
            (reason, run_id),
        )
        tx.execute(
            "UPDATE solver_job SET status = 'ABORTED' WHERE run_id = ? AND status = 'RESERVED'",
            (run_id,),
        )
        end_run(tx, run_id, "ERROR", reason)


def invoke(pack: LoadedPack, graph_input: dict[str, Any], model: ChatModel) -> AgentRun:
    """Run 1개를 observe부터 호출한다. WAIT·DONE·Budget 소진·비활성 중 하나로 끝난다."""
    if set(graph_input) != GRAPH_INPUT_KEYS:
        raise ValueError(f"graph input accepts only {sorted(GRAPH_INPUT_KEYS)}")
    run_id = graph_input["run_id"]
    graph = graph_module.build_graph(StoreRunPort(pack), model, replanning_spec, replanning_prompt)
    try:
        graph.invoke({"run_id": run_id}, {"recursion_limit": replanning_spec.RECURSION_LIMIT})
    except GraphRecursionError:
        _fail(run_id, "RECURSION_LIMIT")
    except Exception as e:  # 모델·도구 예외는 Run ERROR로 드러낸다
        log.exception("run %s failed", run_id)
        _fail(run_id, f"EXCEPTION: {type(e).__name__}")
    with db.read() as conn:
        run = get_run(conn, run_id)
    assert run is not None
    return run
