"""Where a trending attribute meets its specification limit — an ICH Q1E-shaped estimate.

Fits the attribute against time by least squares, takes a one-sided 95% confidence bound and
intersects it with the acceptance criterion. That is one step of Q1E, not a shelf-life
determination: poolability testing across batches, worst-case batch choice and model choice are
not done here, and every result says so.

The bound's side follows the direction of change (a rising impurity is bounded from above), derived
from the fit, since the wrong side overstates the period. Extrapolation is capped at Q1E §2.4's
limit — twice the long-term period and at most twelve months beyond it; a crossing past that is
reported as unsupported, not as a number.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from scipy import stats

from chemclaw.analytical.specification import AcceptanceCriterion
from chemclaw.core.units import Measurement, UnitError

#: The confidence level Q1E specifies for the one-sided bound.
CONFIDENCE = 0.95

# The fewest timepoints a regression may be fitted to. Not a statistical recommendation: with two
# points there are zero residual degrees of freedom and no confidence band.
MINIMUM_TIMEPOINTS = 3


class StabilityError(ValueError):
    """Data no regression here can be fitted to.

    A `ValueError` so the message, which names the problem, reaches a model verbatim.
    """


@dataclass(frozen=True)
class Timepoint:
    """One measurement of one attribute at one storage time."""

    # Months on stability: the unit Q1E's periods and extrapolation limits are written in.
    months: float
    value: Measurement


@dataclass(frozen=True)
class TrendEstimate:
    """A fitted trend, where its confidence bound meets the limit, and what that is worth."""

    #: Change per month, in the attribute's own unit. Sign is the direction of drift.
    slope_per_month: float
    #: The fitted value at time zero, in the attribute's own unit.
    intercept: float
    # Coefficient of determination. Reported, not gated: a flat, in-control attribute legitimately
    # has
    # a low value.
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

    Hand-written because the confidence band needs the residual standard error, which
    `scipy.stats.linregress` does not return.
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
    # Zero total variance makes r² 0/0; report 1.0, since a flat attribute is the hoped-for result.
    r_squared = 1.0 if total == 0 else 1.0 - residual_sum / total
    standard_error = math.sqrt(residual_sum / (n - 2))
    return slope, intercept, r_squared, standard_error


def estimate_trend(timepoints: list[Timepoint], criterion: AcceptanceCriterion) -> TrendEstimate:
    """Fit the attribute against time and find where its 95% bound meets the criterion.

    The bound at time `t` is `fit(t) ± t_{0.95,n-2} · s · sqrt(1/n + (t - x̄)²/Sxx)` — the
    confidence
    interval for the mean response, as Q1E uses. The crossing is found by bisection over the
    permitted
    extrapolation window, since the bound is not linear in `t`.

    Args:
        timepoints: At least `MINIMUM_TIMEPOINTS` measurements of one attribute, in units of one
            dimension. Order does not matter.
        criterion: The limit the trend is read against. Only the bound on the side the attribute is
            heading towards is used.

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
    rising = _side_the_attribute_approaches(
        slope,
        intercept,
        criterion,
        reference,
        drifting=_is_a_drift(months, slope, standard_error),
    )
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


def _is_a_drift(months: list[float], slope: float, standard_error: float) -> bool:
    """Whether the fitted slope is distinguishable from zero at the band's own confidence level.

    Compares the slope against `t_{0.95,n-2} · s / sqrt(Sxx)`, using `CONFIDENCE` so the module
    applies
    one standard. With a perfect fit (`s == 0`) any non-zero slope is a drift. `_fit` guarantees
    `Sxx > 0`.
    """
    n = len(months)
    if standard_error == 0.0:
        return slope != 0.0
    mean_x = sum(months) / n
    sxx = sum((x - mean_x) ** 2 for x in months)
    quantile = float(stats.t.ppf(CONFIDENCE, n - 2))
    return abs(slope) > quantile * standard_error / math.sqrt(sxx)


def _states_a_bound(criterion: AcceptanceCriterion, *, rising: bool) -> bool:
    """Whether the criterion limits the side an attribute with this sign of slope is heading."""
    return (criterion.maximum if rising else criterion.minimum) is not None


def _side_the_attribute_approaches(
    slope: float,
    intercept: float,
    criterion: AcceptanceCriterion,
    unit: str,
    *,
    drifting: bool,
) -> bool:
    """True when the upper bound is the relevant one — from the slope, or from the criterion.

    Real data is never exactly flat, so the slope's sign alone would refuse an in-control impurity
    whose noise happens to fit a negative slope against a maximum-only specification. So:

    * the criterion bounds the side the slope points at — use that side;
    * it does not, and the slope is a real drift (`_is_a_drift`) — still that side, so `_bound_for`
      raises: there is nothing to reach;
    * it does not, and the slope is noise — the criterion decides: the side it states, or, with
      both,
      the bound the fitted value sits nearer to, which the widening band reaches first.
    """
    rising = slope > 0
    if slope != 0.0 and (drifting or _states_a_bound(criterion, rising=rising)):
        return rising
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
        StabilityError: The criterion states no bound on that side.
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

    Returns `(None, why)` when it does not — the common answer for a stable product, deliberately
    not
    spelled as a very large number.
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
    # Search from the first measurement, not time zero: the band is widest far from the data, so a
    # programme whose first pull is late would otherwise cross at t=0 before any data exists.
    start = min(months)
    if _past_limit(bound_at(start), limit, rising):
        return 0.0, (
            f"the confidence bound is already past the limit at the first timepoint ({start:g} "
            "months), so this data does not support any period — check the fit and the limit "
            "before reading anything else here"
        )
    if not _past_limit(bound_at(ceiling), limit, rising):
        return None, (
            f"the 95% bound does not reach the limit within {ceiling:g} months, which is as far as "
            f"ICH Q1E §2.4 permits extrapolating {observed:g} months of data (twice the observed "
            "period, and no more than 12 months beyond it). That is a statement about this data's "
            "reach, not a finding that the attribute never reaches the limit"
        )
    # Bracketed by the two points just tested: inside the limit at `start`, past it at `ceiling`.
    low, high = start, ceiling
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
