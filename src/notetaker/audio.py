"""Audio preprocessing with ffmpeg: normalize, measure, find pauses, and split into chunks."""

import asyncio
import itertools
import math
import re
from pathlib import Path

# How far a chunk boundary may move to land in a pause instead of mid-word.
SNAP_WINDOW_SECONDS = 30.0

_SILENCE = re.compile(r"silence_(start|end): (-?[\d.]+)")


class AudioError(Exception):
    """The file could not be decoded or processed."""


async def _run(*args: str) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError as exc:
        raise AudioError(f"{args[0]} is not installed") from exc
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        detail = stderr.decode(errors="replace").strip().splitlines()[-1:] or ["unknown error"]
        raise AudioError(f"Unsupported or corrupt audio ({detail[0]})")
    # ffmpeg writes filter output (e.g. silencedetect) to stderr.
    return stdout.decode(errors="replace") + stderr.decode(errors="replace")


async def probe_duration(path: Path) -> float:
    output = await _run(
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)
    )
    try:
        return float(output.strip().splitlines()[0])
    except (ValueError, IndexError) as exc:
        raise AudioError("Unsupported or corrupt audio (no duration)") from exc


async def normalize(src: Path, dst: Path) -> None:
    """Convert to mono 16 kHz 32 kbps mp3: about 14 MB per hour, with no loss for speech."""
    await _run(
        "ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(src),
        "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", str(dst),
    )  # fmt: skip


async def find_pauses(path: Path, noise_db: int = -35, min_seconds: float = 0.4) -> list[float]:
    """Return the midpoints of silent stretches, which make good places to cut."""
    output = await _run(
        "ffmpeg", "-nostdin", "-v", "info", "-i", str(path),
        "-af", f"silencedetect=n={noise_db}dB:d={min_seconds}", "-f", "null", "-",
    )  # fmt: skip
    pauses, start = [], None
    for kind, value in _SILENCE.findall(output):
        if kind == "start":
            start = max(float(value), 0.0)
        elif start is not None:
            pauses.append((start + float(value)) / 2)
            start = None
    return pauses


async def extract(src: Path, start: float, end: float, dst: Path) -> None:
    await _run(
        "ffmpeg", "-nostdin", "-y", "-v", "error",
        "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(src), "-c", "copy", str(dst),
    )  # fmt: skip


def plan_chunks(
    duration: float, max_seconds: float, pauses: list[float] | None = None
) -> list[tuple[float, float]]:
    """Split `duration` into near-equal chunks of at most ~`max_seconds`.

    Each boundary moves to the nearest pause within SNAP_WINDOW_SECONDS, so words are not cut.
    Equal sizes avoid a tiny final chunk, which gives the diarization model too little context.
    """
    if duration <= 0:
        return []
    count = math.ceil(duration / max_seconds)
    size = duration / count
    bounds = [0.0]
    for i in range(1, count):
        target = i * size
        nearby = [p for p in pauses or [] if abs(p - target) <= SNAP_WINDOW_SECONDS]
        cut = min(nearby, key=lambda p: abs(p - target)) if nearby else target
        bounds.append(max(cut, bounds[-1] + 1.0))
    bounds.append(duration)
    return list(itertools.pairwise(bounds))
