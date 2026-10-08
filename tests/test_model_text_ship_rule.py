"""The ship rule for a model-text batch: `ship_decision` and the per-run metrics it reads.

The rule (`D-2026-10-08-model-facing-text-changes-ship-behind-an-evaluation`): no metric worse than
the control by more than the control's own spread, and the per-request prefix shrinks. Everything
here is arithmetic over supplied numbers; no gateway is involved and nothing here is evidence about
any text.
"""

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from chemclaw.evals.live import ProbeOutcome
from chemclaw.evals.live_judge import Judgement, Verdict
from chemclaw.evals.model_text import (
    METRIC_NAMES,
    MINIMUM_RUNS,
    SHIP_MINIMUM_RUNS,
    ArmCoverage,
    CoverageReport,
    GradedProbe,
    MetricVerdict,
    ShipDecision,
    TooFewRuns,
    completed,
    power_sentence,
    render_table,
    restrict_to_common,
    rule_power,
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
#: Three-run samples below, so the statistical tests ask for no more than the spread needs; the
#: ship floor (`SHIP_MINIMUM_RUNS`) has its own tests further down.
SHRUNK: dict[str, Any] = {
    "control_prefix_tokens": 70_000,
    "candidate_prefix_tokens": 69_000,
    "minimum_runs": MINIMUM_RUNS,
}


def _candidate(**replacing: Sequence[float | None]) -> dict[str, Sequence[float | None]]:
    """The control's runs, with named metrics replaced."""
    return {**{k: list(v) for k, v in CONTROL.items()}, **replacing}


def _decide(candidate: Mapping[str, Sequence[float | None]], **options: Any) -> ShipDecision:
    """The rule applied to the shared control and a candidate, with a shrunk prefix by default."""
    return ship_decision(CONTROL, candidate, **{**SHRUNK, **options})


def _verdict(decision: ShipDecision, metric: str) -> MetricVerdict:
    """One metric's verdict."""
    return next(v for v in decision.metrics if v.metric == metric)


def test_identical_arms_with_a_smaller_prefix_ship() -> None:
    """Nothing worse and a smaller prefix is the whole rule."""
    decision = _decide(_candidate())
    assert decision.ship, decision.reason
    assert all(v.ok for v in decision.metrics)
    assert all("no worse" in v.reason for v in decision.metrics)


@pytest.mark.parametrize(
    ("candidate_prefix", "claims", "ok"),
    [
        (69_000, False, True),  # smaller, no claim
        (70_000, False, True),  # equal, no claim: a neutral rewording
        (70_100, False, True),  # grew by exactly the tolerance
        (70_101, False, False),  # grew past it
        (69_999, True, True),  # claims a saving and shows one token of it
        (70_000, True, False),  # claims a saving and shows none
        (71_000, True, False),
    ],
)
def test_the_prefix_is_held_to_a_tolerance_and_to_the_batchs_own_claim(
    candidate_prefix: int, claims: bool, ok: bool
) -> None:
    """A neutral edit may move a little; a batch that says it saves tokens has to show it."""
    decision = _decide(
        _candidate(), candidate_prefix_tokens=candidate_prefix, claims_token_saving=claims
    )
    assert decision.prefix.ok is ok, decision.prefix.reason
    assert decision.ship is ok
    assert all(v.ok for v in decision.metrics)
    assert ("per-request prefix" in decision.reason) is (not ok)


def test_the_prefix_tolerance_is_an_input() -> None:
    """A deployment with more headroom (or less) states its own."""
    assert _decide(_candidate(), candidate_prefix_tokens=70_400, prefix_tolerance_tokens=400).ship
    assert not _decide(
        _candidate(), candidate_prefix_tokens=70_400, prefix_tolerance_tokens=399
    ).ship


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
    assert "per-request prefix (tokens) | lower | 70,000 | 69,000 | -1,000 | +100 allowed" in table
    assert "Runs per arm: 3 (a ship verdict needs 3)" in table
    assert "Power, first-call argument validity: σ≈" in table
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


# ------------------------------------------------------------ run count, power and coverage


def _five(**replacing: Sequence[float | None]) -> dict[str, Sequence[float | None]]:
    """Five-run samples with the control's means and ranges: two extra runs at the median."""
    padded = {k: [*v, v[2], v[2]] for k, v in CONTROL.items()}
    return {**padded, **replacing}


def test_the_default_ship_floor_is_five_runs_and_three_runs_can_never_ship() -> None:
    """Three runs give a spread and no ship: the metrics are reported, the verdict underpowered."""
    assert SHIP_MINIMUM_RUNS == 5
    three = ship_decision(
        CONTROL, _candidate(), control_prefix_tokens=70_000, candidate_prefix_tokens=69_000
    )
    assert all(v.ok for v in three.metrics)
    assert three.underpowered
    assert not three.ship
    assert three.reason.startswith("underpowered: 3 run(s) per arm and a ship verdict needs 5")


def test_five_runs_ship_an_identical_candidate_and_four_do_not() -> None:
    """The floor is on the weakest metric's measured runs, in either arm."""
    five = ship_decision(_five(), _five(), **{**SHRUNK, "minimum_runs": SHIP_MINIMUM_RUNS})
    assert five.ship and five.runs == 5
    four = {k: v[:4] for k, v in _five().items()}
    result = ship_decision(four, four, **{**SHRUNK, "minimum_runs": SHIP_MINIMUM_RUNS})
    assert result.underpowered and not result.ship and result.runs == 4


def test_a_metric_run_short_in_one_arm_sets_the_runs_the_verdict_stands_on() -> None:
    """Five runs of everything but three of one metric is a three-run verdict."""
    thin = _five(refusal_correctness=[0.7, None, 0.72, None, 0.74])
    result = ship_decision(_five(), thin, **{**SHRUNK, "minimum_runs": SHIP_MINIMUM_RUNS})
    assert result.runs == 3 and result.underpowered


def test_a_failing_metric_is_reported_as_failing_not_as_underpowered() -> None:
    """Underpowered only explains a verdict that would otherwise ship."""
    worse = _candidate(argument_validity=[0.5, 0.5, 0.5])
    result = ship_decision(CONTROL, worse, control_prefix_tokens=1, candidate_prefix_tokens=0)
    assert result.reason == "failing: first-call argument validity"
    assert result.underpowered


def test_the_ship_floor_cannot_be_set_below_what_a_spread_needs() -> None:
    """`minimum_runs` is a flag upwards only."""
    with pytest.raises(ValueError, match="below the 3"):
        ship_decision(CONTROL, CONTROL, **{**SHRUNK, "minimum_runs": 2})


def test_the_rules_power_is_simulated_reproducibly_and_matches_what_the_guide_says() -> None:
    """The figures in the evaluation guide are `rule_power`'s, not an assertion."""
    three, five = rule_power(3), rule_power(5)
    assert rule_power(3) is three, "cached, so one simulation per run count"
    assert three.pass_probability[1.0] == pytest.approx(0.71, abs=0.02)
    assert three.pass_probability[1.5] == pytest.approx(0.55, abs=0.02)
    assert three.six_metric_neutral_fail == pytest.approx(0.36, abs=0.03)
    assert five.six_metric_neutral_fail == pytest.approx(0.05, abs=0.02)
    # The rule gets quieter, not sharper, as runs are added: its floor widens with them.
    assert five.pass_probability[1.0] > three.pass_probability[1.0]
    assert five.expected_range > three.expected_range
    sentence = power_sentence(5)
    assert "5 runs per arm" in sentence and "catches a true regression of" in sentence


def test_a_report_says_what_its_floor_can_and_cannot_see() -> None:
    """Every metric carries its own power note; a control that never varied says it has none."""
    decision = _decide(_candidate())
    assert all("σ≈" in v.power_note for v in decision.metrics)
    flat = {name: [value[0]] * 3 for name, value in CONTROL.items()}
    note = _verdict(ship_decision(flat, flat, **SHRUNK), "task_success").power_note
    assert "never varied" in note


def _record(
    probe_id: str,
    *,
    ok: bool = True,
    transport: bool = False,
    verdict: Verdict | None = "served",
    tokens: int | None = 100,
) -> GradedProbe:
    """One probe record, complete unless told otherwise."""
    record = _graded(probe_id, "A", verdict if ok else "ungraded", tokens=tokens, billed=tokens)
    if transport:
        record.outcome.transport_error = "ConnectError: refused"
    return record


def test_a_probe_the_instrument_failed_on_is_dropped_but_a_silent_system_is_not() -> None:
    """Completed means the harness got its data; an unanswered turn is a result, not a hole."""
    assert completed(_record("a"))
    assert not completed(_record("a", ok=False))
    assert not completed(_record("a", transport=True))
    assert not completed(_record("a", tokens=None))
    silent = _record("a")
    silent.outcome.answered = False
    assert completed(silent)


def test_both_arms_are_compared_on_the_probes_every_run_of_both_completed() -> None:
    """A probe one arm could not grade in one run leaves the comparison everywhere."""
    runs = {
        "control": [[_record("a"), _record("b"), _record("c")] for _ in range(3)],
        "candidate": [
            [_record("a"), _record("b", ok=False), _record("c")],
            [_record("a"), _record("b"), _record("c", transport=True)],
            [_record("a"), _record("b"), _record("c")],
        ],
    }
    kept, report = restrict_to_common(runs)
    assert report.selected == 3 and report.common == 1
    assert {a.arm: a.dropped for a in report.arms} == {"control": 0, "candidate": 2}
    assert report.arms[1].share == pytest.approx(2 / 3)
    for arm in kept.values():
        assert all([r.probe.id for r in one] == ["a"] for one in arm)


def test_the_metrics_of_the_two_arms_then_share_a_denominator() -> None:
    """Without the restriction the candidate's mean would be over fewer, easier probes."""
    runs = {
        "control": [[_record("a", tokens=100), _record("b", tokens=900)]] * 3,
        "candidate": [[_record("a", tokens=100), _record("b", ok=False, tokens=None)]] * 3,
    }
    kept, _ = restrict_to_common(runs)
    assert run_metrics(kept["control"][0])["tokens_per_turn"] == 100.0
    assert run_metrics(kept["candidate"][0])["tokens_per_turn"] == 100.0
    unrestricted = run_metrics(runs["control"][0])["tokens_per_turn"]
    assert unrestricted == 500.0


def test_an_arm_that_dropped_too_many_probes_blocks_the_ship_and_is_reported() -> None:
    """Above the stated share the comparison is refused, whatever the metrics say."""
    report = CoverageReport(
        selected=20,
        common=15,
        arms=[
            ArmCoverage(arm="control", dropped=0, share=0.0),
            ArmCoverage(arm="candidate", dropped=5, share=0.25),
        ],
    )
    blocked = ship_decision(CONTROL, _candidate(), **SHRUNK, coverage=report)
    assert not blocked.ship
    assert blocked.coverage is not None and not blocked.coverage.ok
    assert "candidate 25%" in blocked.coverage.reason
    assert blocked.reason == "failing: probe coverage"
    table = render_table(blocked, evidence="x")
    assert "probes dropped (share) | lower | 0 (0.0%) | 5 (25.0%)" in table
    allowed = ship_decision(CONTROL, _candidate(), **SHRUNK, coverage=report, max_drop_share=0.25)
    assert allowed.ship
    assert allowed.coverage is not None and "15 of 20 probes" in allowed.coverage.reason
