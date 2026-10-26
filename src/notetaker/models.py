"""Domain models shared by the API and the workers."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


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


# --- Transcripts ---------------------------------------------------------------------------


class TranscriptSegment(BaseModel):
    id: int
    speaker: str  # Diarization label, e.g. "A". Display names live in `Note.speakers`.
    start: float  # Seconds from the start of the recording.
    end: float
    text: str


class Transcript(BaseModel):
    segments: list[TranscriptSegment]
    duration_seconds: float | None = None

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segments).strip()


# --- Summaries: what the model writes --------------------------------------------------------
# Every claim must cite transcript segments and include a verbatim quote, so it can be checked.


class CitedItem(BaseModel):
    text: str = Field(description="The point, written concisely.")
    segment_ids: list[int] = Field(description="IDs of the transcript segments supporting it.")
    quote: str = Field(description="A short verbatim excerpt from those segments.")


class CitedAction(BaseModel):
    task: str = Field(description="What needs to be done, as an imperative.")
    owner: str | None = Field(
        description="Who committed to or was assigned it. Null unless explicit in the transcript."
    )
    due: str | None = Field(description="Deadline as stated in the transcript, or null.")
    segment_ids: list[int] = Field(description="IDs of the transcript segments supporting it.")
    quote: str = Field(description="A short verbatim excerpt from those segments.")


class DraftTopic(BaseModel):
    title: str
    summary: str = Field(description="One or two sentences.")
    start_segment: int = Field(description="ID of the segment where this topic begins.")


class SectionNotes(BaseModel):
    """Notes extracted from one section of a long transcript (the "map" step)."""

    topics: list[DraftTopic]
    key_points: list[CitedItem]
    decisions: list[CitedItem]
    action_items: list[CitedAction]


class SummaryDraft(SectionNotes):
    """The model's complete answer, before verification."""

    title: str = Field(description="A short, descriptive title for the note.")
    overview: str = Field(description="Two to four sentences capturing the essence.")


# --- Summaries: what we store, after verification --------------------------------------------


class GroundedItem(CitedItem):
    verified: bool = False  # True when the quote was found in the cited transcript segments.

    @model_validator(mode="before")
    @classmethod
    def _from_legacy_string(cls, data: Any) -> Any:
        # Notes created before v1.1 stored plain strings.
        return {"text": data, "segment_ids": [], "quote": ""} if isinstance(data, str) else data


class ActionItem(CitedAction):
    verified: bool = False
    owner_verified: bool = False  # True when the owner is the speaker or is named in the quote.

    @model_validator(mode="before")
    @classmethod
    def _from_legacy_string(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"task": data, "owner": None, "due": None, "segment_ids": [], "quote": ""}
        return data


class Topic(BaseModel):
    title: str
    summary: str
    start: float  # Seconds from the start of the recording.


class Summary(BaseModel):
    title: str
    overview: str
    topics: list[Topic] = []
    key_points: list[GroundedItem] = []
    decisions: list[GroundedItem] = []
    action_items: list[ActionItem] = []
    # Share of items whose quote was found in the transcript; None when there are no items.
    grounding_score: float | None = None


# --- Notes --------------------------------------------------------------------------------


class Note(BaseModel):
    id: str
    filename: str
    content_type: str
    size_bytes: int
    audio_file: str  # Name of the stored file inside the storage directory.
    status: NoteStatus = NoteStatus.UPLOADED
    duration_seconds: float | None = None
    transcript: str | None = None
    segments: list[TranscriptSegment] = []
    speakers: dict[str, str] = {}  # Diarization label -> display name, e.g. {"A": "Sara"}.
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
    duration_seconds: float | None
    transcript: str | None
    segments: list[TranscriptSegment]
    speakers: dict[str, str]
    summary: Summary | None
    error: str | None
    created_at: datetime
    updated_at: datetime


class NoteEvent(BaseModel):
    """Payload published to Kafka. Workers re-read the note from MongoDB, so it stays tiny."""

    note_id: str
