"""Deterministic verification of model-written summaries against the transcript.

Every summary item cites transcript segments and quotes them. Here we check that the quote
really appears there, so invented points and commitments get flagged instead of trusted.
"""

import itertools
import re
from collections.abc import Iterable, Sequence
from difflib import SequenceMatcher

from .models import (
    ActionItem,
    CitedAction,
    CitedItem,
    GroundedItem,
    Summary,
    SummaryDraft,
    Topic,
    TranscriptSegment,
)

FUZZY_THRESHOLD = 0.9  # Tolerates small transcription/punctuation differences in quotes.

_NON_WORD = re.compile(r"[^\w]+")


def normalize(text: str) -> str:
    return _NON_WORD.sub(" ", text.lower()).strip()


def contains_quote(quote: str, text: str) -> bool:
    """True if `quote` appears in `text`, ignoring case and punctuation, allowing tiny typos."""
    q, t = normalize(quote), normalize(text)
    if not q:
        return False
    if q in t:
        return True
    q_words, t_words = q.split(), t.split()
    n = len(q_words)
    if n == 0 or len(t_words) < n:
        return False
    matcher = SequenceMatcher(autojunk=False)
    matcher.set_seq2(q)  # SequenceMatcher caches details about seq2, so keep the quote there.
    for size in {max(n - 1, 1), n, n + 1}:
        for i in range(len(t_words) - size + 1):
            matcher.set_seq1(" ".join(t_words[i : i + size]))
            # Cheap upper bounds first; the full ratio is only computed for close windows.
            if (
                matcher.real_quick_ratio() >= FUZZY_THRESHOLD
                and matcher.quick_ratio() >= FUZZY_THRESHOLD
                and matcher.ratio() >= FUZZY_THRESHOLD
            ):
                return True
    return False


def locate(
    quote: str, cited_ids: Sequence[int], segments: Sequence[TranscriptSegment]
) -> list[int] | None:
    """Return the IDs of the segments containing `quote`, or None if it is nowhere.

    The cited segments are checked first. If the model cited the wrong ones, we search the whole
    transcript (single segments and adjacent pairs, since quotes can span a boundary).
    """
    by_id = {s.id: s for s in segments}
    cited = [by_id[i] for i in sorted(set(cited_ids)) if i in by_id]
    if cited and contains_quote(quote, " ".join(s.text for s in cited)):
        return [s.id for s in cited]
    for seg in segments:
        if contains_quote(quote, seg.text):
            return [seg.id]
    for a, b in itertools.pairwise(segments):
        if contains_quote(quote, f"{a.text} {b.text}"):
            return [a.id, b.id]
    return None


def owner_is_supported(
    owner: str,
    segment_ids: Iterable[int],
    segments: Sequence[TranscriptSegment],
    speakers: dict[str, str],
) -> bool:
    """An owner is supported if they are the speaker of a cited segment or named in it."""
    wanted = normalize(owner)
    if not wanted:
        return False
    candidates = {wanted, wanted.split()[0]}
    by_id = {s.id: s for s in segments}
    for seg in (by_id[i] for i in segment_ids if i in by_id):
        # The model sees speakers as their display name, or "Speaker A" when unnamed.
        names = {
            normalize(seg.speaker),
            normalize(f"Speaker {seg.speaker}"),
            normalize(speakers.get(seg.speaker, "")),
        }
        if candidates & names:
            return True
        text = f" {normalize(seg.text)} "
        if any(f" {c} " in text for c in candidates):
            return True
    return False


def _verify_item(item: CitedItem, segments: Sequence[TranscriptSegment]) -> GroundedItem:
    found = locate(item.quote, item.segment_ids, segments)
    return GroundedItem(
        text=item.text,
        quote=item.quote,
        segment_ids=found if found is not None else item.segment_ids,
        verified=found is not None,
    )


def _verify_action(
    item: CitedAction, segments: Sequence[TranscriptSegment], speakers: dict[str, str]
) -> ActionItem:
    found = locate(item.quote, item.segment_ids, segments)
    ids = found if found is not None else item.segment_ids
    return ActionItem(
        task=item.task,
        owner=item.owner,
        due=item.due,
        quote=item.quote,
        segment_ids=ids,
        verified=found is not None,
        owner_verified=bool(
            found is not None
            and item.owner
            and owner_is_supported(item.owner, ids, segments, speakers)
        ),
    )


def verify(
    draft: SummaryDraft,
    segments: Sequence[TranscriptSegment],
    speakers: dict[str, str] | None = None,
    drop_unverified: bool = False,
) -> Summary:
    speakers = speakers or {}
    key_points = [_verify_item(i, segments) for i in draft.key_points]
    decisions = [_verify_item(i, segments) for i in draft.decisions]
    actions = [_verify_action(i, segments, speakers) for i in draft.action_items]

    checked = [*key_points, *decisions, *actions]
    score = sum(i.verified for i in checked) / len(checked) if checked else None
    if drop_unverified:
        key_points = [i for i in key_points if i.verified]
        decisions = [i for i in decisions if i.verified]
        actions = [i for i in actions if i.verified]

    starts = {s.id: s.start for s in segments}
    topics = [
        Topic(title=t.title, summary=t.summary, start=starts.get(t.start_segment, 0.0))
        for t in draft.topics
    ]
    return Summary(
        title=draft.title,
        overview=draft.overview,
        topics=sorted(topics, key=lambda t: t.start),
        key_points=key_points,
        decisions=decisions,
        action_items=actions,
        grounding_score=score,
    )
