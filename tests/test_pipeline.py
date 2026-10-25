from pathlib import Path

import pytest

from notetaker.ai import MOCK_TRANSCRIPT, MockSummarizer, MockTranscriber
from notetaker.config import Settings
from notetaker.models import Note, NoteStatus, Summary
from notetaker.pipeline import Pipeline
from notetaker.storage import AudioStorage

from .conftest import InMemoryNoteRepository, RecordingBus


class FailingTranscriber:
    async def transcribe(self, audio_path: Path) -> str:
        raise RuntimeError("provider unavailable")


class SilentTranscriber:
    async def transcribe(self, audio_path: Path) -> str:
        return ""


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


async def test_full_pipeline_produces_summary(
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
    assert transcribed.transcript == MOCK_TRANSCRIPT
    assert [(t, e.note_id) for t, e in bus.events] == [(settings.topic_transcribed, note.id)]

    await pipeline.summarize(note.id)
    done = repo.notes[note.id]
    assert done.status is NoteStatus.COMPLETED
    assert isinstance(done.summary, Summary)
    assert done.summary.action_items  # The mock transcript contains follow-ups.


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


async def test_mock_summarizer_separates_actions_from_points() -> None:
    summary = await MockSummarizer().summarize(
        "Quarterly review. The launch went well. Revenue grew. Alex will send the report tomorrow."
    )
    assert summary.title == "Quarterly review"
    assert summary.overview == "The launch went well. Revenue grew."
    assert summary.key_points == ["The launch went well.", "Revenue grew."]
    assert summary.action_items == ["Alex will send the report tomorrow."]
