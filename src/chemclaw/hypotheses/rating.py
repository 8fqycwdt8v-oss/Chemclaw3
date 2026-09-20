"""Rating a set of hypotheses from pairwise judgements, on the Elo scale.

**Why this is a Bradley-Terry fit and not the sequential Elo update rule.** Elo is an *online*
estimator: it was designed for a stream of games against a drifting population, where each result
nudges a running rating by `K·(outcome − expected)`. That makes it order-dependent — the same set of
comparisons replayed in a different sequence yields different numbers — and it produces no interval,
only a point. Both properties are wrong here. A tournament over one fixed pool of hypotheses has a
*complete* set of comparisons available at once, and the whole reason a rating is being shown to a
chemist is to say how firmly the ordering is held. `retrieval/fanout.py` already records why
order-dependence is unacceptable in a place a user can see: "one sweep's evidence [differing] from
the next for no reason a chemist could see — a reproducibility problem".

So this fits the model Elo approximates. Bradley-Terry says

    P(i beats j) = sigmoid(s · (r_i − r_j)),   s = ln(10) / 400

and the `s` is chosen so the ratings land on the familiar Elo scale: a 400-point gap is 10-to-1
odds,
and `ANCHOR` is the conventional 1500. The number a chemist reads is therefore an Elo number in the
only sense that matters — same scale, same interpretation — computed by maximum likelihood over all
the comparisons at once rather than by a running nudge.

**The prior is not optional and is not a tuning knob.** With no regularization a hypothesis that won
every comparison has an unbounded maximum-likelihood rating: the likelihood keeps increasing as its
rating runs to infinity, which is *complete separation*, and it is the common case in a small
tournament rather than an edge case. A Normal prior on each rating about `ANCHOR` bounds it and, at
`PRIOR_SD`, costs a well-separated candidate a few tens of points while turning an infinite estimate
into a finite one with an honest standard error. It also means an unjudged hypothesis reads exactly
`ANCHOR`, which is the truthful answer to "what do we know about this one".

**The standard error is the point of the exercise**, and is why the Hessian is formed rather than
just the gradient. `science/bo/engine.py:323` refuses to surface BoFire's acquisition score because
"carrying it would invite reading it as a confidence"; a bare Elo has exactly that failure mode. A
rating reported as `1612 ± 140 over 4 comparisons` cannot be read as a probability, and a rating
reported as `1612` will be. So `Rating` carries all three and `report.py` renders all three.

**Which standard error, though, is a question the first draft of this module got wrong, and it was
measured rather than reasoned.** The likelihood depends only on rating *differences*: the whole
vector can slide, and only the prior pins it. So each rating's *marginal* variance is dominated by
the uncertainty in where the field as a whole sits, which is not a quantity anybody is asking about
— and worse, the marginals are strongly positively correlated, so combining two of them with
`hypot` to ask "is A really ahead of B" overstates the spread by a factor of five. Measured on
twenty drawn comparisons between two hypotheses: the marginal error is ±285 and `hypot` gives ±403,
while the difference is actually known to ±76.

Two identified quantities are reported instead, and neither is the raw marginal:

- `Rating.standard_error` is the error of `r_i − mean(r)`, this hypothesis's position *within the
  field*. That is what a reader means by "± on a rating", and it is invariant to the slide.
- `RatingTable.difference` is the error of `r_i − r_j` from the full covariance, which is what
  `separated` tests. An ordering inside that interval is an artefact of which comparisons happened
  to run, and the report must not claim it.
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

# Standard deviation of the Normal prior each rating is shrunk toward `ANCHOR` by. 400 points is one
# full scale-unit: wide enough that the data decides the ordering, tight enough that an undefeated
# hypothesis lands near 1900 instead of at infinity.
PRIOR_SD = 400.0

# Newton is quadratically convergent on this objective (it is strictly concave), so the iteration
# count is a safety net rather than a schedule; measured, a 30-candidate fit converges in 4-5.
# The posterior probability at which the report is willing to say one hypothesis outranks another.
# 0.95 is the conventional line; below it the table shows the pair as tied rather than ordered.
DECISIVE_PROBABILITY = 0.95

_MAX_ITERATIONS = 100
_TOLERANCE = 1e-9


@dataclass(frozen=True, slots=True)
class Judgement:
    """One pairwise comparison: `winner` was judged better than `loser`, or it was a draw.

    A draw is `outcome=0.5` with the pair in either order, and is the honest record of a judge that
    could not separate two hypotheses — which is information about the *pair*, not a failure, and is
    exactly what keeps two near-duplicates from acquiring a spurious gap.

    `weight` exists so a comparison judged in both directions (to measure position bias) can be
    entered as two half-weight judgements rather than double-counted as two independent ones.
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

    All three are reported together, always. `comparisons` is the count of judgements this
    hypothesis took part in, so a rating at `ANCHOR` with `comparisons=0` is legible as "never
    judged" rather than as "average".

    `standard_error` is the error of this rating *relative to the field's mean*, not its marginal
    error — see the module docstring for the measurement that forced the distinction.
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

    Because `rate` fits under a proper prior, the fitted gap and its error describe an approximate
    *posterior*, so the honest summary of "is A really ahead of B" is a probability rather than a
    significance test. `probability_ahead` is that number.

    **It is a probability about the ranking, not about the chemistry.** It says how confident the
    fit is that the judges prefer `ahead` to `behind` — nothing whatever about either hypothesis
    being true. Keeping that distinction is the whole reason this module reports intervals at all
    (see the module docstring on `science/bo/engine.py:323`).

    A frequentist two-standard-error rule was tried first and rejected by measurement: it calls a
    clean five-nil sweep undecided, because the Wald interval is badly conservative when the prior
    is doing the bounding, while the posterior puts that same sweep at 0.95.
    """

    ahead: str
    behind: str
    gap: float
    standard_error: float

    @property
    def probability_ahead(self) -> float:
        """Posterior probability that `ahead` really does outrank `behind`.

        Exactly 0.5 when the ratings are tied, which is the correct reading of "the judges could not
        separate these two" rather than a missing value.
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

    The covariance is kept rather than discarded because the interesting question is never "what is
    A's rating" but "is A ahead of B", and those two have very different error bars: the ratings
    share the uncertainty in where the whole field sits, which cancels in their difference.
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

        `Var(r_i − r_j) = C_ii + C_jj − 2·C_ij`. Using each rating's own error instead — even
        combined in quadrature — overstates this by several times, because the shared uncertainty
        about the field's overall level does not affect their difference.
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
    """The probability the Elo scale assigns to A beating B.

    Exposed for tests and for the report.
    """
    return _expected(rating_a - rating_b)


def rate(
    hypothesis_ids: Sequence[str],
    judgements: Iterable[Judgement],
    *,
    prior_sd: float = PRIOR_SD,
) -> RatingTable:
    """Fit Elo-scale ratings to `judgements` by penalized maximum likelihood.

    Deterministic and order-independent: the result depends on the *multiset* of judgements, never
    on the sequence they arrived in, which is what makes a tournament reproducible for a chemist who
    re-reads it.

    Raises `ValueError` on a judgement naming a hypothesis absent from `hypothesis_ids`, because
    silently dropping one would quietly change the ordering the caller is about to act on.
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
    # **Weight, not row count.** A pair judged in both presentation orders enters as two
    # half-weight judgements, so counting rows told a chemist "over 5 comparisons" for a hypothesis
    # the fit had given total weight 4 — a displayed evidence count that disagreed with the interval
    # printed beside it. Rounding keeps it a whole number for the reader while tracking what the fit
    # actually used.
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
    # The error of `r_i − mean(r)`: the contrast `e_i − 1/n` applied to the covariance. This is the
    # identified quantity — a rating's position within the field — rather than the marginal, whose
    # size is mostly the unknowable overall level of the field.
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
