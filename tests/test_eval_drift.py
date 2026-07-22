"""Eval drift detection (plan F10-F2): aggregation, the noise band, and the committed baseline.

The pure logic (aggregate → compare vs baseline → alert only past epsilon) is tested directly, plus
the drift activity's real I/O path (load the committed case-set + baseline) offline — calling the
activity function directly, since a Temporal activity is a plain async function. A guard test pins
the committed `evals/baseline.json` still matches the current case-set, so a metric change without a
baseline refresh trips here (in CI) rather than as a silent false alert in production.
"""

import asyncio
from pathlib import Path

import pytest

import evals  # noqa: F401 — registers the metrics used by the case-set
from chemclaw.config import settings
from evals.baseline import (
    Baseline,
    aggregate_metrics,
    detect_drift,
    load_baseline,
    save_baseline,
)
from evals.harness import EvalReport, ScoredResult, load_eval_cases, run_eval
from workflows.eval_drift import check_eval_drift


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
    """A move larger than epsilon alerts; one within the band is silent."""
    baseline = Baseline(case_set_version="v", metrics={"f1": 0.80, "recall": 0.60})
    alerts = detect_drift(baseline, {"f1": 0.60, "recall": 0.62}, epsilon=0.05)
    assert [a.metric for a in alerts] == ["f1"]  # recall moved 0.02 (< 0.05), not flagged
    assert alerts[0].delta == pytest.approx(-0.20)


def test_detect_drift_flags_a_vanished_metric() -> None:
    """A baseline metric absent from the current run is a regression (silently dropped scoring)."""
    baseline = Baseline(case_set_version="v", metrics={"f1": 0.80})
    alerts = detect_drift(baseline, {}, epsilon=0.05)
    assert [a.metric for a in alerts] == ["f1"]
    assert alerts[0].current_value == 0.0


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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pointing the check at a baseline shifted past epsilon raises exactly that metric's alert."""
    # Re-run the committed case-set for the true current aggregates, then shift one past the band.
    current = aggregate_metrics(run_eval(load_eval_cases(settings.eval_case_dir), "now"))
    shifted = dict(current)
    shifted["f1"] = current["f1"] + 1.0  # a full unit past the 0.05 band
    path = str(tmp_path / "baseline.json")
    save_baseline(Baseline(case_set_version="shifted", metrics=shifted), path)
    monkeypatch.setattr(settings, "eval_baseline_path", path)
    alerts = asyncio.run(check_eval_drift())
    assert [a.metric for a in alerts] == ["f1"]
