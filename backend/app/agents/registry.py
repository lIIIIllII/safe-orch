"""agent_type별 등록부.

agent_type마다 AgentSpec·prompt·Observation 계산(observer)·Action 실행기(executor)·exec_contract_version을
묶는다. 이 모듈을 import하는 곳은 runtime(과 테스트)뿐이고, observers·executors를 import하는 곳은 이
모듈(과 테스트)뿐이다. 다음 Agent는 specs·prompts·observers·executors에 파일을 더하고 여기에 한 줄을 더한다.
"""

from app.agents.executors.coordination import CoordinationExecutor
from app.agents.executors.event_response import EventResponseExecutor
from app.agents.executors.intake import IntakeExecutor
from app.agents.executors.main import MainExecutor
from app.agents.executors.replanning import ReplanningExecutor
from app.agents.executors.schedule_review import ScheduleReviewExecutor
from app.agents.observers import coordination as coordination_observer
from app.agents.observers import event_response as event_response_observer
from app.agents.observers import intake as intake_observer
from app.agents.observers import main as main_observer
from app.agents.observers import replanning as replanning_observer
from app.agents.observers import schedule_review as schedule_review_observer
from app.agents.prompts import coordination as coordination_prompt
from app.agents.prompts import event_response as event_response_prompt
from app.agents.prompts import intake as intake_prompt
from app.agents.prompts import main as main_prompt
from app.agents.prompts import replanning as replanning_prompt
from app.agents.prompts import schedule_review as schedule_review_prompt
from app.agents.specs import coordination as coordination_spec
from app.agents.specs import event_response as event_response_spec
from app.agents.specs import intake as intake_spec
from app.agents.specs import main as main_spec
from app.agents.specs import replanning as replanning_spec
from app.agents.specs import schedule_review as schedule_review_spec
from app.agents.types import AgentBinding

BINDINGS: dict[str, AgentBinding] = {
    main_spec.AGENT_TYPE: AgentBinding(
        spec=main_spec.SPEC,
        prompt=main_prompt,
        observer=main_observer,
        executor=MainExecutor,
        exec_contract_version="main-c5",  # 기록만. 일정 Case의 일정 검토 호출 (AG-36)
    ),
    replanning_spec.AGENT_TYPE: AgentBinding(
        spec=replanning_spec.SPEC,
        prompt=replanning_prompt,
        observer=replanning_observer,
        executor=ReplanningExecutor,
        exec_contract_version="replanning-c9",  # 기록만. 조건의 희망 영역 삭제, 기준 위치 (CV-29)
    ),
    coordination_spec.AGENT_TYPE: AgentBinding(
        spec=coordination_spec.SPEC,
        prompt=coordination_prompt,
        observer=coordination_observer,
        executor=CoordinationExecutor,
        exec_contract_version="coordination-c7",  # 기록만. 사전 확인 단계 삭제 (AG-34)
    ),
    event_response_spec.AGENT_TYPE: AgentBinding(
        spec=event_response_spec.SPEC,
        prompt=event_response_prompt,
        observer=event_response_observer,
        executor=EventResponseExecutor,
        exec_contract_version="event-response-c7",  # 기록만. 신고자 되묻기 삭제
    ),
    schedule_review_spec.AGENT_TYPE: AgentBinding(
        spec=schedule_review_spec.SPEC,
        prompt=schedule_review_prompt,
        observer=schedule_review_observer,
        executor=ScheduleReviewExecutor,
        exec_contract_version="schedule-review-c1",  # 기록만. 묶음안까지 (AG-36)
    ),
    intake_spec.AGENT_TYPE: AgentBinding(
        spec=intake_spec.SPEC,
        prompt=intake_prompt,
        observer=intake_observer,
        executor=IntakeExecutor,
        exec_contract_version="intake-c12",  # 기록만. 완료의 시각은 기준 위치가 된다 (CV-29)
    ),
}
