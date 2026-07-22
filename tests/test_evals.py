"""Behavioral tests for the evaluation & metric layer (plan Phase 2b).

They prove the acceptance criteria of CHECKMATE 2b: metrics are pure and
config-thresholded, the harness runs reproducibly over the versioned case-set and
renders a citable report, and the tool-utility A/B surfaces at least one task where
tooling does *not* help (the selective-steering evidence, F8/F9).
"""

import math
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from chemclaw.config import settings
from chemclaw.errors import ChemclawError
from evals.ab import TaskScores, compare_tool_utility
from evals.harness import (
    EvalCaseError,
    load_eval_cases,
    main,
    render_report,
    run_eval,
)
from evals.metric import (
    EvalCase,
    MetricError,
    get_metric,
    registered_names,
)


def test_e_factor_and_pmi_from_mass_balance() -> None:
    """E-factor is waste/product and PMI = E-factor + 1 on the same balance."""
    case = EvalCase(
        id="c",
        metrics=["e_factor", "pmi"],
        output={"input_masses_kg": [90, 10], "product_mass_kg": 10},
    )
    e = get_metric("e_factor")(case)
    pmi = get_metric("pmi")(case)
    assert e.value == pytest.approx(9.0)  # waste 90 / product 10
    assert pmi.value == pytest.approx(10.0)  # input 100 / product 10
    assert pmi.value == pytest.approx(e.value + 1.0)


def test_metric_threshold_comes_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pass/fail line is the config value, not a hardcoded constant (G3)."""
    # waste 30 / product 10 -> E-factor 3.0
    case = EvalCase(
        id="c", metrics=["e_factor"], output={"input_masses_kg": [30, 10], "product_mass_kg": 10}
    )
    monkeypatch.setattr(settings, "eval_efactor_max", 5.0)
    assert get_metric("e_factor")(case).passed is True  # 3.0 <= 5.0
    monkeypatch.setattr(settings, "eval_efactor_max", 2.0)
    assert get_metric("e_factor")(case).passed is False  # 3.0 > 2.0


def test_prediction_error_needs_reference() -> None:
    """An accuracy metric without a reference fails clearly, not with a crash (G4)."""
    case = EvalCase(id="c", metrics=["prediction_error"], output={"predicted": 1.0})
    with pytest.raises(MetricError, match="reference"):
        get_metric("prediction_error")(case)


def test_bo_regret_is_direction_aware() -> None:
    """Regret is non-negative and a progress metric (no pass threshold)."""
    case = EvalCase(
        id="c",
        metrics=["bo_regret"],
        output={"best_value": 95.0, "direction": "maximize"},
        reference={"optimum": 98.7},
    )
    r = get_metric("bo_regret")(case)
    assert r.value == pytest.approx(3.7)
    assert r.passed is None


def test_bad_mass_balance_is_a_clear_error() -> None:
    """A zero product mass (division) is a named MetricError, not a ZeroDivisionError."""
    case = EvalCase(id="c", metrics=["pmi"], output={"input_masses_kg": [1], "product_mass_kg": 0})
    with pytest.raises(MetricError, match="product_mass_kg"):
        get_metric("pmi")(case)


def test_seed_metrics_are_registered() -> None:
    """The seed metrics populate the registry on import (2b.5 extension seam)."""
    assert {"e_factor", "pmi", "prediction_error", "bo_regret"} <= set(registered_names())


def test_harness_runs_over_versioned_case_set_and_gates() -> None:
    """The harness scores the real case-set reproducibly and flags the failing case."""
    cases = load_eval_cases(settings.eval_case_dir)
    assert {c.id for c in cases} == {
        "bo-regret-reizman",
        "green-esterification",
        "pharma-solvent-heavy",
        "solubility-benzene",
        "retrieval-precision-recall",
    }
    report = run_eval(cases, case_set_version="v1")
    failed_ids = {r.case_id for r in report.failed()}
    assert failed_ids == {"pharma-solvent-heavy"}  # only the solvent-heavy case fails the gate
    # Reproducible: same inputs -> identical values.
    assert run_eval(cases, "v1").model_dump() == report.model_dump()


def test_report_is_citable() -> None:
    """Each report row carries its case id and the metric provenance (G5)."""
    cases = load_eval_cases(settings.eval_case_dir)
    text = render_report(run_eval(cases, "v1"))
    assert "case-set v1" in text
    assert "solubility-benzene" in text
    assert "Delaney" not in text  # provenance is the metric's, not the note body
    assert "tolerance" in text  # prediction_error provenance cites its threshold
    assert "**FAIL**" in text  # the failing gated case is visible


def test_load_rejects_malformed_case(tmp_path: Path) -> None:
    """A case file missing required fields fails loudly with its path (G4)."""
    (tmp_path / "bad.md").write_text("---\nid: x\n---\nno metrics\n", encoding="utf-8")
    with pytest.raises(EvalCaseError, match="invalid eval case"):
        load_eval_cases(str(tmp_path))


def test_load_rejects_misspelled_key(tmp_path: Path) -> None:
    """An unknown top-level key is rejected, not silently dropped (G4)."""
    text = "---\nid: x\nmetrics: [e_factor]\noutputt: {}\n---\ntypo in `output`\n"
    (tmp_path / "typo.md").write_text(text, encoding="utf-8")
    with pytest.raises(EvalCaseError, match="invalid eval case"):
        load_eval_cases(str(tmp_path))


def test_missing_case_dir_raises(tmp_path: Path) -> None:
    """A missing case directory raises, not a vacuously green empty report (G4)."""
    with pytest.raises(EvalCaseError, match="does not exist"):
        load_eval_cases(str(tmp_path / "nope"))


def test_empty_case_dir_raises(tmp_path: Path) -> None:
    """A directory with zero cases raises — an empty case-set gates nothing (G4)."""
    with pytest.raises(EvalCaseError, match="empty case-set"):
        load_eval_cases(str(tmp_path))


def test_cli_exits_nonzero_on_unloadable_case_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The CLI is red when the case-set cannot be loaded (mistyped directory)."""
    monkeypatch.setattr(sys, "argv", ["evals.harness", str(tmp_path / "missing")])
    assert main() == 1


