"""The ship rule for a model-text batch, as code: pure statistics over per-run metric samples.

A batch of rewritten tool descriptions or prompt blocks ships only when no metric is worse than the
control by more than the control's own run-to-run spread, with the per-request prefix held to the
batch's claim (`D-2026-10-08-the-text-evaluation-states-its-power-and-holds-the-prefix-to-its-claim`
refines the decision that set the rule). This module runs no model and reads no file:
`ship_decision` takes one number per run per arm,
`run_metrics` folds one run's graded probes into those numbers, `restrict_to_common` puts both arms
on the probes both completed, and `render_table` is the table a pull request carries.

- **Noise floor** is the control arm's range, `max - min` over its runs; a standard deviation of a
  few runs is itself too noisy to bound anything.
- **Worse by** compares arm means, oriented per metric so that positive always means "worse".
- **A metric passes** when `worse_by <= noise floor`. With a zero floor any worsening above float
  error fails: a control that never varied has shown no noise to hide behind.
- **Runs**: both arms need `MINIMUM_RUNS` measured runs of every metric for a spread to exist, and
  `SHIP_MINIMUM_RUNS` for a verdict to be a ship. Below it the metrics are reported and the
  decision is "underpowered", never a ship.
- **Power** is stated, not hidden: `rule_power` simulates this rule on normal noise, so every report
  says how often a regression of a given size passes and how often a neutral edit fails.
- **Prefix**: it may not grow beyond a tolerance, and a batch that claims a token saving must show
  one.
"""

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from typing import Final

from pydantic import BaseModel, ConfigDict

from chemclaw.core.markdown import render_table as markdown_table
from chemclaw.evals.delegation import MINIMUM_REPEATS
from chemclaw.evals.live import ProbeOutcome
from chemclaw.evals.live_judge import Judgement
from chemclaw.evals.probe import Probe
from chemclaw.evals.tool_utility import VERDICT_SCORES

#: Measured runs of every metric each arm must have for a spread to exist: the delegation floor.
MINIMUM_RUNS: Final = MINIMUM_REPEATS

#: Runs per arm below which the rule cannot return a ship. At three, a neutral edit fails some
#: metric of six about one time in three; at five, about one in twenty (`rule_power`). It does not
#: make a regression easier to catch: the range floor widens with the run count, so a one-deviation
#: regression passes more often at five runs than at three, and the report says so.
SHIP_MINIMUM_RUNS: Final = 5

#: How much the per-request prefix may grow before a batch that claims no saving is refused. A
#: neutral rewording moves a few tokens either way; the context floor's headroom is a few hundred.
DEFAULT_PREFIX_TOLERANCE_TOKENS: Final = 100

#: The share of the selected probes an arm may fail to complete before the comparison is refused.
DEFAULT_MAX_DROP_SHARE: Final = 0.10

#: Float error is not a regression: a worsening smaller than this is arithmetic, not behaviour.
_TOLERANCE: Final = 1e-9

#: Simulated trials per run count, and the regressions (in run-to-run standard deviations) shown.
_TRIALS: Final = 20_000
_SEED: Final = 20261008
POWER_SHIFTS: Final = (0.5, 1.0, 1.5, 2.0)


@dataclass(frozen=True)
class MetricSpec:
    """One metric: its name, what it is, and which direction is better."""

    name: str
    label: str
    higher_is_better: bool
    unit: str


#: The six metrics the ADR fixes, in report order.
METRICS: Final[tuple[MetricSpec, ...]] = (
    MetricSpec("tool_selection_accuracy", "tool selection accuracy", True, "share"),
    MetricSpec("argument_validity", "first-call argument validity", True, "share"),
    MetricSpec("refusal_correctness", "refusal correctness", True, "share"),
    MetricSpec("task_success", "graded task success", True, "score"),
    MetricSpec("tokens_per_turn", "tokens per turn", False, "tokens"),
    MetricSpec("turn_cost", "turn cost", False, "billed tokens"),
)
METRIC_NAMES: Final = tuple(spec.name for spec in METRICS)


