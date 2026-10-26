"""Speech-to-text and summarization providers."""

import asyncio
import base64
import re
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from openai import AsyncOpenAI
from pydantic import BaseModel

from . import audio
from .config import AIProvider, Settings
from .models import (
    CitedAction,
    CitedItem,
    DraftTopic,
    SectionNotes,
    SummaryDraft,
    Transcript,
    TranscriptSegment,
)

# --- Interfaces ---------------------------------------------------------------------------


class Transcriber(Protocol):
    async def transcribe(self, audio_path: Path) -> Transcript: ...


class Summarizer(Protocol):
    async def summarize(
        self, segments: Sequence[TranscriptSegment], speakers: dict[str, str]
    ) -> SummaryDraft: ...


# --- Transcription ------------------------------------------------------------------------

MAX_REFERENCE_SPEAKERS = 4  # OpenAI accepts up to 4 known-speaker reference clips.
MIN_REFERENCE_SECONDS = 2.5  # References must be 2-10 s; keep a margin for frame-level cuts.
MAX_REFERENCE_SECONDS = 10.0


def merge_chunks(
    chunks: Sequence[Sequence[TranscriptSegment]], offsets: Sequence[float]
) -> list[TranscriptSegment]:
    """Shift each chunk's timestamps by its start offset and number segments globally."""
    merged: list[TranscriptSegment] = []
    for segments, offset in zip(chunks, offsets, strict=True):
        for seg in segments:
            if seg.text.strip():
                merged.append(
                    TranscriptSegment(
                        id=len(merged),
                        speaker=seg.speaker,
                        start=round(seg.start + offset, 2),
                        end=round(seg.end + offset, 2),
                        text=seg.text.strip(),
                    )
                )
    return merged


class OpenAITranscriber:
    """Normalizes and chunks the audio, then transcribes each chunk with speaker labels.

    Diarization labels are only consistent within one request, so after the first chunk we
    cut a short reference clip per speaker and pass those to every later chunk. That way
    "A" in chunk 1 is still "A" in chunk 5.
    """

    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        chunk_seconds: float,
        max_seconds: float,
        concurrency: int,
    ) -> None:
        self._client = client
        self._model = model
        self._chunk_seconds = chunk_seconds
        self._max_seconds = max_seconds
        self._concurrency = concurrency
        self._diarize = "diarize" in model

    async def transcribe(self, audio_path: Path) -> Transcript:
        with tempfile.TemporaryDirectory(prefix="notetaker-") as tmp:
            work = Path(tmp)
            normalized = work / "audio.mp3"
            await audio.normalize(audio_path, normalized)
            duration = await audio.probe_duration(normalized)
            if duration > self._max_seconds:
                raise audio.AudioError(
                    f"Recording is {duration / 60:.0f} minutes; "
                    f"the limit is {self._max_seconds / 60:.0f}"
                )

            pauses = await audio.find_pauses(normalized) if duration > self._chunk_seconds else []
            chunks = audio.plan_chunks(duration, self._chunk_seconds, pauses)
            if len(chunks) == 1:
                paths = [normalized]
            else:
                paths = [work / f"chunk-{i}.mp3" for i in range(len(chunks))]
                for path, (start, end) in zip(paths, chunks, strict=True):
                    await audio.extract(normalized, start, end, path)

            lengths = [end - start for start, end in chunks]
            first = await self._request(paths[0], lengths[0])
            references = (
                await self._speaker_references(normalized, first, work)
                if self._diarize and len(paths) > 1
                else {}
            )

            semaphore = asyncio.Semaphore(self._concurrency)

            async def run(path: Path, length: float) -> list[TranscriptSegment]:
                async with semaphore:
                    return await self._request(path, length, references)

            rest = await asyncio.gather(
                *(run(p, n) for p, n in zip(paths[1:], lengths[1:], strict=True))
            )

        segments = merge_chunks([first, *rest], [start for start, _ in chunks])
        return Transcript(segments=segments, duration_seconds=round(duration, 2))

    async def _request(
        self, path: Path, length: float, references: dict[str, str] | None = None
    ) -> list[TranscriptSegment]:
        kwargs: dict[str, Any] = {"model": self._model}
        if self._diarize:
            kwargs |= {"response_format": "diarized_json", "chunking_strategy": "auto"}
            if references:
                kwargs |= {
                    "known_speaker_names": list(references),
                    "known_speaker_references": list(references.values()),
                }
        with path.open("rb") as file:
            result = await self._client.audio.transcriptions.create(file=file, **kwargs)

        if not self._diarize:  # Plain models return text only: one segment per chunk.
            return [TranscriptSegment(id=0, speaker="A", start=0.0, end=length, text=result.text)]
        return [
            TranscriptSegment(id=0, speaker=s.speaker, start=s.start, end=s.end, text=s.text)
            for s in result.segments
        ]

    async def _speaker_references(
        self, normalized: Path, segments: Sequence[TranscriptSegment], work: Path
    ) -> dict[str, str]:
        """Pick the longest clean segment of each speaker and encode it as a data URL."""
        order: list[str] = []
        best: dict[str, TranscriptSegment] = {}
        for seg in segments:
            if seg.speaker not in order:
                order.append(seg.speaker)
            if seg.speaker not in order[:MAX_REFERENCE_SPEAKERS]:
                continue
            if seg.end - seg.start < MIN_REFERENCE_SECONDS:
                continue
            current = best.get(seg.speaker)
            if current is None or seg.end - seg.start > current.end - current.start:
                best[seg.speaker] = seg

        references = {}
        for i, (label, seg) in enumerate(best.items()):
            clip = work / f"reference-{i}.mp3"
            end = min(seg.end, seg.start + MAX_REFERENCE_SECONDS)
            await audio.extract(normalized, seg.start, end, clip)
            data = base64.b64encode(clip.read_bytes()).decode()
            references[label] = f"data:audio/mpeg;base64,{data}"
        return references


