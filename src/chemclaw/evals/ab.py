"""Per-task tool-utility A/B comparison.

Tool augmentation does not help uniformly and can introduce its own errors, so this compares a
metric with tools against the same metric without them, per task, to credit tools only where they
measurably help. A pure comparison over already-scored values; it runs no model.
"""

from pydantic import BaseModel, Field

from chemclaw.core.config import settings


class TaskScores(BaseModel):
    """One task scored twice by the same metric: baseline vs. tool-augmented.

    Scores must be finite: a NaN would make every epsilon comparison false (a
    silent "no effect") and poison `net_delta`, so it is rejected at the model.
    """

    task_id: str = Field(min_length=1)
    baseline: float = Field(allow_inf_nan=False)
    augmented: float = Field(allow_inf_nan=False)


class ToolUtility(BaseModel):
    """The signed benefit of tools on one task, in the metric's "better" direction."""

    task_id: str
    delta: float
    verdict: str  # "helped" | "hurt" | "no effect"


class ABSummary(BaseModel):
    """Aggregate tool utility over a task set — the selective-steering evidence."""

    higher_is_better: bool
    utilities: list[ToolUtility]
    helped: list[str]
    hurt: list[str]
    no_effect: list[str]
    net_delta: float


def compare_tool_utility(tasks: list[TaskScores], higher_is_better: bool) -> ABSummary:
    """Compare augmented vs. baseline per task and aggregate where tools help/hurt.

    `delta` is oriented so positive means "tools improved the metric". A delta within +/-
    `eval_ab_epsilon` (the metric's noise floor) counts as no effect. An empty task list is rejected
    rather than reported as a benign "no effect anywhere".
    """
    if not tasks:
        raise ValueError("empty task list — an A/B comparison over nothing proves nothing")
    epsilon = settings.eval_ab_epsilon
    utilities: list[ToolUtility] = []
    helped: list[str] = []
    hurt: list[str] = []
    no_effect: list[str] = []
    for task in tasks:
        delta = (
            task.augmented - task.baseline if higher_is_better else task.baseline - task.augmented
        )
        if delta > epsilon:
            verdict, bucket = "helped", helped
        elif delta < -epsilon:
            verdict, bucket = "hurt", hurt
        else:
            verdict, bucket = "no effect", no_effect
        bucket.append(task.task_id)
        utilities.append(ToolUtility(task_id=task.task_id, delta=delta, verdict=verdict))
    return ABSummary(
        higher_is_better=higher_is_better,
        utilities=utilities,
        helped=helped,
        hurt=hurt,
        no_effect=no_effect,
        net_delta=sum(u.delta for u in utilities),
    )
