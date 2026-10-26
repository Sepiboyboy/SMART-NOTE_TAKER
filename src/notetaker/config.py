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
    # Long recordings take a while; a worker must not be evicted from its group mid-task.
    consumer_max_poll_minutes: int = Field(default=30, gt=0)

    # Audio. Uploads are re-encoded and split before transcription, so they may exceed
    # OpenAI's 25 MB per-request limit.
    max_upload_mb: int = Field(default=200, gt=0)
    max_audio_minutes: int = Field(default=180, gt=0)
    chunk_seconds: int = Field(default=600, ge=60)

    # AI
    ai_provider: AIProvider = AIProvider.OPENAI
    openai_api_key: SecretStr | None = None
    transcription_model: str = "gpt-4o-transcribe-diarize"
    summary_model: str = "gpt-5-mini"
    openai_max_retries: int = Field(default=3, ge=0)
    openai_timeout_seconds: float = Field(default=300.0, gt=0)
    ai_concurrency: int = Field(default=3, gt=0)  # Parallel requests per note.

    # Summaries
    summary_single_pass_tokens: int = Field(default=24_000, gt=0)  # Longer: map-reduce.
    summary_section_tokens: int = Field(default=8_000, gt=0)
    drop_unverified_items: bool = False  # False: keep them, flagged as unverified.

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
