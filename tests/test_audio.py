import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from notetaker import audio
from notetaker.ai import OpenAITranscriber
from notetaker.audio import AudioError, plan_chunks

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg is not installed",
)


def test_plan_chunks_short_recording_is_one_chunk() -> None:
    assert plan_chunks(120, 600) == [(0.0, 120)]
    assert plan_chunks(0, 600) == []


def test_plan_chunks_splits_evenly_instead_of_leaving_a_tiny_tail() -> None:
    chunks = plan_chunks(1300, 600)
    assert len(chunks) == 3
    assert all(abs((end - start) - 1300 / 3) < 1e-6 for start, end in chunks)


def test_plan_chunks_exact_multiple() -> None:
    assert plan_chunks(1200, 600) == [(0.0, 600.0), (600.0, 1200)]


def test_plan_chunks_snaps_boundaries_to_nearby_pauses() -> None:
    chunks = plan_chunks(1200, 600, pauses=[100.0, 585.5, 640.0])
    assert chunks == [(0.0, 585.5), (585.5, 1200)]


def test_plan_chunks_ignores_far_pauses() -> None:
    assert plan_chunks(1200, 600, pauses=[500.0]) == [(0.0, 600.0), (600.0, 1200)]


async def make_tone(path: Path, seconds: int, gap_at: float | None = None) -> None:
    """Generate a sine tone, optionally with a second of silence in the middle."""
    source = f"sine=frequency=440:duration={seconds}"
    args = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", source]
    if gap_at is not None:
        args += ["-af", f"volume=enable='between(t,{gap_at},{gap_at + 1})':volume=0"]
    await audio._run(*args, str(path))


@needs_ffmpeg
async def test_normalize_probe_and_extract(tmp_path: Path) -> None:
    src, normalized = tmp_path / "tone.wav", tmp_path / "tone.mp3"
    await make_tone(src, seconds=1500)  # 25 minutes.
    await audio.normalize(src, normalized)

    assert normalized.stat().st_size < src.stat().st_size / 10
    duration = await audio.probe_duration(normalized)
    assert duration == pytest.approx(1500, abs=0.5)

    chunks = plan_chunks(duration, 600)
    assert len(chunks) == 3
    start, end = chunks[1]
    part = tmp_path / "part.mp3"
    await audio.extract(normalized, start, end, part)
    assert await audio.probe_duration(part) == pytest.approx(end - start, abs=0.5)


@needs_ffmpeg
async def test_find_pauses_locates_silence(tmp_path: Path) -> None:
    src = tmp_path / "gap.wav"
    await make_tone(src, seconds=10, gap_at=4.0)
    pauses = await audio.find_pauses(src)
    assert len(pauses) == 1
    assert pauses[0] == pytest.approx(4.5, abs=0.2)


@needs_ffmpeg
async def test_corrupt_audio_raises_audio_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"this is not audio")
    with pytest.raises(AudioError, match="Unsupported or corrupt audio"):
        await audio.normalize(bad, tmp_path / "out.mp3")


class FakeTranscriptions:
    """Stands in for `client.audio.transcriptions`, recording each request."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create(self, file, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:  # First chunk: two speakers.
            segments = [
                SimpleNamespace(speaker="A", start=0.0, end=5.0, text="Hello from A."),
                SimpleNamespace(speaker="B", start=6.0, end=14.0, text="And B replies."),
            ]
        else:
            segments = [SimpleNamespace(speaker="B", start=1.0, end=3.0, text="Later B.")]
        return SimpleNamespace(segments=segments, text="")


@needs_ffmpeg
async def test_openai_transcriber_chunks_and_keeps_speakers_consistent(tmp_path: Path) -> None:
    src = tmp_path / "meeting.wav"
    await make_tone(src, seconds=1500)
    transcriptions = FakeTranscriptions()
    client = SimpleNamespace(audio=SimpleNamespace(transcriptions=transcriptions))
    transcriber = OpenAITranscriber(
        client,  # type: ignore[arg-type]
        "gpt-4o-transcribe-diarize",
        chunk_seconds=600,
        max_seconds=3600,
        concurrency=2,
    )

    transcript = await transcriber.transcribe(src)

    assert len(transcriptions.calls) == 3
    first, *later = transcriptions.calls
    assert first["response_format"] == "diarized_json"
    assert "known_speaker_names" not in first
    for call in later:
        assert call["known_speaker_names"] == ["A", "B"]
        assert all(
            r.startswith("data:audio/mpeg;base64,") for r in call["known_speaker_references"]
        )

    assert [s.id for s in transcript.segments] == [0, 1, 2, 3]
    assert transcript.segments[2].start == pytest.approx(500 + 1.0, abs=1.0)
    assert transcript.duration_seconds == pytest.approx(1500, abs=0.5)


@needs_ffmpeg
async def test_openai_transcriber_enforces_max_duration(tmp_path: Path) -> None:
    src = tmp_path / "long.wav"
    await make_tone(src, seconds=120)
    transcriber = OpenAITranscriber(
        SimpleNamespace(),  # type: ignore[arg-type]
        "gpt-4o-transcribe-diarize",
        chunk_seconds=600,
        max_seconds=60,
        concurrency=1,
    )
    with pytest.raises(AudioError, match="limit is 1"):
        await transcriber.transcribe(src)