# --- Summarization ------------------------------------------------------------------------

_RULES = """\
The transcript is given as lines of the form `[S<id> <mm:ss> <speaker>] <text>`.

Rules:
- Use only information stated in the transcript. Never invent names, numbers, dates or \
commitments.
- For every key point, decision and action item, list the IDs of the supporting segments in \
`segment_ids`, and copy a short verbatim excerpt (5-25 words) from those segments into `quote`. \
Copy the words exactly; do not paraphrase the quote.
- Decisions are conclusions the participants actually agreed on, not proposals or ideas.
- Action items are concrete follow-ups. Set `owner` only if someone explicitly volunteered or \
was explicitly assigned the task; otherwise null. Never infer an owner from who raised a topic. \
Set `due` only if a deadline is stated; otherwise null.
- Topics outline the conversation in order; `start_segment` is the segment where each begins.
- Write in the same language as the transcript."""

SUMMARY_INSTRUCTIONS = (
    "You turn transcripts of meetings, lectures and voice notes into accurate, well-organized "
    "notes.\n\n" + _RULES
)

SECTION_INSTRUCTIONS = (
    "You are given one section of a longer transcript. Extract its topics, key points, "
    "decisions and action items. Another step will merge all sections.\n\n" + _RULES
)

MERGE_INSTRUCTIONS = """\
You are given notes extracted from consecutive sections of one long transcript, as JSON.
Merge them into the final notes for the whole recording:
- Combine duplicate or overlapping items and keep the important ones.
- Keep topics in chronological order, merging ones that continue across sections.
- Write a title and a two-to-four sentence overview of the whole recording.
- Every item you output must come from the input. Keep its `segment_ids` and `quote` exactly as
  given (when merging duplicates, keep one of their quotes). Never create new items.
- Write in the same language as the notes."""