class TooFewRuns(ValueError):
    """An arm has fewer than `MINIMUM_RUNS` measured runs of a metric, so no spread exists."""


# ----------------------------------------------------------------------------------------- power


@dataclass(frozen=True)
class RulePower:
    """What the range rule does on pure noise, for one run count.

    `expected_range` is the floor in run-to-run standard deviations; `pass_probability` maps a true
    regression of that many deviations (0 is a neutral edit) to the chance one metric passes.
    """

    runs: int
    expected_range: float
    pass_probability: Mapping[float, float]

    @property
    def neutral_fail(self) -> float:
        """The chance a neutral edit fails one metric."""
        return 1.0 - self.pass_probability[0.0]

    @property
    def six_metric_neutral_fail(self) -> float:
        """The chance a neutral edit fails at least one of the six independent metrics."""
        return 1.0 - self.pass_probability[0.0] ** len(METRICS)


@cache
def rule_power(runs: int) -> RulePower:
    """Simulate the rule: `runs` per arm of unit-normal noise, a regression added to the candidate.

    Seeded, so the figures a report prints are reproducible. The same draws serve every regression
    size, so the table is monotone. Needs no model and costs well under a second per run count.
    """
    rng = random.Random(_SEED + runs)
    shifts = (0.0, *POWER_SHIFTS)
    passes = dict.fromkeys(shifts, 0)
    total_range = 0.0
    for _ in range(_TRIALS):
        control = [rng.gauss(0.0, 1.0) for _ in range(runs)]
        noise = [rng.gauss(0.0, 1.0) for _ in range(runs)]
        floor = max(control) - min(control)
        total_range += floor
        base = math.fsum(control) / runs - math.fsum(noise) / runs
        for shift in shifts:
            passes[shift] += -base + shift <= floor
    return RulePower(runs, total_range / _TRIALS, {s: passes[s] / _TRIALS for s in shifts})


def power_sentence(runs: int) -> str:
    """The rule's power at `runs`, in one sentence a report can carry."""
    power = rule_power(runs)
    caught = ", ".join(
        f"{shift:g}σ {1 - power.pass_probability[shift]:.0%}" for shift in (1.0, *POWER_SHIFTS[2:])
    )
    return (
        f"With {runs} runs per arm this rule fails a neutral edit on a given metric "
        f"{power.neutral_fail:.0%} of the time ({power.six_metric_neutral_fail:.0%} on some metric "
        f"of six) and catches a true regression of {caught} (σ is one metric's run-to-run "
        "standard deviation). A regression smaller than the noise floor is not detectable."
    )


# ------------------------------------------------------------------------------------- verdicts


class ArmSummary(BaseModel):
    """One arm's runs of one metric, reduced."""

    model_config = ConfigDict(frozen=True)

    runs: int
    mean: float
    low: float
    high: float

    @property
    def spread(self) -> float:
        """The observed range, `max - min`."""
        return self.high - self.low


class MetricVerdict(BaseModel):
    """Whether one metric lets the batch ship, and why, in a sentence a reviewer can check."""

    model_config = ConfigDict(frozen=True)

    metric: str
    label: str
    higher_is_better: bool
    control: ArmSummary | None
    candidate: ArmSummary | None
    worse_by: float | None
    noise_floor: float | None
    ok: bool
    reason: str
    power_note: str = ""


class PrefixVerdict(BaseModel):
    """The per-request prefix: within tolerance, and smaller when the batch claims a saving."""

    model_config = ConfigDict(frozen=True)

    control_tokens: int
    candidate_tokens: int
    claims_token_saving: bool
    tolerance_tokens: int
    ok: bool
    reason: str


class ArmCoverage(BaseModel):
    """How many of the selected probes one arm failed to complete in every run."""

    model_config = ConfigDict(frozen=True)

    arm: str
    dropped: int
    share: float


class CoverageReport(BaseModel):
    """Which probes the comparison stands on: those every run of both arms completed."""

    model_config = ConfigDict(frozen=True)

    selected: int
    common: int
    arms: list[ArmCoverage]


class CoverageVerdict(BaseModel):
    """Whether either arm dropped too many probes for the comparison to mean anything."""

    model_config = ConfigDict(frozen=True)

    report: CoverageReport
    max_drop_share: float
    ok: bool
    reason: str


