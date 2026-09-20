"""Choosing which hypotheses to compare, round by round.

**Swiss, not round robin, and the reason is the model-call budget.** A round robin over `n`
hypotheses costs `n(n-1)/2` comparisons — 45 at ten candidates, 190 at twenty — and every one of
those is a judged model call against gathered evidence. Swiss pairing spends `n/2` per round and
separates a field of `n` in about `log2(n)` rounds, so ten hypotheses cost 20 comparisons instead of
45 and twenty cost 50 instead of 190. The information is concentrated where it decides the ordering:
after the first round, candidates are compared against others on the same score, which is where the
ranking is still genuinely uncertain.

**Pure functions over explicit state, not a stateful pairer.** This runs inside a Temporal workflow,
where a replay must reproduce the same command sequence exactly; a pairing that depended on hidden
state or on iteration order of a set would be a nondeterminism bug of the kind
`D-2026-09-14-the-seam-shipped-a-replay-break-and-the-adr-said-nothing-changes` records. Every
ordering here is total — score, then comparison count, then id — so the same history always produces
the same pairs, and nothing consults a clock or a random source.

A rematch is permitted only when no unplayed opponent remains, and the bye goes to whoever has had
the fewest byes so far. Both are the conventional Swiss rules, and both matter more here than in
chess: the field is small enough that the "no unplayed opponent" case is reached routinely.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence


def rounds_for(count: int) -> int:
    """How many Swiss rounds it takes to separate a field of `count` hypotheses.

    `ceil(log2(count))`, the standard result: each round halves the number of candidates sharing a
    score. Zero for a field of one or none, because there is nothing to compare.
    """
    if count < 2:
        return 0
    return int(math.ceil(math.log2(count)))


def comparisons_for(count: int, rounds: int | None = None) -> int:
    """Total comparisons a full Swiss tournament over `count` hypotheses will run.

    Exposed so a caller can price the run *before* starting it — the whole point of choosing Swiss
    was the budget, and a caller that cannot see the number cannot act on it.
    """
    if count < 2:
        return 0
    return (count // 2) * (rounds_for(count) if rounds is None else rounds)


def pair_round(
    hypothesis_ids: Sequence[str],
    *,
    scores: Mapping[str, float],
    played: frozenset[frozenset[str]] = frozenset(),
    byes: Mapping[str, int] | None = None,
) -> tuple[list[tuple[str, str]], str | None]:
    """Pair one Swiss round. Returns the pairs and whoever drew the bye, if anyone.

    `scores` is running tournament score (1 per win, 0.5 per draw); a hypothesis absent from it
    scores 0. `played` holds the pairs already compared, as two-element frozensets, so a rematch is
    avoided while an unplayed opponent exists. Pairs are returned in a stable order.

    Raises `ValueError` on a duplicate id, because a hypothesis paired against itself later is a
    confusing failure a long way from its cause.
    """
    ids = list(hypothesis_ids)
    if len(set(ids)) != len(ids):
        raise ValueError("hypothesis_ids contains duplicates")
    if len(ids) < 2:
        return [], None

    bye_counts = dict(byes or {})
    ordered = sorted(ids, key=lambda name: (-scores.get(name, 0.0), name))

    bye: str | None = None
    if len(ordered) % 2 == 1:
        # The conventional rule is "lowest score", refined to "fewest byes so far" so a small field
        # does not hand the same candidate every bye and leave it unjudged.
        bye = min(ordered, key=lambda name: (bye_counts.get(name, 0), scores.get(name, 0.0), name))
        ordered = [name for name in ordered if name != bye]

    pairs: list[tuple[str, str]] = []
    unpaired = list(ordered)
    while unpaired:
        first = unpaired.pop(0)
        opponent_at = next(
            (
                position
                for position, other in enumerate(unpaired)
                if frozenset({first, other}) not in played
            ),
            None,
        )
        # Everyone left has already met `first`; a rematch adds real information here, because a
        # second judgement of the same pair is an independent draw from the judge.
        position = 0 if opponent_at is None else opponent_at
        pairs.append((first, unpaired.pop(position)))

    return pairs, bye
