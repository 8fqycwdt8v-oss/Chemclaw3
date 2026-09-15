"""Where a trending attribute meets its limit, and the several ways that question has no answer.

The arithmetic is a least-squares line and a t-interval, and almost none of this file is about that.
What it drives is the cases where returning a number would be worse than returning nothing: an
extrapolation past what the data supports, an attribute with no drift to take a direction from, and
a criterion that states no limit on the side the attribute is heading.

Every fixture is a plausible stability dataset rather than a generated one, because the failure this
module can produce is a number that looks like a shelf life — and a fixture nobody would recognise
as a real study makes that failure invisible.
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
    """The side is derived from the slope, and getting it backwards lengthens the estimate.

    That is the error direction that matters: bounding a rising impurity from *below* puts the
    confidence band on the side the attribute is moving away from, so the crossing comes out later
    than the data supports — an estimate that is wrong in the optimistic direction, which is the
    one nobody catches by eye.
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
    """The whole point of a confidence bound, asserted as the inequality rather than as a figure.

    A fit that reported the *line's* crossing would be an estimate with no confidence in it at all,
    and it would always be the longer number. Checked against the line computed here from the
    returned slope and intercept, so the assertion cannot drift with the fixture.
    """
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
    """The failure worth preventing: arithmetic will produce 60 months from six months of data.

    ICH Q1E §2.4 allows extrapolation to at most twice the observed period and never more than 12
    months beyond it. A slow-moving attribute's line crosses far outside that, and returning the
    number would hand a reader a figure whose confidence band has stopped meaning anything.

    Asserted with the crossing established as real first — the fitted line *does* reach the limit —
    so this is the window refusing it, not the attribute failing to get there.
    """
    slow = _points("area%", [(0, 0.10), (3, 0.12), (6, 0.13), (9, 0.15), (12, 0.16)])
    criterion = AcceptanceCriterion("imp", maximum=Measurement.of(0.50, "area%"))
    estimate = estimate_trend(slow, criterion)

    line_reaches = (0.50 - estimate.intercept) / estimate.slope_per_month
    assert line_reaches > 24.0, "fixture no longer crosses outside the window; the test is inert"
    assert estimate.months_to_limit is None
    assert "Q1E" in estimate.note and "24" in estimate.note


def test_the_window_is_twelve_months_beyond_rather_than_twice_when_that_is_shorter() -> None:
    """Q1E's limit is the *lesser* of the two rules, and a long study is where they differ.

    At 12 months observed the two agree (24 either way), which is why the fixtures above cannot
    tell them apart. At 24 months observed they do not: twice is 48, twelve-beyond is 36, and an
    implementation taking only the first would extrapolate a year further than Q1E permits.
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
    """A defect this module shipped with, caught by driving a real stability shape.

    Every timepoint identical is what a well-behaved impurity at the reporting threshold really
    produces, and it fits a slope of exactly 0.0. `slope > 0` classified that as *falling*, looked
    for a minimum, and refused a specification that quite correctly stated only a maximum.

    With no drift the confidence band still widens, so a bound does move and the only question is
    which. Taken from the criterion: the side it states, or the nearer one when it states both.
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
    """Returning "never reaches it" would be an answer about a different question.

    An impurity rising against a specification that sets only a minimum has nothing in front of it.
    The honest answer names that, because a `None` crossing already means "not within the window"
    and overloading it with "there is no limit at all" would merge two very different situations.
    """
    with pytest.raises(StabilityError, match="rising.*no maximum"):
        estimate_trend(RISING, AcceptanceCriterion("imp", minimum=Measurement.of(0.01, "area%")))
    with pytest.raises(StabilityError, match="falling.*no minimum"):
        estimate_trend(
            FALLING, AcceptanceCriterion("assay", maximum=Measurement.of(102.0, "% w/w"))
        )


def test_too_few_timepoints_are_refused_because_the_band_would_be_infinitely_narrow() -> None:
    """Two points fit a line with zero residual degrees of freedom — and zero uncertainty.

    Refused rather than fitted, because the failure is silent: the arithmetic runs, the residual
    standard error is 0, and the bound comes back sitting exactly on the fitted line. A caller
    would get their most confident-looking answer from their least informative data.
    """
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
    """A study pulled in ppm and reported in percent is an ordinary mess, not an error.

    `Measurement.to` is what reconciles them and the criterion is converted the same way, so the
    regression runs in one frame. Asserted against the same data expressed wholly in percent, which
    is the property — the mixed study must produce the *same* estimate, not merely produce one.
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
    """The one sentence that must survive every branch, because the number reads like one.

    A retest period or shelf life is what ICH Q1E derives from a procedure this implements one step
    of — the poolability testing across batches, the worst-case batch and the judgment about the
    model are all outside it. Asserted across the crossing and non-crossing branches together,
    since a note is easy to write on the path somebody tested and forget on the other.
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
    """0/0 has to be decided, and calling it a failed fit would libel the best possible data.

    Total variance is zero, so `1 - residual/total` is undefined. The model explains everything
    there is to explain — which is nothing — and a stability study that produces this has gone
    perfectly. Reporting 0.0 would read as "the fit explains none of the variation".
    """
    flat = _points("area%", [(0, 0.10), (3, 0.10), (6, 0.10), (9, 0.10)])
    estimate = estimate_trend(
        flat, AcceptanceCriterion("imp", maximum=Measurement.of(0.50, "area%"))
    )
    assert estimate.r_squared == 1.0
