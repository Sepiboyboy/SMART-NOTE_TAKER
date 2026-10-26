"""The two processing stages, independent of Kafka so they can be tested directly."""

import logging

from .ai import Summarizer, Transcriber
from .audio import AudioError
from .grounding import verify
from .messaging import EventBus
from .models import NoteEvent, NoteStatus, TranscriptSegment
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
        drop_unverified: bool = False,
    ) -> None:
        self.repo = repo
        self.storage = storage
        self.bus = bus
        self.transcriber = transcriber
        self.summarizer = summarizer
        self.transcribed_topic = transcribed_topic
        self.drop_unverified = drop_unverified

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
        except AudioError as exc:
            await self._fail(note_id, NoteStatus.TRANSCRIBING, str(exc))
            return
        except Exception as exc:
            await self._fail(note_id, NoteStatus.TRANSCRIBING, f"Transcription failed: {exc}")
            return

        if not transcript.text:
            await self._fail(
                note_id, NoteStatus.TRANSCRIBING, "No speech was detected in the audio"
            )
            return

        await self.repo.transition(
            note_id,
            {NoteStatus.TRANSCRIBING},
            NoteStatus.TRANSCRIBED,
            transcript=transcript.text,
            segments=transcript.segments,
            duration_seconds=transcript.duration_seconds,
        )
        await self.bus.publish(self.transcribed_topic, NoteEvent(note_id=note_id))
        log.info(
            "Transcribed note %s (%d segments, %d speakers)",
            note_id,
            len(transcript.segments),
            len({s.speaker for s in transcript.segments}),
        )

    async def summarize(self, note_id: str) -> None:
        note = await self.repo.transition(
            note_id, {NoteStatus.TRANSCRIBED, NoteStatus.SUMMARIZING}, NoteStatus.SUMMARIZING
        )
        if note is None or note.transcript is None:
            log.info("Note %s is not awaiting summarization; skipping", note_id)
            return

        # Notes transcribed before v1.1 have text but no segments.
        segments = note.segments or [
            TranscriptSegment(id=0, speaker="A", start=0.0, end=0.0, text=note.transcript)
        ]
        try:
            draft = await self.summarizer.summarize(segments, note.speakers)
        except Exception as exc:
            await self._fail(note_id, NoteStatus.SUMMARIZING, f"Summarization failed: {exc}")
            return

        summary = verify(draft, segments, note.speakers, drop_unverified=self.drop_unverified)
        await self.repo.transition(
            note_id, {NoteStatus.SUMMARIZING}, NoteStatus.COMPLETED, summary=summary
        )
        log.info("Summarized note %s (grounding score %s)", note_id, summary.grounding_score)

    async def _fail(self, note_id: str, current: NoteStatus, error: str) -> None:
        log.error("Note %s failed: %s", note_id, error)
        await self.repo.transition(note_id, {current}, NoteStatus.FAILED, error=error)
