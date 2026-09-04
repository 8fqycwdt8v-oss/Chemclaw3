"""Turning two graded answers to one question into the tool-utility A/B this corpus never ran.

Why this exists: `evals/ab.py::compare_tool_utility` has always been able to compare a metric with
tools against the same metric without them, and nothing ever produced its inputs from a live run —
the one registered caller (`autonomy.plan_execute_utility`) reads four hand-written floats out of a
case file. So the comparison the corpus most needs was implemented, registered, gated, and never
measured.

**What the two arms are.** The augmented arm is the front door's default agent. The baseline arm is
the *same question* asked of `data/evals/profiles/no-tools.yaml`, a profile whose `tool_names: []`
structurally removes every capability tool (and, by `ToolScopedSkills`, every skill about them)
before the graph is compiled. That is the comparison ChemToolAgent reports and this repository could
not reproduce: tool augmentation does not consistently beat the base model, and it hurts on general
chemistry questions.

**Why a verdict becomes a number rather than a rate.** `compare_tool_utility` scores *per task* so
that "tools helped here and hurt there" survives, which an aggregate served-rate destroys — and the
per-task delta is what selective steering would need. The scale is the judge's own five verdicts
collapsed onto one axis, with `fabricated` **below** `unserved` because the whole point of the
comparison is that tools introduce an error class of their own: an answer that invents a citation is
worse than one that declines, and a scale that put them level would score the failure mode this
measurement exists to find as a tie.

**`ungraded` is dropped, loudly.** It means the judge itself failed, which is evidence about the
grader rather than about either arm; scoring it as anything would put a grader outage into the
system's own utility number. `paired_tasks` returns what it dropped so a report can say so.
"""

from collections.abc import Mapping, Sequence

from chemclaw.evals.ab import ABSummary, TaskScores, compare_tool_utility
from chemclaw.evals.live_judge import Judgement
from chemclaw.evals.probe import Probe

#: One judge verdict, on the axis the A/B compares. Higher is better, and the span is deliberate:
#: `fabricated` is a negative rather than a zero, so a probe whose tool-armed answer invents
#: something scores *below* the same probe answered by a model that declined.
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
        `ungraded`. The second element is returned rather than logged because a comparison over 30
        probes that silently became one over 11 is the failure this repository keeps finding in its
        own harnesses.

    Raises:
        UnpairedProbe: A probe is missing from an arm entirely. Unlike an `ungraded` verdict — which
            is a grader failure and is dropped — a missing id means the two arms did not ask the
            same set, so every aggregate below would be comparing different questions.
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

    Bucket A is "the capability exists, tools should win"; bucket C is "there is no capability, the
    honest answer is a refusal, and tools are an opportunity to fabricate one". A single aggregate
    over both would let a gain on one cancel a loss on the other, which is exactly the averaging
    that made the deleted routing measurement unable to answer its own question.

    A bucket with no paired task is absent from the result rather than present and empty:
    `compare_tool_utility` refuses an empty task list on purpose, and inventing a benign-looking
    zero for it here would defeat that refusal one layer up.
    """
    bucket_of = {probe.id: probe.bucket for probe in probes}
    summaries: dict[str, ABSummary] = {}
    for bucket in sorted({bucket_of[task.task_id] for task in tasks}):
        chosen = [task for task in tasks if bucket_of[task.task_id] == bucket]
        summaries[bucket] = compare_tool_utility(chosen, higher_is_better=True)
    if tasks:
        summaries["all"] = compare_tool_utility(list(tasks), higher_is_better=True)
    return summaries
