"""Speech-to-text and summarization providers."""

import re
from pathlib import Path
from typing import Protocol

from openai import AsyncOpenAI

from .config import AIProvider, Settings
from .models import Summary

SUMMARY_INSTRUCTIONS = """\
You turn raw transcripts of voice notes, meetings, and lectures into clear, faithful notes.
Only use information present in the transcript. Do not invent names, dates, or numbers.
Write in the same language as the transcript."""


class Transcriber(Protocol):
    async def transcribe(self, audio_path: Path) -> str: ...


class Summarizer(Protocol):
    async def summarize(self, transcript: str) -> Summary: ...


class OpenAITranscriber:
    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        self._client = client
        self._model = model

    async def transcribe(self, audio_path: Path) -> str:
        with audio_path.open("rb") as audio:
            result = await self._client.audio.transcriptions.create(model=self._model, file=audio)
        return result.text.strip()


class OpenAISummarizer:
    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        self._client = client
        self._model = model

    async def summarize(self, transcript: str) -> Summary:
        response = await self._client.responses.parse(
            model=self._model,
            instructions=SUMMARY_INSTRUCTIONS,
            input=transcript,
            text_format=Summary,
        )
        if response.output_parsed is None:
            raise RuntimeError("Model returned no structured summary")
        return response.output_parsed


MOCK_TRANSCRIPT = (
    "Weekly product sync for the mobile team. "
    "The mobile release is on track for Friday after QA signed off on the payment flow. "
    "Onboarding completion went up twelve percent since we shortened the sign-up form. "
    "Support tickets about notifications are still high, so that is our main risk. "
    "Sara will investigate the notification delays before Wednesday. "
    "We need to update the help center articles for the new checkout. "
    "Next week we will review the analytics dashboard together."
)

_ACTION_HINT = re.compile(r"\b(will|need to|needs to|should|must|todo|follow up)\b", re.IGNORECASE)


class MockTranscriber:
    """Returns a canned transcript, so the full pipeline can run without an API key."""

    async def transcribe(self, audio_path: Path) -> str:
        return MOCK_TRANSCRIPT


class MockSummarizer:
    """A simple extractive summarizer: deterministic, offline, and good enough for demos."""

    async def summarize(self, transcript: str) -> Summary:
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", transcript) if s.strip()]
        actions = [s for s in sentences if _ACTION_HINT.search(s)]
        points = [s for s in sentences[1:] if s not in actions]
        first = sentences[0].rstrip(".!?").split() if sentences else ["Untitled"]
        title = " ".join(first[:8]) + ("..." if len(first) > 8 else "")
        return Summary(
            title=title,
            overview=" ".join(sentences[1:3] or sentences[:1]),
            key_points=points[:5],
            action_items=actions[:5],
        )


def build_ai(settings: Settings) -> tuple[Transcriber, Summarizer]:
    if settings.ai_provider is AIProvider.MOCK:
        return MockTranscriber(), MockSummarizer()

    assert settings.openai_api_key is not None  # Enforced by Settings validation.
    client = AsyncOpenAI(
        api_key=settings.openai_api_key.get_secret_value(),
        max_retries=settings.openai_max_retries,
        timeout=settings.openai_timeout_seconds,
    )
    return (
        OpenAITranscriber(client, settings.transcription_model),
        OpenAISummarizer(client, settings.summary_model),
    )
