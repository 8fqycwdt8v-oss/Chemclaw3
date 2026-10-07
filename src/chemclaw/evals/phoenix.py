"""Publish an archived probe run to Phoenix, so two runs can be diffed instead of described.

Reads transcripts already on disk (`evals/live.py` writes `{probe, outcome}` per probe, the judge
writes `grades.json`) and writes them into an existing Phoenix. Runs no model.

The mapping:

- A **dataset example** is a *probe*, read from the committed corpus in `data/evals/probes/`, not
  from the run: it is the axis runs are compared along. Phoenix versions the dataset when
  examples change, and an experiment names the version it ran against. Building examples from a
  run would make an incomplete run look like a shrunken corpus.
- An **experiment run** is a `ProbeOutcome`; one experiment per archived directory.
- An **evaluation** is a judgement about an outcome: the judge's verdict is
  `annotator_kind="LLM"`, signals derived from the outcome itself are `annotator_kind="CODE"`,
  and the two are never flattened into one column.

No verdict is re-derived: `live_judge.judgement_from_transcript` rehydrates each transcript.
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

# Files in a transcript directory that are not transcripts. Named rather than pattern-matched, since
# a probe id is arbitrary.
_NOT_TRANSCRIPTS = frozenset({"grades.json", "evidence.json", "summary.md"})


@dataclass(frozen=True)
class PublishedRun:
    """What one publish produced, in Phoenix's own identifiers.

    Returned so the CLI can print a URL, and so the counts show a partial publish (fewer runs than
    examples).
    """

    dataset_id: str
    dataset_version_id: str
    experiment_id: str
    examples: int
    runs: int
    evaluations: int


def load_transcripts(directory: Path) -> list[tuple[Probe, ProbeOutcome]]:
    """Every archived probe in `directory`, rehydrated, in probe-id order.

    Sorted so re-publishing a directory yields the same example order.

    Args:
        directory: A transcript directory written by `cli/live_probes.py`.

    Returns:
        The `(probe, outcome)` pairs it holds.

    Raises:
        FileNotFoundError: The directory does not exist, rather than an empty publish.
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

    Optional: an ungraded run is still worth publishing for its objective signals.

    Args:
        directory: The transcript directory, or the run directory beside it. Both are searched,
            because `cli/live_probes.py` writes `grades.json` next to the transcripts for a probe
            run and one level up for an archived set.

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

    `output` is the reference — what the probe declared it wanted (`expects_tools`,
    `forbids_claims`, `asserts_absent`) — not anything the system produced. `needs_bundle` goes on
    `metadata`: it records which deployment the probe was scored against, which changes with
    configuration alone, so two lanes' runs stay distinguishable.
    """
    return (
        {"question": probe.question, "persona": probe.persona, "direction": probe.direction},
        {
            "expects_tools": sorted(probe.expects_tools),
            "forbids_claims": list(probe.forbids_claims),
            "asserts_absent": list(probe.asserts_absent),
        },
        {
            "probe_id": probe.id,
            "section": probe.section,
            "bucket": probe.bucket,
            "needs_bundle": probe.needs_bundle,
        },
    )


def _run_output(outcome: ProbeOutcome) -> dict[str, Any]:
    """What the system did with one probe, as the experiment run's output.

    The answer plus the surface it used; judgements about the outcome are published as evaluations,
    not duplicated here.
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

    Yes/no questions are scored 1.0/0.0 so Phoenix can aggregate them across an experiment. An
    unmeasurable signal is omitted, never scored zero: `expected_tools_met is None` (the probe
    expects no tool) and an `ungraded` verdict (the judge failed) publish nothing, since a zero
    would be a claim about the run.
    """
    if outcome.expected_tools_met is not None:
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
    # `failed_loudly` (a failure the chemist could see) is published for every observed turn, so its
    # aggregate is the run's loud-failure rate. A transport death publishes no row: whether the
    # system announced its failure was not observed. `publish_run` marks such a run with `error=`.
    if outcome.transport_error is None:
        yield {
            "name": "failed_loudly",
            "annotator_kind": "CODE",
            "score": 1.0 if outcome.failed_loudly else 0.0,
            "label": outcome.error_code or ("tool_failed" if outcome.tools_failed else "clean"),
            "explanation": ", ".join(outcome.tools_failed) or None,
        }
    if judgement is not None:
        verdict = str(judgement.get("verdict", "ungraded"))
        # `ungraded` is a fact about the grading pass, not the system, so it publishes no score.
        if verdict != "ungraded":
            yield {
                "name": "judge_verdict",
                "annotator_kind": "LLM",
                "score": 1.0 if verdict == "served" else 0.0,
                "label": verdict,
                "explanation": str(judgement.get("reason") or "") or None,
            }


def _window(outcome: ProbeOutcome, at: datetime) -> tuple[datetime, datetime]:
    """The interval Phoenix records a run over, from the latency the transcript kept.

    Transcripts hold a duration but no wall clock, so the caller supplies the anchor; the span is
    what carries meaning, not the instant.
    """
    return at, at + timedelta(seconds=outcome.latency_seconds)


def publish_corpus(
    client: Any, *, dataset_name: str | None = None, probe_dir: str | None = None
) -> Any:
    """Publish the committed probe corpus as the dataset every run is an experiment over.

    Idempotent: Phoenix cuts a new version only when the examples differ, so calling this before
    every publish keeps the dataset equal to `data/evals/probes/`.

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

    The corpus is published first, so the experiment names the dataset version it was measured
    against; a run covering fewer probes is an experiment with fewer runs.

    Args:
        directory: A transcript directory written by `cli/live_probes.py`.
        experiment_name: What this run is called in Phoenix — the arm, the model, the date.
        client: A `phoenix.client.Client`, injected so the CLI owns the endpoint and tests can
            record calls.
        dataset_name: The dataset to publish into; defaults to the configured one.
        probe_dir: The corpus to publish as the dataset; defaults to the configured one.
        now: The anchor the run windows are measured from; defaults to publish time.

    Returns:
        The identifiers and counts of what was written.

    Raises:
        FileNotFoundError: No such transcript directory.
        ValueError: The directory holds no transcripts, or holds a probe the corpus does not.
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

    Matched on `probe_id`, not position, so a corpus that gains a probe cannot shift results onto
    the wrong questions.
    """
    by_probe = {
        str((example.get("metadata") or {}).get("probe_id")): str(example["id"])
        for example in dataset.examples
    }
    missing = [probe.id for probe, _ in transcripts if probe.id not in by_probe]
    if missing:
        raise ValueError(f"dataset version is missing example(s) for probe(s): {sorted(missing)}")
    return by_probe
