"""Does the tournament's ranking carry information, and how much judging does that take?

Measures the instrument: given a judge of stated accuracy, how well do Swiss pairing and a
Bradley-Terry fit (`hypotheses/pairing.py`, `hypotheses/rating.py`) recover a known ordering,
compared with the null of a shuffled ordering. Simulated, so the ground truth is fixed and it runs
in CI without a model. Whether a language model is an accurate judge of real chemistry is a
different question (`backtest_shape`), which needs a credential. A tournament that does not beat its
own shuffle is decoration.
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

    `top_one` matters most, since the report names a single next experiment: the fraction of runs
    whose top-rated hypothesis really was the best. `spearman` says whether the rest of the table is
    informative.
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

    Written out (six lines for distinct ranks) to avoid a scipy dependency used once.
    """
    n = len(order)
    if n < 2:
        return 0.0
    position = {name: index for index, name in enumerate(order)}
    d_squared = sum((position[name] - index) ** 2 for index, name in enumerate(truth))
    return 1.0 - (6.0 * d_squared) / (n * (n * n - 1))


def _judge(stronger: str, weaker: str, accuracy: float, rng: random.Random) -> tuple[str, str]:
    """A judge that names the genuinely better hypothesis with probability `accuracy`.

    A flat accuracy is the pessimistic model: Swiss pairs close hypotheses after round one, so
    making error depend on closeness would flatter the tournament.
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

    The null is a shuffled order.
    """
    rng = random.Random(seed)
    hits = nulls = 0
    spearman_total = null_spearman_total = 0.0
    comparisons_total = 0

    for _ in range(runs):
        # Ids are re-assigned to truth ranks every run: with fixed ids the best hypothesis is also
        # the lexically first, and any ordering artefact in the pairing would read as recovery that
        # the null cannot see.
        truth = [f"h{i}" for i in range(field)]
        rng.shuffle(truth)
        entered = sorted(truth)
        rank = {name: index for index, name in enumerate(truth)}
        scores: dict[str, float] = dict.fromkeys(truth, 0.0)
        byes: dict[str, int] = {}
        played: set[frozenset[str]] = set()
        judgements: list[Judgement] = []

        for _round in range(rounds if rounds is not None else rounds_for(field)):
            # `entered` rather than `truth`: the pairing breaks a score tie by input position, so
            # handing it the true ranking would hand it the answer.
            pairs, bye = pair_round(entered, scores=scores, played=frozenset(played), byes=byes)
            if bye is not None:
                byes[bye] = byes.get(bye, 0) + 1
                scores[bye] += 0.5
            for left, right in pairs:
                played.add(frozenset({left, right}))
                stronger, weaker = (left, right) if rank[left] < rank[right] else (right, left)
                winner, loser = _judge(stronger, weaker, judge_accuracy, rng)
                judgements.append(Judgement(winner=winner, loser=loser))
                scores[winner] += 1.0
                comparisons_total += 1

        recovered = [row.hypothesis_id for row in rate(entered, judgements).ranked()]
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

    The backtest: take `optimization-campaign` notes whose decisive run is on file, truncate before
    it, generate hypotheses, and ask whether the top-rated one names the actual cause — against a
    shuffled ordering and generation order. It needs a model credential, and a simulated judge
    cannot stand in for the thing under test.
    """
    return (
        "Truncate `optimization-campaign` series before the decisive run; generate; rank; compare "
        "Elo-top-1 against the recorded cause, with shuffled order and generation order as nulls. "
        "Needs CHEMCLAW_LLM_BASE_URL and a credential; never run."
    )
