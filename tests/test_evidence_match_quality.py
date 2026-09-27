"""An absent answer used to be indistinguishable from a present one (W14 finding B).

`GraphRetriever` ranks `complete or scored`: when no note matches every term of the query it
widens to *any* term. Measured over the shipped corpus, complete matches in the top 8 were **0 of
8** on questions the corpus does answer — so widening is the normal path, not the fallback, and
the leg always fills `retrieval_top_k`. `gather_evidence("what is the melting point of
ibuprofen")` returns sixteen chunks about aspirin, DCM and route scoring, shape-identical to a
successful query, while `gather_evidence`'s own docstring tells the model that empty means
"nothing on file, never invented".

**Neither count discriminates, which is why this is not a classifier.** Measured on the 19
`knowledge.yaml` probes against three absent-answer questions: mean top-chunk term coverage is
0.372 where the answer is present and **0.400** where it is absent, and complete matches are 1/19
against 0/3. What separates them is *which* terms matched — `melting` and `point` did,
`ibuprofen` did not; `yield` did, `heck` and `tributylamine` did not — and that is a judgment the
model can make and this system cannot. So the chunk carries the terms it matched and the model
compares them against the question it asked.

`restated_as_position` overwrites `score` with the merged rank in both merge modes, which is what
destroyed the one number that used to carry match quality. Any signal added here has to survive
that, and the last test in this file is that assertion.
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
    """The measured defect: a chunk that matched two framing words looks like an answer.

    The corpus here holds nothing about ibuprofen. The melting-point note for a *different*
    compound matches `melting` and `point`, the widening rule serves it, and before this field the
    chunk was byte-identical in shape to one that had matched the whole question — same excerpt,
    same citation, same score.
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
    """`None` is not `[]`, for the reason `Hits.found` gives one class over.

    The share, warehouse, vendored-dataset and verifier legs build their chunks from raw document
    text and never tokenise a query. An empty list there would assert "this document matched none
    of your words", which is a claim nobody checked; the default says "this source did not report
    it". A note-backed chunk's empty list, by contrast, is a real statement — the dense leg can
    surface a note that shares no word with the question at all.
    """
    assert (
        EvidenceChunk(content="a document", source_note_id="doc-1", retriever="share").matched_terms
        is None
    )


def test_match_quality_survives_the_rewrite_that_destroyed_the_last_one() -> None:
    """`restated_as_position` is why `score` could not carry this, so it is asserted here.

    It rebuilds every chunk with `model_copy(update={"score": ...})` in *both* merge modes, so a
    field is safe only for as long as nobody adds it to that update dict. This is the assertion
    that fails if someone does.
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
