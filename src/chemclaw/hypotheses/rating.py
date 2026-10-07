"""Rating a set of hypotheses from pairwise judgements, on the Elo scale.

A Bradley-Terry fit rather than sequential Elo updates: the tournament has every comparison at
once, and the result must be order-independent and carry an interval. The model is

    P(i beats j) = sigmoid(s · (r_i − r_j)),   s = ln(10) / 400

so a 400-point gap is 10-to-1 odds and `ANCHOR` is 1500. A Normal prior about `ANCHOR` keeps an
undefeated hypothesis finite (complete separation is the common case in a small field) and leaves
an unjudged one at exactly `ANCHOR`.

The likelihood depends only on differences, so raw marginal errors mostly measure where the field
as a whole sits. Two identified quantities are reported instead: `Rating.standard_error` is the
error of `r_i − mean(r)`, and `RatingTable.difference` is the error of `r_i − r_j` from the full
covariance, which is what `separated` tests. A bare rating invites being read as a confidence, so
every rating is reported with its error and comparison count.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np

# The Elo scale: `SCALE` points of advantage is 10:1 odds, and ratings are centred on `ANCHOR`.
SCALE = 400.0
ANCHOR = 1500.0
_S = math.log(10.0) / SCALE

# Standard deviation of the Normal prior each rating is shrunk toward `ANCHOR` by: wide enough
# that the data decides the ordering, tight enough that an undefeated hypothesis stays finite.
PRIOR_SD = 400.0

# The posterior probability at which the report is willing to say one hypothesis outranks another.
# 0.95 is the conventional line; below it the table shows the pair as tied rather than ordered.
DECISIVE_PROBABILITY = 0.95

# Newton is quadratically convergent on this objective (it is strictly concave), so the iteration
# count is a safety net rather than a schedule; measured, a 30-candidate fit converges in 4-5.
_MAX_ITERATIONS = 100
_TOLERANCE = 1e-9


@dataclass(frozen=True, slots=True)
class Judgement:
    """One pairwise comparison: `winner` was judged better than `loser`, or it was a draw.

    A draw is `outcome=0.5` with the pair in either order — information about the pair, which keeps
    near-duplicates from acquiring a spurious gap. `weight` lets a pair judged in both presentation
    orders enter as two half-weight judgements rather than two independent ones.
    """

    winner: str
    loser: str
    outcome: float = 1.0
    weight: float = 1.0

    def __post_init__(self) -> None:
        """Reject the three shapes that would corrupt a fit rather than merely skew it."""
        if not 0.0 <= self.outcome <= 1.0:
            raise ValueError(f"outcome must be in [0, 1], got {self.outcome}")
        if self.weight <= 0.0:
            raise ValueError(f"weight must be positive, got {self.weight}")
        if self.winner == self.loser:
            raise ValueError(f"a hypothesis cannot be compared with itself: {self.winner!r}")


@dataclass(frozen=True, slots=True)
class Rating:
    """A fitted rating, its standard error within the field, and the evidence behind it.

    `comparisons=0` at `ANCHOR` reads as "never judged" rather than "average". `standard_error` is
    relative to the field's mean, not the marginal error.
    """

    hypothesis_id: str
    rating: float
    standard_error: float
    comparisons: int

    @property
    def unjudged(self) -> bool:
        """True when nothing was compared against this one, so the prior is the whole answer."""
        return self.comparisons == 0


@dataclass(frozen=True, slots=True)
class Separation:
    """How far apart two ratings are, how well that gap is known, and whether it may be acted on.

    The fit is under a proper prior, so `probability_ahead` is an approximate posterior probability
    that the judges prefer `ahead` to `behind` — a statement about the ranking, never about either
    hypothesis being true. A posterior rather than a Wald two-standard-error rule, which is too
    conservative when the prior does the bounding.
    """

    ahead: str
    behind: str
    gap: float
    standard_error: float

    @property
    def probability_ahead(self) -> float:
        """Posterior probability that `ahead` really does outrank `behind`.

        Exactly 0.5 when the ratings are tied.
        """
        if self.standard_error <= 0.0:
            return 1.0 if self.gap > 0.0 else 0.5
        return 0.5 * (1.0 + math.erf(self.gap / (self.standard_error * math.sqrt(2.0))))

    @property
    def decisive(self) -> bool:
        """Whether the ordering of this pair is firm enough for a report to assert it."""
        return self.probability_ahead >= DECISIVE_PROBABILITY


class RatingTable:
    """Fitted ratings plus the covariance needed to compare any two of them honestly.

    The ratings share the uncertainty in where the field sits, which cancels in a difference, so
    "is A ahead of B" needs the covariance rather than the two errors.
    """

    __slots__ = ("_covariance", "_index", "_ratings")

    def __init__(
        self, ratings: dict[str, Rating], covariance: np.ndarray, index: dict[str, int]
    ) -> None:
        """Hold a fit. Built by `rate`; the covariance and index are its internals."""
        self._ratings = ratings
        self._covariance = covariance
        self._index = index

    def __getitem__(self, hypothesis_id: str) -> Rating:
        """One hypothesis's rating."""
        return self._ratings[hypothesis_id]

    def __contains__(self, hypothesis_id: object) -> bool:
        """Whether this table holds a rating for `hypothesis_id`."""
        return hypothesis_id in self._ratings

    def __len__(self) -> int:
        """How many hypotheses were rated."""
        return len(self._ratings)

    @property
    def ratings(self) -> dict[str, Rating]:
        """Every rating, keyed by hypothesis id."""
        return dict(self._ratings)

    def ranked(self) -> list[Rating]:
        """Ratings best-first, ties broken by id so the order is total and reproducible."""
        return sorted(self._ratings.values(), key=lambda r: (-r.rating, r.hypothesis_id))

    def difference(self, first: str, second: str) -> Separation:
        """The gap between two ratings and its standard error, from the full covariance.

        `Var(r_i − r_j) = C_ii + C_jj − 2·C_ij`; combining the individual errors would overstate it.
        """
        i, j = self._index[first], self._index[second]
        variance = self._covariance[i, i] + self._covariance[j, j] - 2.0 * self._covariance[i, j]
        gap = self._ratings[first].rating - self._ratings[second].rating
        ahead, behind = (first, second) if gap >= 0.0 else (second, first)
        return Separation(
            ahead=ahead,
            behind=behind,
            gap=abs(gap),
            standard_error=math.sqrt(max(float(variance), 0.0)),
        )


