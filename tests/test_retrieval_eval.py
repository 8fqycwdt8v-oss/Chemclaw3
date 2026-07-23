"""Pin the retrieval gold-set metrics against the fixed corpus (audit KM-13).

These are regression pins, not mocks: each expected recall/precision is computed from the real
`GraphRetriever` over the versioned `evals/retrieval_corpus/` fixture. If a change to the substring
filter or the evidence path moves what a query surfaces, one of these numbers moves and the test
fails — which is the whole point of the KM-13 gate. The gold cases and their expected-source lists
live in `evals/cases/retrieval-*.md`; this file loads those exact cases and scores them.
"""

from pathlib import Path
from typing import Any

import pytest

import evals  # noqa: F401 — registers the retrieval metrics on import
import evals.retrieval
from chemclaw.config import settings
from evals.harness import load_eval_cases, run_eval
from evals.metric import get_metric, registered_names
from report.evidence import EvidenceChunk
from report.retrievers import GraphRetriever

_REPO = Path(__file__).resolve().parent.parent
_CORPUS = _REPO / "evals" / "retrieval_corpus"

# (case id, expected recall, expected precision, gate pass). Pinned from the fixture corpus.
_EXPECTED = {
    "retrieval-suzuki": (1.0, 1.0, True),
    "retrieval-coupling": (1.0, 1.0, True),
    # The literal-miss case: "cross-coupling" reaches the playbook but not the Suzuki reaction.
    "retrieval-cross-coupling-literal-miss": (0.5, 1.0, False),
    "retrieval-reflux-conditions": (1.0, 1.0, True),
    "retrieval-coupling-playbook-filter": (1.0, 1.0, True),
}


@pytest.fixture
def _corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the retrieval metrics at the repo's gold corpus regardless of the test cwd."""
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


def test_both_metrics_share_one_retrieval(monkeypatch: pytest.MonkeyPatch, _corpus: None) -> None:
    """Recall + precision on one case run live retrieval once (memoized), not once each."""
    calls: list[str] = []
    real_retrieve = GraphRetriever.retrieve

    async def counting(
        self: GraphRetriever, query: str, filters: dict[str, Any]
    ) -> list[EvidenceChunk]:
        calls.append(query)
        return await real_retrieve(self, query, filters)

    monkeypatch.setattr(GraphRetriever, "retrieve", counting)
    evals.retrieval._RETRIEVAL_MEMO.clear()
    case = {c.id: c for c in load_eval_cases(settings.eval_case_dir)}["retrieval-suzuki"]

    recall = get_metric("retrieval_recall")(case)
    precision = get_metric("retrieval_precision")(case)

    assert calls == [case.output["query"]]  # one sweep, both metrics scored from it
    assert recall.value == pytest.approx(1.0)
    assert precision.value == pytest.approx(1.0)


def test_run_eval_scores_the_full_gold_set(_corpus: None) -> None:
    """The harness runs every gold case; exactly the known literal-miss case fails its gate."""
    retrieval_cases = [
        c for c in load_eval_cases(settings.eval_case_dir) if c.id.startswith("retrieval-")
    ]
    report = run_eval(retrieval_cases, "retrieval-gold-v1")
    failed_recall = {r.case_id for r in report.failed() if r.result_metric == "retrieval_recall"}
    assert failed_recall == {"retrieval-cross-coupling-literal-miss"}