class ShipDecision(BaseModel):
    """The whole decision: every metric, the prefix, the coverage, power and the verdict."""

    model_config = ConfigDict(frozen=True)

    ship: bool
    underpowered: bool
    runs: int
    minimum_runs: int
    metrics: list[MetricVerdict]
    prefix: PrefixVerdict
    coverage: CoverageVerdict | None
    power: str
    reason: str


def _summarise(name: str, arm: str, values: Sequence[float | None]) -> ArmSummary | None:
    """The measured runs of one metric in one arm; `None` when no run measured it.

    Raises:
        ValueError: A value is not a finite number.
        TooFewRuns: Some but fewer than `MINIMUM_RUNS` runs measured it.
    """
    measured = [value for value in values if value is not None]
    if any(not math.isfinite(value) for value in measured):
        raise ValueError(f"{arm} {name}: runs contain a value that is not a finite number")
    if not measured:
        return None
    if len(measured) < MINIMUM_RUNS:
        raise TooFewRuns(
            f"{arm} arm has {len(measured)} measured run(s) of {name}; the spread needs at least "
            f"{MINIMUM_RUNS}. Run more, do not lower the floor."
        )
    return ArmSummary(
        runs=len(measured),
        mean=math.fsum(measured) / len(measured),
        low=min(measured),
        high=max(measured),
    )


def _power_note(floor: float, runs: int) -> str:
    """What this metric's floor can and cannot see, in the metric's own units."""
    power = rule_power(runs)
    if floor <= _TOLERANCE:
        return "the control never varied, so any worsening fails and its noise is unknown"
    sigma = floor / power.expected_range
    return (
        f"σ≈{sigma:.3g}: a regression of 1σ passes {power.pass_probability[1.0]:.0%}, "
        f"of 2σ {power.pass_probability[2.0]:.0%}"
    )


def _verdict(
    spec: MetricSpec, control: ArmSummary | None, candidate: ArmSummary | None
) -> MetricVerdict:
    """Apply the rule to one metric."""
    if control is None or candidate is None:
        missing = "control" if control is None else "candidate"
        return MetricVerdict(
            metric=spec.name,
            label=spec.label,
            higher_is_better=spec.higher_is_better,
            control=control,
            candidate=candidate,
            worse_by=None,
            noise_floor=None,
            ok=False,
            reason=f"unmeasured: the {missing} arm recorded no {spec.label}, so it cannot be shown "
            "not to be worse",
        )
    delta = candidate.mean - control.mean
    worse_by = -delta if spec.higher_is_better else delta
    floor = control.spread
    ok = worse_by <= floor + _TOLERANCE
    if worse_by <= _TOLERANCE:
        reason = "no worse than the control"
    elif ok:
        reason = f"worse by {worse_by:.4g}, within the control's own spread of {floor:.4g}"
    elif floor <= _TOLERANCE:
        reason = f"worse by {worse_by:.4g} against a control that never varied (spread 0)"
    else:
        reason = f"worse by {worse_by:.4g}, more than the control's own spread of {floor:.4g}"
    return MetricVerdict(
        metric=spec.name,
        label=spec.label,
        higher_is_better=spec.higher_is_better,
        control=control,
        candidate=candidate,
        worse_by=worse_by,
        noise_floor=floor,
        ok=ok,
        reason=reason,
        power_note=_power_note(floor, min(control.runs, candidate.runs)),
    )


def _prefix_verdict(
    control: int, candidate: int, *, claims_token_saving: bool, tolerance: int
) -> PrefixVerdict:
    """The prefix rule: within `tolerance` of the control, and smaller when a saving is claimed."""
    delta = candidate - control
    if claims_token_saving:
        ok = delta < 0
        reason = (
            f"the claimed saving is shown: smaller by {-delta} tokens"
            if ok
            else f"claims a token saving and shows none: {candidate} against {control}"
        )
    elif delta < 0:
        ok, reason = True, f"smaller by {-delta} tokens"
    elif delta <= tolerance:
        ok, reason = True, f"grew by {delta} tokens, within the {tolerance}-token tolerance"
    else:
        ok, reason = False, f"grew by {delta} tokens, more than the {tolerance}-token tolerance"
    return PrefixVerdict(
        control_tokens=control,
        candidate_tokens=candidate,
        claims_token_saving=claims_token_saving,
        tolerance_tokens=tolerance,
        ok=ok,
        reason=reason,
    )


