"""Turning two graded answers to one question into the tool-utility A/B.

Produces `evals/ab.py::compare_tool_utility`'s inputs from a live run. The augmented arm is the
default agent; the baseline arm asks the same question of `data/evals/profiles/no-tools.yaml`, which
removes the tools and replaces the system prompt, so its delta is prompt-and-tools, not tools alone.
`data/evals/profiles/tools-removed.yaml` varies only the tools.

Scored per task, so "tools helped here and hurt there" survives. Judge verdicts map to one axis with
`fabricated` below `unserved`, since an invented answer is worse than a refusal. `ungraded` (the
judge failed) is dropped, and `paired_tasks` returns what it dropped.
"""

from collections.abc import Mapping, Sequence

from chemclaw.evals.ab import ABSummary, TaskScores, compare_tool_utility
from chemclaw.evals.live_judge import Judgement
from chemclaw.evals.probe import Probe

# One judge verdict on the A/B axis; higher is better. `fabricated` is negative, below a declined
# answer.
VERDICT_SCORES: Mapping[str, float] = {
    "served": 1.0,
    "partial": 0.5,
    "unserved": 0.0,
    "fabricated": -1.0,
}


class UnpairedProbe(ValueError):
    """A probe that reached only one arm — the one thing a paired comparison cannot absorb."""


def paired_tasks(
    probes: Sequence[Probe],
    augmented: Mapping[str, Judgement],
    baseline: Mapping[str, Judgement],
) -> tuple[list[TaskScores], list[str]]:
    """Score each probe's two verdicts into one `TaskScores`; return the tasks and what was dropped.

    Args:
        probes: The probes both arms were asked, in report order.
        augmented: Verdict per probe id from the arm that had tools.
        baseline: Verdict per probe id from the toolless arm.

    Returns:
        `(tasks, dropped)` — the paired scores, and the ids left out because at least one arm was
        `ungraded`, so a report can say how much smaller the comparison became.

    Raises:
        UnpairedProbe: A probe is missing from an arm entirely: the arms did not ask the same set,
            so every aggregate would compare different questions.
    """
    tasks: list[TaskScores] = []
    dropped: list[str] = []
    for probe in probes:
        if probe.id not in augmented or probe.id not in baseline:
            missing = "augmented" if probe.id not in augmented else "baseline"
            raise UnpairedProbe(f"probe {probe.id!r} has no {missing} verdict — the arms differ")
        with_tools, without = augmented[probe.id], baseline[probe.id]
        if with_tools.verdict == "ungraded" or without.verdict == "ungraded":
            dropped.append(probe.id)
            continue
        tasks.append(
            TaskScores(
                task_id=probe.id,
                baseline=VERDICT_SCORES[without.verdict],
                augmented=VERDICT_SCORES[with_tools.verdict],
            )
        )
    return tasks, dropped


def by_bucket(probes: Sequence[Probe], tasks: Sequence[TaskScores]) -> dict[str, ABSummary]:
    """One summary per bucket, plus `"all"` — because the buckets ask opposite questions.

    In bucket A tools should win; in bucket C the honest answer is a refusal and tools are a chance
    to fabricate. A single aggregate would let one cancel the other. A bucket with no paired task is
    absent, since `compare_tool_utility` refuses an empty list.
    """
    bucket_of = {probe.id: probe.bucket for probe in probes}
    summaries: dict[str, ABSummary] = {}
    for bucket in sorted({bucket_of[task.task_id] for task in tasks}):
        chosen = [task for task in tasks if bucket_of[task.task_id] == bucket]
        summaries[bucket] = compare_tool_utility(chosen, higher_is_better=True)
    if tasks:
        summaries["all"] = compare_tool_utility(list(tasks), higher_is_better=True)
    return summaries
