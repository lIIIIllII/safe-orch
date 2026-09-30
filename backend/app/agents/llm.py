"""모델 호출 (설계서 §11.1·§11.2, 부록 A.16).

그래프는 ChatModel 프로토콜(bind_tools + invoke)만 안다. 운영 모델은 ChatOpenAI이고(D4에서 연결),
테스트는 같은 프로토콜의 스크립트 모델을 주입한다. 운영 코드에 테스트용 분기는 없다.
"""

from collections.abc import Callable, Sequence
from typing import Any, Protocol

from langchain_core.messages import AIMessage, BaseMessage

from app.config import Settings

TIMEOUT_S = 30
MAX_RETRIES = 1


class Runnable(Protocol):
    def invoke(self, messages: Sequence[BaseMessage]) -> AIMessage: ...


class ChatModel(Protocol):
    def bind_tools(self, tools: Sequence[dict[str, Any]], **kwargs: Any) -> Runnable: ...


ModelFactory = Callable[[], ChatModel]


def bind(model: ChatModel, tools: Sequence[dict[str, Any]]) -> Runnable:
    """도구 호출을 강제하고 병렬 호출을 끈다 (§11.2)."""
    return model.bind_tools(tools, tool_choice="any", parallel_tool_calls=False)


def model_id(model: Any) -> str:
    return str(getattr(model, "model_name", None) or type(model).__name__)


def openai_model(settings: Settings) -> ChatModel:
    """운영 모델. API 키가 없으면 예외(호출한 Run은 ERROR가 된다)."""
    from langchain_openai import ChatOpenAI

    key = settings.openai_api_key.get_secret_value()
    if not key or not settings.openai_model:
        raise RuntimeError("OPENAI_API_KEY / OPENAI_MODEL not configured")
    return ChatOpenAI(
        model=settings.openai_model,
        api_key=key,
        temperature=0,
        timeout=TIMEOUT_S,
        max_retries=MAX_RETRIES,
    )
