"""Eval drift detection (plan F10-F2): aggregation, the noise band, and the committed baseline.

The pure logic (aggregate → compare vs baseline → alert only past epsilon) is tested directly, plus
the drift activity's real I/O path (load the committed case-set + baseline) offline — calling the
activity function directly, since a Temporal activity is a plain async function. A guard test pins
the committed `evals/baseline.json` still matches the current case-set, so a metric change without a
baseline refresh trips here (in CI) rather than as a silent false alert in production.
"""

import asyncio
import logging
from pathlib import Path

import pytest

import chemclaw.evals  # noqa: F401 — registers the metrics used by the case-set
from chemclaw.core.config import settings
from chemclaw.durable.eval_drift import check_eval_drift
from chemclaw.evals.baseline import (
    Baseline,
    aggregate_metrics,
    detect_drift,
    load_baseline,
    save_baseline,
)
from chemclaw.evals.harness import EvalReport, ScoredResult, load_eval_cases, run_eval


def _report(*pairs: tuple[str, float]) -> EvalReport:
    return EvalReport(
        case_set_version="v",
        results=[
            ScoredResult(
                case_id=f"c{i}",
                result_metric=name,
                value=value,
                unit=None,
                passed=None,
                provenance="p",
            )
            for i, (name, value) in enumerate(pairs)
        ],
    )


def test_aggregate_metrics_means_over_cases() -> None:
    """Each metric's aggregate is the mean of its per-case values."""
    agg = aggregate_metrics(_report(("f1", 1.0), ("f1", 0.0), ("recall", 0.5)))
    assert agg == {"f1": 0.5, "recall": 0.5}


def test_detect_drift_flags_only_moves_past_epsilon() -> None:
    """A move larger than the (relative) band alerts; one within it is silent."""
    baseline = Baseline(case_set_version="v", metrics={"f1": 0.80, "recall": 0.60})
    alerts = detect_drift(baseline, {"f1": 0.60, "recall": 0.62}, epsilon=0.05)
    # f1 moved 0.20 (band 0.05×0.80=0.04); recall moved 0.02 (band 0.05×0.60=0.03), not flagged.
    assert [a.metric for a in alerts] == ["f1"]
    assert alerts[0].delta == pytest.approx(-0.20)
    assert alerts[0].vanished is False  # a scored move, not an absence


def test_detect_drift_band_is_relative_to_scale() -> None:
    """The same epsilon is a proportional band, so a big-magnitude metric tolerates a bigger move.

    A 1.0 absolute move is drift for an [0, 1] metric but noise for one near 35 — the exact failure
    a single absolute epsilon caused. Relative-to-baseline makes one knob correct for both scales.
    """
    baseline = Baseline(case_set_version="v", metrics={"f1": 0.60, "e_factor": 35.0})
    # +1.0 on each: f1 far past its 0.03 band (flagged); e_factor within its 1.75 band (silent).
    alerts = detect_drift(baseline, {"f1": 1.60, "e_factor": 36.0}, epsilon=0.05)
    assert [a.metric for a in alerts] == ["f1"]


def test_detect_drift_flags_a_vanished_metric() -> None:
    """A baseline metric absent from the current run is a regression (silently dropped scoring)."""
    baseline = Baseline(case_set_version="v", metrics={"f1": 0.80})
    alerts = detect_drift(baseline, {}, epsilon=0.05)
    assert [a.metric for a in alerts] == ["f1"]
    assert alerts[0].current_value == 0.0
    assert alerts[0].vanished is True  # absent, not "scored 0.0"


def test_baseline_round_trips(tmp_path: Path) -> None:
    """A baseline saved to JSON reloads identically."""
    path = str(tmp_path / "baseline.json")
    baseline = Baseline(case_set_version="v1", metrics={"f1": 0.5, "precision": 0.9})
    save_baseline(baseline, path)
    assert load_baseline(path) == baseline


def test_committed_baseline_matches_current_case_set() -> None:
    """The committed baseline + default epsilon produce no alerts — it tracks the case-set."""
    alerts = asyncio.run(check_eval_drift())
    assert alerts == []


def test_drift_activity_alerts_on_a_perturbed_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Pointing the check at a baseline shifted past epsilon raises exactly that metric's alert.

    The alert is also logged at WARNING: the push-back channel is must-deliver but unconsumed, so
    the log is where an operator actually meets a regression.
    """
    # Re-run the committed case-set for the true current aggregates, then shift one past the band.
    current = aggregate_metrics(run_eval(load_eval_cases(settings.eval_case_dir), "now"))
    shifted = dict(current)
    shifted["f1"] = current["f1"] + 1.0  # a full unit past the 0.05 band
    path = str(tmp_path / "baseline.json")
    save_baseline(Baseline(case_set_version="shifted", metrics=shifted), path)
    monkeypatch.setattr(settings, "eval_baseline_path", path)
    with caplog.at_level(logging.WARNING):
        alerts = asyncio.run(check_eval_drift())
    assert [a.metric for a in alerts] == ["f1"]
    assert "eval drift" in caplog.text and "'f1'" in caplog.text


def test_a_vanished_metric_is_not_logged_as_a_zero_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A vanished metric logs as absent, never as "scored 0.0" — the two mean different bugs."""
    current = aggregate_metrics(run_eval(load_eval_cases(settings.eval_case_dir), "now"))
    path = str(tmp_path / "baseline.json")
    save_baseline(
        Baseline(case_set_version="ghost", metrics={**current, "no_such_metric": 0.9}), path
    )
    monkeypatch.setattr(settings, "eval_baseline_path", path)
    with caplog.at_level(logging.WARNING):
        alerts = asyncio.run(check_eval_drift())
    assert [a.metric for a in alerts] == ["no_such_metric"]
    assert "disappeared from the run" in caplog.text
    assert "0.0000" not in caplog.text  # never rendered as a score


def test_no_drift_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    """A clean run is silent — a warning per scheduled check would train operators to ignore it."""
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(check_eval_drift()) == []
    assert caplog.text == ""


def test_the_live_retriever_metrics_are_under_drift_detection() -> None:
    """`retrieval_recall`/`retrieval_precision` must be in the baseline, or they have no signal.

    `detect_drift` iterates the *baseline*, not the run, so a metric missing from `baseline.json`
    is not weakly covered — it is uncovered. These two are the only metrics that run a live
    retriever, and `retrieval_recall` is the gated one, so their absence meant the system's core
    quality signal could regress silently. It did: collapsing both to 0.0 produced no alert.

    Asserted by name rather than by count so adding an unrelated metric cannot satisfy it.
    """
    baseline = load_baseline(settings.eval_baseline_path)
    assert {"retrieval_recall", "retrieval_precision"} <= baseline.metrics.keys()


def test_a_collapsed_retrieval_score_now_raises_an_alert() -> None:
    """The regression that was previously silent now fires — the behavioural half of the fix."""
    baseline = load_baseline(settings.eval_baseline_path)
    collapsed = dict(baseline.metrics, retrieval_recall=0.0, retrieval_precision=0.0)
    alerts = {
        alert.metric for alert in detect_drift(baseline, collapsed, settings.eval_drift_epsilon)
    }
    assert {"retrieval_recall", "retrieval_precision"} <= alerts