def test_cli_reports_failing_gate_but_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failing gated metric is reported, not an exit code — gating is the tests' job.

    The versioned case-set deliberately contains a gate-failing demonstration case
    (pharma-solvent-heavy), so the CLI must render its FAIL loudly while exiting 0;
    which cases must pass/fail is pinned by this suite, and only an unloadable or
    unscorable case-set exits non-zero.
    """
    monkeypatch.setattr(sys, "argv", ["evals.harness", settings.eval_case_dir, "v1"])
    assert main() == 0
    assert "**FAIL**" in capsys.readouterr().out  # the report still shows the red gate


def test_eval_errors_are_chemclaw_errors() -> None:
    """Both eval error types share the one bad-data base (reject-and-continue)."""
    assert issubclass(MetricError, ChemclawError)
    assert issubclass(EvalCaseError, ChemclawError)


def test_bo_regret_requires_direction() -> None:
    """A missing direction is an error, not a silent maximize (sign-flip, G4)."""
    case = EvalCase(
        id="c", metrics=["bo_regret"], output={"best_value": 1.0}, reference={"optimum": 2.0}
    )
    with pytest.raises(MetricError, match="direction"):
        get_metric("bo_regret")(case)


def test_boolean_is_not_a_number() -> None:
    """YAML parses `yes` as True; scoring it as 1.0 would be silently wrong (G4)."""
    case = EvalCase(
        id="c", metrics=["prediction_error"], output={"predicted": True}, reference={"actual": 1.0}
    )
    with pytest.raises(MetricError, match="must be a number"):
        get_metric("prediction_error")(case)


def test_zero_input_mass_entry_is_allowed() -> None:
    """An unused feed (0 kg) is a valid entry; only the product mass must be > 0."""
    case = EvalCase(
        id="c", metrics=["pmi"], output={"input_masses_kg": [0, 20], "product_mass_kg": 10}
    )
    assert get_metric("pmi")(case).value == pytest.approx(2.0)


def test_task_scores_reject_non_finite() -> None:
    """A NaN/inf score is rejected at the model, not silently 'no effect' (G4)."""
    for bad in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValidationError):
            TaskScores(task_id="t", baseline=bad, augmented=1.0)
        with pytest.raises(ValidationError):
            TaskScores(task_id="t", baseline=1.0, augmented=bad)


def test_unknown_metric_names_the_case() -> None:
    """A mistyped metric name surfaces as a case-named error, not a raw crash (G4)."""
    case = EvalCase(id="c", metrics=["e_facto"], output={})
    with pytest.raises(EvalCaseError, match="case 'c' metric 'e_facto'"):
        run_eval([case], "v1")


def test_non_scalar_mass_is_a_clear_error() -> None:
    """A list where a scalar mass is expected is a MetricError, not a TypeError (G4)."""
    case = EvalCase(
        id="c", metrics=["pmi"], output={"input_masses_kg": [1], "product_mass_kg": [1, 2]}
    )
    with pytest.raises(MetricError, match="product_mass_kg"):
        get_metric("pmi")(case)


def test_mass_balance_violation_is_rejected() -> None:
    """A product heavier than the total input is impossible, not a negative-E pass (G4)."""
    case = EvalCase(
        id="c", metrics=["e_factor"], output={"input_masses_kg": [1], "product_mass_kg": 5}
    )
    with pytest.raises(MetricError, match="mass balance"):
        get_metric("e_factor")(case)


def test_tool_utility_surfaces_where_tools_do_not_help() -> None:
    """A/B over a task set finds a task where augmentation hurts (F8/F9 evidence)."""
    tasks = [
        TaskScores(task_id="t1", baseline=0.5, augmented=0.9),  # tools help
        TaskScores(task_id="t2", baseline=0.8, augmented=0.6),  # tools hurt
        TaskScores(task_id="t3", baseline=0.7, augmented=0.7),  # no effect
    ]
    summary = compare_tool_utility(tasks, higher_is_better=True)
    assert summary.helped == ["t1"]
    assert summary.hurt == ["t2"]  # the selective-steering proof: tools are not universal
    assert summary.no_effect == ["t3"]
    assert summary.net_delta == pytest.approx(0.2)


def test_tool_utility_respects_direction() -> None:
    """For a lower-is-better metric, a smaller augmented value counts as help."""
    tasks = [TaskScores(task_id="t", baseline=5.0, augmented=2.0)]
    summary = compare_tool_utility(tasks, higher_is_better=False)
    assert summary.helped == ["t"]
    assert summary.utilities[0].delta == pytest.approx(3.0)


def test_sub_epsilon_delta_is_no_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    """A delta within +/- epsilon lands in "no effect", not helped/hurt.

    Guards the noise-floor band: with the old 0.0 default a delta this small was
    wrongly credited as "helped"; a positive epsilon must absorb it.
    """
    monkeypatch.setattr(settings, "eval_ab_epsilon", 0.01)
    tasks = [TaskScores(task_id="t", baseline=0.700, augmented=0.705)]  # delta 0.005 < 0.01
    summary = compare_tool_utility(tasks, higher_is_better=True)
    assert summary.no_effect == ["t"]
    assert summary.helped == []
    assert summary.hurt == []
