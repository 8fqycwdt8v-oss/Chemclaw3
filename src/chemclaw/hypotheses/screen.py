"""The mechanical screen: the only stage allowed to remove a hypothesis.

Removal is mechanical because a model critic with deletion power learns to delete; the critic here
only attaches `Objection`s. Two rules, each checkable by hand:

- A hypothesis with no usable refutation condition is removed — the degenerate case ("unknown",
  too short). This is shallow on purpose; a fluent but unfalsifiable sentence is the critic's to
  object to.
- Two hypotheses refuted by the same observation, with agreeing statements, are merged. Prose
  similarity alone never merges anything.

Every removal and merge is reported in the outcome.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from chemclaw.hypotheses.models import Hypothesis, ScreenMerge, ScreenRejection

#: A refutation condition needs at least this many words. Words, not characters: a character
#: floor rejects concise concrete conditions like `"yield > 90%"`.
_MIN_REFUTATION_WORDS = 2

#: Refutation conditions that are syntactically present and semantically empty, matched on the
#: whole normalised field (never as a substring). Normalised at definition because the comparison
#: is against normalised text (`"n/a"` becomes `"n a"`).
_VACUOUS_RAW = (
    "n/a",
    "na",
    "none",
    "nothing",
    "unknown",
    "unclear",
    "tbd",
    "not applicable",
    "not known",
    "cannot be refuted",
    "cannot be tested",
    "no",
)

#: Jaccard overlap at which two normalised fields are treated as the same text. High, because a
#: false merge silently deletes an alternative while a false split costs one table row.
_MERGE_THRESHOLD = 0.85

_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True, slots=True)
class ScreenResult:
    """What survived the screen, what did not, and why — all three, always."""

    kept: list[Hypothesis]
    rejected: list[ScreenRejection]
    merged: list[ScreenMerge]


def _normalise(text: str) -> str:
    return " ".join(_WORD.findall(text.lower()))


#: `_VACUOUS_RAW` put through the same normalisation the field is, so the two are comparable.
_VACUOUS = frozenset(_normalise(entry) for entry in _VACUOUS_RAW)


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _shingles(text: str) -> frozenset[tuple[str, ...]]:
    """Adjacent word pairs, so word order survives the comparison.

    A bag of words scores "A reacts faster than B" and its converse at 1.0. A one-word string
    degrades to that single word.
    """
    words = _tokens(text)
    if len(words) < 2:
        return frozenset((word,) for word in words)
    return frozenset(zip(words, words[1:], strict=False))


def similarity(left: str, right: str) -> float:
    """Order-sensitive Jaccard overlap of two strings, 0.0 to 1.0.

    Two empty strings are 1.0, so two hypotheses both omitting optional `mechanism` do not disagree.
    """
    a, b = _shingles(left), _shingles(right)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _refutation_is_usable(hypothesis: Hypothesis) -> tuple[bool, str]:
    """Whether `refuted_if` names something that could be observed. Returns (ok, why-not)."""
    normalised = _normalise(hypothesis.refuted_if)
    if not normalised:
        return False, "refuted_if is blank once punctuation is removed"
    if normalised in _VACUOUS:
        return False, f"refuted_if is a placeholder: {hypothesis.refuted_if.strip()!r}"
    if len(_tokens(normalised)) < _MIN_REFUTATION_WORDS:
        return False, f"refuted_if is too short to name an observation: {hypothesis.refuted_if!r}"
    if normalised == _normalise(hypothesis.statement):
        return False, "refuted_if restates the hypothesis rather than naming a contradicting result"
    return True, ""


def screen(
    hypotheses: Iterable[Hypothesis], *, merge_threshold: float = _MERGE_THRESHOLD
) -> ScreenResult:
    """Apply the two mechanical rules, preserving input order among survivors.

    Deterministic; the first of a duplicate pair in input order is kept.
    """
    candidates: Sequence[Hypothesis] = list(hypotheses)
    rejected: list[ScreenRejection] = []
    merged: list[ScreenMerge] = []
    kept: list[Hypothesis] = []

    for candidate in candidates:
        usable, why = _refutation_is_usable(candidate)
        if not usable:
            rejected.append(
                ScreenRejection(
                    hypothesis_id=candidate.id, rule="no-refutation-condition", detail=why
                )
            )
            continue

        duplicate_of: Hypothesis | None = None
        overlap = 0.0
        for survivor in kept:
            refutation_overlap = similarity(candidate.refuted_if, survivor.refuted_if)
            if refutation_overlap < merge_threshold:
                continue
            # The refutation conditions agree; the statements must agree too, or these are two
            # claims tested the same way and must not be collapsed.
            statement_overlap = similarity(candidate.statement, survivor.statement)
            if statement_overlap < merge_threshold:
                continue
            duplicate_of = survivor
            overlap = min(refutation_overlap, statement_overlap)
            break

        if duplicate_of is not None:
            merged.append(
                ScreenMerge(kept=duplicate_of.id, merged=candidate.id, similarity=round(overlap, 4))
            )
            continue
        kept.append(candidate)

    return ScreenResult(kept=kept, rejected=rejected, merged=merged)
