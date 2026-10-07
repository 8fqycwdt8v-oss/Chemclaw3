"""Behavioral tests for the evaluation and metric layer.

Metrics are pure and config-thresholded, the harness runs reproducibly over the versioned case-set
and renders a citable report, and the tool-utility A/B surfaces at least one task where tooling
does not help.
"""

import math
import re
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.evals.ab import TaskScores, compare_tool_utility
from chemclaw.evals.harness import (
    EvalCaseError,
    EvalReport,
    ScoredResult,
    load_eval_cases,
    main,
    render_report,
    run_eval,
)
from chemclaw.evals.metric import (
    EvalCase,
    MetricError,
    MetricResult,
    gated_names,
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
    """The harness scores the real chemistry case-set reproducibly and flags the failing case."""
    cases = load_eval_cases(settings.eval_case_dir)
    # Named positively rather than as "everything not retrieval-*": other case families have their
    # own fixtures, and a negative filter would make each new family break the exact-equality
    # assertion below. One list serves both the membership check and the filter.
    chemistry = {
        "bo-regret-reizman",
        "green-esterification",
        "pharma-solvent-heavy",
        "solubility-benzene",
    }
    assert chemistry <= {c.id for c in cases}
    chem_cases = [c for c in cases if c.id in chemistry]
    report = run_eval(chem_cases, case_set_version="v1")
    failed_ids = {r.case_id for r in report.failed()}
    assert failed_ids == {"pharma-solvent-heavy"}  # only the solvent-heavy case fails the gate
    # Reproducible: same inputs -> identical values.
    assert run_eval(chem_cases, "v1").model_dump() == report.model_dump()


def test_report_is_citable() -> None:
    """Each report row carries its case id and the metric provenance (G5)."""
    cases = load_eval_cases(settings.eval_case_dir)
    text = render_report(run_eval(cases, "v1"))
    assert "case-set v1" in text
    assert "solubility-benzene" in text
    assert "Delaney" not in text  # provenance is the metric's, not the note body
    assert "tolerance" in text  # prediction_error provenance cites its threshold
    assert "**FAIL**" in text  # the failing gated case is visible


def test_report_cells_escape_pipes() -> None:
    """A provenance containing '|' (set-cardinality notation) cannot split its table row.

    precision/recall/prediction_error legitimately write pipes into provenance; without
    escaping, a Markdown renderer shifts their values under the wrong headers (G5).
    """
    report = EvalReport(
        case_set_version="v1",
        results=[
            ScoredResult(
                case_id="c",
                result_metric="precision",
                value=0.5,
                unit=None,
                passed=None,
                provenance="precision = |predicted ∩ expected| 1 / |predicted| 2",
            )
        ],
    )
    row = next(line for line in render_report(report).splitlines() if line.startswith("| c |"))
    assert len(re.findall(r"(?<!\\)\|", row)) == 7  # 6 columns = exactly 7 raw delimiters
    assert "\\|predicted ∩ expected\\|" in row  # the notation survives, escaped


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
    monkeypatch.setattr(sys, "argv", ["chemclaw.evals.harness", str(tmp_path / "missing")])
    assert main() == 1


def test_cli_reports_failing_gate_but_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failing gated metric is reported, not turned into an exit code, without `--strict`.

    The case-set contains a deliberately gate-failing demonstration case, so the CLI renders its
    FAIL while exiting 0; only an unloadable or unscorable case-set exits non-zero.
    """
    monkeypatch.setattr(
        sys,
        "argv",
        ["chemclaw.evals.harness", settings.eval_case_dir, "--case-set-version", "v1"],
    )
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


def test_tool_utility_rejects_empty_task_list() -> None:
    """tasks=[] raises instead of returning a vacuous 'no effect anywhere' summary (G4)."""
    with pytest.raises(ValueError, match="empty task list"):
        compare_tool_utility([], higher_is_better=True)


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


def test_strict_mode_gates_on_a_regression_and_not_on_a_demonstration() -> None:
    """`--strict` gates on a regression and not on a demonstration.

    Some shipped cases exist to demonstrate a gate firing. `expect_pass` separates "this failed"
    from "this was supposed to fail", so `make eval-strict` can fail on the difference.
    """
    from chemclaw.evals.harness import main

    # The shipped case-set: three gated metrics fail, all of them by design.
    report = run_eval(load_eval_cases(settings.eval_case_dir), "v1")
    assert report.failed(), "the demonstration cases stopped failing; this test proves nothing"
    assert report.regressions() == []
    assert main(["--strict"]) == 0

    # A *new* case is gated unless someone deliberately says otherwise.
    assert EvalCase(id="x", metrics=["pmi"]).expect_pass is True
    assert report.inert_demonstrations() == []


def test_strict_mode_fails_when_a_declared_demonstration_stops_demonstrating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--strict` fails when a declared demonstration stops demonstrating.

    `expect_pass: false` is an assertion, not a mute: a gate that quietly stops firing (e.g. a
    raised PMI limit) removes the by-design failure and would otherwise leave the command green.
    """
    from chemclaw.evals.harness import main

    monkeypatch.setattr(settings, "eval_efactor_max", 1000.0)
    monkeypatch.setattr(settings, "eval_pmi_max", 1000.0)
    report = run_eval(load_eval_cases(settings.eval_case_dir), "v1")

    assert report.regressions() == []  # nothing *failed* that should not have
    assert report.inert_demonstrations() == ["pharma-solvent-heavy"]
    assert main(["--strict"]) == 1
    assert "no longer fails" in render_report(report)


def test_every_gated_metric_has_a_case_that_makes_it_fail() -> None:
    """Every gated metric has a case that makes it fail.

    The per-case check cannot see a metric scored only over cases written to pass; such a metric
    could return a constant "perfect" and stay green. Asserted over the shipped case-set, since the
    claim is about what CI scores.
    """
    cases = load_eval_cases(settings.eval_case_dir)
    report = run_eval(cases, "v1")
    gated = {r.result_metric for r in report.results if r.passed is not None}
    assert gated, "no metric in the shipped set is gated; this test proves nothing"
    assert report.gates_no_demonstration_can_fire() == []

    # The positive control, without which this assertion is satisfied by the subject returning an
    # empty list unconditionally: drop every demonstration and *every* gated metric must be named.
    without = run_eval([c for c in cases if c.expect_pass], "v1")
    assert set(without.gates_no_demonstration_can_fire()) == gated


def test_strict_mode_fails_when_a_gated_metric_stops_measuring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--strict` fails when a gated metric stops measuring.

    A metric patched to answer "perfect" through the registry leaves the case-set untouched. The
    report names the metric as well as the case, so a reader looks at the right one.
    """
    from chemclaw.evals import metric as metric_module
    from chemclaw.evals.harness import main

    perfect = MetricResult(
        metric="prediction_error", value=0.0, passed=True, provenance="stopped measuring"
    )
    # Substituting the registry entry is the mutation: what breaks is the *metric*, and the
    # case-set stays untouched.
    registry = metric_module._REGISTRY
    monkeypatch.setitem(registry, "prediction_error", lambda case: perfect)
    report = run_eval(load_eval_cases(settings.eval_case_dir), "v1")

    assert report.regressions() == []  # nothing *failed* that should not have
    assert report.gates_no_demonstration_can_fire() == ["prediction_error"]
    assert main(["--strict"]) == 1
    assert "no demonstration case" in render_report(report)


def test_a_gated_metric_whose_cases_are_all_gone_is_still_owed_a_demonstration(
    tmp_path: Path,
) -> None:
    """A gated metric whose cases are all gone is still owed a demonstration.

    If `gated` were derived from the run's results, deleting a metric's cases would remove it from
    the check entirely. A case-set change is also when the baseline is re-recorded, which would
    erase the only other signal.
    """
    dropped = "runaway_rate"
    for case in Path(settings.eval_case_dir).glob("*.md"):
        text = case.read_text(encoding="utf-8")
        if dropped not in text:
            (tmp_path / case.name).write_text(text, encoding="utf-8")

    report = run_eval(load_eval_cases(str(tmp_path)), "v1")

    assert dropped not in {r.result_metric for r in report.results}, (
        "the fixture still scores the metric, so this test is not about a case-set that lost it"
    )
    assert dropped in report.gates_no_demonstration_can_fire()
    assert report.regressions() == [] and report.inert_demonstrations() == [], (
        "another clause is producing the finding; this test would then pass without its subject"
    )
    assert main([str(tmp_path), "--strict"]) == 1


def test_every_scored_metrics_gatedness_is_the_one_it_declares() -> None:
    """Every scored metric's gatedness is the one it declares.

    The registry flag is what makes an absent metric visible, so it is reconciled with behaviour
    both
    ways: a metric returning a verdict must be declared gated, and one that never does must not be.
    """
    report = run_eval(load_eval_cases(settings.eval_case_dir), "v1")
    observed: dict[str, bool] = {}
    for result in report.results:
        observed[result.result_metric] = observed.get(result.result_metric, False) or (
            result.passed is not None
        )
    declared = gated_names()

    assert observed, "the shipped case-set scored nothing; this test proves nothing"
    wrong = {name: seen for name, seen in observed.items() if seen != (name in declared)}
    assert not wrong, (
        f"these metrics' declared gatedness disagrees with what they scored: {wrong}. A metric "
        "returning a verdict must register `gated=True`, and one that never does must not."
    )


def test_an_ungated_metric_owes_the_set_no_demonstration() -> None:
    """An ungated metric owes the set no demonstration.

    Metrics reporting a number rather than a verdict (`passed is None`) have no threshold to fail.
    """
    report = run_eval(load_eval_cases(settings.eval_case_dir), "v1")
    ungated = {r.result_metric for r in report.results if r.passed is None}

    assert "turn_cost_ratio" in ungated
    assert not (ungated & set(report.gates_no_demonstration_can_fire()))


def test_strict_mode_reads_the_unfireable_check_and_not_only_the_inert_one(
    tmp_path: Path,
) -> None:
    """`--strict` exits 1 for an unfireable gate alone, with the other two clauses empty.

    The registry-substitution test above also makes a demonstration case stop failing, so its exit
    code comes from a different clause. Here every `expect_pass: false` case is removed, so
    `regressions()` and `inert_demonstrations()` are empty (asserted) and only the clause under test
    can produce the 1.
    """
    for case in Path(settings.eval_case_dir).glob("*.md"):
        if "expect_pass: false" not in case.read_text(encoding="utf-8"):
            (tmp_path / case.name).write_text(case.read_text(encoding="utf-8"), encoding="utf-8")

    report = run_eval(load_eval_cases(str(tmp_path)), "v1")
    assert report.regressions() == []
    assert report.inert_demonstrations() == []
    assert report.gates_no_demonstration_can_fire(), "no gate is unfireable; this proves nothing"

    assert main([str(tmp_path), "--strict"]) == 1
    # And the same case-set without `--strict` is a 0, so the 1 above is the flag reading the
    # check rather than the run being broken.
    assert main([str(tmp_path)]) == 0


def test_a_real_regression_fails_strict_mode() -> None:
    """The behaviour the CI step's name promises: a science regression is a non-zero exit."""
    solvent_heavy = next(
        case
        for case in load_eval_cases(settings.eval_case_dir)
        if case.id == "pharma-solvent-heavy"
    )
    # The same failing case, but no longer declared as a demonstration — which is exactly the shape
    # of a real regression: a gated metric that was expected to pass and did not.
    regressed = solvent_heavy.model_copy(update={"expect_pass": True})
    report = run_eval([regressed], "v1")

    assert report.failed()
    assert report.regressions() == report.failed()
