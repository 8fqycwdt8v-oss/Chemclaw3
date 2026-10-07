"""Whether delegation pays, measured per *task* rather than as a delegation rate.

Three arms rather than `evals/ab.py`'s two, and two kinds of effect: the claimed benefit is cost (a
helper reads in its own context) and the claimed risk is quality (the caller sees only a summary).
So quality reuses `compare_tool_utility` and the two cost axes (tokens, wall clock) are reported
beside it, never folded in. The unit is a task, because a delegation rate is a mediator, not an
outcome.

The arms: `no-helper` (asked not to call `task`; the tool cannot be removed, since
`SubAgentMiddleware` is required upstream), `helper` (a helper on the caller's model) and
`helper-routed` (a helper on its own model route). A baseline run that delegated anyway is
**contaminated** and dropped.

Medians, not means, so one timed-out repeat cannot dominate; aggregated per `(task, arm)` and
reported per task. The comparison is intention-to-treat: arms are compared as assigned, and how
often an arm actually delegated is reported beside the result (`delegated_in`/`repeats`) rather than
used to select tasks. A non-delegating repeat dilutes the effect toward zero, the conservative
direction. Only `contaminated` and `incomplete` (missing data) drop a task.

This module runs no model; the run half is `evals/delegation_run.py` plus `cli/live_probes --suite
delegation`, which needs a real gateway model.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from chemclaw.evals.ab import ABSummary, TaskScores, compare_tool_utility

#: The arm a task is compared *against*: the model answering without calling `task`.
BASELINE_ARM = "no-helper"

#: How many repeats of one `(task, arm)` pair make an aggregate worth reporting. A floor: below
#: three, a median is one or two observations.
MINIMUM_REPEATS = 3


class ArmRun(BaseModel):
    """One task, answered once, by one arm.

    `delegated` is recorded on every arm rather than only on the baseline, because it is the one
    fact that says whether the arm did what its name claims. A `helper` arm that never called `task`
    is measuring the baseline under a different label, and a report that could not see that would
    attribute the baseline's own numbers to delegation.
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    arm: str = Field(min_length=1)
    #: The task outcome on one axis where higher is better — `evals/tool_utility.VERDICT_SCORES`
    #: is the scale this is meant to carry, so a fabricated answer scores *below* a declined one.
    quality: float = Field(allow_inf_nan=False)
    #: What the turn actually billed, from `turn_costs`. The cost claim is about where tokens are
    #: billed, so an estimate would measure the wrong thing.
    billed_tokens: int = Field(ge=0)
    wall_clock_seconds: float = Field(ge=0.0, allow_inf_nan=False)
    #: Whether this run actually spawned a helper.
    delegated: bool


class ArmAggregate(BaseModel):
    """One `(task, arm)` pair over its repeats."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    arm: str
    repeats: int
    quality: float
    billed_tokens: float
    wall_clock_seconds: float
    # How many of the repeats spawned a helper; a count, because "one of three" is its own fact.
    delegated_in: int


class TaskComparison(BaseModel):
    """What delegation did to one task, on all three axes, signed the same way throughout."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    #: Positive means the arm answered *better* than the baseline.
    quality_delta: float
    #: Positive means the arm **spent more**. Named for the direction rather than for "better", so
    #: nothing has to remember which way cheap points on which axis.
    token_delta: float
    wall_clock_delta: float
    #: The quality verdict from `compare_tool_utility`, above its noise floor:
    #: helped / hurt / no effect.
    verdict: str
    # How many of the arm's repeats actually delegated, out of how many it ran: the compliance a
    # reader needs to judge dilution.
    delegated_in: int
    repeats: int


class DelegationReport(BaseModel):
    """The answer to "did delegation pay", with the two ways it could fail to kept separate."""

    model_config = ConfigDict(extra="forbid")

    baseline_arm: str
    arm: str
    comparisons: list[TaskComparison]
    quality: ABSummary
    #: Tasks whose baseline run spawned a helper, so the pair compares delegation with delegation.
    contaminated: list[str]
    #: Tasks that reached one arm only, or reached an arm fewer than `MINIMUM_REPEATS` times.
    incomplete: list[str]
    #: Tasks where the *arm* never delegated, so the pair compares the baseline with itself.
    undelegated: list[str]
    #: Tasks where the arm delegated in some repeats and not others: still compared
    #: (intention-to-treat), but listed because the aggregate mixes two behaviours.
    partially_delegated: list[str]
    #: Median across compared tasks of `arm / baseline`; below 1.0 means delegation was cheaper.
    #: `None` means every compared task had a zero baseline on this axis, which must not be rendered
    #: as 1.0.
    median_token_ratio: float | None
    median_wall_clock_ratio: float | None

    @property
    def compared(self) -> int:
        """How many tasks carried the comparison — the report's real denominator."""
        return len(self.comparisons)


class NoComparableTask(ValueError):
    """Every task was dropped, so the report would describe an empty set as a result."""


def aggregate_runs(runs: Iterable[ArmRun]) -> list[ArmAggregate]:
    """Collapse repeats of each `(task, arm)` pair to its median on every axis.

    Quality is a four-point ordinal scale (`VERDICT_SCORES`), so a mean is not a verdict, and it
    uses `median_low`, which always returns an observation (`median` averages the two middle values
    on an even count). The cost axes are continuous and use `median`.
    """
    grouped: dict[tuple[str, str], list[ArmRun]] = defaultdict(list)
    for run in runs:
        grouped[(run.task_id, run.arm)].append(run)
    return [
        ArmAggregate(
            task_id=task_id,
            arm=arm,
            repeats=len(group),
            quality=statistics.median_low(r.quality for r in group),
            billed_tokens=statistics.median(float(r.billed_tokens) for r in group),
            wall_clock_seconds=statistics.median(r.wall_clock_seconds for r in group),
            delegated_in=sum(1 for r in group if r.delegated),
        )
        for (task_id, arm), group in sorted(grouped.items())
    ]


