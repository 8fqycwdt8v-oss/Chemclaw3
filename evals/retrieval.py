"""Score a retriever's precision/recall/F1 over query→expected-notes cases (plan F10-F1).

The base eval harness scores *static* output/reference payloads; a retrieval case instead pins a
query and the note ids that *should* come back, and the retriever's live output is what gets scored.
This module bridges the two: it runs a `SourceRetriever` over each case's query, turns the returned
note ids into an `EvalCase` (predicted vs expected), and hands them to the existing `run_eval` — the
report/threshold machinery is reused unchanged (no second eval system, D-A14). It is how F10-A's
hybrid retrieval gets a measurable P/R/F1 rather than an anecdote.
"""

from pydantic import BaseModel, ConfigDict, Field

from evals.harness import EvalReport, run_eval
from evals.metric import EvalCase
from report.evidence import SourceRetriever

# The classification metrics every retrieval case is scored by (F10-F1). Fixed here, not per-case:
# a retrieval case's job is to pin query→expected, and precision/recall/F1 are the retrieval-quality
# triple — a case does not get to omit one.
_RETRIEVAL_METRICS = ["precision", "recall", "f1"]


class RetrievalCase(BaseModel):
    """One retrieval eval case: a query and the note ids it should surface.

    Extra keys are rejected so a misspelled `expected_notes` (vs `expected_note_ids`) fails loudly
    instead of scoring against an empty ground truth (G4).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    expected_note_ids: list[str] = Field(min_length=1)
    filters: dict[str, str] = Field(default_factory=dict)


async def score_retrieval_case(case: RetrievalCase, retriever: SourceRetriever) -> EvalCase:
    """Run `retriever` over the case's query and build the predicted-vs-expected `EvalCase`.

    Predicted ids are the distinct source-note ids the retriever returned, order-deduplicated so a
    note cited by several chunks counts once. The result is a plain `EvalCase` the base harness
    scores with the classification metrics — retrieval-specific logic ends here.
    """
    chunks = await retriever.retrieve(case.query, dict(case.filters))
    predicted = list(dict.fromkeys(chunk.source_note_id for chunk in chunks))
    return EvalCase(
        id=case.id,
        metrics=list(_RETRIEVAL_METRICS),
        output={"predicted_note_ids": predicted},
        reference={"expected_note_ids": case.expected_note_ids},
    )


async def run_retrieval_eval(
    cases: list[RetrievalCase], retriever: SourceRetriever, case_set_version: str
) -> EvalReport:
    """Score every retrieval case against `retriever` into a versioned report (via `run_eval`)."""
    scored = [await score_retrieval_case(case, retriever) for case in cases]
    return run_eval(scored, case_set_version)
