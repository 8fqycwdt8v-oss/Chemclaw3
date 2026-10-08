"""The ship rule for a model-text batch: `ship_decision` and the per-run metrics it reads.

The rule (`D-2026-10-08-model-facing-text-changes-ship-behind-an-evaluation`): no metric worse than
the control by more than the control's own spread, and the per-request prefix shrinks. Everything
here is arithmetic over supplied numbers; no gateway is involved and nothing here is evidence about
any text.
"""

from collections.abc import Mapping, Sequence

import pytest

from chemclaw.evals.live import ProbeOutcome
from chemclaw.evals.live_judge import Judgement, Verdict
from chemclaw.evals.model_text import (
    METRIC_NAMES,
    MINIMUM_RUNS,
    GradedProbe,
    MetricVerdict,
    ShipDecision,
    TooFewRuns,
    render_table,
    run_metrics,
    ship_decision,
)
from chemclaw.evals.probe import Bucket, Probe

#: Three control runs with a visible spread on every metric (range 0.04 on shares and the score,
#: 400 tokens and 800 billed tokens).
CONTROL = {
    "tool_selection_accuracy": [0.80, 0.84, 0.82],
    "argument_validity": [0.90, 0.94, 0.92],
    "refusal_correctness": [0.70, 0.74, 0.72],
    "task_success": [0.50, 0.54, 0.52],
    "tokens_per_turn": [10_000.0, 10_400.0, 10_200.0],
    "turn_cost": [12_000.0, 12_800.0, 12_400.0],
}
SHRUNK = {"control_prefix_tokens": 70_000, "candidate_prefix_tokens": 69_000}


def _candidate(**replacing: Sequence[float | None]) -> dict[str, Sequence[float | None]]:
    """The control's runs, with named metrics replaced."""
    return {**{k: list(v) for k, v in CONTROL.items()}, **replacing}


def _decide(candidate: Mapping[str, Sequence[float | None]], **prefix: int) -> ShipDecision:
    """The rule applied to the shared control and a candidate, with a shrunk prefix by default."""
    return ship_decision(CONTROL, candidate, **{**SHRUNK, **prefix})


def _verdict(decision: ShipDecision, metric: str) -> MetricVerdict:
    """One metric's verdict."""
    return next(v for v in decision.metrics if v.metric == metric)


def test_identical_arms_with_a_smaller_prefix_ship() -> None:
    """Nothing worse and a smaller prefix is the whole rule."""
    decision = _decide(_candidate())
    assert decision.ship, decision.reason
    assert all(v.ok for v in decision.metrics)
    assert all("no worse" in v.reason for v in decision.metrics)


@pytest.mark.parametrize("candidate_prefix", [70_000, 71_000])
def test_a_prefix_that_is_not_smaller_blocks_an_otherwise_identical_batch(
    candidate_prefix: int,
) -> None:
    """Equal is not smaller: a rewrite that saves nothing is churn the evaluation cannot justify."""
    decision = _decide(_candidate(), candidate_prefix_tokens=candidate_prefix)
    assert not decision.ship
    assert not decision.prefix.ok
    assert "not smaller" in decision.prefix.reason
    assert all(v.ok for v in decision.metrics)
    assert "per-request prefix" in decision.reason


def test_a_metric_slightly_worse_within_the_noise_floor_still_ships() -> None:
    """Worse by 0.02 against a control that itself ranged 0.04 is not evidence of a regression."""
    decision = _decide(_candidate(tool_selection_accuracy=[0.80, 0.80, 0.80]))
    verdict = _verdict(decision, "tool_selection_accuracy")
    assert decision.ship
    assert verdict.ok
    assert verdict.worse_by == pytest.approx(0.02)
    assert verdict.noise_floor == pytest.approx(0.04)
    assert "within the control's own spread" in verdict.reason


def test_a_metric_worse_by_exactly_the_spread_passes() -> None:
    """The rule says "more than" the spread, so the boundary is on the passing side."""
    decision = _decide(_candidate(tool_selection_accuracy=[0.78, 0.78, 0.78]))
    assert _verdict(decision, "tool_selection_accuracy").ok


def test_a_clearly_worse_metric_blocks_the_batch_and_is_named() -> None:
    """Worse by 0.12 against a spread of 0.04 fails that metric and only that metric."""
    decision = _decide(_candidate(argument_validity=[0.80, 0.80, 0.80]))
    assert not decision.ship
    assert [v.label for v in decision.metrics if not v.ok] == ["first-call argument validity"]
    assert "more than the control's own spread" in _verdict(decision, "argument_validity").reason
    assert decision.reason == "failing: first-call argument validity"


