"""A widened retrieval hit says which terms of the question it matched.

`GraphRetriever` widens to any-term matches when no note matches every term, which is the normal
path, so an absent answer returns full-looking chunks. No count discriminates present from absent
answers; which terms matched does, and that is the model's judgment. So each chunk carries its
matched terms. `restated_as_position` overwrites `score` in both merge modes, so the signal must
survive it, which the last test asserts.
"""

import asyncio
from pathlib import Path

from chemclaw.kg.graph import invalidate_cache
from chemclaw.kg.note import Note
from chemclaw.kg.render import render_note
from chemclaw.retrieval.evidence import EvidenceChunk
from chemclaw.retrieval.hybrid import restated_as_position
from chemclaw.retrieval.retrievers import GraphRetriever


def _corpus(directory: Path, *notes: Note) -> str:
    """Write notes into a fresh knowledge tree and return its root path."""
    root = directory / "knowledge"
    for note in notes:
        path = root / note.type / f"{note.id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_note(note), encoding="utf-8")
    invalidate_cache()
    return str(root)


def test_a_widened_hit_says_which_of_the_question_it_actually_matched(tmp_path: Path) -> None:
    """A widened hit says which of the question it actually matched.

    The corpus holds nothing about ibuprofen; a melting-point note for another compound matches only
    `melting` and `point`, and the field is what tells it apart from a full match.
    """
    root = _corpus(
        tmp_path,
        Note(
            id="compound-acetylsalicylic-acid",
            type="compound",
            body="Acetylsalicylic acid melts at 135 C; the melting point is the purity check.",
        ),
    )

    (chunk,) = asyncio.run(
        GraphRetriever(root).retrieve("what is the melting point of ibuprofen", {})
    )

    assert chunk.matched_terms is not None
    assert set(chunk.matched_terms) == {"melting", "point"}
    assert "ibuprofen" not in chunk.matched_terms


def test_a_note_that_matched_the_whole_question_says_so_in_the_same_field(tmp_path: Path) -> None:
    """The control, and the half that makes the field readable rather than alarming.

    A field that only ever reported partial matches would be a warning label on every chunk. This
    is the same corpus answering a question it does hold, and the terms it names are the question.
    """
    root = _corpus(
        tmp_path,
        Note(
            id="compound-acetylsalicylic-acid",
            type="compound",
            body="Acetylsalicylic acid melts at 135 C; the melting point is the purity check.",
        ),
    )

    (chunk,) = asyncio.run(
        GraphRetriever(root).retrieve("what is the melting point of acetylsalicylic acid", {})
    )

    assert chunk.matched_terms is not None
    assert {"melting", "point", "acetylsalicylic", "acid"} <= set(chunk.matched_terms)


def test_a_source_that_cannot_report_term_matching_says_unknown_rather_than_none_matched(
    tmp_path: Path,
) -> None:
    """A source that cannot report term matching says unknown (`None`), not none matched (`[]`).

    Legs built from raw document text never tokenise the query, so `[]` would be an unchecked claim.
    A note-backed chunk's `[]` is real: the dense leg can surface a note sharing no word with the
    question.
    """
    assert (
        EvidenceChunk(content="a document", source_note_id="doc-1", retriever="share").matched_terms
        is None
    )


def test_match_quality_survives_the_rewrite_that_destroyed_the_last_one() -> None:
    """Match quality survives `restated_as_position`.

    It rebuilds every chunk with `model_copy(update={"score": ...})`; this fails if someone adds the
    field to that update.
    """
    chunks = [
        EvidenceChunk(
            content="a note",
            source_note_id=f"n-{index}",
            retriever="graph",
            score=0.9,
            matched_terms=["biaryl"],
        )
        for index in range(3)
    ]

    restated = restated_as_position(chunks)

    assert [chunk.score for chunk in restated] == [1.0, 0.5, 0.3333]
    assert all(chunk.matched_terms == ["biaryl"] for chunk in restated)
