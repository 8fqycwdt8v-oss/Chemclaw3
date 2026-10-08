"""The ship rule for a model-text batch, as code: pure statistics over per-run metric samples.

A batch of rewritten tool descriptions or prompt blocks ships only when no metric is worse than the
control by more than the control's own run-to-run spread and the per-request prefix shrinks
(`D-2026-10-08-model-facing-text-changes-ship-behind-an-evaluation`). This module runs no model
and reads no file: `ship_decision` takes one number per run per arm, `run_metrics` folds one run's
graded probes into those numbers, and `render_table` is the table a pull request carries.

Statistics, chosen to need no distributional assumption from three runs:

- **Noise floor** is the control arm's range, `max - min` over its runs. A standard deviation of
  three runs is itself too noisy to bound anything; the range is the spread actually observed.
- **Worse by** compares arm means, oriented per metric so that positive always means "worse".
- **Verdict**: a metric passes when `worse_by <= noise floor`. With a zero floor any worsening above
  float error fails: a control that never varied has shown no noise to hide behind.
- **Runs**: both arms need `MINIMUM_RUNS` measured runs of every metric, or the call is refused
  rather than answered. A metric with no measured run in an arm is `unmeasured` and blocks shipping.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel, ConfigDict

from chemclaw.core.markdown import render_table as markdown_table
from chemclaw.evals.delegation import MINIMUM_REPEATS
from chemclaw.evals.live import ProbeOutcome
from chemclaw.evals.live_judge import Judgement
from chemclaw.evals.probe import Probe
from chemclaw.evals.tool_utility import VERDICT_SCORES

#: Measured runs of every metric each arm must have: the same floor the delegation comparison holds.
MINIMUM_RUNS: Final = MINIMUM_REPEATS

#: Float error is not a regression: a worsening smaller than this is arithmetic, not behaviour.
_TOLERANCE: Final = 1e-9


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


class PrefixVerdict(BaseModel):
    """The per-request prefix, which must shrink."""

    model_config = ConfigDict(frozen=True)

    control_tokens: int
    candidate_tokens: int
    ok: bool
    reason: str


class ShipDecision(BaseModel):
    """The whole decision: every metric, the prefix, and the one-line verdict."""

    model_config = ConfigDict(frozen=True)

    ship: bool
    metrics: list[MetricVerdict]
    prefix: PrefixVerdict
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
    )


def ship_decision(
    control: Mapping[str, Sequence[float | None]],
    candidate: Mapping[str, Sequence[float | None]],
    *,
    control_prefix_tokens: int,
    candidate_prefix_tokens: int,
) -> ShipDecision:
    """Whether a batch ships: no metric worse than the control's spread, and a smaller prefix.

    Args:
        control: Metric name → one value per control run (`None` where a run could not measure it).
        candidate: The same for the candidate arm, run under the same probes and gateway.
        control_prefix_tokens: The shipped text's per-request prefix (inventory `prefix.total`).
        candidate_prefix_tokens: The candidate's.

    Raises:
        ValueError: A metric name is unknown or missing, or a value is not finite.
        TooFewRuns: An arm has some but fewer than `MINIMUM_RUNS` measured runs of a metric.
    """
    for arm, runs in (("control", control), ("candidate", candidate)):
        unknown = sorted(set(runs) - set(METRIC_NAMES))
        absent = [name for name in METRIC_NAMES if name not in runs]
        if unknown or absent:
            raise ValueError(f"{arm} metrics: unknown {unknown}, missing {absent}")
    verdicts = [
        _verdict(
            spec,
            _summarise(spec.name, "control", control[spec.name]),
            _summarise(spec.name, "candidate", candidate[spec.name]),
        )
        for spec in METRICS
    ]
    smaller = candidate_prefix_tokens < control_prefix_tokens
    prefix = PrefixVerdict(
        control_tokens=control_prefix_tokens,
        candidate_tokens=candidate_prefix_tokens,
        ok=smaller,
        reason=(
            f"smaller by {control_prefix_tokens - candidate_prefix_tokens} tokens"
            if smaller
            else f"not smaller: {candidate_prefix_tokens} against the control's "
            f"{control_prefix_tokens}"
        ),
    )
    failing = [verdict.label for verdict in verdicts if not verdict.ok]
    if not prefix.ok:
        failing.append("per-request prefix")
    return ShipDecision(
        ship=not failing,
        metrics=verdicts,
        prefix=prefix,
        reason="no metric is worse than the control's own spread and the prefix shrinks"
        if not failing
        else "failing: " + ", ".join(failing),
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
    """The results table a pull request carries: both arms' mean and spread, and the verdict.

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
            "—",
            "pass" if prefix.ok else "**FAIL**",
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
    return f"{evidence}\n\n{table}\n\n{verdict_line} — {decision.reason}\n"
