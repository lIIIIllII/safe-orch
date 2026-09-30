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
    openai_model: str = ""
    demo_mode: bool = True
    pack: str = "shipyard"  # domain_packs/<pack>/ (부록 A.4)
    dispatch_worker: bool = True  # 기동 시 dispatch 워커 스레드 시작 (부록 A.15)
    dispatch_poll_s: float = 0.5

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
