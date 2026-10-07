"""Whether a measured result meets an acceptance criterion, and what happens when it cannot say.

Most tests drive the two verdicts a boolean cannot hold — a criterion nothing measured, and a
result in a unit the limit cannot be compared with — since a naive implementation scores both
as a pass. This module is `Measurement.compare`'s production caller.
"""

from __future__ import annotations

import pytest

from chemclaw.analytical.specification import (
    AcceptanceCriterion,
    SpecificationError,
    evaluate,
)
from chemclaw.core.units import Measurement


def _m(value: float, unit: str, *, uncertainty: float | None = None) -> Measurement:
    """A measurement, spelled the way a certificate of analysis writes it."""
    return Measurement.of(value, unit, uncertainty=uncertainty)


def test_a_result_inside_both_bounds_is_within_and_an_exact_limit_still_meets_it() -> None:
    """Limits are inclusive (the pharmacopoeial convention), asserted at both ends."""
    criterion = AcceptanceCriterion(
        name="assay", minimum=_m(98.0, "% w/w"), maximum=_m(102.0, "% w/w")
    )
    for value in (98.0, 100.0, 102.0):
        (result,) = evaluate([criterion], {"assay": _m(value, "% w/w")})
        assert result.verdict == "within", f"{value} % w/w was scored {result.verdict}"


def test_a_criterion_nothing_measured_is_reported_and_is_never_a_pass() -> None:
    """A criterion nothing measured is reported as `not_measured` and is never a pass.

    Every criterion produces a row, in the specification's order; a filter over results would hide
    the unanswered ones.
    """
    specification = [
        AcceptanceCriterion(name="assay", minimum=_m(98.0, "% w/w")),
        AcceptanceCriterion(name="water content", maximum=_m(0.5, "% w/w")),
        AcceptanceCriterion(name="residual solvent", maximum=_m(500.0, "ppm")),
    ]
    results = evaluate(specification, {"assay": _m(99.5, "% w/w")})
    assert [r.criterion for r in results] == [c.name for c in specification]
    assert [r.verdict for r in results] == ["within", "not_measured", "not_measured"]
    assert all(r.measured is None for r in results if r.verdict == "not_measured")


def test_a_result_the_limit_cannot_be_compared_with_is_indeterminate_not_a_pass() -> None:
    """An incomparable result is `indeterminate`, not a pass.

    Area percent against a weight-percent limit shares a dimension, so only the basis check sees it;
    mass against percent reaches the other branch of `compare`.
    """
    (basis,) = evaluate(
        [AcceptanceCriterion(name="impurity", maximum=_m(0.50, "% w/w"))],
        {"impurity": _m(0.30, "area%")},
    )
    assert basis.verdict == "indeterminate", (
        "an area percent was compared against a weight-percent limit and scored; the basis check "
        "in Measurement.compare is not reaching this caller"
    )
    assert "area" in basis.detail and "w/w" in basis.detail

    (dimension,) = evaluate(
        [AcceptanceCriterion(name="assay", minimum=_m(98.0, "% w/w"))],
        {"assay": _m(99.0, "mg")},
    )
    assert dimension.verdict == "indeterminate"


def test_one_incomparable_result_does_not_cost_the_verdicts_of_the_others() -> None:
    """One incomparable result does not cost the other criteria their verdicts.

    The refusal is caught per criterion, so the other rows survive.
    """
    specification = [
        AcceptanceCriterion(name="assay", minimum=_m(98.0, "% w/w")),
        AcceptanceCriterion(name="impurity", maximum=_m(0.50, "% w/w")),
        AcceptanceCriterion(name="water content", maximum=_m(0.5, "% w/w")),
    ]
    results = evaluate(
        specification,
        {
            "assay": _m(99.5, "% w/w"),
            "impurity": _m(0.30, "area%"),
            "water content": _m(0.9, "% w/w"),
        },
    )
    assert [r.verdict for r in results] == ["within", "indeterminate", "outside"]


def test_a_result_within_the_limits_whose_uncertainty_crosses_one_says_so() -> None:
    """A result within the limits whose uncertainty crosses one is flagged.

    The verdict stays `within`; the flag marks it as an investigation rather than a release.
    """
    (flagged,) = evaluate(
        [AcceptanceCriterion(name="impurity", maximum=_m(0.50, "area%"))],
        {"impurity": _m(0.48, "area%", uncertainty=0.05)},
    )
    assert flagged.verdict == "within"
    assert flagged.limit_within_uncertainty is True
    assert "precision" in flagged.detail

    (comfortable,) = evaluate(
        [AcceptanceCriterion(name="impurity", maximum=_m(0.50, "area%"))],
        {"impurity": _m(0.20, "area%", uncertainty=0.05)},
    )
    assert comfortable.verdict == "within"
    assert comfortable.limit_within_uncertainty is False