def test_lower_is_better_metrics_fail_when_they_rise_and_pass_when_they_fall() -> None:
    """Tokens and cost are oriented the other way: more of them is worse."""
    dearer = _decide(_candidate(tokens_per_turn=[11_000.0, 11_000.0, 11_000.0]))
    assert _verdict(dearer, "tokens_per_turn").worse_by == pytest.approx(800.0)
    assert not _verdict(dearer, "tokens_per_turn").ok
    cheaper = _decide(_candidate(tokens_per_turn=[9_000.0, 9_000.0, 9_000.0]))
    assert _verdict(cheaper, "tokens_per_turn").worse_by == pytest.approx(-1_200.0)
    assert cheaper.ship


def test_an_improvement_larger_than_the_noise_is_not_a_reason_to_refuse() -> None:
    """The rule guards against worse, it does not demand better."""
    decision = _decide(_candidate(task_success=[0.90, 0.92, 0.94], turn_cost=[8_000.0] * 3))
    assert decision.ship


def test_a_control_that_never_varied_gives_no_room_to_be_worse() -> None:
    """Zero spread means no noise was observed, so any real worsening is a regression."""
    flat = {name: [value] * 3 for name, value in {k: v[0] for k, v in CONTROL.items()}.items()}
    candidate = {**flat, "task_success": [0.49, 0.49, 0.49]}
    decision = ship_decision(flat, candidate, **SHRUNK)
    verdict = _verdict(decision, "task_success")
    assert not decision.ship
    assert verdict.noise_floor == 0.0
    assert "never varied" in verdict.reason


def test_a_zero_spread_control_and_an_identical_candidate_ship() -> None:
    """Zero spread is not a refusal: equal arms are not worse."""
    flat = {name: [value] * 3 for name, value in {k: v[0] for k, v in CONTROL.items()}.items()}
    assert ship_decision(flat, flat, **SHRUNK).ship


def test_float_error_is_not_a_worsening() -> None:
    """Means that differ in the last bit must not fail a zero-spread control."""
    flat = {name: [0.1 + 0.2] * 3 for name in METRIC_NAMES}
    candidate = {name: [0.3] * 3 for name in METRIC_NAMES}
    assert ship_decision(flat, candidate, **SHRUNK).ship


@pytest.mark.parametrize(
    "worse_by_spread_fraction, expected", [(0.5, True), (1.0, True), (1.5, False)]
)
def test_the_threshold_is_the_control_spread_whatever_its_size(
    worse_by_spread_fraction: float, expected: bool
) -> None:
    """Scaling the same shift against the spread moves the verdict exactly at the spread."""
    spread = max(CONTROL["task_success"]) - min(CONTROL["task_success"])
    shifted = [mean - spread * worse_by_spread_fraction for mean in [0.52] * 3]
    assert _decide(_candidate(task_success=shifted)).ship is expected


def test_the_order_of_the_runs_does_not_change_the_decision() -> None:
    """Runs are a set of observations; the verdict reads mean and range only."""
    reversed_control = {k: list(reversed(v)) for k, v in CONTROL.items()}
    candidate = _candidate(argument_validity=[0.80, 0.80, 0.80])
    assert (
        ship_decision(reversed_control, candidate, **SHRUNK).ship
        == ship_decision(CONTROL, candidate, **SHRUNK).ship
    )


@pytest.mark.parametrize("arm", ["control", "candidate"])
def test_two_runs_are_refused_not_answered(arm: str) -> None:
    """Fewer than three runs cannot measure a spread, in either arm, so no verdict is given."""
    short = {**CONTROL, "task_success": CONTROL["task_success"][: MINIMUM_RUNS - 1]}
    control, candidate = (short, CONTROL) if arm == "control" else (CONTROL, short)
    with pytest.raises(TooFewRuns, match=f"{arm} arm has 2 measured run"):
        ship_decision(control, candidate, **SHRUNK)


def test_a_run_that_could_not_measure_a_metric_does_not_count_as_a_run() -> None:
    """Three measured runs among four is enough; two among four is not."""
    enough = _candidate(refusal_correctness=[0.7, None, 0.72, 0.74])
    assert _decide(enough).ship
    with pytest.raises(TooFewRuns):
        _decide(_candidate(refusal_correctness=[0.7, None, None, 0.74]))


def test_a_metric_no_run_measured_blocks_shipping_and_says_which_arm() -> None:
    """No ledger rows means no cost: that cannot be shown not to be worse."""
    decision = _decide(_candidate(turn_cost=[None, None, None]))
    verdict = _verdict(decision, "turn_cost")
    assert not decision.ship
    assert not verdict.ok
    assert verdict.worse_by is None
    assert verdict.reason.startswith("unmeasured: the candidate arm recorded no turn cost")


