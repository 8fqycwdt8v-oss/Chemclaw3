"""Pin the retrieval gold-set metrics against the fixed corpus (audit KM-13).

These are regression pins, not mocks: each expected recall/precision is computed from the real
`GraphRetriever` over the versioned `data/evals/retrieval_corpus/` fixture. If a change to the
substring filter or the evidence path moves what a query surfaces, one of these numbers moves and
the test
fails — which is the whole point of the KM-13 gate. The gold cases and their expected-source lists
live in `data/evals/cases/retrieval-*.md`; this file loads those exact cases and scores them.
"""

import asyncio
import shutil
from pathlib import Path
from typing import Any

import pytest

import chemclaw.evals  # noqa: F401 — registers the retrieval metrics on import
from chemclaw.core.config import NOTE_INDEX_SOURCES, EvalSettings, settings
from chemclaw.evals.harness import load_eval_cases, run_eval
from chemclaw.evals.metric import EvalCase, MetricError, get_metric, registered_names
from chemclaw.evals.retrieval import retrieval_recall
from chemclaw.retrieval.evidence import EvidenceChunk
from chemclaw.retrieval.retrievers import GraphRetriever

_REPO = Path(__file__).resolve().parent.parent
# Derived from the setting's own default rather than spelled out, so moving the corpus (D-156 put it
# under `data/`) cannot leave this pointing at nothing. It did exactly that once: the stale literal
# made every gold case score `0/2 expected sources retrieved`, which reads as a retrieval regression
# rather than as a missing directory. `_corpus` below asserts the directory exists for the same
# reason — an empty corpus and a wrong path produce identical numbers.
_CORPUS = _REPO / EvalSettings.model_fields["eval_retrieval_corpus_dir"].default

# (case id, expected recall, expected precision, gate pass). Pinned from the fixture corpus.
_EXPECTED = {
    "retrieval-suzuki": (1.0, 3 / 8, True),
    "retrieval-coupling": (1.0, 1 / 2, True),
    # The literal-miss case: "cross-coupling" reaches the playbook but not the Suzuki reaction.
    "retrieval-cross-coupling-literal-miss": (0.5, 1 / 3, False),
    "retrieval-reflux-conditions": (1.0, 1 / 2, True),
    "retrieval-coupling-playbook-filter": (1.0, 1 / 3, True),
    "retrieval-organozinc-tag-filter": (1.0, 1 / 2, True),
    "retrieval-zinc-negishi-last-id": (1.0, 1 / 4, True),
    "retrieval-palladium-degassing": (1.0, 4 / 5, True),
    "retrieval-protodeboronation": (1.0, 2 / 3, True),
    "retrieval-boronic-acid": (1.0, 1 / 2, True),
}


@pytest.fixture
def _corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the retrieval metrics at the repo's gold corpus regardless of the test cwd."""
    assert _CORPUS.is_dir(), (
        f"the gold retrieval corpus is not at {_CORPUS}; every case below would score zero recall "
        "and read as a retrieval regression"
    )
    monkeypatch.setattr(settings, "eval_retrieval_corpus_dir", str(_CORPUS))
    # Pin the gate floor so the expected pass/fail column does not drift with a config edit.
    monkeypatch.setattr(settings, "retrieval_recall_min", 0.75)


def test_retrieval_metrics_are_registered() -> None:
    """Both KM-13 metrics are on the registry (the extension seam, plan 2b.5)."""
    assert {"retrieval_recall", "retrieval_precision"} <= set(registered_names())


@pytest.mark.parametrize("case_id", sorted(_EXPECTED))
def test_gold_case_recall_precision(case_id: str, _corpus: None) -> None:
    """Each gold query scores the pinned recall/precision and gate verdict over the fixture."""
    cases = {c.id: c for c in load_eval_cases(settings.eval_case_dir)}
    assert case_id in cases, f"gold case {case_id} missing from {settings.eval_case_dir}"
    case = cases[case_id]
    exp_recall, exp_precision, exp_pass = _EXPECTED[case_id]

    recall = get_metric("retrieval_recall")(case)
    precision = get_metric("retrieval_precision")(case)

    assert recall.value == pytest.approx(exp_recall), recall.provenance
    assert recall.passed is exp_pass
    assert precision.value == pytest.approx(exp_precision), precision.provenance
    assert precision.passed is None  # precision is a diagnostic, never gated


