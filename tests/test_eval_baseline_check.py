"""The offline baseline comparison (`python -m chemclaw.evals.harness --baseline`).

`data/evals/baseline.json` records what the case-set scored at a known-good point, and for a long
time the only code that read it was `durable/eval_drift.py` — a Temporal workflow disabled by
default. "The case-set scored against the recorded baseline" was therefore not something anyone
could run. These tests pin the command that now does it, and above all pin the two ways it must
*not* mislead: it must not fail on an improvement, and it must not produce a number at all when the
run and the baseline scored different case-sets.

Every assertion goes through the real `compare_to_baseline`/`main`; nothing here mocks the thing
under test, because a mock of a comparison would agree with whatever it was told.
"""

from pathlib import Path

import pytest

import chemclaw.evals  # noqa: F401 — registers the metrics whose directions are read below
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.evals.baseline import (
    Baseline,
    CaseSetMismatchError,
    compare_to_baseline,
    load_baseline,
    render_comparison,
    save_baseline,
)
from chemclaw.evals.harness import EvalReport, ScoredResult, load_eval_cases, main, run_eval
from chemclaw.evals.metric import Direction, direction_of, registered_names

# The committed baseline's own case-set version. Read from the file rather than restated, so these
# tests keep testing the shipped comparison after a `make eval-baseline` refresh renames it.
COMMITTED_VERSION = load_baseline(settings.eval_baseline_path).case_set_version


def _report(version: str, **scores: float) -> EvalReport:
    """A one-case-per-metric report scoring exactly `scores` (the run side of a comparison)."""
    return EvalReport(
        case_set_version=version,
        results=[
            ScoredResult(
                case_id=f"case-{name}",
                result_metric=name,
                value=value,
                unit=None,
                passed=None,
                provenance="fixture",
            )
            for name, value in scores.items()
        ],
    )


def _compare(baseline_metrics: dict[str, float], **scores: float) -> list[str]:
    """The metric names that worsened, comparing `scores` against `baseline_metrics` at 0.05."""
    baseline = Baseline(case_set_version="v", metrics=baseline_metrics)
    report = _report("v", **scores)
    comparison = compare_to_baseline(report, baseline, epsilon=0.05)
    return [row.metric for row in comparison.worsened()]


def test_every_registered_metric_declares_a_direction() -> None:
    """Without a direction the comparison cannot tell an improvement from a regression.

    Asserted over the whole registry rather than a sample: a metric added without one would make
    `--baseline` report that metric backwards, and there is no value at which that is detectable
    from the number alone (0.9 is a good `f1` and a bad `prediction_error`).
    """
    assert registered_names()  # the registry is populated, so this proves something
    for name in registered_names():
        assert isinstance(direction_of(name), Direction)


def test_a_higher_is_better_metric_that_fell_is_worsening() -> None:
    """`f1` sliding 0.80 → 0.60 is the regression the whole comparison exists to catch."""
    assert direction_of("f1") is Direction.HIGHER_IS_BETTER
    assert _compare({"f1": 0.80}, f1=0.60) == ["f1"]


def test_a_lower_is_better_metric_that_rose_is_worsening() -> None:
    """`prediction_error` climbing 0.50 → 1.00 is a regression, though the number went *up*.

    The mirror of the case above, and the reason direction is registered per metric: a comparison
    keyed only on the sign of the delta would call this an improvement.
    """
    assert direction_of("prediction_error") is Direction.LOWER_IS_BETTER
    assert _compare({"prediction_error": 0.50}, prediction_error=1.00) == ["prediction_error"]


def test_an_improvement_past_the_band_does_not_fail() -> None:
    """Both directions, improved past epsilon: drift is reported, the command stays green.

    A gate that failed on a better score is a gate people learn to re-run until it passes, which
    costs the signal on the runs that mattered.
    """
    assert _compare({"f1": 0.60}, f1=0.90) == []
    assert _compare({"prediction_error": 0.50}, prediction_error=0.10) == []


def test_the_improvement_is_still_reported_as_drift() -> None:
    """Green is not silent: an unexpected jump is a finding, it just is not a failure."""
    baseline = Baseline(case_set_version="v", metrics={"f1": 0.60})
    comparison = compare_to_baseline(_report("v", f1=0.90), baseline, epsilon=0.05)
    row = comparison.rows[0]
    assert (row.drifted, row.worsening) == (True, False)
    assert "improved" in render_comparison(comparison)


def test_a_move_inside_the_band_is_not_a_failure() -> None:
    """0.80 → 0.81 is inside the 0.05×0.80 = 0.04 band — noise, in either direction."""
    assert _compare({"f1": 0.80}, f1=0.81) == []
    assert _compare({"f1": 0.80}, f1=0.79) == []


