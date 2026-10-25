"""Domain models shared by the API and the workers."""

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


class NoteStatus(StrEnum):
    """Lifecycle of a note. Each worker moves a note forward exactly one stage."""

    UPLOADED = "uploaded"
    TRANSCRIBING = "transcribing"
    TRANSCRIBED = "transcribed"
    SUMMARIZING = "summarizing"
    COMPLETED = "completed"
    FAILED = "failed"


class Summary(BaseModel):
    title: str = Field(description="A short, descriptive title for the note.")
    overview: str = Field(description="Two to four sentences capturing the essence.")
    key_points: list[str] = Field(description="The most important points, one per item.")
    action_items: list[str] = Field(description="Concrete follow-ups. Empty if there are none.")


class Note(BaseModel):
    id: str
    filename: str
    content_type: str
    size_bytes: int
    audio_file: str  # Name of the stored file inside the storage directory.
    status: NoteStatus = NoteStatus.UPLOADED
    transcript: str | None = None
    summary: Summary | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class NoteOut(BaseModel):
    """Public representation of a note (internal storage details omitted)."""

    id: str
    filename: str
    size_bytes: int
    status: NoteStatus
    transcript: str | None
    summary: Summary | None
    error: str | None
    created_at: datetime
    updated_at: datetime


class NoteEvent(BaseModel):
    """Payload published to Kafka. Workers re-read the note from MongoDB, so it stays tiny."""

    note_id: str
