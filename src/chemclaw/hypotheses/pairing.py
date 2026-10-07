"""Choosing which hypotheses to compare, round by round.

Swiss pairing rather than round robin to bound judged model calls: `n/2` per round over about
`log2(n)` rounds instead of `n(n-1)/2`. Pure functions with no clock or randomness, because this
runs inside a Temporal workflow and must replay identically.

Score ties break by the caller's `hypothesis_ids` order, never by id: ids are model-authored, and
an id-ordered tiebreak hands lexically early names an easier bracket. The caller permutes the field
by a hash of the question. A rematch happens only when no unplayed opponent remains, and the bye
goes to whoever has had the fewest.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence


def rounds_for(count: int) -> int:
    """How many Swiss rounds it takes to separate a field of `count` hypotheses.

    `ceil(log2(count))`; zero for a field of one or none.
    """
    if count < 2:
        return 0
    return int(math.ceil(math.log2(count)))


def comparisons_for(
    count: int, rounds: int | None = None, *, double_judge_first_round: bool = False
) -> int:
    """Total judged comparisons a full Swiss tournament over `count` hypotheses will run.

    Lets a caller price the run before starting it. `double_judge_first_round` is an argument rather
    than read from config so this stays pure; callers pass what they run.
    """
    if count < 2:
        return 0
    per_round = count // 2
    total = per_round * (rounds_for(count) if rounds is None else rounds)
    return total + (per_round if double_judge_first_round else 0)


def pair_round(
    hypothesis_ids: Sequence[str],
    *,
    scores: Mapping[str, float],
    played: frozenset[frozenset[str]] = frozenset(),
    byes: Mapping[str, int] | None = None,
) -> tuple[list[tuple[str, str]], str | None]:
    """Pair one Swiss round. Returns the pairs and whoever drew the bye, if anyone.

    `scores` is running score (1 per win, 0.5 per draw; absent means 0). `played` holds pairs
    already compared as frozensets. Pairs come back in a stable order. Raises `ValueError` on a
    duplicate id.
    """
    ids = list(hypothesis_ids)
    if len(set(ids)) != len(ids):
        raise ValueError("hypothesis_ids contains duplicates")
    if len(ids) < 2:
        return [], None

    bye_counts = dict(byes or {})
    # Input position, never the name — see the module docstring for the 143-Elo artefact that
    # sorting by id produced under a judge with no information at all.
    position = {name: index for index, name in enumerate(ids)}
    ordered = sorted(ids, key=lambda name: (-scores.get(name, 0.0), position[name]))

    bye: str | None = None
    if len(ordered) % 2 == 1:
        # The conventional rule is "lowest score", refined to "fewest byes so far" so a small field
        # does not hand the same candidate every bye and leave it unjudged.
        bye = min(
            ordered,
            key=lambda name: (bye_counts.get(name, 0), scores.get(name, 0.0), position[name]),
        )
        ordered = [name for name in ordered if name != bye]

    pairs: list[tuple[str, str]] = []
    unpaired = list(ordered)
    while unpaired:
        first = unpaired.pop(0)
        opponent_at = next(
            (
                index
                for index, other in enumerate(unpaired)
                if frozenset({first, other}) not in played
            ),
            None,
        )
        # Everyone left has already met `first`, so a rematch is the only move; the caller presents
        # it the other way round so it is a new reading rather than an identical prompt.
        pairs.append((first, unpaired.pop(0 if opponent_at is None else opponent_at)))

    return pairs, bye
