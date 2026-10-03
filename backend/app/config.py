"""환경 설정. 저장소 루트의 .env를 읽는다."""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py → parents[2] = 저장소 루트
REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    db_path: Path = Path("data/safe_orch.db")
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = ""  # 필수. 날짜가 붙은 스냅샷 ID
    # 값이 있을 때만 ChatOpenAI에 넘긴다. 비추론 모델: TEMPERATURE=0·SEED=0,
    # 추론 모델: 두 값을 비우고 REASONING_EFFORT를 가장 낮게
    openai_temperature: float | None = None
    openai_seed: int | None = None
    openai_reasoning_effort: str | None = None
    demo_mode: bool = True
    pack: str = "shipyard"  # domain_packs/<pack>/
    dispatch_worker: bool = True  # 기동 시 dispatch 워커 스레드 시작
    dispatch_poll_s: float = 0.5
    # Agent 자동 시작. 기본은 켜짐이고, 설정은 Agent를 끄는 스위치다.
    # PASS 후보에 동의 대기 항목이 있으면 Coordination이 담당자와 협의하고 확정 뒤 통지한다(기본안 A).
    # 끄면 Supervisor 검토 대기(기본안 B)이고 통지가 없다.
    coordination_enabled: bool = True
    # 지연 신고(DELAY)를 접수하면 Event Response가 대상 작업·사실 수정안을 찾는다.
    # 끄면 Hold만 걸고 Supervisor가 처리한다(Scene 4).
    event_response_enabled: bool = True

    @field_validator("openai_temperature", "openai_seed", "openai_reasoning_effort", mode="before")
    @classmethod
    def _blank_is_none(cls, v: object) -> object:
        """.env의 빈 값은 '넘기지 않음'이다."""
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("db_path")
    @classmethod
    def _resolve_db_path(cls, v: Path) -> Path:
        path = v if v.is_absolute() else REPO_ROOT / v
        path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache
def get_settings() -> Settings:
    return Settings()
