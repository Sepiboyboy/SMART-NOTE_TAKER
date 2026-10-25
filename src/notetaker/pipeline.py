"""The two processing stages, independent of Kafka so they can be tested directly."""

import logging

from .ai import Summarizer, Transcriber
from .messaging import EventBus
from .models import NoteEvent, NoteStatus
from .repository import NoteRepository
from .storage import AudioStorage

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(
        self,
        repo: NoteRepository,
        storage: AudioStorage,
        bus: EventBus,
        transcriber: Transcriber,
        summarizer: Summarizer,
        transcribed_topic: str,
    ) -> None:
        self.repo = repo
        self.storage = storage
        self.bus = bus
        self.transcriber = transcriber
        self.summarizer = summarizer
        self.transcribed_topic = transcribed_topic

    async def transcribe(self, note_id: str) -> None:
        # Claiming from TRANSCRIBING as well lets a redelivered message resume work that a
        # crashed worker started but never finished (its offset was never committed).
        note = await self.repo.transition(
            note_id, {NoteStatus.UPLOADED, NoteStatus.TRANSCRIBING}, NoteStatus.TRANSCRIBING
        )
        if note is None:
            log.info("Note %s is not awaiting transcription; skipping", note_id)
            return

        try:
            transcript = await self.transcriber.transcribe(self.storage.path(note.audio_file))
        except Exception as exc:
            await self._fail(note_id, NoteStatus.TRANSCRIBING, f"Transcription failed: {exc}")
            return

        if not transcript:
            await self._fail(
                note_id, NoteStatus.TRANSCRIBING, "No speech was detected in the audio"
            )
            return

        await self.repo.transition(
            note_id, {NoteStatus.TRANSCRIBING}, NoteStatus.TRANSCRIBED, transcript=transcript
        )
        await self.bus.publish(self.transcribed_topic, NoteEvent(note_id=note_id))
        log.info("Transcribed note %s (%d chars)", note_id, len(transcript))

    async def summarize(self, note_id: str) -> None:
        note = await self.repo.transition(
            note_id, {NoteStatus.TRANSCRIBED, NoteStatus.SUMMARIZING}, NoteStatus.SUMMARIZING
        )
        if note is None or note.transcript is None:
            log.info("Note %s is not awaiting summarization; skipping", note_id)
            return

        try:
            summary = await self.summarizer.summarize(note.transcript)
        except Exception as exc:
            await self._fail(note_id, NoteStatus.SUMMARIZING, f"Summarization failed: {exc}")
            return

        await self.repo.transition(
            note_id, {NoteStatus.SUMMARIZING}, NoteStatus.COMPLETED, summary=summary
        )
        log.info("Summarized note %s", note_id)

    async def _fail(self, note_id: str, current: NoteStatus, error: str) -> None:
        log.error("Note %s failed: %s", note_id, error)
        await self.repo.transition(note_id, {current}, NoteStatus.FAILED, error=error)