def test_memo_shares_one_retrieval_and_observes_corpus_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Recall + precision on one case run live retrieval once — until the corpus changes on disk.

    The memo lives for the process, and the scheduled drift worker is a long-lived process:
    an on-disk corpus edit must be a natural memo miss (fresh retrieval, fresh ids), never a
    stale hit served for the pod's lifetime. No manual `.clear()` — the invalidation is the
    behavior under test.
    """
    corpus = tmp_path / "corpus"
    shutil.copytree(_CORPUS, corpus)
    monkeypatch.setattr(settings, "eval_retrieval_corpus_dir", str(corpus))
    # The memo's invalidation keys off the note tree's stat fingerprint, so this asserts
    # disk-authoritative reads; the TTL window (DA-5) deliberately skips that scan. Pinned to 0 so
    # an on-disk edit is observed immediately, which is the behavior under test.
    monkeypatch.setattr(settings, "graph_cache_ttl_seconds", 0.0)
    monkeypatch.setattr(settings, "retrieval_recall_min", 0.75)
    calls: list[str] = []
    real_retrieve = GraphRetriever.retrieve

    async def counting(
        self: GraphRetriever, query: str, filters: dict[str, Any]
    ) -> list[EvidenceChunk]:
        calls.append(query)
        return await real_retrieve(self, query, filters)

    monkeypatch.setattr(GraphRetriever, "retrieve", counting)
    case = {c.id: c for c in load_eval_cases(settings.eval_case_dir)}["retrieval-suzuki"]

    recall = get_metric("retrieval_recall")(case)
    precision = get_metric("retrieval_precision")(case)

    assert calls == [case.output["query"]]  # one sweep, both metrics scored from it
    assert recall.value == pytest.approx(1.0)
    # 3 of the 8 returned notes are gold. On the six-note fixture this was 1.0, because the cut
    # could not engage and every note in the corpus was returned; on a corpus larger than
    # `retrieval_top_k` a precision below 1 is the ordinary case rather than a regression.
    assert precision.value == pytest.approx(_EXPECTED["retrieval-suzuki"][1])

    # The corpus changes on disk: one of the two expected notes disappears. The memo must
    # miss (a second live retrieval) and the metric must reflect the current corpus.
    # `<type>/<id>.md` — the layout the PR-gate actually files notes under. The corpus used to sit
    # flat, which `validate_kg` reported as six layout problems on a directory whose own README
    # called every file valid.
    (corpus / "reaction" / "reaction-suzuki-biaryl.md").unlink()
    stale_free = get_metric("retrieval_recall")(case)
    assert calls == [case.output["query"]] * 2  # a fresh retrieval, not a stale hit
    assert stale_free.value == pytest.approx(2 / 3)  # 2 of 3 expected sources remain


def test_run_eval_scores_the_full_gold_set(_corpus: None) -> None:
    """The harness runs every gold case; exactly the known literal-miss case fails its gate."""
    retrieval_cases = [
        c for c in load_eval_cases(settings.eval_case_dir) if c.id.startswith("retrieval-")
    ]
    report = run_eval(retrieval_cases, "retrieval-gold-v1")
    failed_recall = {r.case_id for r in report.failed() if r.result_metric == "retrieval_recall"}
    assert failed_recall == {"retrieval-cross-coupling-literal-miss"}


def test_the_metric_refuses_rather_than_mislabel_a_different_retrieval_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switching to hybrid retrieval flips the product to a path this metric does not score.

    That is the entire point of F10-A, and until now the gate kept reporting a graph-only recall
    under the name `retrieval_recall` — a green number about a retriever nobody was running. A
    figure that looks like coverage it does not have is worse than a missing figure, so the metric
    raises and `run_eval` names the case and metric that triggered it.
    """
    case = EvalCase(
        id="retrieval-x",
        metrics=["retrieval_recall"],
        output={"query": "suzuki"},
        reference={"expected_note_ids": ["reaction-suzuki-1"]},
    )

    monkeypatch.setattr(settings, "retrieval_mode", "hybrid")
    with pytest.raises(MetricError, match="retrieval_mode"):
        retrieval_recall(case)

    monkeypatch.setattr(settings, "retrieval_mode", "graph")
    monkeypatch.setattr(settings, "data_sources", "graph,vector")
    with pytest.raises(MetricError, match="active source"):
        retrieval_recall(case)


