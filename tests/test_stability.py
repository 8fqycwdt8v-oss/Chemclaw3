"""Where a trending attribute meets its limit, and the several ways that question has no answer.

Mostly drives the cases where returning a number would be worse than none: extrapolation past
what ICH Q1E permits, an attribute with no drift, and a criterion with no limit on the side the
attribute is heading. Fixtures are plausible stability datasets.
"""

from __future__ import annotations

import pytest

from chemclaw.analytical.specification import AcceptanceCriterion
from chemclaw.analytical.stability import (
    MINIMUM_TIMEPOINTS,
    StabilityError,
    Timepoint,
    estimate_trend,
)
from chemclaw.core.units import Measurement


def _points(unit: str, pairs: list[tuple[float, float]]) -> list[Timepoint]:
    """`(months, value)` pairs as timepoints in one unit."""
    return [Timepoint(months=m, value=Measurement.of(v, unit)) for m, v in pairs]


#: A degradant growing ~0.033 area%/month — reaches its 0.50 limit inside the observed year.
RISING = _points("area%", [(0, 0.10), (3, 0.20), (6, 0.31), (9, 0.39), (12, 0.50)])
#: An assay falling ~0.27 %/month against a 95.0 minimum, crossing beyond the observed data.
FALLING = _points("% w/w", [(0, 99.8), (3, 99.0), (6, 98.1), (9, 97.4), (12, 96.5)])


def test_a_rising_impurity_is_bounded_from_above_and_a_falling_assay_from_below() -> None:
    """A rising impurity is bounded from above and a falling assay from below.

    The wrong side would put the band behind the trend and report an optimistically late crossing.
    """
    rising = estimate_trend(
        RISING, AcceptanceCriterion("imp", maximum=Measurement.of(0.50, "area%"))
    )
    assert rising.slope_per_month > 0
    assert rising.bounded_side == "upper"

    falling = estimate_trend(
        FALLING, AcceptanceCriterion("assay", minimum=Measurement.of(95.0, "% w/w"))
    )
    assert falling.slope_per_month < 0
    assert falling.bounded_side == "lower"


def test_the_bound_crosses_earlier_than_the_fitted_line_does() -> None:
    """The confidence bound crosses earlier than the fitted line, computed from the returned fit."""
    estimate = estimate_trend(
        RISING, AcceptanceCriterion("imp", maximum=Measurement.of(0.50, "area%"))
    )
    assert estimate.months_to_limit is not None
    line_reaches = (0.50 - estimate.intercept) / estimate.slope_per_month
    assert estimate.months_to_limit < line_reaches, (
        f"the bound crossed at {estimate.months_to_limit:.2f} months and the fitted line at "
        f"{line_reaches:.2f} — the bound must be the earlier of the two or it is not a bound"
    )


def test_an_extrapolation_past_what_q1e_permits_is_refused_rather_than_returned() -> None:
    """An extrapolation past what Q1E permits is refused rather than returned.

    Q1E §2.4 allows at most twice the observed period and no more than 12 months beyond. The line
    is shown to reach the limit first, so the window is what refuses.
    """
    slow = _points("area%", [(0, 0.10), (3, 0.12), (6, 0.13), (9, 0.15), (12, 0.16)])
    criterion = AcceptanceCriterion("imp", maximum=Measurement.of(0.50, "area%"))
    estimate = estimate_trend(slow, criterion)

    line_reaches = (0.50 - estimate.intercept) / estimate.slope_per_month
    assert line_reaches > 24.0, "fixture no longer crosses outside the window; the test is inert"
    assert estimate.months_to_limit is None
    assert "Q1E" in estimate.note and "24" in estimate.note


def test_the_window_is_twelve_months_beyond_rather_than_twice_when_that_is_shorter() -> None:
    """The window is the lesser of twice the period and twelve months beyond it.

    The rules agree at 12 months observed, so a 24-month study is needed to tell them apart.
    """
    long_study = _points(
        "area%",
        [(0, 0.10), (6, 0.13), (12, 0.16), (18, 0.19), (24, 0.22)],
    )
    estimate = estimate_trend(
        long_study, AcceptanceCriterion("imp", maximum=Measurement.of(0.34, "area%"))
    )
    assert estimate.observed_months == 24.0
    assert estimate.months_to_limit is None, (
        "the trend reaches 0.34 area% at ~48 months, which is twice the observed period but more "
        "than twelve months beyond it — Q1E permits the shorter of the two"
    )
    assert "36" in estimate.note


