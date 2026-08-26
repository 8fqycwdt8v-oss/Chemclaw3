"""Retrieval carries provenance, so a claim can be qualified by who authored its evidence (D-160).

`NoteRef` has exposed `created_by`, `source` and `confidence` to `find_notes`/`expand_note` since
KM-6. `gather_evidence` — the sweep that gathers most of the evidence an answer is actually built
on — carried none of them. Confidence *reached* the chunk, as `score`, which orders truncation:
being ranked lower is not the same as being told a note is uncertain, and the model was never told.

While everything readable is human-merged that is harmless. It stops being harmless the moment a
second, ungated tier exists, which is why this lands before that one and on its own.
"""

import asyncio
from pathlib import Path

from chemclaw.core.config import settings
from chemclaw.kg.note import Note
from chemclaw.retrieval.evidence import EvidenceChunk
from chemclaw.retrieval.retrievers import GraphRetriever
from chemclaw.retrieval.vector_index import InMemoryNoteIndex, reindex_notes


def _write(directory: Path, note: Note) -> None:
    """Persist a note as the frontmatter+body file the loader reads."""
    lines = [
        "---",
        f"id: {note.id}",
        f"type: {note.type}",
        f"created_by: {note.created_by}",
    ]
    if note.source is not None:
        lines.append(f"source: {note.source}")
    if note.confidence is not None:
        lines.append(f"confidence: {note.confidence}")
    lines += ["---", "", note.body]
    (directory / f"{note.id}.md").write_text("\n".join(lines), encoding="utf-8")


def test_a_graph_chunk_carries_who_wrote_it_and_how_sure_they_were(tmp_path: Path) -> None:
    """The three fields the answer contract now reasons over, on the default retrieval path."""

    async def _run() -> None:
        _write(
            tmp_path,
            Note(
                id="playbook-pd",
                type="playbook",
                created_by="agent",
                source="distilled from 6 campaigns",
                confidence=0.4,
                body="Pd(OAc)2 with SPhos tends to hold at low loading.",
            ),
        )
        chunks = await GraphRetriever(str(tmp_path)).retrieve("SPhos", {})
        assert len(chunks) == 1
        assert chunks[0].created_by == "agent"
        assert chunks[0].source == "distilled from 6 campaigns"
        assert chunks[0].confidence == 0.4

    asyncio.run(_run())


def test_a_human_note_says_human(tmp_path: Path) -> None:
    """The distinction only means something if both sides of it are actually reported."""

    async def _run() -> None:
        _write(
            tmp_path,
            Note(id="reaction-1", type="reaction", created_by="human", body="Ran SPhos at 2 mol%."),
        )
        chunks = await GraphRetriever(str(tmp_path)).retrieve("SPhos", {})
        assert chunks[0].created_by == "human"

    asyncio.run(_run())


def test_provenance_is_not_a_filter(tmp_path: Path) -> None:
    """An agent-authored, low-confidence note is *returned* and qualified, never suppressed.

    Retrieval has no basis for deciding a merged note should not be seen — a human signed it off.
    Dropping it would be the same mistake `conflicts_with` was written to avoid: silently deciding
    on the reader's behalf which of two curated notes counts.
    """

    async def _run() -> None:
        _write(
            tmp_path, Note(id="a", type="playbook", created_by="agent", confidence=0.1, body="X")
        )
        _write(
            tmp_path, Note(id="b", type="playbook", created_by="human", confidence=1.0, body="X")
        )
        chunks = await GraphRetriever(str(tmp_path)).retrieve("X", {})
        assert {c.source_note_id for c in chunks} == {"a", "b"}
        # ...but the trusted one is ranked first, so it survives truncation.
        assert chunks[0].source_note_id == "b"

    asyncio.run(_run())


