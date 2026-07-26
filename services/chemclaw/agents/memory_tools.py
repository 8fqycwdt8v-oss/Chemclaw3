"""Agent tool for the interaction memory layer (plan step 5.5).

A confirmed or corrected answer from a chemist is evidence too. `record_confirmed_answer`
lets the agent capture such an exchange as an episodic `interaction` note and route it
through the **same** PR-gate as every other agent note (a human validates it before it
becomes trusted knowledge, D-005) — the fourth memory source, on the one shared write path.
"""

from agents.tool_registry import tool
from kg.git_submitter import default_submitter
from memory.interaction import propose_confirmed_answer


@tool
async def record_confirmed_answer(
    interaction_id: str,
    question: str,
    answer: str,
    evidence_note_ids: list[str] | None = None,
) -> str:
    """Record a user-confirmed/corrected answer as an `interaction` note via the PR-gate.

    Call this only after the chemist has explicitly confirmed or corrected an answer, so the
    exchange becomes reusable knowledge. It is authored as `agent`, so it lands on a feature
    branch for human sign-off, never straight into the graph.

    Args:
        interaction_id: Stable, unique id for this exchange (becomes note `interaction-<id>`).
        question: The question that was answered.
        answer: The confirmed/corrected answer to preserve.
        evidence_note_ids: Ids of the notes the answer drew on, cited as `[[wikilinks]]`.

    Returns:
        The submitted PR reference.
    """
    return await propose_confirmed_answer(
        interaction_id, question, answer, evidence_note_ids, default_submitter()
    )
