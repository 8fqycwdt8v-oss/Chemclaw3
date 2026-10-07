"""Acceptance criteria, and whether a measured result meets them.

Reports, per criterion, one of four verdicts and decides nothing about the batch:

- `within` — measured, comparable, inside both bounds.
- `outside` — measured, comparable, past a bound.
- `not_measured` — no result. Never a pass, always reported.
- `indeterminate` — a result that cannot be compared to the limit (different dimension, or
  disagreeing bases). Caught per criterion so one bad unit does not hide the other results.

A `within` result whose own uncertainty straddles a bound is flagged by
`SpecificationResult.limit_within_uncertainty` without changing the verdict: whether to investigate
is a judgment. Limits are inclusive (USP General Notices 7.20).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from chemclaw.core.units import Measurement, UnitError

#: What a criterion earns. `not_measured` and `indeterminate` exist so that "no answer" cannot be
#: spelled the same way as "pass"; see the module docstring.
Verdict = Literal["within", "outside", "not_measured", "indeterminate"]


class SpecificationError(ValueError):
    """A specification that cannot be evaluated as written.

    A `ValueError` so it reaches a model verbatim; every message names the criterion and the
    problem.
    """


@dataclass(frozen=True)
class AcceptanceCriterion:
    """One row of a specification: a named attribute and the band it has to fall in.

    At least one bound is required: a criterion with neither would accept every result.
    """

    #: What is being measured, as the specification writes it — "assay", "water content", "largest
    #: unspecified impurity". Matched against a result's name exactly; see `evaluate`.
    name: str
    #: Inclusive lower bound, or `None` for one-sided.
    minimum: Measurement | None = None
    #: Inclusive upper bound, or `None` for one-sided.
    maximum: Measurement | None = None

    def __post_init__(self) -> None:
        """Refuse a nameless criterion, an unbounded one, or bounds that cross.

        Raises:
            SpecificationError: No name, neither bound, bounds of different dimensions, or a
                minimum above its maximum.
        """
        if not self.name.strip():
            raise SpecificationError("an acceptance criterion must name what it measures")
        if self.minimum is None and self.maximum is None:
            raise SpecificationError(
                f"criterion {self.name!r} states neither a minimum nor a maximum, so it would "
                "accept any result — including one in the wrong unit"
            )
        if self.minimum is not None and self.maximum is not None:
            try:
                crossed = self.minimum.compare(self.maximum) > 0
            except UnitError as mismatch:
                raise SpecificationError(
                    f"criterion {self.name!r} has bounds that cannot be compared: {mismatch}"
                ) from mismatch
            if crossed:
                raise SpecificationError(
                    f"criterion {self.name!r} has a minimum ({self.minimum}) above its maximum "
                    f"({self.maximum}), which no result can satisfy"
                )


@dataclass(frozen=True)
class SpecificationResult:
    """One criterion's verdict, with the measurement and criterion so a reader can audit it."""

    criterion: str
    verdict: Verdict
    #: `None` exactly when the verdict is `not_measured`.
    measured: Measurement | None
    #: True when the result is `within` but its uncertainty reaches past a bound. Always False for
    #: any other verdict and when no uncertainty was reported ("not stated" is not "zero").
    limit_within_uncertainty: bool
    detail: str


def evaluate(
    criteria: list[AcceptanceCriterion], measured: dict[str, Measurement]
) -> list[SpecificationResult]:
    """Score every criterion against the results, in the specification's own order.

    Every criterion produces a row, including unmeasured ones. A result naming a criterion the
    specification does not hold is out of scope and ignored, so a broad result set can be scored
    against a narrow specification.

    Args:
        criteria: The specification, in the order it should be reported.
        measured: Results by criterion name, matched exactly: "water content" and "water" are
            different tests.

    Returns:
        One `SpecificationResult` per criterion, in the order given.
    """
    results: list[SpecificationResult] = []
    for criterion in criteria:
        result = measured.get(criterion.name)
        if result is None:
            results.append(
                SpecificationResult(
                    criterion=criterion.name,
                    verdict="not_measured",
                    measured=None,
                    limit_within_uncertainty=False,
                    detail="no result was supplied for this criterion",
                )
            )
            continue
        results.append(_score(criterion, result))
    return results


def _score(criterion: AcceptanceCriterion, result: Measurement) -> SpecificationResult:
    """One measured criterion, with the comparison's own refusal turned into a verdict.

    `Measurement.compare` raises `UnitError` across dimensions or disagreeing bases; that becomes
    one `indeterminate` row rather than failing the whole evaluation.
    """
    breaches: list[str] = []
    try:
        if criterion.minimum is not None and result.compare(criterion.minimum) < 0:
            breaches.append(f"below the minimum of {criterion.minimum}")
        if criterion.maximum is not None and result.compare(criterion.maximum) > 0:
            breaches.append(f"above the maximum of {criterion.maximum}")
    except UnitError as mismatch:
        return SpecificationResult(
            criterion=criterion.name,
            verdict="indeterminate",
            measured=result,
            limit_within_uncertainty=False,
            detail=f"the result cannot be compared with the limit: {mismatch}",
        )

    if breaches:
        return SpecificationResult(
            criterion=criterion.name,
            verdict="outside",
            measured=result,
            limit_within_uncertainty=False,
            detail=f"{result} is " + " and ".join(breaches),
        )
    straddles = _uncertainty_reaches_a_bound(criterion, result)
    return SpecificationResult(
        criterion=criterion.name,
        verdict="within",
        measured=result,
        limit_within_uncertainty=straddles,
        detail=(
            f"{result} is within the limits, but its stated uncertainty reaches past one of them — "
            "the result is inside the specification and indistinguishable from outside it at this "
            "method's precision"
            if straddles
            else f"{result} is within the limits"
        ),
    )


def _uncertainty_reaches_a_bound(criterion: AcceptanceCriterion, result: Measurement) -> bool:
    """Does the result's own spread cross a limit it is otherwise inside?

    Only asked when an uncertainty was reported. The value is shifted by the uncertainty (which is
    in the result's own unit) and compared via `Measurement.compare`, which handles unit conversion
    of the bounds.
    """
    if result.uncertainty is None:
        return False
    spread = abs(result.uncertainty)
    low = Measurement(
        value=result.value - spread, unit=result.unit, uncertainty=None, basis=result.basis
    )
    high = Measurement(
        value=result.value + spread, unit=result.unit, uncertainty=None, basis=result.basis
    )
    if criterion.minimum is not None and low.compare(criterion.minimum) < 0:
        return True
    return criterion.maximum is not None and high.compare(criterion.maximum) > 0
