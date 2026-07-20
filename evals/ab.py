"""Per-task tool-utility A/B comparison (plan step 2b.4).

Why this exists: tool/skill augmentation does **not** help uniformly — it is
task-dependent and can introduce its own error class (docs/research-review.md F8/F9).
This module compares a metric value with tools against the same metric without them,
per task, so the system can steer tool use **selectively** — crediting tools only where
they measurably help and flagging tasks where they hurt. It is a pure comparison over
already-scored values (produced by the metric layer); it does not run any model itself.
"""

from pydantic import BaseModel, Field

from chemclaw.config import settings


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

    `delta` is oriented so positive always means "tools improved the metric": for a
    higher-is-better metric it is `augmented - baseline`, otherwise the reverse. A
    delta within +/- `eval_ab_epsilon` (a per-metric noise floor) counts as no effect,
    so tools are credited or blamed only above measurement noise.
    """
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