def test_an_unstated_uncertainty_is_not_read_as_zero() -> None:
    """An unstated uncertainty is not read as zero, asserted on a value exactly on the limit."""
    (result,) = evaluate(
        [AcceptanceCriterion(name="impurity", maximum=_m(0.50, "area%"))],
        {"impurity": _m(0.50, "area%")},
    )
    assert result.verdict == "within"
    assert result.limit_within_uncertainty is False


def test_the_flag_is_only_ever_set_on_a_result_that_is_within() -> None:
    """The straddle flag is only ever set on a `within` result, checked across all four verdicts."""
    specification = [
        AcceptanceCriterion(name="a", maximum=_m(0.50, "area%")),
        AcceptanceCriterion(name="b", maximum=_m(0.50, "area%")),
        AcceptanceCriterion(name="c", maximum=_m(0.50, "% w/w")),
        AcceptanceCriterion(name="d", maximum=_m(0.50, "area%")),
    ]
    results = evaluate(
        specification,
        {
            "a": _m(0.48, "area%", uncertainty=0.05),
            "b": _m(0.90, "area%", uncertainty=0.05),
            "c": _m(0.30, "area%", uncertainty=0.05),
        },
    )
    by_verdict = {r.verdict: r for r in results}
    assert set(by_verdict) == {"within", "outside", "indeterminate", "not_measured"}
    assert by_verdict["within"].limit_within_uncertainty is True
    for verdict in ("outside", "indeterminate", "not_measured"):
        assert by_verdict[verdict].limit_within_uncertainty is False, (
            f"the straddle flag is set on a {verdict!r} row, where it has no meaning"
        )


def test_the_limits_may_be_in_a_different_unit_from_the_result() -> None:
    """The limits may be in a different unit from the result, for both comparison and straddle.

    The straddle check moves the result's value in its own frame, so it converts correctly.
    """
    criterion = AcceptanceCriterion(name="residual solvent", maximum=_m(500.0, "ppm"))
    (comfortable,) = evaluate([criterion], {"residual solvent": _m(0.02, "%")})
    assert comfortable.verdict == "within"
    assert comfortable.limit_within_uncertainty is False

    (straddling,) = evaluate([criterion], {"residual solvent": _m(0.048, "%", uncertainty=0.005)})
    assert straddling.verdict == "within"
    assert straddling.limit_within_uncertainty is True, (
        "480 ± 50 ppm expressed as a percentage did not register against a 500 ppm limit, so the "
        "straddle check is not converting"
    )


def test_a_specification_that_cannot_be_evaluated_is_refused_when_it_is_written() -> None:
    """Malformed criteria are refused at construction rather than scored.

    A criterion with no bounds accepts everything, and one with min above max fails every batch.
    """
    with pytest.raises(SpecificationError, match="name what it measures"):
        AcceptanceCriterion(name="   ", maximum=_m(1.0, "% w/w"))
    with pytest.raises(SpecificationError, match="neither a minimum nor a maximum"):
        AcceptanceCriterion(name="assay")
    with pytest.raises(SpecificationError, match="above its maximum"):
        AcceptanceCriterion(name="assay", minimum=_m(102.0, "% w/w"), maximum=_m(98.0, "% w/w"))
    with pytest.raises(SpecificationError, match="cannot be compared"):
        AcceptanceCriterion(name="assay", minimum=_m(98.0, "% w/w"), maximum=_m(102.0, "mg"))


def test_a_result_naming_no_criterion_is_out_of_scope_rather_than_an_error() -> None:
    """A result naming no criterion is out of scope, not an error, and is not scored.

    Asserted as the row count, so it cannot be folded silently into a pass.
    """
    results = evaluate(
        [AcceptanceCriterion(name="assay", minimum=_m(98.0, "% w/w"))],
        {"assay": _m(99.0, "% w/w"), "appearance": _m(1.0, "")},
    )
    assert [r.criterion for r in results] == ["assay"]


def test_measurement_compare_now_has_the_production_caller_its_docstring_describes() -> None:
    """`Measurement.compare` has a production caller in `src/`.

    Scanned for rather than asserted behaviourally, because `_score` comparing floats itself would
    pass every test above.
    """
    from pathlib import Path

    source = Path("src/chemclaw/analytical/specification.py").read_text(encoding="utf-8")
    assert ".compare(" in source, (
        "the specification check no longer calls Measurement.compare, so the refusals across "
        "dimensions and bases are no longer this module's; either restore the call or correct "
        "that method's docstring, which claims this caller exists"
    )
