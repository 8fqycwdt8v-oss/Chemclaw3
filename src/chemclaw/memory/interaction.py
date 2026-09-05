"""User interactions as a memory source (plan step 5.5).

A confirmed or corrected answer from a chemist is evidence too. It becomes an episodic
`interaction` note through the **same** PR-gate as every other agent note (same type family,
same gate — no special path), so a validated Q&A re-enters the knowledge base and informs
later retrieval. Any source notes the answer drew on are cited as `[[...]]` back-references.
"""

from chemclaw.kg.note import Note
from chemclaw.kg.pr_gate import propose_note
from chemclaw.kg.submission import NoteSubmitter


def note_from_confirmed_answer(
    interaction_id: str,
    question: str,
    answer: str,
    evidence_note_ids: list[str] | None = None,
    corrected_from: str = "",
) -> Note:
    """Build an agent `interaction` note capturing a confirmed **or corrected** user answer.

    `evidence_note_ids` are the notes the answer relied on (cited as wikilinks); an answer
    with no cited source simply carries none. It is `created_by: agent` because it is still a
    proposal the PR-gate has a human confirm before it becomes trusted knowledge (D-005).

    **A correction used to be stored as a confirmation, which threw away the part worth keeping.**
    The body was rendered `A (confirmed):` unconditionally, while this module's own docstring, the
    tool's docstring and the system prompt all said "confirmed **or corrected**". So the one case
    where the system was demonstrably wrong — the highest-value thing a chemist ever hands it, and
    the only place that fact exists — was written into the record as agreement.

    `corrected_from` is what the system had said. Empty means the chemist confirmed; non-empty
    means they corrected, and carries the superseded answer so a later reader can see *what* was
    wrong rather than only that something was. One field rather than a separate flag, because a
    correction with no account of what it replaced is the same loss one step smaller.
    """
    citations = "".join(f"- [[{note_id}]]\n" for note_id in (evidence_note_ids or []))
    evidence = f"\nEvidence:\n{citations}" if citations else ""
    if corrected_from.strip():
        body = (
            f"Q: {question}\n\nA (corrected by a chemist): {answer}\n\n"
            f"The system had answered: {corrected_from}\n{evidence}"
        )
    else:
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
    corrected_from: str = "",
) -> str:
    """Build the confirmed-answer note and propose it through the PR-gate.

    The single write path for a captured user answer, reached from the agent tool
    (`chemclaw.agent.memory_tools.record_confirmed_answer`). It stays in `memory/` rather than
    inside that tool because building the note out of an interaction is this layer's job and the
    tool's job is the surface; `submitter` is injected so tests fake the PR.

    It used to have a second caller, the durable async-approval workflow, and
    `D-2026-08-27-a-hold-nothing-can-open-is-not-a-hold` deleted it — nothing had ever been able to
    start one. The human decision this note needs is the pull request, which is unchanged.

    Returns:
        The submitter's reference for the opened PR.
    """
    note = note_from_confirmed_answer(
        interaction_id, question, answer, evidence_note_ids, corrected_from
    )
    return await propose_note(note, submitter)
