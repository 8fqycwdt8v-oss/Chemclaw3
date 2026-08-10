"""Eval baseline + drift detection (plan F10-F2): catch silent quality regressions.

A committed baseline (`data/evals/baseline.json`) records the aggregate value of each metric over
the versioned case-set at a known-good point. `detect_drift` re-aggregates a fresh run and flags any
metric that moved further than a *relative* noise band (`eval_drift_epsilon`, a fraction of the
baseline value) from that baseline. Relative, not absolute, because the metrics live on
heterogeneous scales (an `f1` in [0, 1] next to an `e_factor` near 35): one absolute band would be
loose for the bounded metrics and hair-trigger for the large ones. All logic here is pure and
file-based (no Temporal, no network), so it is fully unit-tested; `durable/eval_drift.py` is the
thin durable wrapper that schedules it.

**`compare_to_baseline` is the same comparison as an ordinary command rather than a workflow.**
For a long time the only caller of any of this was `durable/eval_drift.py`, a Temporal workflow
that is off by default (`eval_drift_enabled=False`) — so "the case-set scored against the recorded
baseline" was a number nobody could obtain without a broker. The comparison is pure file-based
arithmetic; it does not need durability to be *run*, only to be *scheduled*. `evals.harness
--baseline` is the offline front end and this is its logic.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from chemclaw.core.errors import ChemclawError
from chemclaw.evals.metric import Direction, direction_of

if TYPE_CHECKING:  # pragma: no cover - `EvalReport` is needed only as an annotation here.
    # Deferred on purpose: `harness` imports this module for its `--baseline` mode, and importing
    # it back at runtime would close the cycle. The dependency really is type-only — nothing here
    # calls into the harness.
    from chemclaw.evals.harness import EvalReport


class Baseline(BaseModel):
    """The known-good aggregate score of each metric over a versioned case-set."""

    case_set_version: str = Field(min_length=1)
    # metric name → aggregate (mean) value across the case-set at baseline time.
    metrics: dict[str, float]


class DriftAlert(BaseModel):
    """One metric that drifted beyond the noise band from baseline (what an operator must see).

    `vanished` distinguishes the two ways a metric drifts: it scored a different value (`vanished`
    False, `current_value` is the new score), or it disappeared from the run entirely because its
    case was removed (`vanished` True, `current_value` is 0.0 as a placeholder). An operator must
    not read a vanished metric as "it scored 0.0".
    """

    metric: str
    baseline_value: float
    current_value: float
    delta: float
    vanished: bool = False


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


def drift_band(baseline_value: float, epsilon: float) -> float:
    """The half-width of the noise band around `baseline_value` (a move inside it is not drift).

    `epsilon * abs(baseline_value)` — a fraction of the baseline, so one knob means the same
    *proportional* sensitivity for an `f1` in [0, 1] and an `e_factor` near 35. A baseline of
    exactly 0 has no proportion to take, so the band falls back to the absolute `epsilon` and a
    move off zero past it is still caught. Shared by `detect_drift` and the reported comparison so
    the number an operator reads is the number the verdict used, not a second copy of the formula.
    """
    return epsilon * abs(baseline_value) if baseline_value else epsilon


def detect_drift(baseline: Baseline, current: dict[str, float], epsilon: float) -> list[DriftAlert]:
    """Flag every baseline metric whose current aggregate moved more than a relative `epsilon`.

    The band is `drift_band(baseline_value, epsilon)` — relative, so one knob is scale-appropriate
    across metrics of different magnitudes. Only metrics in the baseline are checked — a newly
    added metric has no known-good point to regress against yet (adding it to the baseline is
    deliberate). A
    metric that vanished from the current run (its case removed) is flagged: dropping a scored
    metric is exactly the regression this guards against.
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
                    vanished=True,
                )
            )
            continue
        delta = current_value - baseline_value
        if abs(delta) > drift_band(baseline_value, epsilon):
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
    """Write the baseline JSON (used to (re)generate the committed `data/evals/baseline.json`)."""
    Path(path).write_text(baseline.model_dump_json(indent=2) + "\n", encoding="utf-8")


class CaseSetMismatchError(ChemclawError):
    """The run and the baseline scored *different* case-sets, so no comparison exists.

    Not a warning beside a number, because there is no number: a baseline pins the aggregate of one
    set of cases, and the aggregate of a different set is a different quantity that happens to share
    a metric name. Reporting "f1 moved -0.12" across two case-sets would be arithmetic on unrelated
    populations, and it is the more dangerous failure precisely because it looks like a result.
    """


class MetricComparison(BaseModel):
    """One baseline metric beside its current score — the row an operator reads.

    Carries the *numbers* (baseline, current, delta, band), not only the verdict: "drifted" without
    the magnitude cannot tell a metric that fell off a cliff from one that grazed the band, and the
    first thing anyone asks of a red eval is how far.
    """

    metric: str
    # None when this build no longer registers the metric at all — it cannot be scored, so it has
    # no direction to report. Never a stand-in value: a guessed direction next to a real delta is
    # exactly the confidently-mis-signed verdict `direction_of` refuses to produce.
    direction: Direction | None
    baseline_value: float
    # None means the metric was not scored by this run at all. Distinct from 0.0, which is a real
    # score — the same distinction `DriftAlert.vanished` exists to preserve.
    current_value: float | None
    delta: float
    band: float
    drifted: bool
    worsening: bool


