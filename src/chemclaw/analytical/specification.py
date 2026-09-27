"""Acceptance criteria, and whether a measured result meets them.

**This is the caller `core/units.Measurement.compare` was written for and did not have.** That
method's docstring says the refusal across dimensions is the point, because "a specification check
written that way passes a batch that is out of limits" — a present-tense claim about a check that
existed nowhere in `src/`, which is the `audit_events.agent` shape
(`D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution`): a control described by the
thing that would have used it. This module is that check, so the sentence becomes true.

**Nothing here decides anything about a batch.** It reports which criteria a set of measurements
meets, which it does not, which were never measured, and which cannot be compared at all — and the
last two are the reason it exists. A naive implementation returns a boolean per criterion and has
nowhere to put "nobody measured this" or "you gave me an area percent against a weight-percent
limit", so both quietly become *pass*.

**Four verdicts, not two**, and the third and fourth carry the value:

- `within` — measured, comparable, inside both bounds.
- `outside` — measured, comparable, past a bound.
- `not_measured` — the criterion has no result. Never a pass, always reported.
- `indeterminate` — a result exists and cannot be compared to the limit: a different dimension, or
  two stated bases that disagree. `Measurement.compare` refuses these by raising, and catching that
  per criterion rather than at the top is what keeps one bad unit from hiding nine good results.

**A `within` verdict can still be an OOS investigation**, and this is the part a bare comparison
cannot express. A result whose *uncertainty* straddles the limit — 0.48 ± 0.05 % against a 0.50 %
maximum — is inside the specification and indistinguishable from outside it at the method's own
precision. That is the case analytical scientists escalate, and reporting it as a plain pass is the
failure worth preventing. `SpecificationResult.limit_within_uncertainty` says so without changing
the verdict, because whether to investigate is a judgment and whether the number is under the limit
is arithmetic.

**Limits are inclusive.** A result exactly at a bound meets it, which is the pharmacopoeial
convention (USP General Notices 7.20: an acceptance criterion is met at the stated limit). Said
here because the alternative is a silent off-by-one on precisely the values people argue about.
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

    A `ValueError` so it reaches a model verbatim through the tool layer: every message names the
    criterion and what is wrong with it, because "invalid specification" tells a chemist nothing
    about which of twelve rows to fix.
    """


@dataclass(frozen=True)
class AcceptanceCriterion:
    """One row of a specification: a named attribute and the band it has to fall in.

    At least one bound is required and both are allowed. A criterion with neither would accept
    every result including a nonsensical one, which is not a loose specification — it is the
    absence of one, spelled so that it reports as a pass.
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
                minimum above its maximum — which no result can satisfy, so every batch fails a
                criterion nobody meant to write that way.
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
    """One criterion's verdict, and everything a reader needs to check it.

    The measurement and the criterion come back beside the verdict on purpose: a verdict a reader
    cannot audit is one they have to trust, and the whole reason this is code rather than a prompt
    is that it can be checked.
    """

    criterion: str
    verdict: Verdict
    #: `None` exactly when the verdict is `not_measured`.
    measured: Measurement | None
    #: True when the result is `within` but its own uncertainty reaches past a bound — inside the
    #: specification and indistinguishable from outside it at the method's precision. Always False
    #: for any other verdict, and for a result that reported no uncertainty, because "nobody stated
    #: a spread" is not "the spread is zero" (`Measurement.uncertainty` makes the same distinction).
    limit_within_uncertainty: bool
    detail: str


def evaluate(
    criteria: list[AcceptanceCriterion], measured: dict[str, Measurement]
) -> list[SpecificationResult]:
    """Score every criterion against the results, in the specification's own order.

    **Every criterion produces a row**, including the ones nothing measured — that is the
    difference between this and a filter over the results, and it is the direction that matters: a
    specification with twelve rows and nine results has three unanswered questions, not nine
    passes.

    A result naming a criterion the specification does not hold is **not** an error and is not
    silently dropped either: it is out of scope for this call, and the caller still has it. Refusing
    would make a broad result set unusable against a narrow specification, which is the ordinary
    case — a release panel run once and scored against several specifications.

    Args:
        criteria: The specification, in the order it should be reported.
        measured: Results by criterion name. Matched **exactly**: a fuzzy match here would silently
            score "water content" against "water", and two attributes whose names differ by a word
            are routinely different tests.

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

    `Measurement.compare` raises `UnitError` across dimensions and across two stated bases that
    disagree. Caught here rather than allowed out, so that one result in the wrong unit produces one
    `indeterminate` row instead of failing the whole evaluation and leaving a caller with nothing —
    the failure mode that makes people wrap the call in a bare `except` and take the pass.
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

    Only asked of a result that reported an uncertainty. An unreported one is "nobody said" rather
    than zero, and treating it as zero would answer this question confidently for every legacy row
    that predates the field — an answer of `False` that means "not asked".

    The interval is built by moving the *value* rather than by comparing the uncertainty to a gap,
    because the two bounds may be in a different unit from the result and `Measurement.compare` is
    what knows how to convert. The uncertainty is in the result's own unit by construction
    (`Measurement`'s contract), so shifting the value keeps everything in one frame.
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