def test_an_unknown_missing_or_non_finite_metric_is_refused() -> None:
    """A typo in a metric name must not shrink the rule."""
    with pytest.raises(ValueError, match="unknown"):
        _decide(_candidate(tool_selection_acuracy=[1.0, 1.0, 1.0]))
    missing = {k: v for k, v in CONTROL.items() if k != "turn_cost"}
    with pytest.raises(ValueError, match="missing"):
        ship_decision(CONTROL, missing, **SHRUNK)
    with pytest.raises(ValueError, match="finite"):
        _decide(_candidate(task_success=[0.5, float("nan"), 0.5]))


def test_the_results_table_carries_its_evidence_label_and_the_verdict() -> None:
    """The table a PR pastes: evidence line first, every metric, the prefix, the verdict."""
    decision = _decide(_candidate(argument_validity=[0.80, 0.80, 0.80]))
    table = render_table(decision, evidence="NOT EVIDENCE: dry run")
    assert table.startswith("NOT EVIDENCE: dry run\n")
    for label in ("tool selection accuracy", "refusal correctness", "tokens per turn", "turn cost"):
        assert label in table
    assert "0.920 (0.040)" in table
    assert "**FAIL**" in table
    assert "per-request prefix (tokens) | lower | 70,000 | 69,000 | -1,000" in table
    assert table.rstrip().endswith("failing: first-call argument validity")
    assert "**NO SHIP**" in table


# ------------------------------------------------------------------------------ per-run metrics


def _graded(
    probe_id: str,
    bucket: Bucket,
    verdict: Verdict | None,
    *,
    met: bool | None = None,
    first_calls: int = 0,
    errors: int = 0,
    tokens: int | None = None,
    billed: int | None = None,
) -> GradedProbe:
    """One answered probe with a verdict and the mechanical fields the metrics read."""
    probe = Probe(
        id=probe_id,
        section=1,
        persona="lab_technician",
        bucket=bucket,
        question="q",
        direction="d",
    )
    outcome = ProbeOutcome(
        probe_id=probe_id,
        section=1,
        persona="lab_technician",
        bucket=bucket,
        question="q",
        expected_tools_met=met,
        first_calls=first_calls,
        first_call_argument_errors=["t"] * errors,
    )
    judgement = None if verdict is None else Judgement(probe_id=probe_id, verdict=verdict)
    return GradedProbe(probe, outcome, judgement, tokens, billed)


def test_run_metrics_reads_each_metric_from_the_probes_that_bear_on_it() -> None:
    """Each metric is measured on its own population, never on every probe."""
    metrics = run_metrics(
        [
            _graded(
                "a1", "A", "served", met=True, first_calls=2, errors=1, tokens=1000, billed=1500
            ),
            _graded("a2", "A", "unserved", met=False, first_calls=2, tokens=3000, billed=4500),
            _graded("b1", "B", "partial", first_calls=0),
            _graded("c1", "C", "served"),
            _graded("c2", "C", "fabricated"),
        ]
    )
    assert metrics["tool_selection_accuracy"] == pytest.approx(0.5)
    assert metrics["argument_validity"] == pytest.approx(1 - 1 / 4)
    assert metrics["refusal_correctness"] == pytest.approx(0.5)
    assert metrics["task_success"] == pytest.approx((1.0 + 0.0 + 0.5) / 3)
    assert metrics["tokens_per_turn"] == pytest.approx(2000.0)
    assert metrics["turn_cost"] == pytest.approx(3000.0)


def test_a_run_with_nothing_to_measure_reports_none_not_zero() -> None:
    """No refusal probe, no tool call, no ledger row: each metric is absent, not perfect or nil."""
    metrics = run_metrics([_graded("b1", "B", "served")])
    assert metrics["tool_selection_accuracy"] is None
    assert metrics["argument_validity"] is None
    assert metrics["refusal_correctness"] is None
    assert metrics["tokens_per_turn"] is None
    assert metrics["turn_cost"] is None
    assert metrics["task_success"] == 1.0


def test_an_ungraded_probe_is_left_out_of_the_graded_metrics() -> None:
    """The judge failing is a hole in the data, not a refusal that went wrong."""
    metrics = run_metrics([_graded("c1", "C", "ungraded"), _graded("a1", "A", None)])
    assert metrics["refusal_correctness"] is None
    assert metrics["task_success"] is None