def _expected(difference: float) -> float:
    """P(win) for a rating difference, the logistic Elo curve."""
    return 1.0 / (1.0 + math.exp(-_S * difference))


def expected_score(rating_a: float, rating_b: float) -> float:
    """The probability the Elo scale assigns to A beating B."""
    return _expected(rating_a - rating_b)


def rate(
    hypothesis_ids: Sequence[str],
    judgements: Iterable[Judgement],
    *,
    prior_sd: float = PRIOR_SD,
) -> RatingTable:
    """Fit Elo-scale ratings to `judgements` by penalized maximum likelihood.

    Depends only on the multiset of judgements, never their order. Raises `ValueError` on a
    judgement naming a hypothesis absent from `hypothesis_ids` rather than silently dropping it.
    """
    if prior_sd <= 0.0:
        raise ValueError(f"prior_sd must be positive, got {prior_sd}")
    ids = list(dict.fromkeys(hypothesis_ids))
    index = {name: i for i, name in enumerate(ids)}
    entries = list(judgements)
    for judgement in entries:
        for name in (judgement.winner, judgement.loser):
            if name not in index:
                raise ValueError(f"judgement names an unknown hypothesis: {name!r}")
    if not ids:
        return RatingTable({}, np.zeros((0, 0), dtype=float), {})

    n = len(ids)
    # Count weight, not rows: a pair judged in both orders is two half-weight judgements, and the
    # displayed comparison count must match what the fit used.
    weights = [0.0] * n
    for judgement in entries:
        weights[index[judgement.winner]] += judgement.weight
        weights[index[judgement.loser]] += judgement.weight
    counts = [int(round(weight)) for weight in weights]

    offsets = np.zeros(n, dtype=float)  # offsets from ANCHOR; the prior is centred at 0 here
    precision = 1.0 / (prior_sd * prior_sd)
    hessian = np.zeros((n, n), dtype=float)

    for _ in range(_MAX_ITERATIONS):
        gradient = -precision * offsets
        hessian = np.zeros((n, n), dtype=float)
        np.fill_diagonal(hessian, -precision)
        for judgement in entries:
            i = index[judgement.winner]
            j = index[judgement.loser]
            p = _expected(float(offsets[i] - offsets[j]))
            gradient[i] += judgement.weight * _S * (judgement.outcome - p)
            gradient[j] -= judgement.weight * _S * (judgement.outcome - p)
            curvature = judgement.weight * _S * _S * p * (1.0 - p)
            hessian[i, i] -= curvature
            hessian[j, j] -= curvature
            hessian[i, j] += curvature
            hessian[j, i] += curvature
        # `hessian` is negative definite (the prior alone makes it so), hence non-singular.
        step = np.linalg.solve(-hessian, gradient)
        offsets = offsets + step
        if float(np.max(np.abs(step))) < _TOLERANCE:
            break

    covariance = np.linalg.inv(-hessian)
    # The error of `r_i − mean(r)`: the contrast `e_i − 1/n` applied to the covariance.
    centring = np.eye(n) - np.full((n, n), 1.0 / n)
    centred = centring @ covariance @ centring.T
    errors = np.sqrt(np.clip(np.diag(centred), 0.0, None))

    ratings = {
        name: Rating(
            hypothesis_id=name,
            rating=ANCHOR + float(offsets[i]),
            standard_error=float(errors[i]),
            comparisons=counts[i],
        )
        for name, i in index.items()
    }
    return RatingTable(ratings, covariance, index)