def speaker_name(label: str, speakers: dict[str, str]) -> str:
    return speakers.get(label) or f"Speaker {label}"


def format_timestamp(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return (
        f"{minutes:02d}:{secs:02d}"
        if minutes < 60
        else f"{minutes // 60}:{minutes % 60:02d}:{secs:02d}"
    )


def format_transcript(segments: Sequence[TranscriptSegment], speakers: dict[str, str]) -> str:
    return "\n".join(
        f"[S{s.id} {format_timestamp(s.start)} {speaker_name(s.speaker, speakers)}] {s.text}"
        for s in segments
    )


def estimate_tokens(text: str) -> int:
    return len(text) // 4  # Rough, but only used to decide when to split.


def split_sections(
    segments: Sequence[TranscriptSegment], target_tokens: int
) -> list[list[TranscriptSegment]]:
    """Split into sections of about `target_tokens`, cutting at natural boundaries.

    Once a section reaches the target, it ends at the next speaker change or pause longer than
    two seconds, or forcibly at 1.25x the target.
    """
    sections: list[list[TranscriptSegment]] = []
    current: list[TranscriptSegment] = []
    size = 0
    for seg in segments:
        if current and size >= target_tokens:
            prev = current[-1]
            natural = seg.speaker != prev.speaker or seg.start - prev.end > 2.0
            if natural or size >= target_tokens * 1.25:
                sections.append(current)
                current, size = [], 0
        current.append(seg)
        size += estimate_tokens(seg.text) + 4  # Plus the "[S12 03:14 Name]" prefix.
    if current:
        sections.append(current)
    return sections


class StructuredSummarizer:
    """Summarizes short transcripts in one call, and long ones section by section.

    Long transcripts follow the approach of "Action-Item-Driven Summarization of Long Meeting
    Transcripts" (Golia & Kalita, 2023): extract from each section in parallel, then merge.
    Segment IDs are global, so citations stay valid through the merge.
    """

    def __init__(self, single_pass_tokens: int, section_tokens: int, concurrency: int) -> None:
        self.single_pass_tokens = single_pass_tokens
        self.section_tokens = section_tokens
        self.concurrency = concurrency

    async def _parse[M: BaseModel](self, instructions: str, text: str, schema: type[M]) -> M:
        raise NotImplementedError

    async def summarize(
        self, segments: Sequence[TranscriptSegment], speakers: dict[str, str]
    ) -> SummaryDraft:
        transcript = format_transcript(segments, speakers)
        if estimate_tokens(transcript) <= self.single_pass_tokens:
            return await self._parse(SUMMARY_INSTRUCTIONS, transcript, SummaryDraft)

        semaphore = asyncio.Semaphore(self.concurrency)

        async def extract(section: list[TranscriptSegment]) -> SectionNotes:
            async with semaphore:
                text = format_transcript(section, speakers)
                return await self._parse(SECTION_INSTRUCTIONS, text, SectionNotes)

        notes = await asyncio.gather(
            *(extract(s) for s in split_sections(segments, self.section_tokens))
        )
        merged_input = "\n\n".join(
            f"## Section {i + 1}\n{n.model_dump_json()}" for i, n in enumerate(notes)
        )
        return await self._parse(MERGE_INSTRUCTIONS, merged_input, SummaryDraft)


class OpenAISummarizer(StructuredSummarizer):
    def __init__(self, client: AsyncOpenAI, model: str, **kwargs: int) -> None:
        super().__init__(**kwargs)
        self._client = client
        self._model = model

    async def _parse[M: BaseModel](self, instructions: str, text: str, schema: type[M]) -> M:
        response = await self._client.responses.parse(
            model=self._model, instructions=instructions, input=text, text_format=schema
        )
        if response.output_parsed is None:
            raise RuntimeError("Model returned no structured output")
        return response.output_parsed


# --- Offline mock provider ----------------------------------------------------------------

MOCK_SEGMENTS = [
    ("A", "Weekly product sync for the mobile team."),
    ("A", "The mobile release is on track for Friday after QA signed off on the payment flow."),
    ("B", "Onboarding completion went up twelve percent since we shortened the sign-up form."),
    ("B", "Support tickets about notifications are still high, so that is our main risk."),
    ("A", "We agreed to delay the dark mode work until after the release."),
    ("B", "Sara will investigate the notification delays before Wednesday."),
    ("A", "We need to update the help center articles for the new checkout."),
    ("B", "Next week we will review the analytics dashboard together."),
]

_ACTION_HINT = re.compile(r"\b(will|need to|needs to|should|must|todo|follow up)\b", re.I)
_DECISION_HINT = re.compile(r"\b(agreed|decided|we'll go with|approved)\b", re.I)
_OWNER = re.compile(r"^([A-Z][a-z]+) (?:will|should|is going to)\b")
_DUE = re.compile(r"\b(?:by|before|on) (Monday|Tuesday|Wednesday|Thursday|Friday|tomorrow)\b")


class MockTranscriber:
    """Returns a canned two-speaker transcript, so the pipeline can run without an API key."""

    async def transcribe(self, audio_path: Path) -> Transcript:
        segments, clock = [], 0.0
        for i, (speaker, text) in enumerate(MOCK_SEGMENTS):
            length = round(len(text.split()) / 2.5, 1)  # ~150 words per minute.
            segments.append(
                TranscriptSegment(id=i, speaker=speaker, start=clock, end=clock + length, text=text)
            )
            clock = round(clock + length + 0.6, 1)
        return Transcript(segments=segments, duration_seconds=clock)


class MockSummarizer:
    """A deterministic extractive summarizer that produces properly cited items."""

    async def summarize(
        self, segments: Sequence[TranscriptSegment], speakers: dict[str, str]
    ) -> SummaryDraft:
        if not segments:
            return SummaryDraft(
                title="Untitled", overview="", topics=[], key_points=[], decisions=[],
                action_items=[],
            )  # fmt: skip
        first, rest = segments[0], segments[1:]
        decisions = [s for s in rest if _DECISION_HINT.search(s.text)]
        actions = [s for s in rest if _ACTION_HINT.search(s.text) and s not in decisions]
        points = [s for s in rest if s not in decisions and s not in actions]

        def cited(seg: TranscriptSegment) -> CitedItem:
            return CitedItem(text=seg.text, segment_ids=[seg.id], quote=seg.text)

        def action(seg: TranscriptSegment) -> CitedAction:
            owner, due = _OWNER.search(seg.text), _DUE.search(seg.text)
            return CitedAction(
                task=seg.text,
                owner=owner.group(1) if owner else None,
                due=due.group(1) if due else None,
                segment_ids=[seg.id],
                quote=seg.text,
            )

        words = first.text.rstrip(".!?").split()
        return SummaryDraft(
            title=" ".join(words[:8]) + ("..." if len(words) > 8 else ""),
            overview=" ".join(s.text for s in rest[:2]) or first.text,
            topics=[DraftTopic(title="Discussion", summary=first.text, start_segment=first.id)],
            key_points=[cited(s) for s in points[:5]],
            decisions=[cited(s) for s in decisions[:5]],
            action_items=[action(s) for s in actions[:5]],
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
    transcriber = OpenAITranscriber(
        client,
        settings.transcription_model,
        chunk_seconds=settings.chunk_seconds,
        max_seconds=settings.max_audio_minutes * 60,
        concurrency=settings.ai_concurrency,
    )
    summarizer = OpenAISummarizer(
        client,
        settings.summary_model,
        single_pass_tokens=settings.summary_single_pass_tokens,
        section_tokens=settings.summary_section_tokens,
        concurrency=settings.ai_concurrency,
    )
    return transcriber, summarizer