def _coverage_verdict(report: CoverageReport, max_drop_share: float) -> CoverageVerdict:
    """Refuse a comparison in which either arm dropped more than `max_drop_share` of its probes."""
    over = [arm for arm in report.arms if arm.share > max_drop_share]
    return CoverageVerdict(
        report=report,
        max_drop_share=max_drop_share,
        ok=not over,
        reason=(
            f"compared on the {report.common} of {report.selected} probes both arms completed"
            if not over
            else "dropped more than "
            f"{max_drop_share:.0%} of the probes: "
            + ", ".join(f"{arm.arm} {arm.share:.0%}" for arm in over)
        ),
    )


def ship_decision(
    control: Mapping[str, Sequence[float | None]],
    candidate: Mapping[str, Sequence[float | None]],
    *,
    control_prefix_tokens: int,
    candidate_prefix_tokens: int,
    claims_token_saving: bool = False,
    prefix_tolerance_tokens: int = DEFAULT_PREFIX_TOLERANCE_TOKENS,
    minimum_runs: int = SHIP_MINIMUM_RUNS,
    coverage: CoverageReport | None = None,
    max_drop_share: float = DEFAULT_MAX_DROP_SHARE,
) -> ShipDecision:
    """Whether a batch ships: no metric worse than the control's spread, prefix held to its claim.

    Args:
        control: Metric name → one value per control run (`None` where a run could not measure it).
        candidate: The same for the candidate arm, run under the same probes and gateway.
        control_prefix_tokens: The shipped text's per-request prefix (inventory `prefix.total`).
        candidate_prefix_tokens: The candidate's, measured in the same environment.
        claims_token_saving: Whether the batch says it saves tokens; if so it must show a saving.
        prefix_tolerance_tokens: How far the prefix may grow when no saving is claimed.
        minimum_runs: Runs per arm below which the verdict can only be "underpowered".
        coverage: Which probes the comparison stands on, when the caller restricted to the common.
        max_drop_share: The most either arm may have dropped before the comparison is refused.

    Raises:
        ValueError: A metric name is unknown or missing, a value is not finite, or `minimum_runs` is
            below `MINIMUM_RUNS`.
        TooFewRuns: An arm has some but fewer than `MINIMUM_RUNS` measured runs of a metric.
    """
    if minimum_runs < MINIMUM_RUNS:
        raise ValueError(f"minimum_runs {minimum_runs} is below the {MINIMUM_RUNS} a spread needs")
    for arm, samples in (("control", control), ("candidate", candidate)):
        unknown = sorted(set(samples) - set(METRIC_NAMES))
        absent = [name for name in METRIC_NAMES if name not in samples]
        if unknown or absent:
            raise ValueError(f"{arm} metrics: unknown {unknown}, missing {absent}")
    summaries = [
        (
            spec,
            _summarise(spec.name, "control", control[spec.name]),
            _summarise(spec.name, "candidate", candidate[spec.name]),
        )
        for spec in METRICS
    ]
    verdicts = [_verdict(spec, one, other) for spec, one, other in summaries]
    measured = [min(one.runs, other.runs) for _, one, other in summaries if one and other]
    runs = min(measured) if measured else 0
    underpowered = runs < minimum_runs
    prefix = _prefix_verdict(
        control_prefix_tokens,
        candidate_prefix_tokens,
        claims_token_saving=claims_token_saving,
        tolerance=prefix_tolerance_tokens,
    )
    probes = None if coverage is None else _coverage_verdict(coverage, max_drop_share)
    failing = [verdict.label for verdict in verdicts if not verdict.ok]
    if not prefix.ok:
        failing.append("per-request prefix")
    if probes is not None and not probes.ok:
        failing.append("probe coverage")
    if failing:
        reason = "failing: " + ", ".join(failing)
    elif underpowered:
        reason = (
            f"underpowered: {runs} run(s) per arm and a ship verdict needs {minimum_runs}. No "
            "metric is worse than the control's spread, which at this run count is not a ship"
        )
    else:
        reason = "no metric is worse than the control's own spread and the prefix is held"
    return ShipDecision(
        ship=not failing and not underpowered,
        underpowered=underpowered,
        runs=runs,
        minimum_runs=minimum_runs,
        metrics=verdicts,
        prefix=prefix,
        coverage=probes,
        power=power_sentence(max(runs, MINIMUM_RUNS)),
        reason=reason,
    )