def _ratio(arm: float, baseline: float) -> float | None:
    """`arm / baseline`, or `None` where the baseline is zero and the ratio says nothing.

    A zero baseline is legitimate (a turn that failed before billing); the task is dropped from this
    axis only.
    """
    return arm / baseline if baseline > 0 else None


def compare_arms(
    runs: Sequence[ArmRun],
    arm: str,
    baseline_arm: str = BASELINE_ARM,
    minimum_repeats: int = MINIMUM_REPEATS,
) -> DelegationReport:
    """Compare one delegation arm against the baseline, per task, on quality and both costs.

    Args:
        runs: Every recorded run, for any arm. Arms other than the two named are ignored, so one
            recording of a three-arm campaign answers both comparisons without being re-run.
        arm: The delegating arm under test.
        baseline_arm: The arm that was asked not to delegate.
        minimum_repeats: How many repeats of a `(task, arm)` pair make it comparable.

    Returns:
        The per-task comparison, the quality A/B, and the ways a task can fail to carry the
        comparison — each as a list of task ids.

    Raises:
        ValueError: `runs` is empty, or the two arms are the same arm.
        NoComparableTask: Every task was dropped; an empty comparison would read as "no effect
            anywhere".
    """
    if not runs:
        raise ValueError("no runs — a delegation comparison over nothing proves nothing")
    if arm == baseline_arm:
        raise ValueError(
            f"arm and baseline are the same arm ({arm!r}); there is nothing to compare"
        )

    by_task: dict[str, dict[str, ArmAggregate]] = defaultdict(dict)
    for aggregate in aggregate_runs(runs):
        if aggregate.arm in (arm, baseline_arm):
            by_task[aggregate.task_id][aggregate.arm] = aggregate

    comparisons: list[TaskComparison] = []
    scores: list[TaskScores] = []
    contaminated: list[str] = []
    incomplete: list[str] = []
    undelegated: list[str] = []
    partially_delegated: list[str] = []
    token_ratios: list[float] = []
    wall_clock_ratios: list[float] = []

    for task_id in sorted(by_task):
        arms = by_task[task_id]
        base = arms.get(baseline_arm)
        under_test = arms.get(arm)
        if base is None or under_test is None:
            incomplete.append(task_id)
            continue
        if base.repeats < minimum_repeats or under_test.repeats < minimum_repeats:
            incomplete.append(task_id)
            continue
        # The one behavioural drop: a baseline that delegated is not a baseline.
        if base.delegated_in:
            contaminated.append(task_id)
            continue
        # The arm's own behaviour is reported, never a reason to drop the task (intention-to-treat).
        # Conditioning on compliance would flatter the arm, and requiring delegation in every repeat
        # would make the instrument refuse to report at realistic compliance rates.
        if not under_test.delegated_in:
            undelegated.append(task_id)
        elif under_test.delegated_in < under_test.repeats:
            partially_delegated.append(task_id)

        scores.append(
            TaskScores(task_id=task_id, baseline=base.quality, augmented=under_test.quality)
        )
        comparisons.append(
            TaskComparison(
                task_id=task_id,
                quality_delta=under_test.quality - base.quality,
                token_delta=under_test.billed_tokens - base.billed_tokens,
                wall_clock_delta=under_test.wall_clock_seconds - base.wall_clock_seconds,
                # Filled once `compare_tool_utility` has applied its noise floor, below.
                verdict="",
                delegated_in=under_test.delegated_in,
                repeats=under_test.repeats,
            )
        )
        tokens = _ratio(under_test.billed_tokens, base.billed_tokens)
        if tokens is not None:
            token_ratios.append(tokens)
        wall_clock = _ratio(under_test.wall_clock_seconds, base.wall_clock_seconds)
        if wall_clock is not None:
            wall_clock_ratios.append(wall_clock)

    # Refuse only an empty comparison. Nothing is dropped for the arm's behaviour, so there is no
    # surviving share to bound; `incomplete` is missing data, reported beside the report.
    if not comparisons:
        raise NoComparableTask(
            f"no task carried the comparison: {len(contaminated)} contaminated (the baseline "
            f"delegated), {len(incomplete)} incomplete (a missing arm, or fewer than "
            f"{minimum_repeats} repeats). A report over an empty set would read as 'no effect "
            "anywhere'."
        )

    quality = compare_tool_utility(scores, higher_is_better=True)
    verdicts = {utility.task_id: utility.verdict for utility in quality.utilities}
    comparisons = [
        comparison.model_copy(update={"verdict": verdicts[comparison.task_id]})
        for comparison in comparisons
    ]

    return DelegationReport(
        baseline_arm=baseline_arm,
        arm=arm,
        comparisons=comparisons,
        quality=quality,
        contaminated=contaminated,
        incomplete=incomplete,
        undelegated=undelegated,
        partially_delegated=partially_delegated,
        median_token_ratio=statistics.median(token_ratios) if token_ratios else None,
        median_wall_clock_ratio=(
            statistics.median(wall_clock_ratios) if wall_clock_ratios else None
        ),
    )
