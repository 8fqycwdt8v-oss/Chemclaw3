"""Does the tournament's ranking carry information, and how much judging does that take?

**The question this answers, and the one it does not.** It measures the *instrument*: given a judge
of a stated accuracy, does Swiss pairing plus a Bradley-Terry fit recover a known ordering, and by
how much does it beat the null of not ranking at all? That is a property of
`hypotheses/pairing.py` and `hypotheses/rating.py`, it is simulable, it needs no model, and it runs
in CI. What it cannot tell anybody is whether a *language model* judging real chemistry is an
accurate judge — that is `backtest_shape` below, which needs a credential and has never run.

Both halves are here on purpose. `D-2026-08-15-a-capability-that-ships-off-is-not-a-capability`
deleted 1,442 lines of the nearest previous attempt at this feature partly because the measurement
meant to justify it had a denominator that depended on the model volunteering the behaviour being
measured. Simulating the ranking machinery has no such problem: the ground truth is constructed, so
the denominator is fixed, and a regression in the pairing or the fit shows up as a number moving.

**The null control is the point.** `D-2026-08-16` measured a revision loop that cleared 10 of 39
flags and looked like it worked, until re-scoring the same answers *unchanged* cleared 2.0 per roll
and the benefit over doing nothing turned out to be zero. So every figure here is reported beside
the same statistic computed on a shuffled ordering, and a tournament that does not beat its own
shuffle is decoration.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass

from chemclaw.hypotheses.pairing import pair_round, rounds_for
from chemclaw.hypotheses.rating import Judgement, rate


@dataclass(frozen=True, slots=True)
class Recovery:
    """How well one simulated tournament recovered the ordering it was given.

    `top_one` is the statistic that matters most for this feature, because the report names a single
    next experiment: it is the fraction of runs whose top-rated hypothesis really was the best one.
    `spearman` says whether the rest of the table is informative or only the leader is.
    """

    field: int
    judge_accuracy: float
    comparisons: int
    top_one: float
    spearman: float
    null_top_one: float
    null_spearman: float

    @property
    def beats_null(self) -> bool:
        """Whether ranking did better than not ranking, on both statistics."""
        return self.top_one > self.null_top_one and self.spearman > self.null_spearman


def _spearman(order: Sequence[str], truth: Sequence[str]) -> float:
    """Rank correlation between a recovered order and the true one, -1 to 1.

    Written out rather than pulled from scipy: it is six lines for distinct ranks, and this
    package already avoids a dependency it would use once.
    """
    n = len(order)
    if n < 2:
        return 0.0
    position = {name: index for index, name in enumerate(order)}
    d_squared = sum((position[name] - index) ** 2 for index, name in enumerate(truth))
    return 1.0 - (6.0 * d_squared) / (n * (n * n - 1))


def _judge(stronger: int, weaker: int, accuracy: float, rng: random.Random) -> tuple[int, int]:
    """A judge that names the genuinely better hypothesis with probability `accuracy`.

    A blunt model of a judge, deliberately: making the error rate depend on how close the pair is
    would flatter the tournament, because the pairs Swiss generates after round one are exactly the
    close ones. A flat accuracy is the pessimistic assumption.
    """
    return (stronger, weaker) if rng.random() < accuracy else (weaker, stronger)


def simulate(
    *,
    field: int,
    judge_accuracy: float,
    runs: int = 200,
    seed: int = 0,
    rounds: int | None = None,
) -> Recovery:
    """Run `runs` tournaments over a constructed ordering and report what was recovered.

    Truth is `0 > 1 > ... > field-1`, which costs nothing in generality — the pairing never sees an
    id's meaning — and makes the null trivially statable: a shuffled order.
    """
    rng = random.Random(seed)
    truth = [f"h{i}" for i in range(field)]
    hits = nulls = 0
    spearman_total = null_spearman_total = 0.0
    comparisons_total = 0

    for _ in range(runs):
        scores: dict[str, float] = dict.fromkeys(truth, 0.0)
        byes: dict[str, int] = {}
        played: set[frozenset[str]] = set()
        judgements: list[Judgement] = []

        for _round in range(rounds if rounds is not None else rounds_for(field)):
            pairs, bye = pair_round(truth, scores=scores, played=frozenset(played), byes=byes)
            if bye is not None:
                byes[bye] = byes.get(bye, 0) + 1
                scores[bye] += 0.5
            for left, right in pairs:
                played.add(frozenset({left, right}))
                stronger, weaker = (
                    (left, right) if truth.index(left) < truth.index(right) else (right, left)
                )
                won, lost = _judge(truth.index(stronger), truth.index(weaker), judge_accuracy, rng)
                winner, loser = f"h{won}", f"h{lost}"
                judgements.append(Judgement(winner=winner, loser=loser))
                scores[winner] += 1.0
                comparisons_total += 1

        recovered = [row.hypothesis_id for row in rate(truth, judgements).ranked()]
        hits += recovered[0] == truth[0]
        spearman_total += _spearman(recovered, truth)

        shuffled = truth[:]
        rng.shuffle(shuffled)
        nulls += shuffled[0] == truth[0]
        null_spearman_total += _spearman(shuffled, truth)

    return Recovery(
        field=field,
        judge_accuracy=judge_accuracy,
        comparisons=comparisons_total // runs,
        top_one=hits / runs,
        spearman=spearman_total / runs,
        null_top_one=nulls / runs,
        null_spearman=null_spearman_total / runs,
    )


def backtest_shape() -> str:
    """What a corpus backtest would be, and why this module does not run one.

    The measurement that would settle whether a *model* judges chemistry well is: take
    `optimization-campaign` notes whose decisive run is on file, truncate the series before it,
    generate hypotheses for what was going on, and ask whether the Elo-top-1 names the cause that
    actually turned out to be right — against two nulls, a shuffled ordering and generation order.

    It is not run here because it needs a model credential, and a simulated judge cannot stand in
    for the thing under test. Saying so is the point: `evals/delegation.py` has sat unrun against a
    model for the same reason and its docstring says so plainly, which is better than a number
    produced by a mock and reported as a measurement.
    """
    return (
        "Truncate `optimization-campaign` series before the decisive run; generate; rank; compare "
        "Elo-top-1 against the recorded cause, with shuffled order and generation order as nulls. "
        "Needs CHEMCLAW_LLM_BASE_URL and a credential; never run."
    )
