"""Seed scientific metrics.

(Plural `metrics` = the concrete scored functions; the interface and registry are in
`chemclaw.evals.metric`.)

Green-chemistry **E-factor** and **Process Mass Intensity**, **prediction error** against a held-out
reference, **BO regret**, and set-based precision/recall/F1. Each is a pure function of an
`EvalCase`; thresholds come from config. Importing this module registers them.
"""

from typing import Any

from chemclaw.core.config import settings
from chemclaw.evals.metric import Direction, EvalCase, MetricError, MetricResult, metric


class _ProcessMasses:
    """The mass balance a green-chemistry metric reads from a case's output.

    A plain parser, not a Pydantic model, so an output carrying keys for other metrics is accepted.
    """

    def __init__(self, output: dict[str, Any]) -> None:
        """Validate and hold the input masses and product mass (kg).

        Rejects a product mass exceeding the total input: physically impossible, and it would yield
        a negative E-factor that passes the gate.
        """
        self.inputs = _nonnegative_masses(output.get("input_masses_kg"))
        self.product = _positive_scalar(output.get("product_mass_kg"), "product_mass_kg")
        if self.product > sum(self.inputs):
            raise MetricError(
                f"product_mass_kg {self.product:.4g} exceeds total input "
                f"{sum(self.inputs):.4g} kg — mass balance violated"
            )


def _nonnegative_masses(raw: Any) -> list[float]:
    """Coerce a non-empty list of non-negative input masses, else `MetricError`.

    Zero is allowed for an input (an unused feed); the product mass, which divides, must be > 0.
    """
    if not isinstance(raw, (list, tuple)) or not raw:
        raise MetricError("output.input_masses_kg must be a non-empty list of masses")
    masses = [_scalar(x, "output.input_masses_kg entry") for x in raw]
    if any(m < 0 for m in masses):
        raise MetricError("output.input_masses_kg must be non-negative")
    return masses


def _positive_scalar(raw: Any, field: str) -> float:
    """Coerce a strictly positive scalar (a product mass divides), else `MetricError`."""
    value = _scalar(raw, f"output.{field}")
    if value <= 0:
        raise MetricError(f"output.{field} must be > 0")
    return value


@metric("e_factor", Direction.LOWER_IS_BETTER, gated=True)
def e_factor(case: EvalCase) -> MetricResult:
    """Green-chemistry E-factor: kg waste per kg product (Sheldon).

    Waste is total input mass minus product mass. Lower is better; the pass limit is
    `eval_efactor_max`. Computed from the output mass balance alone.
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


@metric("pmi", Direction.LOWER_IS_BETTER, gated=True)
def process_mass_intensity(case: EvalCase) -> MetricResult:
    """Process Mass Intensity: total input mass per kg product (PMI = E-factor + 1).

    Lower is better; the pass limit is `eval_pmi_max`. Computed from the output mass balance alone.
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


