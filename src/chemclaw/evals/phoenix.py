"""Publish an archived probe run to Phoenix, so two runs can be diffed instead of described.

**What was missing, precisely.** `D-2026-08-11-a-model-call-is-a-span-and-phoenix-is-a-deployment`
shipped the *trace* half of AG-13 — a span per model call, carrying its token counts — and said in
as many words that it did not close the row: what remained was "datasets, run-over-run diffing and
annotation … a *deployment* someone runs against the probe transcripts". This module is that, and
nothing more: it reads transcripts that already exist on disk and writes them into a Phoenix that
already exists on the network.

**It runs no model, and that is the point.** Every probe run this repo has ever taken is already
persisted — `evals/live.py` writes `{probe, outcome}` per probe as each lands, and the judge's
verdicts sit beside them in `grades.json`. So the experiment surface AG-13 asks for does not need a
credential, a benchmark or a re-run to exist; it needs the record loaded somewhere that can compare
two of them. `tasks/live-test/` alone holds three arms of the same corpus (190 probes, a 92-probe
sonnet arm, a 6-probe after-fix set), which is a run-over-run comparison nobody could see.

**The mapping, and why each half is where it is.**

- A **dataset example** is a *probe*, read from the committed corpus in `data/evals/probes/` — not
  from the run being published. It is the part that does not change between runs, which is what
  makes it the axis two runs are compared along. Phoenix versions a dataset when its examples
  change, so re-publishing an unchanged corpus adds no version and an edited one adds exactly
  one — and an experiment names the version it ran against, so a corpus edit can never silently
  re-label an older run's results.

  **The dataset is the corpus and not the run, and that distinction was measured rather than
  assumed.** Building the examples from a run's own transcripts looks equivalent and is not:
  publishing the archived 92-probe sonnet arm that way cut the dataset's newest version from 190
  examples to 92, so a run that merely *covered less* was recorded as a corpus that had *lost* 98
  questions. Reading the corpus instead makes an incomplete run show up as an experiment with
  fewer runs, which is what an incomplete run is.
- An **experiment run** is a *`ProbeOutcome`*: what the system did with that question on one
  occasion. One experiment per archived directory.
- An **evaluation** is a *judgement about that outcome*, and there are two kinds. The judge's
  verdict is `annotator_kind="LLM"` because a stronger model produced it; the signals derived from
  the outcome itself — did it call the tools the probe expected, did it cite what it named, did it
  fail loudly — are `annotator_kind="CODE"`. Phoenix carries that distinction natively and this
  module refuses to flatten it: "a model said this answer was served" and "the transport recorded
  no error" are not the same class of claim, and a surface that showed them as one column would
  invite exactly the conflation the judge exists to avoid.

**Nothing here re-derives a verdict.** `live_judge.judgement_from_transcript` already rehydrates a
stored transcript into `(Probe, ProbeOutcome)`, and it exists for the same reason this module reuses
it: re-asking 190 live questions to correct a downstream bug would change the thing being measured.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from chemclaw.core.config import settings
from chemclaw.evals.live import ProbeOutcome, load_probes
from chemclaw.evals.live_judge import judgement_from_transcript
from chemclaw.evals.probe import Probe

# The files a transcript directory holds that are not transcripts. `evals/live.py` writes one
# `<probe-id>.json` per probe and `cli/live_probes.py` writes its outputs into the same directory
# "so outputs live with the transcripts that produced them" — so the directory is a mixed bag and
# the reader has to know which is which. Named rather than pattern-matched: a probe id is
# arbitrary, and a rule like "skip anything without a dash" would silently drop a real probe.
_NOT_TRANSCRIPTS = frozenset({"grades.json", "evidence.json", "summary.md"})

# What one probe's turn is recorded as having cost when the outcome carries no latency. Phoenix
# requires a start and an end on every run, and refusing to publish a probe for want of a duration
# would drop exactly the failed turns most worth looking at — a transport error is the case where
# `latency_seconds` is most likely missing and least likely irrelevant.
_UNKNOWN_DURATION = timedelta(0)


@dataclass(frozen=True)
class PublishedRun:
    """What one publish produced, in Phoenix's own identifiers.

    Returned rather than logged because the caller is a CLI that has to print a URL somebody can
    open, and because the counts are the check: an experiment with fewer runs than the dataset has
    examples is a partial publish, which is a thing to notice rather than a thing to infer.
    """

    dataset_id: str
    dataset_version_id: str
    experiment_id: str
    examples: int
    runs: int
    evaluations: int


def load_transcripts(directory: Path) -> list[tuple[Probe, ProbeOutcome]]:
    """Every archived probe in `directory`, rehydrated, in probe-id order.

    Sorted so two publishes of the same directory produce the same example order, which is what
    lets Phoenix recognise a re-publish as the same version rather than a new one.

    Args:
        directory: A transcript directory written by `cli/live_probes.py`.

    Returns:
        The `(probe, outcome)` pairs it holds.

    Raises:
        FileNotFoundError: The directory does not exist — a typo'd path is worth a hard stop
            rather than an empty publish that reads as "the run had no probes".
    """
    if not directory.is_dir():
        raise FileNotFoundError(f"no transcript directory at {directory}")
    return [
        judgement_from_transcript(json.loads(path.read_text()))
        for path in sorted(directory.glob("*.json"))
        if path.name not in _NOT_TRANSCRIPTS
    ]


def load_grades(directory: Path) -> dict[str, Mapping[str, Any]]:
    """The judge's verdicts for `directory`, keyed by probe id; empty when it was never graded.

    Absent rather than required: a run whose judge pass never happened is still worth publishing —
    the objective signals do not need a grader — and demanding `grades.json` would make the most
    common state of a fresh run unpublishable.

    Args:
        directory: The transcript directory, or the run directory beside it. Both are searched,
            because `cli/live_probes.py` writes `grades.json` next to the transcripts for a probe
            run and one level up for the archived sets in `tasks/live-test/`.

    Returns:
        `{probe_id: judgement}` for whichever file was found first.
    """
    for candidate in (directory / "grades.json", directory.parent / "grades.json"):
        if not candidate.is_file():
            continue
        graded = json.loads(candidate.read_text())
        return {str(entry["probe_id"]): entry for entry in graded}
    return {}


def _example(probe: Probe) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """One probe as Phoenix's `(input, output, metadata)` triple.

    `output` is the *reference* — what the probe declared it wanted — rather than anything the
    system produced. That is Phoenix's convention for a dataset example and it is also the honest
    one here: `expects_tools` and `forbids_claims` are the corpus's claim about a right answer, and
    they belong on the axis, not in the run.
    """
    return (
        {"question": probe.question, "persona": probe.persona, "direction": probe.direction},
        {
            "expects_tools": sorted(probe.expects_tools),
            "expects_specialist": probe.expects_specialist,
            "forbids_claims": list(probe.forbids_claims),
        },
        {"probe_id": probe.id, "section": probe.section, "bucket": probe.bucket},
    )


def _run_output(outcome: ProbeOutcome) -> dict[str, Any]:
    """What the system did with one probe, as the experiment run's output.

    The answer plus the surface it used. Deliberately not the whole `ProbeOutcome`: the fields that
    are *judgements* about the outcome become evaluations below, where they can carry a score and
    an annotator kind, and duplicating them here would put the same claim on the page twice with
    nothing saying which one to believe.
    """
    return {
        "answer": outcome.answer,
        "tools_called": list(outcome.tools_called),
        "tools_failed": list(outcome.tools_failed),
        "specialists": list(outcome.specialists),
        "jobs_started": list(outcome.jobs_started),
        "notes_proposed": outcome.notes_proposed,
    }


def _evaluations(
    outcome: ProbeOutcome, judgement: Mapping[str, Any] | None
) -> Iterator[dict[str, Any]]:
    """Every judgement about one outcome, each with the kind of thing that made it.

    Scores are 1.0/0.0 rather than a bare label wherever the question is a yes/no, because Phoenix
    aggregates a score across an experiment and a label only groups: "expected tools met" is the
    number a second run should be compared on, and a label would make that comparison a manual
    read of two lists.
    """
    yield {
        "name": "expected_tools_met",
        "annotator_kind": "CODE",
        "score": 1.0 if outcome.expected_tools_met else 0.0,
        "label": str(outcome.expected_tools_met).lower(),
    }
    yield {
        "name": "answered",
        "annotator_kind": "CODE",
        "score": 1.0 if outcome.answered else 0.0,
        "label": str(outcome.answered).lower(),
    }
    # Uncited note ids are the fabrication signal that needs no model: the probe's own transcript
    # recorded which ids the answer named and which of them no retrieval returned.
    yield {
        "name": "uncited_note_ids",
        "annotator_kind": "CODE",
        "score": float(len(outcome.uncited_note_ids)),
        "label": "clean" if not outcome.uncited_note_ids else "uncited",
        "explanation": ", ".join(outcome.uncited_note_ids) or None,
    }
    # A failure the chemist can see is a different outcome from a failure they cannot, which is why
    # `failed_loudly` is recorded rather than folded into `answered`.
    if outcome.error_code or outcome.transport_error:
        yield {
            "name": "failed_loudly",
            "annotator_kind": "CODE",
            "score": 1.0 if outcome.failed_loudly else 0.0,
            "label": outcome.error_code or "transport_error",
            "explanation": outcome.transport_error,
        }
    if judgement is not None:
        verdict = str(judgement.get("verdict", "ungraded"))
        yield {
            "name": "judge_verdict",
            "annotator_kind": "LLM",
            "score": 1.0 if verdict == "served" else 0.0,
            "label": verdict,
            "explanation": str(judgement.get("reason") or "") or None,
        }


def _window(outcome: ProbeOutcome, at: datetime) -> tuple[datetime, datetime]:
    """The interval Phoenix records a run over, from the latency the transcript kept.

    The transcripts hold a duration and no wall-clock, so the caller supplies the anchor. That is
    stated rather than hidden: an archived run's runs are stamped at publish time, and the number
    that carries meaning is the *span*, not the instant.
    """
    duration = (
        timedelta(seconds=outcome.latency_seconds)
        if outcome.latency_seconds is not None
        else _UNKNOWN_DURATION
    )
    return at, at + duration


def publish_corpus(
    client: Any, *, dataset_name: str | None = None, probe_dir: str | None = None
) -> Any:
    """Publish the committed probe corpus as the dataset every run is an experiment over.

    Idempotent by construction rather than by a check: Phoenix compares the examples it is given
    against the current version and only cuts a new one when they differ, so calling this before
    every publish costs a request and keeps the dataset equal to what is in `data/evals/probes/`.

    Args:
        client: A `phoenix.client.Client`.
        dataset_name: Defaults to the configured one.
        probe_dir: Defaults to the configured corpus directory.

    Returns:
        The Phoenix `Dataset`, carrying the version id this publish resolved to.
    """
    examples = [_example(probe) for probe in load_probes(probe_dir)]
    return client.datasets.create_dataset(
        name=dataset_name or settings.phoenix_dataset_name,
        inputs=[inputs for inputs, _, _ in examples],
        outputs=[outputs for _, outputs, _ in examples],
        metadata=[metadata for _, _, metadata in examples],
        dataset_description="ChemClaw live probes — the committed corpus in data/evals/probes/.",
    )


def publish_run(
    directory: Path,
    *,
    experiment_name: str,
    client: Any,
    dataset_name: str | None = None,
    probe_dir: str | None = None,
    now: datetime | None = None,
) -> PublishedRun:
    """Publish one archived transcript directory as a Phoenix experiment over the probe dataset.

    The corpus is published first, so the dataset is always the questions this repo currently
    commits and the experiment names the version it was measured against. A run covering fewer
    probes than the corpus holds is an experiment with fewer runs — it does not shrink the dataset.

    Args:
        directory: A transcript directory written by `cli/live_probes.py`.
        experiment_name: What this run is called in Phoenix — the arm, the model, the date.
        client: A `phoenix.client.Client`. Injected rather than constructed here so the CLI owns
            the endpoint and the tests can drive this against a recorder.
        dataset_name: The dataset to publish into; defaults to the configured one.
        probe_dir: The corpus to publish as the dataset; defaults to the configured one.
        now: The anchor the run windows are measured from; defaults to publish time.

    Returns:
        The identifiers and counts of what was written.

    Raises:
        FileNotFoundError: No such transcript directory.
        ValueError: The directory holds no transcripts — an empty publish would create an
            experiment that says a run happened and found nothing, which is not what happened —
            or it holds a probe the corpus does not.
    """
    transcripts = load_transcripts(directory)
    if not transcripts:
        raise ValueError(f"no probe transcripts in {directory}")
    grades = load_grades(directory)
    anchor = now or datetime.now(UTC)

    dataset = publish_corpus(client, dataset_name=dataset_name, probe_dir=probe_dir)
    experiment = client.experiments.create(
        dataset_id=dataset.id,
        dataset_version_id=dataset.version_id,
        experiment_name=experiment_name,
        experiment_description=f"Archived run from {directory}",
        experiment_metadata={"source_directory": str(directory), "graded": bool(grades)},
    )

    example_ids = _example_ids(dataset, transcripts)
    runs = evaluations = 0
    for probe, outcome in transcripts:
        start, end = _window(outcome, anchor)
        run = client.experiments.log_run(
            experiment_id=experiment["id"],
            dataset_example_id=example_ids[probe.id],
            output=_run_output(outcome),
            start_time=start,
            end_time=end,
            error=outcome.transport_error,
        )
        runs += 1
        for evaluation in _evaluations(outcome, grades.get(probe.id)):
            client.experiments.log_evaluation(experiment_run_id=run["id"], **evaluation)
            evaluations += 1

    return PublishedRun(
        dataset_id=dataset.id,
        dataset_version_id=dataset.version_id,
        experiment_id=experiment["id"],
        examples=len(dataset.examples),
        runs=runs,
        evaluations=evaluations,
    )


def _example_ids(dataset: Any, transcripts: Sequence[tuple[Probe, ProbeOutcome]]) -> dict[str, str]:
    """`{probe_id: dataset_example_id}`, matched on the metadata the examples were written with.

    Matched on `probe_id` rather than on position. Position would work today and would break the
    first time a corpus gains a probe: a run publishing 190 outcomes against a 191-example version
    would attach every result after the insertion to the wrong question, and nothing about the
    resulting numbers would look wrong.
    """
    by_probe = {
        str((example.get("metadata") or {}).get("probe_id")): str(example["id"])
        for example in dataset.examples
    }
    missing = [probe.id for probe, _ in transcripts if probe.id not in by_probe]
    if missing:
        raise ValueError(f"dataset version is missing example(s) for probe(s): {sorted(missing)}")
    return by_probe