class BaselineComparison(BaseModel):
    """A whole run scored against the committed baseline, one row per baseline metric."""

    case_set_version: str = Field(min_length=1)
    epsilon: float
    rows: list[MetricComparison]

    def worsened(self) -> list[MetricComparison]:
        """The rows that must fail the command — drift in the bad direction, or a lost metric."""
        return [row for row in self.rows if row.worsening]


def is_worsening(alert: DriftAlert) -> bool:
    """Whether a drift alert moved the way that is *bad* for its metric.

    A drift check alone cannot gate a build: `detect_drift` is symmetric by design (an operator
    watching a schedule wants to know that anything moved), but a command that fails on an
    improvement would be a command everyone learns to re-run until it passes. The metric's
    registered `Direction` supplies the sign; a vanished metric is always bad, since a scored metric
    that stopped being scored is lost coverage regardless of which way it used to point.
    """
    if alert.vanished:
        return True
    if direction_of(alert.metric) is Direction.HIGHER_IS_BETTER:
        return alert.delta < 0
    return alert.delta > 0


def _known_direction(name: str) -> Direction | None:
    """The metric's registered direction, or None if this build no longer has that metric.

    A baseline can outlive a metric (it is a committed file, the registry is code). That is a
    regression the comparison must still *report* — it just cannot report a direction for it.
    """
    try:
        return direction_of(name)
    except ValueError:
        return None


def compare_to_baseline(
    report: EvalReport, baseline: Baseline, epsilon: float
) -> BaselineComparison:
    """Score a fresh report against the committed baseline, metric by metric.

    Raises `CaseSetMismatchError` when the report and the baseline name different case-sets — the
    one situation where producing a number would be worse than producing nothing.

    Rows are emitted for every metric *in the baseline* (in `detect_drift`'s order), because that is
    what has a known-good value to regress against; a metric the run added but the baseline never
    pinned has nothing to compare to and belongs in the next baseline refresh, not in this verdict.
    """
    if report.case_set_version != baseline.case_set_version:
        raise CaseSetMismatchError(
            f"case-set mismatch: this run scored {report.case_set_version!r} but the baseline was "
            f"recorded on {baseline.case_set_version!r}. Their aggregates are different "
            "quantities, so no comparison is reported. Score the baseline's case-set version, "
            "or refresh the "
            "baseline (`make eval-baseline`) if the case-set genuinely changed."
        )
    current = aggregate_metrics(report)
    alerts = {alert.metric: alert for alert in detect_drift(baseline, current, epsilon)}
    rows: list[MetricComparison] = []
    for name, baseline_value in sorted(baseline.metrics.items()):
        alert = alerts.get(name)
        current_value = current.get(name)
        rows.append(
            MetricComparison(
                metric=name,
                direction=_known_direction(name),
                baseline_value=baseline_value,
                current_value=current_value,
                delta=(current_value - baseline_value) if current_value is not None else 0.0,
                band=drift_band(baseline_value, epsilon),
                drifted=alert is not None,
                worsening=alert is not None and is_worsening(alert),
            )
        )
    return BaselineComparison(
        case_set_version=baseline.case_set_version, epsilon=epsilon, rows=rows
    )


def _verdict(row: MetricComparison) -> str:
    """The one-word reading of a row, in the report's own vocabulary."""
    if row.current_value is None:
        return "**VANISHED**"
    if not row.drifted:
        return "within band"
    return "**WORSE**" if row.worsening else "improved"


def render_comparison(comparison: BaselineComparison) -> str:
    """Render the comparison as a citable Markdown table (the same shape as the eval report)."""
    lines = [
        f"# Baseline comparison (case-set {comparison.case_set_version}, "
        f"epsilon {comparison.epsilon:g})",
        "",
        "| Metric | Better | Baseline | Current | Delta | Band | Verdict |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in comparison.rows:
        current = "—" if row.current_value is None else f"{row.current_value:.6g}"
        delta = "—" if row.current_value is None else f"{row.delta:+.4g}"
        better = "—" if row.direction is None else row.direction.value
        lines.append(
            f"| {row.metric} | {better} | {row.baseline_value:.6g} | {current} "
            f"| {delta} | {row.band:.4g} | {_verdict(row)} |"
        )
    worsened = comparison.worsened()
    lines += [
        "",
        f"**{len(worsened)} of {len(comparison.rows)} baseline metric(s) worsened** beyond the "
        f"noise band.",
    ]
    if worsened:
        # Named again below the table: the table is long enough that a single **WORSE** cell in the
        # middle of it is easy to scroll past, and this is the line a CI log tail will show.
        lines.append("")
        lines.append(
            "Worsened: "
            + ", ".join(
                f"{row.metric} ({row.baseline_value:.6g} → "
                + ("absent" if row.current_value is None else f"{row.current_value:.6g}")
                + (
                    ", no longer registered)"
                    if row.direction is None
                    else f", {row.direction.value} is better)"
                )
                for row in worsened
            )
            + "."
        )
    return "\n".join(lines) + "\n"