def test_a_flat_attribute_has_no_direction_and_is_not_read_as_falling() -> None:
    """A flat attribute has no direction and is not read as falling.

    With no drift the side is taken from the criterion: the side it states, or the nearer when it
    states both.
    """
    flat = _points("area%", [(0, 0.10), (3, 0.10), (6, 0.10), (9, 0.10)])
    estimate = estimate_trend(
        flat, AcceptanceCriterion("imp", maximum=Measurement.of(0.50, "area%"))
    )
    assert estimate.slope_per_month == 0.0
    assert estimate.bounded_side == "upper"
    assert estimate.months_to_limit is None

    two_sided = estimate_trend(
        _points("% w/w", [(0, 100.0), (3, 100.0), (6, 100.0), (9, 100.0)]),
        AcceptanceCriterion(
            "assay", minimum=Measurement.of(98.0, "% w/w"), maximum=Measurement.of(102.0, "% w/w")
        ),
    )
    assert two_sided.bounded_side == "upper", (
        "a flat value at 100.0 is equidistant from 98 and 102; the tie has to resolve to something "
        "rather than raise, and upper is the documented resolution"
    )


def test_a_criterion_with_no_limit_on_the_side_the_attribute_is_heading_is_refused() -> None:
    """A criterion with no limit on the side the attribute is heading is refused.

    A `None` crossing means "not within the window", which is a different answer.
    """
    with pytest.raises(StabilityError, match="rising.*no maximum"):
        estimate_trend(RISING, AcceptanceCriterion("imp", minimum=Measurement.of(0.01, "area%")))
    with pytest.raises(StabilityError, match="falling.*no minimum"):
        estimate_trend(
            FALLING, AcceptanceCriterion("assay", maximum=Measurement.of(102.0, "% w/w"))
        )


def test_too_few_timepoints_are_refused_because_the_band_would_be_infinitely_narrow() -> None:
    """Two timepoints are refused: zero residual freedom gives an infinitely narrow band."""
    criterion = AcceptanceCriterion("imp", maximum=Measurement.of(0.50, "area%"))
    with pytest.raises(StabilityError, match="too few"):
        estimate_trend(_points("area%", [(0, 0.10), (6, 0.20)]), criterion)
    assert MINIMUM_TIMEPOINTS == 3


def test_timepoints_at_a_single_time_are_refused_rather_than_divided_by_zero() -> None:
    """Four results from one pull is replicate data, not a trend, and `Sxx` is 0."""
    criterion = AcceptanceCriterion("imp", maximum=Measurement.of(0.50, "area%"))
    with pytest.raises(StabilityError, match="same time"):
        estimate_trend(_points("area%", [(6, 0.10), (6, 0.11), (6, 0.12)]), criterion)


def test_timepoints_in_different_units_of_one_dimension_are_reconciled() -> None:
    """Timepoints in different units of one dimension are reconciled.

    A mixed ppm/percent study must give the same estimate as the same data wholly in percent.
    """
    criterion = AcceptanceCriterion("imp", maximum=Measurement.of(0.50, "%"))
    mixed = [
        Timepoint(months=0, value=Measurement.of(0.10, "%")),
        Timepoint(months=3, value=Measurement.of(2000.0, "ppm")),
        Timepoint(months=6, value=Measurement.of(0.31, "%")),
        Timepoint(months=9, value=Measurement.of(3900.0, "ppm")),
        Timepoint(months=12, value=Measurement.of(0.50, "%")),
    ]
    consistent = _points("%", [(0, 0.10), (3, 0.20), (6, 0.31), (9, 0.39), (12, 0.50)])
    assert estimate_trend(mixed, criterion).months_to_limit == pytest.approx(
        estimate_trend(consistent, criterion).months_to_limit
    )


def test_a_timepoint_measuring_something_else_entirely_is_refused() -> None:
    """A mass among percentages is a transcription error, and the fit would silently absorb it."""
    criterion = AcceptanceCriterion("imp", maximum=Measurement.of(0.50, "area%"))
    wrong = [
        Timepoint(months=0, value=Measurement.of(0.10, "area%")),
        Timepoint(months=6, value=Measurement.of(0.30, "mg")),
        Timepoint(months=12, value=Measurement.of(0.50, "area%")),
    ]
    with pytest.raises(StabilityError, match="not all the same kind"):
        estimate_trend(wrong, criterion)


def test_every_estimate_says_it_is_not_a_shelf_life() -> None:
    """Every estimate, crossing or not, says it is not a shelf life.

    Q1E's shelf life also needs poolability, worst-case batch and model judgment, outside this step.
    """
    crossing = estimate_trend(
        RISING, AcceptanceCriterion("imp", maximum=Measurement.of(0.50, "area%"))
    )
    assert "Q1E" in crossing.note and "never as one" in crossing.note

    slow = _points("area%", [(0, 0.10), (3, 0.12), (6, 0.13), (9, 0.15), (12, 0.16)])
    no_crossing = estimate_trend(
        slow, AcceptanceCriterion("imp", maximum=Measurement.of(0.50, "area%"))
    )
    assert "not a finding that the attribute never reaches the limit" in no_crossing.note


