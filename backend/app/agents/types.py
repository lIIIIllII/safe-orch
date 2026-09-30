"""그래프와 Gateway가 주고받는 값 (설계서 §11.2, 부록 A.16). store를 import하지 않는다."""

from dataclasses import dataclass
from typing import Literal

GatewayKind = Literal["CONTINUE", "WAIT", "DONE", "REJECTED", "INACTIVE"]


@dataclass(frozen=True)
class StepMeta:
    model_id: str
    prompt_version: str
    llm_attempts: int = 1


@dataclass(frozen=True)
class GatewayResult:
    """그래프 edge는 kind로만 분기한다(§11.1 경계 규칙 1). DONE이면 end_status로 Run을 끝낸다."""

    kind: GatewayKind
    reason: str | None = None
    end_status: str | None = None
    end_reason: str | None = None