# ------------------------------------------------------------------------------- per-run metrics


@dataclass(frozen=True)
class GradedProbe:
    """One probe answered once in one run: the outcome, its grade, and its booked spend."""

    probe: Probe
    outcome: ProbeOutcome
    judgement: Judgement | None
    tokens: int | None
    billed: int | None


def completed(record: GradedProbe) -> bool:
    """Whether the harness got what it needs from a probe: it arrived, was graded, and was booked.

    A turn that answered nothing is *completed* and scores as the failure it is; only a failure of
    the instrument drops a probe.
    """
    return (
        record.outcome.transport_error is None
        and record.judgement is not None
        and record.judgement.verdict in VERDICT_SCORES
        and record.tokens is not None
        and record.billed is not None
    )


def restrict_to_common(
    runs: Mapping[str, Sequence[Sequence[GradedProbe]]],
) -> tuple[dict[str, list[list[GradedProbe]]], CoverageReport]:
    """Keep only the probes that every run of every arm completed, and report what was dropped.

    Arms compared on different probes are compared on different questions: a probe that failed to
    grade in one arm and not the other would move a denominator. A probe is dropped from an arm
    when it did not complete (`completed`) in some run of that arm, and from the comparison when it
    is dropped from either; every run is then restricted to the same probes.
    """
    selected = {r.probe.id for arm in runs.values() for one in arm for r in one}
    dropped: dict[str, set[str]] = {}
    for name, arm in runs.items():
        done = [{r.probe.id for r in one if completed(r)} for one in arm]
        completed_everywhere = set(selected)
        for ids in done:
            completed_everywhere &= ids
        dropped[name] = selected - completed_everywhere if done else set(selected)
    common = selected - set().union(*dropped.values())
    report = CoverageReport(
        selected=len(selected),
        common=len(common),
        arms=[
            ArmCoverage(
                arm=name, dropped=len(ids), share=len(ids) / len(selected) if selected else 0.0
            )
            for name, ids in dropped.items()
        ],
    )
    kept = {
        name: [[r for r in one if r.probe.id in common] for one in arm]
        for name, arm in runs.items()
    }
    return kept, report


def _mean(values: Sequence[float]) -> float | None:
    return math.fsum(values) / len(values) if values else None


def run_metrics(records: Sequence[GradedProbe]) -> dict[str, float | None]:
    """One run's six metrics from its graded probes; `None` where the run had nothing to measure.

    - tool selection: share of probes declaring an applicable `expects_tools` that called one;
    - argument validity: `1 - errors / first calls`, pooled over every tool first call in the run;
    - refusal correctness: share of graded bucket-C probes the judge calls `served` (an honest
      refusal; `fabricated` and `unserved` are the failures);
    - task success: mean `VERDICT_SCORES` over graded bucket A and B probes;
    - tokens per turn and turn cost: means of the booked ledger rows (`turn_costs`), over the probes
      that have one, since a missing row is a hole and not a free turn.

    Callers compare arms on the probes both completed (`restrict_to_common`), so these means share
    a denominator across arms.
    """
    selected = [
        r.outcome.expected_tools_met for r in records if r.outcome.expected_tools_met is not None
    ]
    first_calls = sum(r.outcome.first_calls for r in records)
    errors = sum(len(r.outcome.first_call_argument_errors) for r in records)
    graded = [r for r in records if r.judgement and r.judgement.verdict in VERDICT_SCORES]
    refusals = [r for r in graded if r.probe.bucket == "C"]
    tasks = [r for r in graded if r.probe.bucket in {"A", "B"}]
    return {
        "tool_selection_accuracy": _mean([float(met) for met in selected]),
        "argument_validity": (1.0 - errors / first_calls) if first_calls else None,
        "refusal_correctness": _mean(
            [float(r.judgement.verdict == "served") for r in refusals if r.judgement]
        ),
        "task_success": _mean([VERDICT_SCORES[r.judgement.verdict] for r in tasks if r.judgement]),
        "tokens_per_turn": _mean([float(r.tokens) for r in records if r.tokens is not None]),
        "turn_cost": _mean([float(r.billed) for r in records if r.billed is not None]),
    }


