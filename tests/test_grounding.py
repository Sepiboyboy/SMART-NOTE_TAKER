from notetaker.grounding import contains_quote, locate, owner_is_supported, verify
from notetaker.models import (
    CitedAction,
    CitedItem,
    DraftTopic,
    GroundedItem,
    Summary,
    SummaryDraft,
    TranscriptSegment,
)

SEGMENTS = [
    TranscriptSegment(id=0, speaker="A", start=0, end=4, text="Let's start with vendors."),
    TranscriptSegment(
        id=1, speaker="B", start=5, end=12, text="Priya mentioned the vendor onboarding backlog."
    ),
    TranscriptSegment(
        id=2, speaker="A", start=13, end=20, text="I'll draft the contract by Thursday."
    ),
    TranscriptSegment(id=3, speaker="B", start=21, end=25, text="Great, we agreed on Acme"),
    TranscriptSegment(id=4, speaker="B", start=25, end=28, text="as our new supplier."),
]


def draft(**overrides) -> SummaryDraft:
    base = dict(
        title="Vendors", overview="", topics=[], key_points=[], decisions=[], action_items=[]
    )
    return SummaryDraft(**(base | overrides))


def test_contains_quote_ignores_case_and_punctuation() -> None:
    assert contains_quote("i'll DRAFT the contract", "I'll draft the contract by Thursday.")


def test_contains_quote_tolerates_small_differences() -> None:
    assert contains_quote(
        "Ill draft the contracts by Thursday", "I'll draft the contract by Thursday"
    )


def test_contains_quote_rejects_paraphrase_and_empty() -> None:
    assert not contains_quote(
        "I will write up the agreement", "I'll draft the contract by Thursday"
    )
    assert not contains_quote("", "anything")


def test_locate_uses_cited_segments_then_corrects_wrong_ids() -> None:
    assert locate("vendor onboarding backlog", [1], SEGMENTS) == [1]
    assert locate("vendor onboarding backlog", [0], SEGMENTS) == [1]


def test_locate_finds_quotes_spanning_two_segments() -> None:
    assert locate("we agreed on Acme as our new supplier", [], SEGMENTS) == [3, 4]


def test_locate_returns_none_for_invented_quotes() -> None:
    assert locate("Priya will begin vendor onboarding", [1], SEGMENTS) is None


def test_owner_supported_by_speaker_name_or_mention() -> None:
    assert owner_is_supported("Sara", [2], SEGMENTS, {"A": "Sara"})
    assert owner_is_supported("Speaker A", [2], SEGMENTS, {})
    assert owner_is_supported("Priya", [1], SEGMENTS, {})  # Named in the cited text.
    assert not owner_is_supported("Omid", [2], SEGMENTS, {"A": "Sara"})


def test_verify_flags_invented_commitment() -> None:
    """The classic failure: assigning a task to someone who never agreed to it."""
    summary = verify(
        draft(
            action_items=[
                CitedAction(
                    task="Begin vendor onboarding",
                    owner="Priya",
                    due=None,
                    segment_ids=[1],
                    quote="Priya will begin vendor onboarding",
                ),
                CitedAction(
                    task="Draft the contract",
                    owner="Sara",
                    due="Thursday",
                    segment_ids=[2],
                    quote="I'll draft the contract by Thursday",
                ),
            ]
        ),
        SEGMENTS,
        {"A": "Sara"},
    )
    invented, real = summary.action_items
    assert not invented.verified and not invented.owner_verified
    assert real.verified and real.owner_verified
    assert summary.grounding_score == 0.5


def test_verify_can_drop_unverified_items_but_score_counts_them() -> None:
    summary = verify(
        draft(
            key_points=[
                CitedItem(text="Backlog", segment_ids=[1], quote="vendor onboarding backlog"),
                CitedItem(text="Made up", segment_ids=[1], quote="revenue doubled last year"),
            ]
        ),
        SEGMENTS,
        drop_unverified=True,
    )
    assert [p.text for p in summary.key_points] == ["Backlog"]
    assert summary.grounding_score == 0.5


def test_verify_maps_topic_start_segment_to_time() -> None:
    summary = verify(
        draft(
            topics=[
                DraftTopic(title="Contract", summary="", start_segment=2),
                DraftTopic(title="Intro", summary="", start_segment=0),
            ]
        ),
        SEGMENTS,
    )
    assert [(t.title, t.start) for t in summary.topics] == [("Intro", 0), ("Contract", 13)]
    assert summary.grounding_score is None


def test_legacy_string_summaries_still_load() -> None:
    summary = Summary.model_validate(
        {"title": "Old", "overview": "o", "key_points": ["a point"], "action_items": ["a task"]}
    )
    assert summary.key_points == [GroundedItem(text="a point", segment_ids=[], quote="")]
    assert summary.action_items[0].task == "a task"
    assert not summary.action_items[0].verified
