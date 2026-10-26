"""Persistence for notes."""

from collections.abc import Iterable
from typing import Any, Protocol

from pydantic import BaseModel
from pymongo import DESCENDING, AsyncMongoClient, ReturnDocument

from .models import Note, NoteStatus, utcnow


class NoteRepository(Protocol):
    async def create(self, note: Note) -> None: ...

    async def get(self, note_id: str) -> Note | None: ...

    async def list_recent(self, limit: int) -> list[Note]: ...

    async def delete(self, note_id: str) -> Note | None: ...

    async def set_speakers(self, note_id: str, speakers: dict[str, str]) -> Note | None: ...

    async def transition(
        self,
        note_id: str,
        from_statuses: Iterable[NoteStatus],
        to_status: NoteStatus,
        **fields: Any,
    ) -> Note | None:
        """Atomically move a note to `to_status` if it is currently in one of `from_statuses`.

        Returns the updated note, or None if the note is missing or in another state.
        This compare-and-set is what makes the workers safe to re-run on redelivered messages.
        """
        ...


class MongoNoteRepository:
    def __init__(self, url: str, database: str) -> None:
        self._client: AsyncMongoClient = AsyncMongoClient(url, tz_aware=True)
        self._notes = self._client[database]["notes"]

    async def init(self) -> None:
        await self._client.admin.command("ping")
        await self._notes.create_index([("created_at", DESCENDING)])

    async def close(self) -> None:
        await self._client.close()

    async def create(self, note: Note) -> None:
        await self._notes.insert_one(_to_doc(note))

    async def get(self, note_id: str) -> Note | None:
        return _from_doc(await self._notes.find_one({"_id": note_id}))

    async def list_recent(self, limit: int) -> list[Note]:
        cursor = self._notes.find().sort("created_at", DESCENDING).limit(limit)
        return [_from_doc(doc) async for doc in cursor]

    async def delete(self, note_id: str) -> Note | None:
        return _from_doc(await self._notes.find_one_and_delete({"_id": note_id}))

    async def set_speakers(self, note_id: str, speakers: dict[str, str]) -> Note | None:
        doc = await self._notes.find_one_and_update(
            {"_id": note_id},
            {"$set": {"speakers": speakers, "updated_at": utcnow()}},
            return_document=ReturnDocument.AFTER,
        )
        return _from_doc(doc)

    async def transition(
        self,
        note_id: str,
        from_statuses: Iterable[NoteStatus],
        to_status: NoteStatus,
        **fields: Any,
    ) -> Note | None:
        update = {**_dump(fields), "status": to_status.value, "updated_at": utcnow()}
        doc = await self._notes.find_one_and_update(
            {"_id": note_id, "status": {"$in": [s.value for s in from_statuses]}},
            {"$set": update},
            return_document=ReturnDocument.AFTER,
        )
        return _from_doc(doc)


def _dump(fields: dict[str, Any]) -> dict[str, Any]:
    return {k: _plain(v) for k, v in fields.items()}


def _plain(value: Any) -> Any:
    """Convert models (also inside lists) into the plain dicts MongoDB stores."""
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _to_doc(note: Note) -> dict[str, Any]:
    doc = note.model_dump(mode="python")
    doc["_id"] = doc.pop("id")
    return doc


def _from_doc(doc: dict[str, Any] | None) -> Note | None:
    if doc is None:
        return None
    doc["id"] = doc.pop("_id")
    return Note.model_validate(doc)
