"""Where a trending attribute meets its specification limit — an ICH Q1E-shaped estimate.

`specification.py` answers "does this batch meet specification **today**". This answers the
question that follows it: given the same attribute measured at several timepoints, when does the
trend reach the limit? That is what a retest period or a shelf life is derived from, and it is the
calculation analytical development does by hand in a spreadsheet.

**It is an estimate, not a shelf life, and the gap is not a technicality.** ICH Q1E derives a
retest period or shelf life from a procedure this module implements one step of: a least-squares
fit with a one-sided 95% confidence bound, intersected with the acceptance criterion. What it does
**not** do is everything that makes that procedure a determination — poolability testing across
batches (Q1E §2.3's ANCOVA on slopes and intercepts, at the 0.25 significance level), the choice of
the worst-case batch, the statistical justification for a common intercept, and the judgment about
whether a linear model is right for the attribute at all. A single batch's regression is an input
to that procedure, never its output. Every result carries that sentence; nothing here returns a
number called a shelf life.

**The bound is one-sided and its side is the direction of change.** An impurity that grows is
bounded from *above* — the upper confidence limit is what must stay under the specification — and
an assay that falls is bounded from below. Getting that backwards produces a longer estimate than
the data supports, which is the error direction that matters, so the side is derived from the fitted
slope rather than taken as an argument.

**Extrapolation is bounded, and the bound is Q1E's rather than this module's taste.** Q1E §2.4
permits extrapolation to at most **twice** the period covered by long-term data, and never more than
**twelve months beyond** it. An intersection past that is reported as *beyond what this data
supports* rather than returned as a number, because the arithmetic will happily produce 60 months
from six months of data and the regression's own confidence band is the thing that stops meaning
anything out there.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from scipy import stats

from chemclaw.analytical.specification import AcceptanceCriterion
from chemclaw.core.units import Measurement, UnitError

#: The confidence level Q1E specifies for the one-sided bound.
CONFIDENCE = 0.95

#: The fewest timepoints a regression may be fitted to. Three is not a statistical recommendation —
#: Q1E expects considerably more — it is the point below which the arithmetic stops existing: two
#: points fit a line with zero residual degrees of freedom, so there is no confidence band at all
#: and the estimate would come back infinitely precise.
MINIMUM_TIMEPOINTS = 3


class StabilityError(ValueError):
    """Data no regression here can be fitted to.

    A `ValueError` so the message reaches a model verbatim, and every one names what is wrong with
    the data rather than reporting a number derived from it.
    """


@dataclass(frozen=True)
class Timepoint:
    """One measurement of one attribute at one storage time."""

    #: Months on stability. Months rather than a date because that is the unit Q1E's periods,
    #: pull schedules and extrapolation limits are all written in, and converting dates here would
    #: put a calendar in a module that has no business holding one.
    months: float
    value: Measurement


@dataclass(frozen=True)
class TrendEstimate:
    """A fitted trend, where its confidence bound meets the limit, and what that is worth."""

    #: Change per month, in the attribute's own unit. Sign is the direction of drift.
    slope_per_month: float
    #: The fitted value at time zero, in the attribute's own unit.
    intercept: float
    #: Coefficient of determination. Reported rather than gated: a low value on a flat, in-control
    #: attribute is the *expected* result — there is nothing to explain — so refusing on it would
    #: refuse exactly the batches that are behaving.
    r_squared: float
    #: Months at which the one-sided 95% bound reaches the limit, or `None` when it does not within
    #: the extrapolation Q1E permits. `note` says which.
    months_to_limit: float | None
    #: The longest timepoint in the data. Every estimate is read against this.
    observed_months: float
    #: `"upper"` when the attribute is rising against a maximum, `"lower"` when falling against a
    #: minimum — derived from the fitted slope, never supplied.
    bounded_side: str
    note: str


def _fit(months: list[float], values: list[float]) -> tuple[float, float, float, float]:
    """Least squares on `values` against `months`, returning slope, intercept, r², residual s.

    Written out rather than taken from `scipy.stats.linregress` for one reason: the residual
    standard error is what the confidence band needs and that function does not return it, so a
    caller would compute it separately from the fit it belongs to. One function, one set of
    residuals.
    """
    n = len(months)
    mean_x = sum(months) / n
    mean_y = sum(values) / n
    sxx = sum((x - mean_x) ** 2 for x in months)
    if sxx == 0:
        raise StabilityError(
            "every timepoint is at the same time on stability, so there is no trend to fit — "
            "a regression needs at least two distinct times"
        )
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(months, values, strict=True))
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    residuals = [y - (intercept + slope * x) for x, y in zip(months, values, strict=True)]
    total = sum((y - mean_y) ** 2 for y in values)
    residual_sum = sum(r**2 for r in residuals)
    # A perfectly flat attribute has zero total variance, where r² is 0/0. Reported as 1.0: the
    # model explains everything there is to explain, which is 'nothing'. Calling it 0.0 would read
    # as a failed fit on exactly the data a stability study hopes for.
    r_squared = 1.0 if total == 0 else 1.0 - residual_sum / total
    standard_error = math.sqrt(residual_sum / (n - 2))
    return slope, intercept, r_squared, standard_error


def estimate_trend(timepoints: list[Timepoint], criterion: AcceptanceCriterion) -> TrendEstimate:
    """Fit the attribute against time and find where its 95% bound meets the criterion.

    The bound at time `t` is `fit(t) ± t_{0.95,n-2} · s · sqrt(1/n + (t - x̄)²/Sxx)` — the
    confidence interval for the *mean response*, which is what Q1E uses, rather than a prediction
    interval for a future single result. Solved for the crossing by bisection over the permitted
    extrapolation window, because the bound is not linear in `t` and the intersection has no closed
    form.

    Args:
        timepoints: At least `MINIMUM_TIMEPOINTS` measurements of one attribute, in one unit or in
            units of one dimension. Order does not matter.
        criterion: The limit the trend is read against. The bound's side follows the slope, so a
            criterion needs only the bound the attribute is heading towards; one stating both is
            fine and the relevant one is used.

    Returns:
        The fit, the crossing if there is one inside Q1E's extrapolation window, and a note saying
        what the number is and is not.

    Raises:
        StabilityError: Too few timepoints, timepoints at a single time, negative times, values
            whose units cannot be reconciled with each other or with the criterion, or a criterion
            with no bound on the side the attribute is drifting towards.
    """
    if len(timepoints) < MINIMUM_TIMEPOINTS:
        raise StabilityError(
            f"{len(timepoints)} timepoint(s) is too few to fit a trend with a confidence band; "
            f"at least {MINIMUM_TIMEPOINTS} are needed, because two leave no residual degrees of "
            "freedom and the band would come back infinitely narrow"
        )
    if any(point.months < 0 for point in timepoints):
        raise StabilityError("a timepoint is at a negative time on stability")

    reference = timepoints[0].value.unit.symbol
    try:
        values = [point.value.to(reference).value for point in timepoints]
    except UnitError as mismatch:
        raise StabilityError(
            f"the timepoints are not all the same kind of quantity: {mismatch}"
        ) from mismatch
    months = [point.months for point in timepoints]

    slope, intercept, r_squared, standard_error = _fit(months, values)
    observed = max(months)
    rising = _side_the_attribute_approaches(slope, intercept, criterion, reference)
    bound = _bound_for(rising, criterion, reference)
    crossing, note = _crossing(
        months=months,
        slope=slope,
        intercept=intercept,
        standard_error=standard_error,
        limit=bound.value,
        rising=rising,
        observed=observed,
    )
    return TrendEstimate(
        slope_per_month=slope,
        intercept=intercept,
        r_squared=r_squared,
        months_to_limit=crossing,
        observed_months=observed,
        bounded_side="upper" if rising else "lower",
        note=note,
    )


def _side_the_attribute_approaches(
    slope: float, intercept: float, criterion: AcceptanceCriterion, unit: str
) -> bool:
    """True when the upper bound is the relevant one — from the slope, or from the criterion.

    **A slope of exactly zero is not "falling", and reading it that way was a real defect.** The
    ordinary branch is the sign of the drift: an attribute rising towards a maximum is bounded from
    above, one falling towards a minimum from below. A perfectly flat attribute — every timepoint
    identical, which a well-behaved impurity at the reporting threshold really does produce — has no
    drift to take a sign from, and `slope > 0` silently classified it as falling and then refused a
    specification that states only a maximum.

    With no drift the confidence band still widens with extrapolation, so a bound does still move
    and the question is only *which one*. Answered from the criterion: the side it states, or, when
    it states both, the one the fitted value sits nearer to — which is the one the widening band
    reaches first.
    """
    if slope > 0:
        return True
    if slope < 0:
        return False
    if criterion.maximum is None:
        return False
    if criterion.minimum is None:
        return True
    upper = criterion.maximum.to(unit).value
    lower = criterion.minimum.to(unit).value
    return abs(upper - intercept) <= abs(intercept - lower)


def _bound_for(rising: bool, criterion: AcceptanceCriterion, unit: str) -> Measurement:
    """The limit the attribute is drifting towards, in the timepoints' own unit.

    Raises:
        StabilityError: The criterion states no bound on that side — an impurity rising against a
            specification that only sets a minimum has nothing to reach, and returning "never" for
            it would be an answer about the wrong question.
    """
    wanted = criterion.maximum if rising else criterion.minimum
    side = "maximum" if rising else "minimum"
    direction = "rising" if rising else "falling"
    if wanted is None:
        raise StabilityError(
            f"the attribute is {direction} and criterion {criterion.name!r} states no {side}, so "
            "there is no limit for the trend to reach on the side it is heading"
        )
    try:
        return wanted.to(unit)
    except UnitError as mismatch:
        raise StabilityError(
            f"the {side} of criterion {criterion.name!r} cannot be expressed in the timepoints' "
            f"unit: {mismatch}"
        ) from mismatch


def _crossing(
    *,
    months: list[float],
    slope: float,
    intercept: float,
    standard_error: float,
    limit: float,
    rising: bool,
    observed: float,
) -> tuple[float | None, str]:
    """Where the one-sided bound reaches `limit`, inside the extrapolation Q1E permits.

    Returns `(None, why)` when it does not — which is a real answer and the common one for a stable
    product, and is deliberately not spelled as a very large number.
    """
    n = len(months)
    mean_x = sum(months) / n
    sxx = sum((x - mean_x) ** 2 for x in months)
    quantile = float(stats.t.ppf(CONFIDENCE, n - 2))

    def bound_at(time: float) -> float:
        """The one-sided 95% confidence bound on the mean response at `time`."""
        half_width = quantile * standard_error * math.sqrt(1.0 / n + (time - mean_x) ** 2 / sxx)
        fitted = intercept + slope * time
        return fitted + half_width if rising else fitted - half_width

    # Q1E §2.4: at most twice the observed period, and never more than 12 months beyond it.
    ceiling = min(2.0 * observed, observed + 12.0)
    if _past_limit(bound_at(0.0), limit, rising):
        return 0.0, (
            "the confidence bound is already past the limit at time zero, so this data does not "
            "support any period — check the fit and the limit before reading anything else here"
        )
    if not _past_limit(bound_at(ceiling), limit, rising):
        return None, (
            f"the 95% bound does not reach the limit within {ceiling:g} months, which is as far as "
            f"ICH Q1E §2.4 permits extrapolating {observed:g} months of data (twice the observed "
            "period, and no more than 12 months beyond it). That is a statement about this data's "
            "reach, not a finding that the attribute never reaches the limit"
        )
    low, high = 0.0, ceiling
    for _ in range(200):
        middle = (low + high) / 2.0
        if _past_limit(bound_at(middle), limit, rising):
            high = middle
        else:
            low = middle
    crossing = (low + high) / 2.0
    beyond = " — beyond the observed data, so it rests on the model" if crossing > observed else ""
    return crossing, (
        f"the one-sided 95% confidence bound reaches the limit at {crossing:.1f} months{beyond}. "
        "This is one batch's regression, which ICH Q1E treats as an input to a retest period or "
        "shelf life and never as one: the poolability testing across batches, the worst-case batch "
        "and the judgment about whether a linear model fits this attribute are all outside it"
    )


def _past_limit(bound: float, limit: float, rising: bool) -> bool:
    """Has the bound reached the limit, on the side the attribute is drifting towards?"""
    return bound >= limit if rising else bound <= limit
