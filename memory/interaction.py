"""User interactions as a memory source (plan step 5.5).

A confirmed or corrected answer from a chemist is evidence too. It becomes an episodic
`interaction` note through the **same** PR-gate as every other agent note (same type family,
same gate — no special path), so a validated Q&A re-enters the knowledge base and informs
later retrieval. Any source notes the answer drew on are cited as `[[...]]` back-references.
"""

from kg.note import Note
from kg.pr_gate import NoteSubmitter, propose_note


def note_from_confirmed_answer(
    interaction_id: str, question: str, answer: str, evidence_note_ids: list[str] | None = None
) -> Note:
    """Build an agent `interaction` note capturing a confirmed/corrected user answer.

    `evidence_note_ids` are the notes the answer relied on (cited as wikilinks); an answer
    with no cited source simply carries none. It is `created_by: agent` because it is still a
    proposal the PR-gate has a human confirm before it becomes trusted knowledge (D-005).
    """
    citations = "".join(f"- [[{note_id}]]\n" for note_id in (evidence_note_ids or []))
    evidence = f"\nEvidence:\n{citations}" if citations else ""
    body = f"Q: {question}\n\nA (confirmed): {answer}\n{evidence}"
    return Note(
        id=f"interaction-{interaction_id}",
        type="interaction",
        created_by="agent",
        source="memory:user-interaction",
        body=body,
    )


async def propose_confirmed_answer(
    interaction_id: str,
    question: str,
    answer: str,
    evidence_note_ids: list[str] | None,
    submitter: NoteSubmitter,
) -> str:
    """Build the confirmed-answer note and propose it through the PR-gate.

    The single write path for a captured user answer, shared by the synchronous agent
    tool (`agents.memory_tools.record_confirmed_answer`) and the durable async-approval
    workflow (`workflows.interaction_approval`) — so both build the note and open the PR
    identically (DRY, two real callers). `submitter` is injected so tests fake the PR.

    Returns:
        The submitter's reference for the opened PR.
    """
    note = note_from_confirmed_answer(interaction_id, question, answer, evidence_note_ids)
    return await propose_note(note, submitter)
