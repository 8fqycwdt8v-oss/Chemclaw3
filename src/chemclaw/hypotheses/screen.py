"""The mechanical screen: the only stage allowed to remove a hypothesis.

**Why removal is mechanical and critique is not.** `D-2026-08-16` measured a model critic with the
power to change what shipped: of ten answers it "improved", eight improvements were deletions — a
protocol the user had asked for, a mechanistic explanation, an offered hazard screen — and the
measured benefit over a null control was zero. Its conclusion is the rule this module implements:
"Any loop scored on flag clearance learns exactly that move." So the critic in this pipeline
attaches `Objection`s that enter the tournament as evidence, and the only thing that can delete a
candidate outright is a rule a reader can check by hand.

Two rules, and no more:

- **A hypothesis with no usable refutation condition is not a hypothesis.** `Hypothesis` already
  makes `refuted_if` non-empty at the schema level, so what is left for the screen is the
  *degenerate* case — the field filled with "unknown", or restating the claim rather than naming an
  observation that would contradict it. This gate is shallow on purpose and the limit is stated
  rather than hidden: it catches the absent and the boilerplate case, and it cannot catch a fluent
  sentence that happens to be unfalsifiable. That one is the critic's to object to, where it costs
  a rating rather than a deletion.
- **Two hypotheses refuted by the same observation are one hypothesis.** The identity of a
  hypothesis is its refutation condition, not its prose, so this compares `refuted_if` and requires
  the *statements* to agree as well before merging. Prose similarity alone never merges anything:
  `D-162` refuses to mint findings out of phrasing because "a pattern-matched motive is
  indistinguishable downstream from testimony", and silently collapsing two real alternatives into
  one would be that failure with the sign flipped.

Every removal and every merge is reported in the outcome. A screen that dropped six of ten
candidates silently would be indistinguishable from a generator that produced four.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from chemclaw.hypotheses.models import Hypothesis, ScreenMerge, ScreenRejection

#: Fields shorter than this, once normalised, cannot name an observation. Measured against the
#: degenerate outputs this catches ("unknown", "n/a", "nothing", "tbd"), all of which are shorter.
_MIN_REFUTATION_CHARS = 12

#: Refutation conditions that are syntactically present and semantically empty. Matched on the
#: whole normalised field, never as a substring, so "no change in yield below 40 C" survives while
#: a bare "no" does not.
_VACUOUS = frozenset(
    {
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
    }
)

#: Jaccard overlap at which two normalised fields are treated as the same text. High, because the
#: cost of a false merge is a silently deleted alternative and the cost of a false split is one
#: extra row in a table.
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


def _tokens(text: str) -> frozenset[str]:
    return frozenset(_WORD.findall(text.lower()))


def similarity(left: str, right: str) -> float:
    """Jaccard overlap of the word sets of two strings, 0.0 to 1.0.

    Two empty strings are 1.0 (identically empty), which matters because `mechanism` is optional and
    two hypotheses both omitting it must not read as disagreeing about it.
    """
    a, b = _tokens(left), _tokens(right)
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
    if len(normalised) < _MIN_REFUTATION_CHARS:
        return False, f"refuted_if is too short to name an observation: {hypothesis.refuted_if!r}"
    if normalised == _normalise(hypothesis.statement):
        return False, "refuted_if restates the hypothesis rather than naming a contradicting result"
    return True, ""


def screen(
    hypotheses: Iterable[Hypothesis], *, merge_threshold: float = _MERGE_THRESHOLD
) -> ScreenResult:
    """Apply the two mechanical rules, preserving input order among survivors.

    Deterministic: the same input multiset gives the same result, and the *first* of a duplicate
    pair in input order is the one kept, so a caller who orders its generators reproducibly gets a
    reproducible field.
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
            # different claims that happen to be tested the same way — which is exactly the
            # interesting case and must not be collapsed.
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