@metric("prediction_error", Direction.LOWER_IS_BETTER, gated=True)
def prediction_error(case: EvalCase) -> MetricResult:
    """Absolute error of a predicted value against a held-out reference.

    Reads `output.predicted` and `reference.actual` (same unit). Passes when the error is within
    `eval_prediction_tolerance`. Requires a reference.
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


@metric("bo_regret", Direction.LOWER_IS_BETTER)
def bo_regret(case: EvalCase) -> MetricResult:
    """Optimization regret: distance from the best value found to the known optimum.

    Reads `output.best_value` and `reference.optimum`, with `output.direction`
    ("maximize"/"minimize") giving the sign. A negative value is kept, not clamped: it means the
    search beat the recorded reference, i.e. the reference is too loose. Ungated (`passed` is None),
    since the scale is problem-specific.
    """
    if case.reference is None:
        raise MetricError("bo_regret needs a reference with `optimum`")
    best = _scalar(case.output.get("best_value"), "output.best_value")
    optimum = _scalar(case.reference.get("optimum"), "reference.optimum")
    # Required, no default: silently assuming "maximize" would sign-flip the
    # regret of a minimize campaign (G4).
    direction = case.output.get("direction")
    if direction is None:
        raise MetricError("output.direction is required (maximize/minimize)")
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
            f"regret = optimum {optimum:.4g} vs best {best:.4g} = {value:.4g} ({direction})"
        ),
    )


def _id_set(raw: Any, field: str) -> set[str]:
    """Coerce a list of note ids into a set of strings, else a `MetricError` naming it.

    A missing key is an empty set, a meaningful score. A non-list is an error: a bare string would
    become a set of characters.
    """
    if raw is None:
        return set()
    if not isinstance(raw, (list, tuple)):
        raise MetricError(f"{field} must be a list of note ids, got {raw!r}")
    return {str(x) for x in raw}


def _classification(case: EvalCase) -> tuple[set[str], set[str]]:
    """The (predicted, expected) id sets a classification metric scores.

    Predicted from `output.predicted_note_ids`, expected from `reference.expected_note_ids`; a case
    without a reference cannot be scored.
    """
    if case.reference is None:
        raise MetricError("classification metrics need a reference with `expected_note_ids`")
    predicted = _id_set(case.output.get("predicted_note_ids"), "output.predicted_note_ids")
    expected = _id_set(case.reference.get("expected_note_ids"), "reference.expected_note_ids")
    return predicted, expected


def precision_recall_f1(predicted: set[str], expected: set[str]) -> tuple[float, float, float]:
    """Return (precision, recall, F1) for a predicted vs expected id set (the shared computation).

    Degenerate cases are defined: with no predictions, precision is 1.0 iff nothing was expected,
    else 0.0; with nothing expected, recall is 1.0; F1 is 0.0 when precision + recall is 0.
    """
    true_positives = len(predicted & expected)
    if predicted:
        precision = true_positives / len(predicted)
    else:
        precision = 1.0 if not expected else 0.0
    recall = true_positives / len(expected) if expected else 1.0
    f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    return precision, recall, f1


@metric("precision", Direction.HIGHER_IS_BETTER)
def precision(case: EvalCase) -> MetricResult:
    """Retrieval/extraction precision: fraction of predicted note ids that were expected.

    Ungated report/drift metric. Reads `output.predicted_note_ids` vs `reference.expected_note_ids`.
    """
    predicted, expected = _classification(case)
    value, _recall, _f1 = precision_recall_f1(predicted, expected)
    return MetricResult(
        metric="precision",
        value=value,
        unit=None,
        passed=None,
        provenance=(
            f"precision = |predicted ∩ expected| {len(predicted & expected)} / "
            f"|predicted| {len(predicted)}"
        ),
    )


@metric("recall", Direction.HIGHER_IS_BETTER)
def recall(case: EvalCase) -> MetricResult:
    """Retrieval/extraction recall: fraction of expected note ids that were predicted.

    Ungated report/drift metric. Reads `output.predicted_note_ids` vs `reference.expected_note_ids`.
    """
    predicted, expected = _classification(case)
    _precision, value, _f1 = precision_recall_f1(predicted, expected)
    return MetricResult(
        metric="recall",
        value=value,
        unit=None,
        passed=None,
        provenance=(
            f"recall = |predicted ∩ expected| {len(predicted & expected)} / "
            f"|expected| {len(expected)}"
        ),
    )


@metric("f1", Direction.HIGHER_IS_BETTER)
def f1(case: EvalCase) -> MetricResult:
    """Retrieval/extraction F1: the harmonic mean of precision and recall.

    Ungated report/drift metric. Reads `output.predicted_note_ids` vs `reference.expected_note_ids`.
    """
    predicted, expected = _classification(case)
    p, r, value = precision_recall_f1(predicted, expected)
    return MetricResult(
        metric="f1",
        value=value,
        unit=None,
        passed=None,
        provenance=f"F1 = harmonic_mean(precision {p:.4g}, recall {r:.4g})",
    )


def _scalar(raw: Any, field: str) -> float:
    """Coerce a required numeric field, else a `MetricError` naming it.

    Booleans are rejected: YAML parses `yes`/`no` as bools, and `float(True)` would score 1.0.
    """
    if raw is None:
        raise MetricError(f"{field} is required")
    if isinstance(raw, bool):
        raise MetricError(f"{field} must be a number, got {raw!r}")
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise MetricError(f"{field} must be a number, got {raw!r}") from exc
