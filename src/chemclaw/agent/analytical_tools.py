"""The agent's way to ask whether results meet a specification, now and over time.

Two tools. `analytical/specification.py` carries the whole argument — why the verdict is not a
boolean, why a criterion nothing measured is reported rather than dropped, why a result the limit
cannot be compared with is `indeterminate` rather than a pass, and why a `within` result whose
uncertainty crosses a limit says so without changing its verdict.

**Nothing in this file decides anything about a batch.** Whether to release, whether to open an
out-of-specification investigation, whether a method's precision is fit for the limit it is being
held to — all judgment, all a chemist's. What is here is the arithmetic and the four answers it can
give.

The docstring below is deliberately short and the rationale a reader wants is in this header
instead: `D-2026-09-14-a-docstring-is-a-prompt-and-a-comment-is-not`, plus
`tests/test_context_floor.py`'s ratchet, which a long tool description moves.
"""

from __future__ import annotations

from chemclaw.analytical.specification import (
    AcceptanceCriterion,
    SpecificationError,
    evaluate,
)
from chemclaw.analytical.stability import StabilityError, Timepoint, estimate_trend
from chemclaw.core.errors import ChemclawError
from chemclaw.core.tool_registry import tool
from chemclaw.core.units import Measurement, UnitError


def _limit(text: str, *, criterion: str, which: str) -> Measurement | None:
    """`"98.0 % w/w"` as a `Measurement`, or `None` for an empty (one-sided) bound.

    Split on the first space so a unit containing one (`% w/w`) survives, which is exactly the
    spelling that carries a basis — and the basis is what stops an area percent being scored
    against a weight-percent limit.

    Raises:
        ChemclawError: The text is not `<number> <unit>`, or names a unit this system does not
            know. Worded for the model, naming the criterion and which bound, because "invalid
            limit" over twelve rows is not something a caller can act on.
    """
    if not text.strip():
        return None
    number, _, unit = text.strip().partition(" ")
    try:
        return Measurement.of(float(number), unit)
    except (ValueError, UnitError) as bad:
        raise ChemclawError(
            f"the {which} of criterion {criterion!r} is {text!r}, which is not a number and a "
            f"unit this system knows: {bad}"
        ) from bad


@tool
def check_against_specification(criteria: list[dict[str, str]], results: dict[str, str]) -> str:
    """Score measured results against acceptance criteria, refusing what it cannot compare.

    Args:
        criteria: `{"name", "minimum", "maximum"}` per row, bounds `"<number> <unit>"`, `""` for
            one-sided. Inclusive. `% w/w`, `area%`, `mol%`, `ppm` are distinct and a mismatch is
            refused, not converted.
        results: Value per criterion name, matched exactly: `"<number> <unit>"` or
            `"<number> ± <spread> <unit>"`.

    Returns:
        A line per criterion — `within`, `outside`, `not_measured`, or `indeterminate` when result
        and limit are incomparable. A `within` whose uncertainty reaches the limit is flagged: a
        finding to report, not a pass.

    Raises:
        ChemclawError: An unnamed or unbounded criterion, bounds that cross, or a limit that is not
            a number and a unit.
    """
    parsed: list[AcceptanceCriterion] = []
    for row in criteria:
        name = row.get("name", "")
        try:
            parsed.append(
                AcceptanceCriterion(
                    name=name,
                    minimum=_limit(row.get("minimum", ""), criterion=name, which="minimum"),
                    maximum=_limit(row.get("maximum", ""), criterion=name, which="maximum"),
                )
            )
        except SpecificationError as refusal:
            raise ChemclawError(str(refusal)) from refusal

    measured: dict[str, Measurement] = {}
    for name, text in results.items():
        value, _, rest = text.strip().partition("±")
        if rest:
            spread, _, unit = rest.strip().partition(" ")
            measured[name] = _measurement(name, value, unit, spread)
        else:
            number, _, unit = text.strip().partition(" ")
            measured[name] = _measurement(name, number, unit, "")

    lines = []
    for scored in evaluate(parsed, measured):
        flag = " [uncertainty reaches the limit]" if scored.limit_within_uncertainty else ""
        lines.append(f"{scored.criterion}: {scored.verdict}{flag} — {scored.detail}")
    return "\n".join(lines)


def _measurement(name: str, value: str, unit: str, spread: str) -> Measurement:
    """One result, refusing anything that is not a number and a unit this system knows.

    Raises:
        ChemclawError: Worded for the model and naming the criterion, because a result that cannot
            be parsed must not silently become a criterion nobody measured — which is the one way
            this tool could turn a bad input into something that reads like a clean specification.
    """
    try:
        return Measurement.of(
            float(value.strip()), unit.strip(), uncertainty=float(spread) if spread else None
        )
    except (ValueError, UnitError) as bad:
        raise ChemclawError(
            f"the result for {name!r} is not a number and a unit this system knows: {bad}"
        ) from bad


@tool
def estimate_stability_trend(criterion: dict[str, str], timepoints: list[dict[str, str]]) -> str:
    """Fit an attribute against time and say where its 95% bound meets the limit.

    **Not a shelf life or a retest period.** ICH Q1E derives those from a procedure this is one
    step of — poolability across batches, the worst-case batch, and whether a linear model fits at
    all are all outside it. Never quote the number as one.

    Args:
        criterion: `{"name", "minimum", "maximum"}`, bounds `"<number> <unit>"`, `""` for one-sided.
            The side used follows the drift, so only the bound the attribute approaches is needed.
        timepoints: `{"months", "value"}` per pull, at least three at two or more distinct times.
            `value` is `"<number> <unit>"`; units of one dimension are reconciled.

    Returns:
        The slope per month, r², which bound was used, and the months at which it reaches the limit
        — or that it does not within twice the observed period (Q1E's extrapolation limit, capped
        at twelve months beyond), which is a statement about the data's reach, not about the
        attribute.

    Raises:
        ChemclawError: Too few or single-time points, units that cannot be reconciled, or a
            criterion with no bound on the side the attribute is heading.
    """
    name = criterion.get("name", "")
    try:
        parsed = AcceptanceCriterion(
            name=name,
            minimum=_limit(criterion.get("minimum", ""), criterion=name, which="minimum"),
            maximum=_limit(criterion.get("maximum", ""), criterion=name, which="maximum"),
        )
    except SpecificationError as refusal:
        raise ChemclawError(str(refusal)) from refusal

    points = []
    for row in timepoints:
        months = row.get("months", "")
        try:
            elapsed = float(months)
        except ValueError as bad:
            raise ChemclawError(f"timepoint {months!r} is not a number of months") from bad
        number, _, unit = row.get("value", "").strip().partition(" ")
        points.append(Timepoint(months=elapsed, value=_measurement(name, number, unit, "")))

    try:
        estimate = estimate_trend(points, parsed)
    except StabilityError as refusal:
        raise ChemclawError(str(refusal)) from refusal

    reach = (
        f"{estimate.months_to_limit:.1f} months"
        if estimate.months_to_limit is not None
        else "not within the permitted extrapolation"
    )
    return (
        f"{name}: slope {estimate.slope_per_month:+.4g} per month, r2 {estimate.r_squared:.3f}, "
        f"{estimate.bounded_side} 95% bound, observed to {estimate.observed_months:g} months.\n"
        f"Reaches the limit: {reach}.\n{estimate.note}"
    )