def test_a_vanished_metric_is_a_failure_and_is_not_reported_as_zero() -> None:
    """A metric that stopped being scored is lost coverage, whichever way it used to point."""
    baseline = Baseline(case_set_version="v", metrics={"f1": 0.80})
    comparison = compare_to_baseline(_report("v", precision=1.0), baseline, epsilon=0.05)
    assert [row.metric for row in comparison.worsened()] == ["f1"]
    assert comparison.rows[0].current_value is None  # absent, never "scored 0.0"
    rendered = render_comparison(comparison)
    assert "VANISHED" in rendered and "absent" in rendered


def test_a_case_set_mismatch_refuses_to_report_a_number() -> None:
    """The most dangerous failure: a delta across two case-sets looks like a result and is not.

    An aggregate is a mean over a population of cases; comparing the mean of one population with
    the mean of another shares only the metric's *name*. So the comparison raises instead of
    producing rows, and the message names both versions.
    """
    baseline = Baseline(case_set_version="autonomy-2026-08-01", metrics={"f1": 0.80})
    with pytest.raises(CaseSetMismatchError) as excinfo:
        compare_to_baseline(_report("unversioned", f1=0.80), baseline, epsilon=0.05)
    message = str(excinfo.value)
    assert "unversioned" in message and "autonomy-2026-08-01" in message
    assert issubclass(CaseSetMismatchError, ChemclawError)


def test_the_mismatch_is_loud_and_red_on_the_command_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """End to end: the default `unversioned` run cannot silently "pass" against a real baseline.

    This is the shape the gap would take in practice — someone runs `--baseline` without declaring
    which case-set they scored — so the CLI must exit 1 and say why, not print a table of zeros.
    """
    assert main(["--baseline"]) == 1
    out = capsys.readouterr().out
    assert "case-set mismatch" in out
    assert "Baseline comparison" not in out  # no numbers were reported


def test_the_committed_baseline_scores_green_offline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The shipped command over the shipped baseline: exit 0, with the actual numbers printed.

    Also the guard that the comparison is *meaningful*: it asserts a row per committed baseline
    metric, so a baseline that stopped overlapping the case-set (which would make the check pass
    vacuously) fails here instead.
    """
    assert main(["--case-set-version", COMMITTED_VERSION, "--baseline"]) == 0
    out = capsys.readouterr().out
    assert f"# Baseline comparison (case-set {COMMITTED_VERSION}" in out
    committed = load_baseline(settings.eval_baseline_path)
    assert committed.metrics, "the committed baseline is empty — nothing is being compared"
    for name, value in committed.metrics.items():
        assert f"| {name} |" in out
        assert f"{value:.6g}" in out  # the number, not just a verdict
    assert f"**0 of {len(committed.metrics)} baseline metric(s) worsened**" in out


def test_the_command_is_red_when_a_committed_metric_worsens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Perturb the baseline so the real run reads as a regression — the exit code must follow.

    `retrieval_recall` is the perturbed one on purpose: it is the only gated metric that runs a
    live retriever, so it is the metric whose silent slide would matter most.
    """
    committed = load_baseline(settings.eval_baseline_path)
    raised = dict(committed.metrics, retrieval_recall=committed.metrics["retrieval_recall"] + 0.5)
    path = str(tmp_path / "baseline.json")
    save_baseline(Baseline(case_set_version=committed.case_set_version, metrics=raised), path)
    monkeypatch.setattr(settings, "eval_baseline_path", path)

    assert main(["--case-set-version", committed.case_set_version, "--baseline"]) == 1
    out = capsys.readouterr().out
    assert "**WORSE**" in out
    assert "Worsened: retrieval_recall" in out
    assert "higher is better" in out


def test_a_hard_gate_failure_outranks_the_drift_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--strict` and `--baseline` compose: both readings are printed, `--strict` sets the code.

    A clean drift check must never turn a real gate regression green, which is what returning the
    comparison's exit code unconditionally would do.
    """
    committed = load_baseline(settings.eval_baseline_path)
    # Take the green-chemistry gates below the shipped values, so a case that is *not* a declared
    # demonstration now fails one — a genuine regression, with drift still clean.
    monkeypatch.setattr(settings, "eval_efactor_max", 0.0)
    monkeypatch.setattr(settings, "eval_pmi_max", 0.0)
    regressed = run_eval(load_eval_cases(settings.eval_case_dir), committed.case_set_version)
    assert regressed.regressions(), "the gates did not actually break; this test proves nothing"

    assert main(["--case-set-version", committed.case_set_version, "--strict", "--baseline"]) == 1
    out = capsys.readouterr().out
    assert f"{len(regressed.regressions())} regression(s)" in out
    # The drift half still ran, and passed — so the 1 came from `--strict`, not from the baseline.
    assert f"**0 of {len(committed.metrics)} baseline metric(s) worsened**" in out
