"""User interactions as a memory source.

A chemist's confirmed or corrected answer becomes an `interaction` note through the ordinary write
path (`kg/record.py`), citing the source notes the answer drew on.
"""

from chemclaw.kg.note import Note
from chemclaw.kg.record import NoteWriter, record_note


def note_from_confirmed_answer(
    interaction_id: str,
    question: str,
    answer: str,
    evidence_note_ids: list[str] | None = None,
    corrected_from: str = "",
) -> Note:
    """Build an agent `interaction` note capturing a confirmed or corrected user answer.

    `evidence_note_ids` are cited as wikilinks. `corrected_from` is what the system had said: empty
    means confirmed, non-empty means corrected, and the superseded answer is kept so a reader can
    see what was wrong.
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


async def record_confirmed_answer_note(
    interaction_id: str,
    question: str,
    answer: str,
    evidence_note_ids: list[str] | None,
    writer: NoteWriter,
    corrected_from: str = "",
) -> str:
    """Build the confirmed-answer note and write it into the graph.

    Called from `chemclaw.agent.memory_tools.record_confirmed_answer`; `writer` is injected so tests
    fake the commit.

    Returns:
        The writer's reference for what landed.
    """
    note = note_from_confirmed_answer(
        interaction_id, question, answer, evidence_note_ids, corrected_from
    )
    return await record_note(note, writer)
