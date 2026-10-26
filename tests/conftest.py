from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from notetaker.api import create_app
from notetaker.config import AIProvider, Settings
from notetaker.models import Note, NoteEvent, NoteStatus, utcnow
from notetaker.storage import AudioStorage


class InMemoryNoteRepository:
    def __init__(self) -> None:
        self.notes: dict[str, Note] = {}

    async def create(self, note: Note) -> None:
        self.notes[note.id] = note

    async def get(self, note_id: str) -> Note | None:
        return self.notes.get(note_id)

    async def list_recent(self, limit: int) -> list[Note]:
        return sorted(self.notes.values(), key=lambda n: n.created_at, reverse=True)[:limit]

    async def delete(self, note_id: str) -> Note | None:
        return self.notes.pop(note_id, None)

    async def set_speakers(self, note_id: str, speakers: dict[str, str]) -> Note | None:
        note = self.notes.get(note_id)
        if note is None:
            return None
        self.notes[note_id] = note.model_copy(update={"speakers": speakers, "updated_at": utcnow()})
        return self.notes[note_id]

    async def transition(
        self,
        note_id: str,
        from_statuses: Iterable[NoteStatus],
        to_status: NoteStatus,
        **fields: Any,
    ) -> Note | None:
        note = self.notes.get(note_id)
        if note is None or note.status not in set(from_statuses):
            return None
        updated = note.model_copy(update={**fields, "status": to_status, "updated_at": utcnow()})
        self.notes[note_id] = updated
        return updated


class RecordingBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, NoteEvent]] = []

    async def publish(self, topic: str, event: NoteEvent) -> None:
        self.events.append((topic, event))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        ai_provider=AIProvider.MOCK,
        storage_dir=tmp_path / "audio",
        max_upload_mb=1,
    )


@pytest.fixture
def repo() -> InMemoryNoteRepository:
    return InMemoryNoteRepository()


@pytest.fixture
def bus() -> RecordingBus:
    return RecordingBus()


@pytest.fixture
def storage(settings: Settings) -> AudioStorage:
    return AudioStorage(settings.storage_dir)


@pytest.fixture
def client(
    settings: Settings, repo: InMemoryNoteRepository, bus: RecordingBus
) -> Iterator[TestClient]:
    with TestClient(create_app(settings, repo=repo, bus=bus)) as test_client:
        yield test_client
