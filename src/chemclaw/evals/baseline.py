"""Eval baseline + drift detection: catch silent quality regressions.

A committed baseline (`data/evals/baseline.json`) records each metric's aggregate over the versioned
case-set at a known-good point. `detect_drift` flags any metric that moved further than a *relative*
band (`eval_drift_epsilon`, a fraction of the baseline value) — relative because metrics live on
different scales. Pure and file-based: `durable/eval_drift.py` schedules it, and
`compare_to_baseline` backs `evals.harness --baseline` offline.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from chemclaw.core.errors import ChemclawError
from chemclaw.core.markdown import MISSING, render_table
from chemclaw.evals.metric import Direction, direction_of, is_live

if TYPE_CHECKING:  # pragma: no cover - `EvalReport` is needed only as an annotation here.
    # Type-only import, deferred: `harness` imports this module, so a runtime import would be a
    # cycle.
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

    A metric scored on no case does not appear.
    """
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for result in report.results:
        totals[result.result_metric] = totals.get(result.result_metric, 0.0) + result.value
        counts[result.result_metric] = counts.get(result.result_metric, 0) + 1
    return {name: totals[name] / counts[name] for name in totals}


def drift_band(baseline_value: float, epsilon: float) -> float:
    """The half-width of the noise band around `baseline_value` (a move inside it is not drift).

    `epsilon * abs(baseline_value)`, so one knob gives the same proportional sensitivity on every
    scale; a baseline of exactly 0 falls back to the absolute `epsilon`. Shared by `detect_drift`
    and the report so both use one formula.
    """
    return epsilon * abs(baseline_value) if baseline_value else epsilon


def detect_drift(baseline: Baseline, current: dict[str, float], epsilon: float) -> list[DriftAlert]:
    """Flag every baseline metric whose current aggregate moved more than a relative `epsilon`.

    Only metrics in the baseline are checked; a new metric has no known-good point yet. A metric
    missing from the current run is flagged, since losing a scored metric is a regression.
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

    An error rather than a warning: an aggregate over a different set of cases is a different
    quantity, and a delta between them would look like a result while meaning nothing.
    """


class MetricComparison(BaseModel):
    """One baseline metric beside its current score — the row an operator reads.

    Carries the *numbers* (baseline, current, delta, band), not only the verdict: "drifted" without
    the magnitude cannot tell a metric that fell off a cliff from one that grazed the band, and the
    first thing anyone asks of a red eval is how far.
    """

    metric: str
    # Whether scoring this metric ran product code (`live`) or read literals a case file commits
    # (pinned). Carried so a reader can tell how many rows a release could actually move.
    live: bool = False
    # None when this build no longer registers the metric; never a guessed direction.
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

    def live_rows(self) -> list[MetricComparison]:
        """The rows whose score came from running product code — what this gate actually watches.

        Pinned rows still catch an edited case file or a changed formula, but no release can move
        them.
        """
        return [row for row in self.rows if row.live]


def is_worsening(alert: DriftAlert) -> bool:
    """Whether a drift alert moved the way that is *bad* for its metric.

    `detect_drift` is symmetric, but a build gate must not fail on an improvement. The metric's
    registered `Direction` supplies the sign; a vanished metric is always bad.
    """
    if alert.vanished:
        return True
    if direction_of(alert.metric) is Direction.HIGHER_IS_BETTER:
        return alert.delta < 0
    return alert.delta > 0


def _known_direction(name: str) -> Direction | None:
    """The metric's registered direction, or None if this build no longer has that metric.

    A committed baseline can outlive a metric; the comparison still reports it, without a direction.
    """
    try:
        return direction_of(name)
    except ValueError:
        return None


def compare_to_baseline(
    report: EvalReport, baseline: Baseline, epsilon: float
) -> BaselineComparison:
    """Score a fresh report against the committed baseline, metric by metric.

    Raises `CaseSetMismatchError` when the report and the baseline name different case-sets. Rows
    are emitted for every metric in the baseline, in `detect_drift`'s order; a metric the baseline
    never pinned belongs in the next baseline refresh.
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
                live=is_live(name),
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
        render_table(
            ["Metric", "Kind", "Better", "Baseline", "Current", "Delta", "Band", "Verdict"],
            [
                [
                    row.metric,
                    "live" if row.live else "pinned",
                    MISSING if row.direction is None else row.direction.value,
                    f"{row.baseline_value:.6g}",
                    MISSING if row.current_value is None else f"{row.current_value:.6g}",
                    MISSING if row.current_value is None else f"{row.delta:+.4g}",
                    f"{row.band:.4g}",
                    _verdict(row),
                ]
                for row in comparison.rows
            ],
        ),
    ]
    worsened = comparison.worsened()
    live = len(comparison.live_rows())
    pinned = len(comparison.rows) - live
    lines += [
        "",
        f"**{len(worsened)} of {len(comparison.rows)} baseline metric(s) worsened** beyond the "
        f"noise band.",
        "",
        # The summary states how many rows are live versus pinned: a pinned metric is arithmetic
        # over committed literals that no release can move, so a bare count would overstate what the
        # gate covers.
        f"{live} live (scored by running product code), {pinned} pinned (arithmetic over "
        "literals committed in the case files, so only a case or formula edit can move them).",
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
