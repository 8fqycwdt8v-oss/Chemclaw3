"""Publishing an archived run is a projection of the record, not a re-derivation of it (AG-13).

These drive `evals/phoenix.py` against a recording stand-in rather than a live Phoenix, for the
reason the module's own docstring gives about the transcripts: the thing under test is the mapping
from what this repo stored to what Phoenix is told, and a server in the loop would make a mapping
bug and a server bug the same failure. The live half is recorded in the ADR, where it belongs — a
real Phoenix 20.1.0 took every archived arm in this repo.

The one test that matters most is `test_a_partial_run_does_not_shrink_the_corpus`: building the
dataset from a run's own transcripts is the obvious implementation, it passes every other check
here, and it silently records a run that covered less as a corpus that lost questions.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from chemclaw.evals.live import load_probes
from chemclaw.evals.phoenix import load_grades, load_transcripts, publish_run

_PROBE_DIR = "data/evals/probes"


@dataclass
class _Dataset:
    """What the client returns from `create_dataset` — id, version, and the examples it holds."""

    id: str
    version_id: str
    examples: list[dict[str, Any]]


@dataclass
class _Datasets:
    """Records every dataset publish, and hands back examples with ids the caller can match on."""

    published: list[dict[str, Any]] = field(default_factory=list)

    def create_dataset(self, **kwargs: Any) -> _Dataset:
        self.published.append(kwargs)
        metadata = list(kwargs["metadata"])
        return _Dataset(
            id="ds-1",
            version_id=f"v-{len(self.published)}",
            examples=[
                {"id": f"ex-{index}", "metadata": entry} for index, entry in enumerate(metadata)
            ],
        )


@dataclass
class _Experiments:
    """Records the experiment, its runs and their evaluations, in the order they were logged."""

    created: list[dict[str, Any]] = field(default_factory=list)
    runs: list[dict[str, Any]] = field(default_factory=list)
    evaluations: list[dict[str, Any]] = field(default_factory=list)

    def create(self, **kwargs: Any) -> dict[str, Any]:
        self.created.append(kwargs)
        return {"id": "exp-1"}

    def log_run(self, **kwargs: Any) -> dict[str, Any]:
        self.runs.append(kwargs)
        return {"id": f"run-{len(self.runs)}"}

    def log_evaluation(self, **kwargs: Any) -> dict[str, Any]:
        self.evaluations.append(kwargs)
        return {"id": f"eval-{len(self.evaluations)}"}


@dataclass
class _Client:
    """The two resources `publish_run` reaches for."""

    datasets: _Datasets = field(default_factory=_Datasets)
    experiments: _Experiments = field(default_factory=_Experiments)


@pytest.fixture
def archived(tmp_path: Path) -> Path:
    """A transcript directory holding two probes from the committed corpus, one of them graded."""
    # The *last* two in corpus order, deliberately. The first two would sit at example positions 0
    # and 1, where matching by id and matching by position agree — and a fixture that cannot tell
    # two implementations apart cannot test which one is in use.
    chosen = load_probes(_PROBE_DIR)[-2:]
    directory = tmp_path / "transcripts"
    directory.mkdir()
    for index, probe in enumerate(chosen):
        outcome = {
            "probe_id": probe.id,
            "section": probe.section,
            "persona": probe.persona,
            "bucket": probe.bucket,
            "question": probe.question,
            "answer": f"answer {index}",
            "answered": True,
            "tools_called": ["gather_evidence"],
            "expected_tools_met": index == 0,
            "uncited_note_ids": [] if index == 0 else ["note-nobody-returned"],
            "latency_seconds": 1.5,
        }
        (directory / f"{probe.id}.json").write_text(
            json.dumps({"probe": probe.model_dump(), "outcome": outcome})
        )
    (directory / "grades.json").write_text(
        json.dumps([{"probe_id": chosen[0].id, "verdict": "served", "reason": "cited its sources"}])
    )
    return directory


def test_a_partial_run_does_not_shrink_the_corpus(archived: Path) -> None:
    """The dataset is every committed probe; a run that covered two of them logs two runs.

    Building the examples from the run's own transcripts passes every other test in this file and
    is wrong: it was measured against a live Phoenix cutting a 190-example version down to 92 when
    the shorter sonnet arm was published, which reads as a corpus that lost 98 questions rather
    than a run that answered fewer.
    """
    client = _Client()
    published = publish_run(archived, experiment_name="arm", client=client, probe_dir=_PROBE_DIR)

    assert published.examples == len(load_probes(_PROBE_DIR))
    assert published.runs == 2
    assert published.runs < published.examples, "the fixture must cover only part of the corpus"


def test_every_probe_is_matched_by_id_and_not_by_position(archived: Path) -> None:
    """A run's outcome attaches to the example carrying its probe id.

    Position would work on a corpus that never gains a probe. On one that does, every result after
    the insertion would attach to the wrong question and nothing about the numbers would look wrong.
    """
    client = _Client()
    publish_run(archived, experiment_name="arm", client=client, probe_dir=_PROBE_DIR)

    # The recorder numbers examples by their position in the corpus, so an example id names the
    # index its probe sits at. The fixture covers the *last* two probes, so an implementation that
    # matched the run's first outcome to the dataset's first example would say `ex-0`/`ex-1`.
    published = client.datasets.published[0]
    index_of = {entry["probe_id"]: position for position, entry in enumerate(published["metadata"])}
    covered = sorted(path.stem for path in archived.glob("*.json") if path.name != "grades.json")
    expected = {f"ex-{index_of[probe_id]}" for probe_id in covered}

    assert {run["dataset_example_id"] for run in client.experiments.runs} == expected
    assert expected != {"ex-0", "ex-1"}, (
        "the fixture no longer distinguishes id-matching from position-matching"
    )


def test_the_judge_and_the_transcript_are_different_kinds_of_claim(archived: Path) -> None:
    """A model's verdict is `LLM`; a signal read off the outcome is `CODE`.

    Flattening them would put "a stronger model called this served" and "the transport recorded no
    error" in one column, which is the conflation the judge exists to avoid.
    """
    client = _Client()
    publish_run(archived, experiment_name="arm", client=client, probe_dir=_PROBE_DIR)

    kinds = {(e["name"], e["annotator_kind"]) for e in client.experiments.evaluations}
    assert ("judge_verdict", "LLM") in kinds
    assert ("expected_tools_met", "CODE") in kinds
    assert not any(name == "judge_verdict" and kind != "LLM" for name, kind in kinds), (
        "a verdict a model produced must not be recorded as a code signal"
    )


def test_an_ungraded_probe_still_publishes_its_objective_signals(archived: Path) -> None:
    """Only one of the two probes was graded; both are published.

    Requiring `grades.json` would make the most common state of a fresh run — transcripts written,
    judge pass not yet run — unpublishable, and the signals that need no grader are exactly the
    ones a fresh run wants to look at.
    """
    client = _Client()
    publish_run(archived, experiment_name="arm", client=client, probe_dir=_PROBE_DIR)

    verdicts = [e for e in client.experiments.evaluations if e["name"] == "judge_verdict"]
    assert len(verdicts) == 1, "exactly one probe was graded"
    assert len(client.experiments.runs) == 2, "both probes were published"


def test_the_uncited_note_is_scored_by_count_not_by_label(archived: Path) -> None:
    """`uncited_note_ids` carries a number, because a second run is compared on the number."""
    client = _Client()
    publish_run(archived, experiment_name="arm", client=client, probe_dir=_PROBE_DIR)

    scores = sorted(
        e["score"] for e in client.experiments.evaluations if e["name"] == "uncited_note_ids"
    )
    assert scores == [0.0, 1.0]


def test_an_empty_directory_is_refused_rather_than_published(tmp_path: Path) -> None:
    """An experiment saying a run happened and found nothing is not what happened."""
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="no probe transcripts"):
        publish_run(empty, experiment_name="arm", client=_Client(), probe_dir=_PROBE_DIR)


def test_a_missing_directory_is_a_hard_stop(tmp_path: Path) -> None:
    """A typo'd path must not read as a run with no probes."""
    with pytest.raises(FileNotFoundError, match="no transcript directory"):
        publish_run(tmp_path / "nope", experiment_name="arm", client=_Client())


def test_the_run_outputs_are_not_the_directorys_bookkeeping(archived: Path) -> None:
    """`grades.json` sits beside the transcripts and is not one of them."""
    loaded = load_transcripts(archived)
    assert len(loaded) == 2
    assert load_grades(archived), "the grades beside the transcripts must still be found"


def test_a_run_window_uses_the_latency_the_transcript_kept(archived: Path) -> None:
    """The span is what carries meaning; the anchor is supplied because the record has no clock."""
    anchor = datetime(2026, 8, 12, tzinfo=UTC)
    client = _Client()
    publish_run(archived, experiment_name="arm", client=client, probe_dir=_PROBE_DIR, now=anchor)

    for run in client.experiments.runs:
        assert run["start_time"] == anchor
        assert (run["end_time"] - run["start_time"]).total_seconds() == 1.5
