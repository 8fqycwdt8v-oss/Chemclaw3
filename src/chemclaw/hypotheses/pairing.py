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
`D-2026-09-14-the-seam-shipped-a-replay-break-and-the-adr-said-nothing-changes` records. Nothing
here consults a clock or a random source, and the same inputs always produce the same pairs.

**The score tie is broken by input position, and it used to be broken by id — which was a defect
big enough to invent a ranking.** Every pairing decision in round one is a tie, and most later ones
are, so whatever breaks that tie decides the whole bracket. Sorting by name gave lexically-early ids
a systematically easier path, and because a Bradley-Terry fit is opponent-strength aware, that
converted identical records into different ratings. Measured through the shipped path with a
*coin-flip* judge — zero information, so every hypothesis is genuinely equal — a field of ten came
out with a **143-point monotone spread ordered by id**, wider than the standard errors printed
beside it. Production ids are `h-<stable_hash(statement)>`, so rephrasing a hypothesis moved it up
the chemist's table.

So the tiebreak is the caller's `hypothesis_ids` order, not the name. That keeps this function
deterministic — the property replay needs — and moves the choice of ordering to the one place that
can make it fair: `durable/hypothesis_tournament.py` permutes the field by a hash of the question,
so no id is favoured across tournaments and any single tournament is still reproducible.

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


def comparisons_for(
    count: int, rounds: int | None = None, *, double_judge_first_round: bool = False
) -> int:
    """Total judged comparisons a full Swiss tournament over `count` hypotheses will run.

    Exposed so a caller can price the run *before* starting it — the whole point of choosing Swiss
    was the budget, and a caller that cannot see the number cannot act on it.

    `double_judge_first_round` is **not** defaulted to the shipped setting, because this is a pure
    function and reading config here would make it lie in tests. It defaults to the cheaper arm and
    callers pass what they run. Omitting it entirely is what made this function under-price every
    default run by `count // 2` — 20 against an actual 25 at a field of ten, on the one number whose
    whole job is to be right before the money is spent.
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
        # Everyone left has already met `first`, so a rematch is the only move. It is worth
        # something only because the caller presents a rematch the other way round — see
        # `durable/hypothesis_tournament.py::_judge`, which mixes the round into the presentation
        # order so a repeat is a genuinely new reading rather than a byte-identical prompt.
        pairs.append((first, unpaired.pop(0 if opponent_at is None else opponent_at)))

    return pairs, bye
