"""Application settings, loaded from environment variables (and an optional `.env` file)."""

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AIProvider(StrEnum):
    OPENAI = "openai"
    MOCK = "mock"  # Deterministic, offline provider for demos and tests.


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Infrastructure
    kafka_bootstrap_servers: str = "localhost:9092"
    mongo_url: str = "mongodb://localhost:27017"
    mongo_database: str = "notetaker"
    storage_dir: Path = Path("./data/audio")

    # Kafka topics and consumer groups
    topic_uploaded: str = "notes.uploaded"
    topic_transcribed: str = "notes.transcribed"
    transcriber_group: str = "notetaker-transcriber"
    summarizer_group: str = "notetaker-summarizer"

    # Upload limits (OpenAI's transcription endpoint accepts files up to 25 MB)
    max_upload_mb: int = Field(default=25, gt=0)

    # AI
    ai_provider: AIProvider = AIProvider.OPENAI
    openai_api_key: SecretStr | None = None
    transcription_model: str = "gpt-4o-mini-transcribe"
    summary_model: str = "gpt-5-mini"
    openai_max_retries: int = Field(default=3, ge=0)
    openai_timeout_seconds: float = Field(default=120.0, gt=0)

    # API
    cors_origins: list[str] = ["*"]
    log_level: str = "INFO"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @model_validator(mode="after")
    def _require_key_for_openai(self) -> "Settings":
        if self.ai_provider is AIProvider.OPENAI and not self.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required when AI_PROVIDER=openai")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
