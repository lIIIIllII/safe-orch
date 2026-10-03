"""agent_type별 등록부.

agent_type마다 AgentSpec·prompt·Observation 계산(observer)·Action 실행기(executor)·exec_contract_version을
묶는다. 이 모듈을 import하는 곳은 runtime(과 테스트)뿐이고, observers·executors를 import하는 곳은 이
모듈(과 테스트)뿐이다. 다음 Agent는 specs·prompts·observers·executors에 파일을 더하고 여기에 한 줄을 더한다.
"""

from app.agents.executors.coordination import CoordinationExecutor
from app.agents.executors.event_response import EventResponseExecutor
from app.agents.executors.intake import IntakeExecutor
from app.agents.executors.replanning import ReplanningExecutor
from app.agents.observers import coordination as coordination_observer
from app.agents.observers import event_response as event_response_observer
from app.agents.observers import intake as intake_observer
from app.agents.observers import replanning as replanning_observer
from app.agents.prompts import coordination as coordination_prompt
from app.agents.prompts import event_response as event_response_prompt
from app.agents.prompts import intake as intake_prompt
from app.agents.prompts import replanning as replanning_prompt
from app.agents.specs import coordination as coordination_spec
from app.agents.specs import event_response as event_response_spec
from app.agents.specs import intake as intake_spec
from app.agents.specs import replanning as replanning_spec
from app.agents.types import AgentBinding

BINDINGS: dict[str, AgentBinding] = {
    replanning_spec.AGENT_TYPE: AgentBinding(
        spec=replanning_spec.SPEC,
        prompt=replanning_prompt,
        observer=replanning_observer,
        executor=ReplanningExecutor,
        exec_contract_version="replanning-c2",  # 기록만. 스킬 층(skill 인자, 순서 조건 제거)
    ),
    coordination_spec.AGENT_TYPE: AgentBinding(
        spec=coordination_spec.SPEC,
        prompt=coordination_prompt,
        observer=coordination_observer,
        executor=CoordinationExecutor,
        exec_contract_version="coordination-c2",  # 기록만
    ),
    event_response_spec.AGENT_TYPE: AgentBinding(
        spec=event_response_spec.SPEC,
        prompt=event_response_prompt,
        observer=event_response_observer,
        executor=EventResponseExecutor,
        exec_contract_version="event-response-c2",  # 기록만
    ),
    intake_spec.AGENT_TYPE: AgentBinding(
        spec=intake_spec.SPEC,
        prompt=intake_prompt,
        observer=intake_observer,
        executor=IntakeExecutor,
        exec_contract_version="intake-c2",  # 기록만
    ),
}
