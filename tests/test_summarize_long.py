from pydantic import BaseModel

from notetaker.ai import (
    MERGE_INSTRUCTIONS,
    SECTION_INSTRUCTIONS,
    SUMMARY_INSTRUCTIONS,
    StructuredSummarizer,
    format_timestamp,
    format_transcript,
    merge_chunks,
    split_sections,
)
from notetaker.models import CitedItem, SectionNotes, SummaryDraft, TranscriptSegment


def seg(
    i: int, speaker: str = "A", start: float | None = None, words: int = 40
) -> TranscriptSegment:
    t = float(i * 10 if start is None else start)
    return TranscriptSegment(id=i, speaker=speaker, start=t, end=t + 9, text="word " * words)


def test_format_transcript_uses_ids_times_and_names() -> None:
    segments = [
        TranscriptSegment(id=0, speaker="A", start=5, end=6, text="Hi."),
        TranscriptSegment(id=1, speaker="B", start=3725, end=3726, text="Bye."),
    ]
    assert format_transcript(segments, {"A": "Sara"}) == (
        "[S0 00:05 Sara] Hi.\n[S1 1:02:05 Speaker B] Bye."
    )
    assert format_timestamp(59.9) == "00:59"


def test_split_sections_waits_for_a_speaker_change() -> None:
    # Each segment is ~54 tokens. The target is reached after segment 1, but the cut waits
    # for the change to speaker B (still within 1.25x the target).
    segments = [seg(0, "A"), seg(1, "A"), seg(2, "A"), seg(3, "B"), seg(4, "B")]
    sections = split_sections(segments, target_tokens=100)
    assert [[s.id for s in sec] for sec in sections] == [[0, 1, 2], [3, 4]]


def test_split_sections_forces_a_cut_in_a_long_monologue() -> None:
    # One speaker, no pauses: cut at 1.25x the target.
    segments = [seg(i, "A", start=i * 9) for i in range(10)]
    sections = split_sections(segments, target_tokens=100)
    assert len(sections) > 1
    assert all(len(sec) <= 3 for sec in sections)


def test_merge_chunks_offsets_times_and_renumbers() -> None:
    chunk1 = [TranscriptSegment(id=0, speaker="A", start=1, end=2, text=" hello ")]
    chunk2 = [
        TranscriptSegment(id=0, speaker="A", start=0.5, end=1.5, text="again"),
        TranscriptSegment(id=1, speaker="B", start=2, end=3, text="  "),  # Empty: dropped.
    ]
    merged = merge_chunks([chunk1, chunk2], [0.0, 600.0])
    assert [(s.id, s.start, s.end, s.text) for s in merged] == [
        (0, 1.0, 2.0, "hello"),
        (1, 600.5, 601.5, "again"),
    ]


class FakeSummarizer(StructuredSummarizer):
    def __init__(self, **kwargs: int) -> None:
        super().__init__(**kwargs)
        self.calls: list[tuple[str, str]] = []

    async def _parse[M: BaseModel](self, instructions: str, text: str, schema: type[M]) -> M:
        self.calls.append((instructions, text))
        if schema is SectionNotes:
            first_id = int(text.split()[0].removeprefix("[S"))
            point = CitedItem(text=f"point {first_id}", segment_ids=[first_id], quote="word word")
            return SectionNotes(topics=[], key_points=[point], decisions=[], action_items=[])
        return SummaryDraft(
            title="T", overview="O", topics=[], key_points=[], decisions=[], action_items=[]
        )


async def test_short_transcript_is_one_call() -> None:
    summarizer = FakeSummarizer(single_pass_tokens=10_000, section_tokens=100, concurrency=2)
    await summarizer.summarize([seg(i) for i in range(5)], {})
    assert [c[0] for c in summarizer.calls] == [SUMMARY_INSTRUCTIONS]


async def test_long_transcript_is_mapped_per_section_then_merged() -> None:
    summarizer = FakeSummarizer(single_pass_tokens=200, section_tokens=100, concurrency=2)
    segments = [seg(i, "A" if i % 2 else "B") for i in range(8)]
    await summarizer.summarize(segments, {})

    *maps, merge = summarizer.calls
    assert len(maps) == len(split_sections(segments, 100)) > 1
    assert all(instructions == SECTION_INSTRUCTIONS for instructions, _ in maps)
    assert merge[0] == MERGE_INSTRUCTIONS
    # Section notes, with their global segment ids, are passed into the merge step.
    assert '"segment_ids":[0]' in merge[1] and "## Section 2" in merge[1]
