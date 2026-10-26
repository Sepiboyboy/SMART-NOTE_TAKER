"""HTTP endpoints."""

from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import (
    APIRouter,
    Body,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)

from ..config import Settings
from ..messaging import EventBus
from ..models import Note, NoteEvent, NoteOut
from ..repository import NoteRepository
from ..storage import AudioStorage, FileTooLargeError

# Common audio/video containers. Everything is re-encoded with ffmpeg before transcription.
ALLOWED_EXTENSIONS = {
    ".aac",
    ".flac",
    ".m4a",
    ".mp3",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpga",
    ".oga",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
}

router = APIRouter()


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _repo(request: Request) -> NoteRepository:
    return request.app.state.repo


def _bus(request: Request) -> EventBus:
    return request.app.state.bus


def _storage(request: Request) -> AudioStorage:
    return request.app.state.storage


@router.get("/healthz", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post(
    "/api/notes",
    response_model=NoteOut,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["notes"],
    summary="Upload an audio file for transcription and summarization",
)
async def create_note(
    request: Request,
    file: Annotated[UploadFile, File(description="Audio recording, up to MAX_UPLOAD_MB.")],
) -> Note:
    settings, storage = _settings(request), _storage(request)
    filename = Path(file.filename or "recording").name
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"Unsupported file type '{extension or 'none'}'. "
            f"Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )

    note_id = uuid4().hex
    audio_file = f"{note_id}{extension}"
    try:
        size = await storage.save(audio_file, file, settings.max_upload_bytes)
    except FileTooLargeError:
        raise HTTPException(
            413,
            f"File exceeds the {settings.max_upload_mb} MB limit",
        ) from None
    if size == 0:
        storage.delete(audio_file)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Uploaded file is empty")

    note = Note(
        id=note_id,
        filename=filename,
        content_type=file.content_type or "application/octet-stream",
        size_bytes=size,
        audio_file=audio_file,
    )
    await _repo(request).create(note)
    await _bus(request).publish(settings.topic_uploaded, NoteEvent(note_id=note_id))
    return note


@router.get("/api/notes", response_model=list[NoteOut], tags=["notes"], summary="List recent notes")
async def list_notes(
    request: Request, limit: Annotated[int, Query(ge=1, le=100)] = 50
) -> list[Note]:
    return await _repo(request).list_recent(limit)


@router.get("/api/notes/{note_id}", response_model=NoteOut, tags=["notes"], summary="Get a note")
async def get_note(request: Request, note_id: str) -> Note:
    note = await _repo(request).get(note_id)
    if note is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Note not found")
    return note


@router.patch(
    "/api/notes/{note_id}/speakers",
    response_model=NoteOut,
    tags=["notes"],
    summary="Rename speakers",
    description='Map diarization labels to names, e.g. `{"A": "Sara"}`. An empty name resets it.',
)
async def rename_speakers(
    request: Request,
    note_id: str,
    names: Annotated[dict[str, str], Body(examples=[{"A": "Sara", "B": "Omid"}])],
) -> Note:
    repo = _repo(request)
    note = await repo.get(note_id)
    if note is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Note not found")
    labels = {s.speaker for s in note.segments}
    unknown = sorted(set(names) - labels)
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown speakers: {', '.join(unknown)}"
        )
    if any(len(name.strip()) > 60 for name in names.values()):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Names must be at most 60 characters"
        )

    speakers = dict(note.speakers)
    for label, name in names.items():
        if name.strip():
            speakers[label] = name.strip()
        else:
            speakers.pop(label, None)
    updated = await repo.set_speakers(note_id, speakers)
    if updated is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Note not found")
    return updated


@router.delete(
    "/api/notes/{note_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["notes"],
    summary="Delete a note and its audio",
)
async def delete_note(request: Request, note_id: str) -> Response:
    note = await _repo(request).delete(note_id)
    if note is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Note not found")
    _storage(request).delete(note.audio_file)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
