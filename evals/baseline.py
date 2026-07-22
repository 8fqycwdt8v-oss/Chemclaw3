"""Eval baseline + drift detection (plan F10-F2): catch silent quality regressions.

A committed baseline (`evals/baseline.json`) records the aggregate value of each metric over the
versioned case-set at a known-good point. `detect_drift` re-aggregates a fresh run and flags any
metric that moved further than a noise band (`eval_drift_epsilon`) from that baseline — the same
"changes within noise are not signal" idea as the A/B epsilon (D-A14), applied over time instead of
across tool variants. All logic here is pure and file-based (no Temporal, no network), so it is
fully unit-tested; `workflows/eval_drift.py` is the thin durable wrapper that schedules it.
"""

from pathlib import Path

from pydantic import BaseModel, Field

from evals.harness import EvalReport


class Baseline(BaseModel):
    """The known-good aggregate score of each metric over a versioned case-set."""

    case_set_version: str = Field(min_length=1)
    # metric name → aggregate (mean) value across the case-set at baseline time.
    metrics: dict[str, float]


class DriftAlert(BaseModel):
    """One metric that drifted beyond the noise band from baseline (what an operator must see)."""

    metric: str
    baseline_value: float
    current_value: float
    delta: float


def aggregate_metrics(report: EvalReport) -> dict[str, float]:
    """Mean value of each metric across every case it scored (the comparable per-run summary).

    Averaging over cases collapses a run to one number per metric, which is what a baseline can pin
    and drift can compare. A metric scored on no case simply does not appear (nothing to average).
    """
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for result in report.results:
        totals[result.result_metric] = totals.get(result.result_metric, 0.0) + result.value
        counts[result.result_metric] = counts.get(result.result_metric, 0) + 1
    return {name: totals[name] / counts[name] for name in totals}


def detect_drift(baseline: Baseline, current: dict[str, float], epsilon: float) -> list[DriftAlert]:
    """Flag every baseline metric whose current aggregate moved more than `epsilon` (absolute).

    Only metrics present in the baseline are checked — a newly added metric has no known-good point
    to regress against yet (adding it to the baseline is a deliberate commit). A metric that
    vanished from the current run (its case removed) is flagged as drift: dropping a scored metric
    is exactly the regression this guards against, so its absence counts as a full-value move.
    """
    alerts: list[DriftAlert] = []
    for metric, baseline_value in sorted(baseline.metrics.items()):
        current_value = current.get(metric)
        if current_value is None:
            alerts.append(
                DriftAlert(
                    metric=metric,
                    baseline_value=baseline_value,
                    current_value=0.0,
                    delta=-baseline_value,
                )
            )
            continue
        delta = current_value - baseline_value
        if abs(delta) > epsilon:
            alerts.append(
                DriftAlert(
                    metric=metric,
                    baseline_value=baseline_value,
                    current_value=current_value,
                    delta=delta,
                )
            )
    return alerts


def load_baseline(path: str) -> Baseline:
    """Read the committed baseline JSON (raises if absent/malformed — a drift run needs it)."""
    return Baseline.model_validate_json(Path(path).read_text(encoding="utf-8"))


def save_baseline(baseline: Baseline, path: str) -> None:
    """Write the baseline JSON (used to (re)generate the committed `evals/baseline.json`)."""
    Path(path).write_text(baseline.model_dump_json(indent=2) + "\n", encoding="utf-8")
