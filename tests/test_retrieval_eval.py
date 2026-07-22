"""Retrieval eval driver (plan F10-F1): score a live retriever's P/R/F1 over query→expected cases.

Uses the real `GraphRetriever` over a fixture corpus so the scoring is deterministic and offline:
the retriever's substring hits become the predicted set, which the classification metrics score
against the case's expected ids — proving a retriever gets a measurable P/R/F1, not an anecdote.
"""

import asyncio
from pathlib import Path

from evals.retrieval import RetrievalCase, run_retrieval_eval, score_retrieval_case
from report.retrievers import GraphRetriever


def _write_note(directory: Path, note_id: str, body: str) -> None:
    (directory / f"{note_id}.md").write_text(
        f"---\nid: {note_id}\ntype: reaction\n---\n{body}\n", encoding="utf-8"
    )


def test_score_retrieval_case_scores_the_graph_retriever(tmp_path: Path) -> None:
    """A query the retriever answers partially yields the matching predicted-vs-expected case."""
    _write_note(tmp_path, "reaction-amide-1", "amide coupling with HATU")
    _write_note(tmp_path, "reaction-amide-2", "amide coupling with EDC")
    _write_note(tmp_path, "reaction-distill", "distillation reflux study")
    retriever = GraphRetriever(notes_dir=str(tmp_path))
    case = RetrievalCase(
        id="amide",
        query="amide coupling",
        expected_note_ids=["reaction-amide-1", "reaction-amide-2"],
    )
    eval_case = asyncio.run(score_retrieval_case(case, retriever))
    predicted = set(eval_case.output["predicted_note_ids"])
    assert predicted == {"reaction-amide-1", "reaction-amide-2"}  # distill note not matched
    assert eval_case.reference == {"expected_note_ids": ["reaction-amide-1", "reaction-amide-2"]}


def test_run_retrieval_eval_reports_perfect_recall(tmp_path: Path) -> None:
    """Scoring the retriever over the case-set reports the P/R/F1 (here: perfect on this query)."""
    _write_note(tmp_path, "reaction-amide-1", "amide coupling with HATU")
    _write_note(tmp_path, "reaction-amide-2", "amide coupling with EDC")
    _write_note(tmp_path, "reaction-distill", "distillation reflux study")
    retriever = GraphRetriever(notes_dir=str(tmp_path))
    cases = [
        RetrievalCase(
            id="amide",
            query="amide coupling",
            expected_note_ids=["reaction-amide-1", "reaction-amide-2"],
        )
    ]
    report = asyncio.run(run_retrieval_eval(cases, retriever, "test"))
    scores = {r.result_metric: r.value for r in report.results}
    assert scores["precision"] == 1.0 and scores["recall"] == 1.0 and scores["f1"] == 1.0
