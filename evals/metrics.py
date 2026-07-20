"""Seed scientific metrics (plan steps 2b.3, 2b.5 / 1d.6).

Deliberately few, per the plan: green-chemistry **E-factor** and **Process Mass
Intensity** (mass-efficiency of a process), **prediction accuracy** against a held-out
reference, and **BO regret** (optimization progress — the metric plan step 1d.6 asks
Phase 1d to register). Each is a pure function of an `EvalCase`; pass/fail thresholds
come from the config, never the code (G3). Importing this module registers them.
"""

from typing import Any

from chemclaw.config import settings
from evals.metric import EvalCase, MetricError, MetricResult, metric


class _ProcessMasses:
    """The mass balance a green-chemistry metric reads from a case's output.

    Kept a plain parser (not a Pydantic model) so a case output carrying extra keys
    for other metrics is accepted; only the mass fields are read and validated here.
    """

    def __init__(self, output: dict[str, Any]) -> None:
        """Validate and hold the input masses and product mass (kg)."""
        self.inputs = _positive_masses(output.get("input_masses_kg"))
        self.product = _positive_scalar(output.get("product_mass_kg"), "product_mass_kg")


def _positive_masses(raw: Any) -> list[float]:
    """Coerce a non-empty list of non-negative input masses, else `MetricError`."""
    if not isinstance(raw, (list, tuple)) or not raw:
        raise MetricError("output.input_masses_kg must be a non-empty list of masses")
    masses = [float(x) for x in raw]
    if any(m < 0 for m in masses):
        raise MetricError("output.input_masses_kg must be non-negative")
    return masses


def _positive_scalar(raw: Any, field: str) -> float:
    """Coerce a strictly positive scalar (a product mass divides), else `MetricError`."""
    if raw is None:
        raise MetricError(f"output.{field} is required")
    value = float(raw)
    if value <= 0:
        raise MetricError(f"output.{field} must be > 0")
    return value


@metric("e_factor")
def e_factor(case: EvalCase) -> MetricResult:
    """Green-chemistry E-factor: kg waste per kg product (Sheldon).

    Waste is total input mass minus product mass. Lower is better; the pass limit is
    `eval_efactor_max`. Computed from the output mass balance alone (no reference).
    """
    masses = _ProcessMasses(case.output)
    waste = sum(masses.inputs) - masses.product
    value = waste / masses.product
    return MetricResult(
        metric="e_factor",
        value=value,
        unit="kg/kg",
        passed=value <= settings.eval_efactor_max,
        provenance=(
            f"E-factor = waste {waste:.4g} kg / product {masses.product:.4g} kg "
            f"(total input {sum(masses.inputs):.4g} kg); limit {settings.eval_efactor_max}"
        ),
    )


@metric("pmi")
def process_mass_intensity(case: EvalCase) -> MetricResult:
    """Process Mass Intensity: total input mass per kg product (PMI = E-factor + 1).

    Lower is better; the pass limit is `eval_pmi_max`. Computed from the output mass
    balance alone (no reference).
    """
    masses = _ProcessMasses(case.output)
    total_input = sum(masses.inputs)
    value = total_input / masses.product
    return MetricResult(
        metric="pmi",
        value=value,
        unit="kg/kg",
        passed=value <= settings.eval_pmi_max,
        provenance=(
            f"PMI = total input {total_input:.4g} kg / product {masses.product:.4g} kg; "
            f"limit {settings.eval_pmi_max}"
        ),
    )


@metric("prediction_error")
def prediction_error(case: EvalCase) -> MetricResult:
    """Absolute error of a predicted value against a held-out reference.

    Reads `output.predicted` and `reference.actual` (same unit). The prediction passes
    when the error is within `eval_prediction_tolerance`. Requires a reference (G4).
    """
    if case.reference is None:
        raise MetricError("prediction_error needs a reference with `actual`")
    predicted = _scalar(case.output.get("predicted"), "output.predicted")
    actual = _scalar(case.reference.get("actual"), "reference.actual")
    value = abs(predicted - actual)
    unit = case.output.get("unit")
    return MetricResult(
        metric="prediction_error",
        value=value,
        unit=str(unit) if unit is not None else None,
        passed=value <= settings.eval_prediction_tolerance,
        provenance=(
            f"|predicted {predicted:.4g} - actual {actual:.4g}| = {value:.4g}; "
            f"tolerance {settings.eval_prediction_tolerance}"
        ),
    )


@metric("bo_regret")
def bo_regret(case: EvalCase) -> MetricResult:
    """Optimization regret: distance from the best value found to the known optimum.

    Plan step 1d.6 — Phase 1d's registered scientific metric. Reads `output.best_value`
    and `reference.optimum`, with `output.direction` ("maximize"/"minimize") giving the
    sign so regret is non-negative. It is a progress metric with no pass threshold
    (`passed` is None): scale is problem-specific, so a report cites it, not gates on it.
    """
    if case.reference is None:
        raise MetricError("bo_regret needs a reference with `optimum`")
    best = _scalar(case.output.get("best_value"), "output.best_value")
    optimum = _scalar(case.reference.get("optimum"), "reference.optimum")
    direction = case.output.get("direction", "maximize")
    if direction == "maximize":
        value = optimum - best
    elif direction == "minimize":
        value = best - optimum
    else:
        raise MetricError(f"output.direction must be maximize/minimize, got {direction!r}")
    return MetricResult(
        metric="bo_regret",
        value=value,
        unit=None,
        passed=None,
        provenance=(
            f"regret = |optimum {optimum:.4g} - best {best:.4g}| = {value:.4g} ({direction})"
        ),
    )


def _scalar(raw: Any, field: str) -> float:
    """Coerce a required numeric field, else a `MetricError` naming it (G4)."""
    if raw is None:
        raise MetricError(f"{field} is required")
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise MetricError(f"{field} must be a number, got {raw!r}") from exc
