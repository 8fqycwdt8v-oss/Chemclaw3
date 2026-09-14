"""Whether delegation pays, measured per *task* rather than as a delegation rate.

**Why this is not `evals/ab.py`.** That module compares one metric with tools against the same
metric without them, pairwise and dimensionless. Delegation cannot be scored that way for two
reasons that are the whole point of the measurement: there are three arms rather than two, and the
claimed benefit is *cost* (a helper reads in its own context and reports back small) while the
claimed risk is *quality* (a summary is model prose about evidence the caller never saw). A single
number that collapsed those would answer "did it pay" with a figure that hides which way it paid.
So the quality axis reuses `compare_tool_utility` and the two cost axes are reported beside it,
never folded in.

**Why the unit is a task.** The corpus this replaces
(`data/evals/probes/m12/routing.yaml`, deleted with the specialist team) measured *delegation rate*
over fifteen one-tool probes, and `D-2026-08-29-a-helper-is-cheaper-and-narrower-than-its-caller`
records why that could not work: a rate is a mediator rather than an outcome, and a one-tool
question gives context isolation no mechanism by which it could appear. Its two runs disagreed
sevenfold because they measured different systems. The denominator problem disappears the moment the
unit is a task: a task either got done, for some spend, in some time.

**The three arms, and why one of them is behavioural rather than structural.** `no-helper` is the
model simply not calling `task`; `helper` is a helper on the caller's model; `helper-routed` is one
on its own model via `CHEMCLAW_MODEL_ROUTES='{"helper": "…"}'`. The first cannot be built by taking
the tool away — `SubAgentMiddleware` is in `create_deep_agent`'s `_REQUIRED_MIDDLEWARE` and
`_apply_excluded_middleware` raises rather than let a profile strip it, and an empty roster makes
upstream re-insert its own ungoverned `general-purpose` subagent. So the baseline arm is *asked* not
to delegate, and whether it complied is an observation rather than an assumption. A baseline run
that delegated anyway is **contaminated**, and `compare_arms` reports it rather than averaging it
in — the same discipline `evals/tool_utility.py` applies to an `ungraded` judge verdict, and for the
same reason: a measurement that quietly absorbs its own failures reports a clean number about
nothing.

**Medians, not means.** One timed-out repeat would dominate a mean on both cost axes and is exactly
what repeats exist to survive. The aggregation is per `(task, arm)` and the report is per task,
because "delegation helped here and hurt there" is the finding a rate destroys and the one selective
routing would need.

This module runs no model. It is a pure comparison over runs somebody else recorded, which is what
makes it testable without a gateway — and the run half is what needs one.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from chemclaw.evals.ab import ABSummary, TaskScores, compare_tool_utility

#: The arm a task is compared *against*: the model answering without calling `task`.
BASELINE_ARM = "no-helper"

#: How many repeats of one `(task, arm)` pair make an aggregate worth reporting.
#:
#: Three is the figure `docs/planning/BACKLOG.md` asks for, and it is a floor rather than a target.
#: Below it a median is the middle of two points or a single observation wearing a robust-sounding
#: name, which is the shape that let the deleted routing corpus report two numbers seven-fold apart
#: as though each were a measurement.
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
    #: What the turn actually billed, from `turn_costs` rather than from an estimator. The whole
    #: cost claim for delegation is that a helper's reading is billed in its own context and only
    #: its report is billed in the caller's, so an estimate would be measuring the wrong thing with
    #: a ratio `agent/context_budget.py` has twice found to be content-dependent.
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
    #: How many of the repeats spawned a helper. Reported rather than reduced to a bool, because
    #: "delegated in one repeat of three" is a different fact from either extreme and is the shape
    #: a behavioural arm actually produces.
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
    #: Median across compared tasks of `arm / baseline`. Below 1.0 means delegation was cheaper.
    #: `None` means this axis had no usable ratio, which is different from a ratio of 1.0 and must
    #: not be rendered as one. It cannot mean "no task was compared" — `NoComparableTask` raises
    #: before a report exists — so it means every compared task had a zero baseline on this axis.
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

    Median rather than mean on all three axes, including quality: a five-point verdict scale is
    ordinal, and averaging `fabricated` with `served` produces a number that names no verdict.

    **Quality takes `median_low`, and the difference is not pedantry.** `statistics.median` returns
    the *mean of the two middle values* on an even-sized group, so at 4 or 6 repeats it reproduces
    exactly the averaging this paragraph forbids — `MINIMUM_REPEATS` is a floor rather than a
    target, so even counts are ordinary. `median_low` returns an observation, which is the property
    the sentence above claims. The two cost axes keep `median`: tokens and seconds are continuous,
    and the midpoint of two runs is a meaningful figure there.
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

    A zero baseline is not an error — a turn that failed before billing anything legitimately
    records zero — but the ratio it produces is either a division by zero or an infinity that would
    dominate a median. Dropping the task from *this axis only* keeps the other two.
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
        The per-task comparison, the quality A/B, and the three ways a task can fail to carry the
        comparison — each as a list of task ids rather than a count, because the next question is
        always *which*.

    Raises:
        ValueError: `runs` is empty, or the two arms are the same arm.
        NoComparableTask: Every task was dropped. An empty comparison reported as a summary is the
            vacuous pass `compare_tool_utility` refuses for an empty task list and
            `load_eval_cases` refuses for an empty case-set (G4); a delegation report over nothing
            would read as "no effect anywhere", which is the answer this measurement most needs to
            be unable to produce by accident.
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
        # Order matters: a run that is both contaminated and undelegated is reported as
        # contaminated, because that is the more serious defect — the baseline is not a baseline.
        if base.delegated_in:
            contaminated.append(task_id)
            continue
        if not under_test.delegated_in:
            undelegated.append(task_id)
            continue

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
            )
        )
        tokens = _ratio(under_test.billed_tokens, base.billed_tokens)
        if tokens is not None:
            token_ratios.append(tokens)
        wall_clock = _ratio(under_test.wall_clock_seconds, base.wall_clock_seconds)
        if wall_clock is not None:
            wall_clock_ratios.append(wall_clock)

    if not comparisons:
        raise NoComparableTask(
            f"no task carried the comparison: {len(contaminated)} contaminated "
            f"(the baseline delegated), {len(undelegated)} undelegated (the arm did not), "
            f"{len(incomplete)} incomplete (a missing arm, or fewer than {minimum_repeats} "
            "repeats). A report over an empty set would read as 'no effect anywhere'."
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
        median_token_ratio=statistics.median(token_ratios) if token_ratios else None,
        median_wall_clock_ratio=(
            statistics.median(wall_clock_ratios) if wall_clock_ratios else None
        ),
    )