def test_the_dense_index_path_carries_the_same_provenance(tmp_path: Path) -> None:
    """One builder feeds both paths, because a partially-provenanced list is the worst state.

    Two retrievers fuse into one evidence list. If the graph path reported authorship and the
    index path did not, an agent-authored note would be qualified or not depending on which
    retriever happened to surface it — indistinguishable, from the model's side, from a note that
    genuinely had no author.
    """
    from chemclaw.retrieval.retrievers import VectorRetriever

    async def _run() -> None:
        _write(
            tmp_path,
            Note(
                id="playbook-pd",
                type="playbook",
                created_by="agent",
                confidence=0.3,
                body="Pd(OAc)2 with SPhos holds at low loading.",
            ),
        )
        index = InMemoryNoteIndex()
        await reindex_notes(index, notes_dir=str(tmp_path))
        retriever = VectorRetriever(index, notes_dir=str(tmp_path))

        chunks = await retriever.retrieve("SPhos at low palladium loading", {})
        assert [c.created_by for c in chunks] == ["agent"]
        assert [c.confidence for c in chunks] == [0.3]

    asyncio.run(_run())


def test_unestablished_authorship_is_empty_not_human() -> None:
    """The default must not assert provenance nobody checked.

    A structural hit is generated from the fingerprint index — its content is a Tanimoto score,
    not a sentence anyone wrote — so there is no author to report. Defaulting to "human" would put
    a false claim in the one field whose entire purpose is to be trusted.
    """
    bare = EvidenceChunk(
        content="Similar reaction (Tanimoto 0.82)", source_note_id="r", retriever="x"
    )
    assert bare.created_by == ""
    assert bare.confidence is None


def test_an_excerpt_windows_on_the_matched_term_rather_than_the_head_of_the_body(
    tmp_path: Path,
) -> None:
    """A reviewer must be able to see what a cited note was retrieved *for*.

    A note matches on its whole searchable text — id, type, SMILES, tags and body — while the chunk
    carried the first `note_excerpt_chars` of the body and nothing windowed it on the match.
    Measured over the committed corpus for the query `yield`: of 38 notes, 32 have bodies longer
    than 240 characters, and `16 chunks, 6 whose 240-char excerpt does NOT contain the matched
    term`. For the conversational tool that is recoverable with `expand_note`; for `report_note` it
    is the final artifact a chemist signs at the PR-gate, where a bullet of frontmatter and a
    citation says nothing about why the note is there.

    `campaign` and `optimization-campaign` are the worst case by construction — their yields,
    purities and outcomes are in a table at the *end* of the body — which is the same failure
    `core/config/retrieval.py` already articulates for `protocol_digest_max_chars`.
    """

    async def _run() -> None:
        preamble = "Acetylation of salicylic acid with acetic anhydride. " * 8
        _write(
            tmp_path,
            Note(
                id="rxn-aspirin-acetylation",
                type="reaction",
                created_by="human",
                body=f"{preamble}\n\nThe isolated yield was 87 percent after recrystallisation.",
            ),
        )
        (chunk,) = await GraphRetriever(str(tmp_path)).retrieve("yield", {})

        assert "yield" in chunk.content, (
            f"the cited excerpt does not contain the term that matched: {chunk.content!r}"
        )
        assert len(chunk.content) <= settings.note_excerpt_chars

    asyncio.run(_run())


def test_an_excerpt_with_no_body_match_still_starts_at_the_beginning(tmp_path: Path) -> None:
    """The control: a note matched on its id, type, tags or SMILES has no body offset to centre on.

    Windowing on nothing would be windowing on the first character anyway, so the head is the
    honest fallback rather than a special case — and it is what every excerpt was before.
    """

    async def _run() -> None:
        body = "Charge the vessel, hold at 80 degrees, then work up into ethyl acetate. " * 6
        _write(tmp_path, Note(id="rxn-esterification", type="reaction", body=body))
        (chunk,) = await GraphRetriever(str(tmp_path)).retrieve("esterification", {})

        assert chunk.content == body.strip()[: settings.note_excerpt_chars]

    asyncio.run(_run())
