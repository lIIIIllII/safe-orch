"""모델 호출 (설계서 §11.1·§11.2, 부록 A.16·A.17).

그래프는 ChatModel 프로토콜(bind_tools + invoke)만 안다. 운영 모델은 ChatOpenAI이고, 테스트는 같은
프로토콜의 스크립트 모델을 주입한다. 운영 코드에 테스트용 분기는 없다.
전송 재시도는 SDK가 아니라 invoke_with_retry가 1회 한다(max_retries=0). 그래야 시도 수를 안다.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import openai
from langchain_core.messages import AIMessage, BaseMessage

from app.config import Settings

TIMEOUT_S = 30
TRANSPORT_RETRIES = 1  # §11.2 전송 재시도 1회 (시도 2회로 계상)

# 다시 시도할 전송 오류와, 다시 시도하지 않는 설정 오류 (A.17)
RETRYABLE = (openai.APIConnectionError, openai.RateLimitError, openai.InternalServerError)
CONFIG_ERRORS = (
    openai.AuthenticationError,
    openai.PermissionDeniedError,
    openai.NotFoundError,
    openai.BadRequestError,
)


class Runnable(Protocol):
    def invoke(self, messages: Sequence[BaseMessage]) -> AIMessage: ...


class ChatModel(Protocol):
    def bind_tools(self, tools: Sequence[dict[str, Any]], **kwargs: Any) -> Runnable: ...


ModelFactory = Callable[[], ChatModel]


def bind(model: ChatModel, tools: Sequence[dict[str, Any]]) -> Runnable:
    """도구 호출을 강제하고 병렬 호출을 끈다 (§11.2)."""
    return model.bind_tools(tools, tool_choice="any", parallel_tool_calls=False)


def model_id(model: Any, message: AIMessage | None = None) -> str:
    """응답이 알려 준 실제 모델(스냅샷)을 먼저, 없으면 설정한 이름."""
    if message is not None and message.response_metadata.get("model_name"):
        return str(message.response_metadata["model_name"])
    return str(getattr(model, "model_name", None) or type(model).__name__)


def model_settings(settings: Settings) -> dict[str, Any]:
    """ChatOpenAI에 넘기는 설정(키 제외). 비어 있는 선택 값은 넘기지 않는다 (A.17)."""
    out: dict[str, Any] = {
        "model": settings.openai_model,
        "timeout": TIMEOUT_S,
        "max_retries": 0,
    }
    for key, value in (
        ("temperature", settings.openai_temperature),
        ("seed", settings.openai_seed),
        ("reasoning_effort", settings.openai_reasoning_effort),
    ):
        if value is not None:
            out[key] = value
    return out


def openai_model(settings: Settings) -> ChatModel:
    """운영 모델. 키나 모델 이름이 없으면 예외(호출한 Run은 ERROR가 된다)."""
    from langchain_openai import ChatOpenAI

    key = settings.openai_api_key.get_secret_value()
    if not key or not settings.openai_model:
        raise RuntimeError("OPENAI_API_KEY / OPENAI_MODEL not configured")
    return ChatOpenAI(api_key=key, **model_settings(settings))


@dataclass(frozen=True)
class LLMCall:
    """모델 호출 결과. error_kind: None(성공), LLM_ERROR(전송 실패 2회), LLM_CONFIG(설정 오류)."""

    message: AIMessage | None
    attempts: int
    error_kind: Literal["LLM_ERROR", "LLM_CONFIG"] | None = None
    error: str | None = None


def invoke_with_retry(runnable: Runnable, messages: Sequence[BaseMessage]) -> LLMCall:
    """전송 오류면 1회 다시 부른다. 설정 오류는 다시 부르지 않는다. 그 밖의 예외는 그대로 낸다."""
    attempts = 0
    while True:
        attempts += 1
        try:
            return LLMCall(runnable.invoke(messages), attempts)
        except CONFIG_ERRORS as e:
            return LLMCall(None, attempts, "LLM_CONFIG", type(e).__name__)
        except RETRYABLE as e:
            # 크레딧 부족은 기다려도 풀리지 않는 설정 문제다 (A.18에서 A.17 보완)
            if getattr(e, "code", None) == "insufficient_quota":
                return LLMCall(None, attempts, "LLM_CONFIG", "insufficient_quota")
            if attempts > TRANSPORT_RETRIES:
                return LLMCall(None, attempts, "LLM_ERROR", type(e).__name__)
