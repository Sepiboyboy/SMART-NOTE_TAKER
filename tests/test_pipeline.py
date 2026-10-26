from pathlib import Path

import pytest

from notetaker.ai import MOCK_SEGMENTS, MockSummarizer, MockTranscriber
from notetaker.audio import AudioError
from notetaker.config import Settings
from notetaker.models import Note, NoteStatus, Summary, Transcript, TranscriptSegment
from notetaker.pipeline import Pipeline
from notetaker.storage import AudioStorage

from .conftest import InMemoryNoteRepository, RecordingBus


class FailingTranscriber:
    async def transcribe(self, audio_path: Path) -> Transcript:
        raise RuntimeError("provider unavailable")


class CorruptAudioTranscriber:
    async def transcribe(self, audio_path: Path) -> Transcript:
        raise AudioError("Unsupported or corrupt audio (Invalid data found)")


class SilentTranscriber:
    async def transcribe(self, audio_path: Path) -> Transcript:
        return Transcript(segments=[], duration_seconds=12.0)


def make_pipeline(repo, storage, bus, settings, transcriber=None, summarizer=None) -> Pipeline:
    return Pipeline(
        repo=repo,
        storage=storage,
        bus=bus,
        transcriber=transcriber or MockTranscriber(),
        summarizer=summarizer or MockSummarizer(),
        transcribed_topic=settings.topic_transcribed,
    )


@pytest.fixture
async def note(repo: InMemoryNoteRepository) -> Note:
    note = Note(
        id="n1", filename="call.mp3", content_type="audio/mpeg", size_bytes=3, audio_file="n1.mp3"
    )
    await repo.create(note)
    return note


async def test_full_pipeline_produces_grounded_summary(
    repo: InMemoryNoteRepository,
    storage: AudioStorage,
    bus: RecordingBus,
    settings: Settings,
    note: Note,
) -> None:
    pipeline = make_pipeline(repo, storage, bus, settings)

    await pipeline.transcribe(note.id)
    transcribed = repo.notes[note.id]
    assert transcribed.status is NoteStatus.TRANSCRIBED
    assert len(transcribed.segments) == len(MOCK_SEGMENTS)
    assert {s.speaker for s in transcribed.segments} == {"A", "B"}
    assert transcribed.transcript and transcribed.transcript.startswith("Weekly product sync")
    assert transcribed.duration_seconds and transcribed.duration_seconds > 0
    assert [(t, e.note_id) for t, e in bus.events] == [(settings.topic_transcribed, note.id)]

    await pipeline.summarize(note.id)
    done = repo.notes[note.id]
    assert done.status is NoteStatus.COMPLETED
    assert isinstance(done.summary, Summary)
    assert done.summary.grounding_score == 1.0
    assert done.summary.decisions[0].quote.startswith("We agreed")
    sara = next(a for a in done.summary.action_items if a.owner == "Sara")
    assert sara.verified and sara.owner_verified
    assert sara.due == "Wednesday"


async def test_summary_uses_renamed_speakers(
    repo: InMemoryNoteRepository,
    storage: AudioStorage,
    bus: RecordingBus,
    settings: Settings,
    note: Note,
) -> None:
    seen: dict[str, str] = {}

    class SpyingSummarizer(MockSummarizer):
        async def summarize(self, segments, speakers):
            seen.update(speakers)
            return await super().summarize(segments, speakers)

    pipeline = make_pipeline(repo, storage, bus, settings, summarizer=SpyingSummarizer())
    await pipeline.transcribe(note.id)
    await repo.set_speakers(note.id, {"A": "Omid"})
    await pipeline.summarize(note.id)
    assert seen == {"A": "Omid"}


async def test_transcription_failure_marks_note_failed(
    repo: InMemoryNoteRepository,
    storage: AudioStorage,
    bus: RecordingBus,
    settings: Settings,
    note: Note,
) -> None:
    pipeline = make_pipeline(repo, storage, bus, settings, transcriber=FailingTranscriber())

    await pipeline.transcribe(note.id)

    failed = repo.notes[note.id]
    assert failed.status is NoteStatus.FAILED
    assert failed.error == "Transcription failed: provider unavailable"
    assert bus.events == []


async def test_corrupt_audio_error_is_shown_as_is(
    repo: InMemoryNoteRepository,
    storage: AudioStorage,
    bus: RecordingBus,
    settings: Settings,
    note: Note,
) -> None:
    pipeline = make_pipeline(repo, storage, bus, settings, transcriber=CorruptAudioTranscriber())
    await pipeline.transcribe(note.id)
    assert repo.notes[note.id].error == "Unsupported or corrupt audio (Invalid data found)"


async def test_silent_audio_marks_note_failed(
    repo: InMemoryNoteRepository,
    storage: AudioStorage,
    bus: RecordingBus,
    settings: Settings,
    note: Note,
) -> None:
    pipeline = make_pipeline(repo, storage, bus, settings, transcriber=SilentTranscriber())

    await pipeline.transcribe(note.id)

    assert repo.notes[note.id].status is NoteStatus.FAILED
    assert "No speech" in (repo.notes[note.id].error or "")


async def test_legacy_note_without_segments_can_be_summarized(
    repo: InMemoryNoteRepository, storage: AudioStorage, bus: RecordingBus, settings: Settings
) -> None:
    await repo.create(
        Note(
            id="old",
            filename="old.mp3",
            content_type="audio/mpeg",
            size_bytes=1,
            audio_file="old.mp3",
            status=NoteStatus.TRANSCRIBED,
            transcript="Intro. Sara will send the deck.",
        )
    )
    await make_pipeline(repo, storage, bus, settings).summarize("old")
    assert repo.notes["old"].status is NoteStatus.COMPLETED


async def test_redelivered_events_are_ignored_once_processed(
    repo: InMemoryNoteRepository,
    storage: AudioStorage,
    bus: RecordingBus,
    settings: Settings,
    note: Note,
) -> None:
    pipeline = make_pipeline(repo, storage, bus, settings)
    await pipeline.transcribe(note.id)
    await pipeline.summarize(note.id)

    await pipeline.transcribe(note.id)
    await pipeline.summarize(note.id)

    assert repo.notes[note.id].status is NoteStatus.COMPLETED
    assert len(bus.events) == 1


async def test_unknown_note_is_skipped(
    repo: InMemoryNoteRepository, storage: AudioStorage, bus: RecordingBus, settings: Settings
) -> None:
    pipeline = make_pipeline(repo, storage, bus, settings)
    await pipeline.transcribe("missing")
    await pipeline.summarize("missing")
    assert repo.notes == {}
    assert bus.events == []


async def test_mock_summarizer_separates_points_decisions_and_actions() -> None:
    texts = [
        "Quarterly review.",
        "The launch went well.",
        "We decided to keep the current pricing.",
        "Alex will send the report by Friday.",
    ]
    segments = [
        TranscriptSegment(id=i, speaker="A", start=i, end=i + 1, text=t)
        for i, t in enumerate(texts)
    ]
    draft = await MockSummarizer().summarize(segments, {})
    assert draft.title == "Quarterly review"
    assert [p.text for p in draft.key_points] == ["The launch went well."]
    assert [d.segment_ids for d in draft.decisions] == [[2]]
    assert draft.action_items[0].owner == "Alex"
    assert draft.action_items[0].due == "Friday"