def test_metric_scores_correctly_from_inside_a_running_event_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A metric that drives live retrieval must not require its caller to be a plain sync frame.

    `_retrieved_ids` used to reach the retriever with a bare `asyncio.run`, which raises
    `RuntimeError: asyncio.run() cannot be called from a running event loop` the moment a metric is
    scored from a coroutine — exactly the shape `chemclaw.durable.eval_drift` (66% async) or any
    future async surface can take if it scores a case inline rather than wrapping `run_eval` in
    `asyncio.to_thread` (R7). Scoring here from inside `asyncio.run(...)` reproduces that shape
    directly and must still return the same, correct number.

    A fresh corpus copy (not the shared fixture corpus) guarantees a live retrieval actually runs
    inside the loop rather than serving a memo hit some other, already-run test left behind.
    """
    corpus = tmp_path / "corpus"
    shutil.copytree(_CORPUS, corpus)
    monkeypatch.setattr(settings, "eval_retrieval_corpus_dir", str(corpus))
    monkeypatch.setattr(settings, "retrieval_recall_min", 0.75)
    cases = {c.id: c for c in load_eval_cases(settings.eval_case_dir)}
    case = cases["retrieval-suzuki"]
    exp_recall, exp_precision, _ = _EXPECTED["retrieval-suzuki"]

    async def _score_inside_a_running_loop() -> tuple[float, float]:
        recall = get_metric("retrieval_recall")(case)
        precision = get_metric("retrieval_precision")(case)
        return recall.value, precision.value

    recall_value, precision_value = asyncio.run(_score_inside_a_running_loop())

    assert recall_value == pytest.approx(exp_recall)
    assert precision_value == pytest.approx(exp_precision)


def test_the_shipped_default_is_still_scored() -> None:
    """The refusal must not fire on the configuration the repository actually ships.

    `graph` mode with no derived-index source is exactly what this metric scores, so the guard has
    to be a divergence check and not a blanket disable.
    """
    assert settings.retrieval_mode == "graph"
    assert not NOTE_INDEX_SOURCES & set(settings.data_source_list)


def test_the_recall_floor_can_see_a_single_lost_gold_note() -> None:
    """The floor must sit strictly above `(n-1)/n` for every gated gold set.

    At the shipped 0.75, a case with **four** gold notes scored exactly 0.75 when one of them was
    lost — and passed. Four of the nine gated cases have four gold notes, so on nearly half the
    case set the floor was blind to the smallest regression that can occur.

    Asserted as the inequality rather than against the number, so that adding a case with a larger
    gold set fails here — loudly, naming the case — instead of quietly reopening the blind spot for
    that case alone.
    """
    # The **shipped** default, off the model field — not `settings.retrieval_recall_min`, which the
    # `_corpus` fixture pins to 0.75 so the other cases' gate outcomes stay stable regardless of it.
    # This test is about what a deployment actually gets, so a fixture's pin would make it assert
    # nothing.
    floor = EvalSettings.model_fields["retrieval_recall_min"].default
    blind: dict[str, float] = {}
    for case in load_eval_cases(settings.eval_case_dir):
        expected = list((case.reference or {}).get("expected_note_ids") or [])
        # Only the cases the gate actually enforces; the literal-miss case is `expect_pass: false`
        # by design and is meant to score below the floor.
        if len(expected) < 2 or not _EXPECTED.get(case.id, (0.0, 0.0, False))[2]:
            continue
        one_lost = (len(expected) - 1) / len(expected)
        if one_lost >= floor:
            blind[case.id] = one_lost
    assert not blind, (
        f"the shipped floor {floor} passes these cases with one gold note lost: "
        f"{blind}. Raise `retrieval_recall_min` above the largest of them."
    )