# ------------------------------------------------------------------------------------ the table


def _fmt(spec: MetricSpec, value: float) -> str:
    """A metric value at the precision its unit deserves."""
    return f"{value:,.0f}" if spec.unit in {"tokens", "billed tokens"} else f"{value:.3f}"


def _cell(spec: MetricSpec, arm: ArmSummary | None) -> str:
    """`mean (spread)` for one arm, or a dash where it was not measured."""
    return "—" if arm is None else f"{_fmt(spec, arm.mean)} ({_fmt(spec, arm.spread)})"


def render_table(decision: ShipDecision, *, evidence: str) -> str:
    """The results table a pull request carries: both arms' mean and spread, power, the verdict.

    `evidence` is printed first and verbatim, so a dry run cannot be pasted into a PR without its
    label coming along.
    """
    by_name = {spec.name: spec for spec in METRICS}
    rows = []
    for verdict in decision.metrics:
        spec = by_name[verdict.metric]
        rows.append(
            [
                verdict.label,
                "higher" if spec.higher_is_better else "lower",
                _cell(spec, verdict.control),
                _cell(spec, verdict.candidate),
                "—" if verdict.worse_by is None else f"{verdict.worse_by:+.4g}",
                "—" if verdict.noise_floor is None else f"{verdict.noise_floor:.4g}",
                "pass" if verdict.ok else "**FAIL**",
            ]
        )
    prefix = decision.prefix
    rows.append(
        [
            "per-request prefix (tokens)",
            "lower",
            f"{prefix.control_tokens:,}",
            f"{prefix.candidate_tokens:,}",
            f"{prefix.candidate_tokens - prefix.control_tokens:+,}",
            "must shrink" if prefix.claims_token_saving else f"+{prefix.tolerance_tokens} allowed",
            "pass" if prefix.ok else "**FAIL**",
        ]
    )
    coverage = decision.coverage
    if coverage is not None:
        control_cover, candidate_cover = (
            next((a for a in coverage.report.arms if a.arm == arm), None)
            for arm in ("control", "candidate")
        )

        def dropped(arm: ArmCoverage | None) -> str:
            return "—" if arm is None else f"{arm.dropped} ({arm.share:.1%})"

        rows.append(
            [
                "probes dropped (share)",
                "lower",
                dropped(control_cover),
                dropped(candidate_cover),
                "—",
                f"≤ {coverage.max_drop_share:.0%}",
                "pass" if coverage.ok else "**FAIL**",
            ]
        )
    table = markdown_table(
        [
            "metric",
            "better",
            "control mean (spread)",
            "candidate mean (spread)",
            "worse by",
            "noise floor",
            "verdict",
        ],
        rows,
        align="llrrrrl",
    )
    verdict_line = "**SHIP**" if decision.ship else "**NO SHIP**"
    notes = [
        f"Runs per arm: {decision.runs} (a ship verdict needs {decision.minimum_runs}). "
        f"{decision.power}",
        f"Prefix: {prefix.reason}.",
    ]
    if coverage is not None:
        notes.append(f"Coverage: {coverage.reason}.")
    notes += [f"Power, {v.label}: {v.power_note}." for v in decision.metrics if v.power_note]
    return (
        f"{evidence}\n\n{table}\n\n"
        + "\n".join(notes)
        + f"\n\n{verdict_line} — {decision.reason}\n"
    )
