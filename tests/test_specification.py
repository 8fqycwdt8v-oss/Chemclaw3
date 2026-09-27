"""Whether a measured result meets an acceptance criterion, and what happens when it cannot say.

The verdicts that matter here are the two a boolean cannot hold. A criterion nothing measured and a
result in a unit the limit cannot be compared with are both "no answer", and every naive
implementation of this spells both of them *pass* — so most of this file drives those, not the
arithmetic.

`Measurement.compare` is what makes the refusals possible and it had **no production caller** until
this module: its own docstring promised that a specification check written against it would not pass
a batch that is out of limits, which was a claim about a check that did not exist. These tests are
what make that sentence checkable.
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
    """Inclusive limits, which is the pharmacopoeial convention and a silent off-by-one otherwise.

    The exact-limit case is asserted at *both* ends, because the two are different comparisons and
    an implementation can easily get one right and the other wrong — and the values people argue
    about are precisely the ones sitting on a bound.
    """
    criterion = AcceptanceCriterion(
        name="assay", minimum=_m(98.0, "% w/w"), maximum=_m(102.0, "% w/w")
    )
    for value in (98.0, 100.0, 102.0):
        (result,) = evaluate([criterion], {"assay": _m(value, "% w/w")})
        assert result.verdict == "within", f"{value} % w/w was scored {result.verdict}"


def test_a_criterion_nothing_measured_is_reported_and_is_never_a_pass() -> None:
    """The failure this module exists to prevent, and the one a filter over results cannot express.

    A specification with three rows and one result has two unanswered questions. Scored as a filter
    — iterate the results, compare each — it has one pass and nothing else, and the batch looks
    tested. So the assertion is that every criterion produces a row, in the specification's order.
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
    """An area percent against a weight-percent limit: same unit, same dimension, different fact.

    This is the case `Measurement.compare`'s basis check exists for, reaching a caller for the first
    time. A dimension check alone cannot see it — both are percent — so an implementation that only
    guarded dimensions would score it `within` and release the batch.

    The mass-against-percent arm is beside it because the two refusals come from different branches
    of `compare`, and a test that drove only one would leave the other to be believed.
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
    """Why the refusal is caught per criterion rather than allowed out of `evaluate`.

    Raising at the top would leave a caller with nothing for a twelve-row specification because one
    row was entered in the wrong unit — and a caller with nothing is a caller who wraps the call in
    a bare `except` and takes the pass. That is the failure this shape avoids, so it is asserted as
    the other rows *surviving* rather than as the exception not being raised.
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
    """The case a bare comparison cannot express, and the one an analyst escalates.

    0.48 ± 0.05 % against a 0.50 % maximum is inside the specification and indistinguishable from
    outside it at the method's own precision. The verdict stays `within` — whether the number is
    under the limit is arithmetic — and the flag is what carries the fact that makes it an
    investigation rather than a release.
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
    """An unstated spread is not a zero one, which is `Measurement`'s own distinction.

    Treating `None` as 0.0 would answer the straddle question confidently for every result that
    reported no spread — including every row written before the field existed — and the answer would
    be `False` meaning "not asked". Asserted on a value sitting *exactly* on the limit, where a zero
    spread and an unknown one are most easily confused.
    """
    (result,) = evaluate(
        [AcceptanceCriterion(name="impurity", maximum=_m(0.50, "area%"))],
        {"impurity": _m(0.50, "area%")},
    )
    assert result.verdict == "within"
    assert result.limit_within_uncertainty is False


def test_the_flag_is_only_ever_set_on_a_result_that_is_within() -> None:
    """A result already outside its limit does not also need telling that its spread crosses one.

    The flag means "inside, and arguably not" — a meaning it loses entirely if it can also appear
    beside `outside`, `not_measured` or `indeterminate`. Checked across all four verdicts in one
    evaluation so a future branch that forgets to set it False is caught.
    """
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
    """A ppm limit against a percent result is the ordinary case, not an error.

    `Measurement.compare` converts, and the straddle check has to convert too — it is built by
    moving the result's *value*, in the result's own frame, precisely so that a limit in another
    unit still works. Asserted here because a straddle check written as "compare the uncertainty to
    the gap" would be numerically wrong across units and right in every same-unit test.
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
    """Four malformed criteria, each refused at construction rather than scored.

    Refused early on purpose: a criterion with no bounds accepts every result including one in the
    wrong unit, and a criterion whose minimum exceeds its maximum fails every batch. Both would be
    reported as ordinary verdicts by a check that validated nothing, and a specification nobody can
    satisfy reads as a manufacturing problem rather than as a typo.
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
    """A release panel run once and scored against several specifications is the ordinary case.

    Refusing an extra result would make a broad panel unusable against a narrow specification — so
    the extra is not scored and not reported here, and the caller still holds it. Asserted as the
    row count, because "ignored" and "silently folded into a pass" look the same from a verdict
    list that is not counted.
    """
    results = evaluate(
        [AcceptanceCriterion(name="assay", minimum=_m(98.0, "% w/w"))],
        {"assay": _m(99.0, "% w/w"), "appearance": _m(1.0, "")},
    )
    assert [r.criterion for r in results] == ["assay"]


def test_measurement_compare_now_has_the_production_caller_its_docstring_describes() -> None:
    """The absence test, in the shape `D-2026-08-26` established for a claim with no producer.

    `Measurement.compare`'s docstring says the cross-dimension refusal matters because "a
    specification check written that way passes a batch that is out of limits". That was a claim
    about a check with no caller in `src/` — every use of `compare` was a test calling it directly,
    which is the `reject_widening` shape CLAUDE.md records as "a claim that a control exists".

    This scans for the caller rather than asserting behaviour, because the behavioural tests above
    would all still pass if `_score` were rewritten to compare floats itself — and the docstring's
    claim would be false again, silently.
    """
    from pathlib import Path

    source = Path("src/chemclaw/analytical/specification.py").read_text(encoding="utf-8")
    assert ".compare(" in source, (
        "the specification check no longer calls Measurement.compare, so the refusals across "
        "dimensions and bases are no longer this module's; either restore the call or correct "
        "that method's docstring, which claims this caller exists"
    )