def test_a_flat_attribute_reports_r_squared_as_one_rather_than_zero() -> None:
    """A flat attribute reports r² as 1, not 0, when total variance is zero."""
    flat = _points("area%", [(0, 0.10), (3, 0.10), (6, 0.10), (9, 0.10)])
    estimate = estimate_trend(
        flat, AcceptanceCriterion("imp", maximum=Measurement.of(0.50, "area%"))
    )
    assert estimate.r_squared == 1.0


def test_an_in_control_profile_whose_noise_happens_to_fall_is_not_read_as_falling() -> None:
    """An in-control profile whose noise happens to fall is not read as falling.

    The slope's sign is overridden only when the fit cannot distinguish it from zero. The two series
    are the same measurements in different orders and must agree; asserting both catches a rule that
    always answers "upper".
    """
    criterion = AcceptanceCriterion("imp", maximum=Measurement.of(0.20, "area%"))
    noise_up = _points("area%", [(0, 0.10), (3, 0.11), (6, 0.10), (9, 0.11)])
    noise_down = _points("area%", [(0, 0.11), (3, 0.10), (6, 0.11), (9, 0.10)])

    up = estimate_trend(noise_up, criterion)
    down = estimate_trend(noise_down, criterion)

    assert up.slope_per_month > 0 and down.slope_per_month < 0, (
        "the premise: these two orderings fit slopes of opposite sign"
    )
    assert up.bounded_side == down.bounded_side == "upper"
    assert up.months_to_limit == down.months_to_limit is None


def test_a_real_drift_towards_an_unspecified_side_is_still_refused() -> None:
    """A real drift toward an unspecified side is still refused."""
    with pytest.raises(StabilityError, match="rising.*no maximum"):
        estimate_trend(RISING, AcceptanceCriterion("imp", minimum=Measurement.of(0.01, "area%")))


def test_the_crossing_is_searched_from_the_first_measurement_not_from_time_zero() -> None:
    """The crossing is searched from the first measurement, not from time zero.

    The band is widest far from the data, so a search anchored at `t = 0` reports a compliant batch
    as already failing. The expected numbers are computed from the returned fit.
    """
    late = _points("% w/w", [(24, 98.5), (30, 98.3), (36, 97.4)])
    minimum = AcceptanceCriterion("assay", minimum=Measurement.of(95.0, "% w/w"))
    estimate = estimate_trend(late, minimum)

    assert estimate.months_to_limit is not None
    assert 36.0 < estimate.months_to_limit < 48.0, (
        f"the crossing must land inside Q1E's window, not at zero: {estimate.months_to_limit}"
    )
    assert estimate.intercept + estimate.slope_per_month * 24.0 > 95.0, (
        "the premise: the fitted assay is comfortably in specification at the first pull"
    )
    assert "at the first timepoint" not in estimate.note
    assert "does not support any period" not in estimate.note


def test_a_bound_already_past_the_limit_at_the_first_pull_still_supports_no_period() -> None:
    """A bound already past the limit at the first pull supports no period, naming that pull."""
    failing = _points("% w/w", [(24, 95.4), (30, 94.4), (36, 94.6)])
    estimate = estimate_trend(
        failing, AcceptanceCriterion("assay", minimum=Measurement.of(95.0, "% w/w"))
    )

    assert estimate.months_to_limit == 0.0
    assert "first timepoint (24 months)" in estimate.note, estimate.note
    assert "does not support any period" in estimate.note


def test_the_bisection_bracket_is_the_first_measurement_and_not_only_the_guard() -> None:
    """The bisection bracket starts at the first measurement, not only the guard in front of it.

    The band is not monotone in `t`, so a bracket from 0.0 can step into a spurious early crossing.
    The guard passes on this data, so only the bracket decides the answer.
    """
    late = _points("% w/w", [(24, 99.75), (27, 100.385), (30, 99.74)])
    minimum = AcceptanceCriterion("assay", minimum=Measurement.of(95.0, "% w/w"))
    estimate = estimate_trend(late, minimum)

    assert estimate.months_to_limit is not None
    assert estimate.months_to_limit > 24.0, (
        "a crossing before the first measurement is a root of the band's low-side dip, not a "
        f"period this study supports: {estimate.months_to_limit}"
    )
    assert estimate.intercept + estimate.slope_per_month * 24.0 > 99.0, (
        "the premise: the fitted assay is nowhere near the limit at the first pull"
    )
