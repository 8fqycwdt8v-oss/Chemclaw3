"""One searchable text for a note, and the drift that proved it was three (D-2026-08-05).

There were three haystacks: `find_notes` searched a note's id, type, SMILES, tags and body; the
`note_text` that fed `GraphRetriever`, the dense embedding and the lexical tsvector searched id,
tags and body; the digest built a third by untyped `getattr` and matched the query as one phrase.
All three carried a docstring claiming agreement with the others.

The consequence is what these tests pin, because it is the one that reaches a chemist: a note the
model finds with `find_notes` must be a note `gather_evidence` can then cite. When the two read
different text, the agent reports a note it cannot subsequently support — which is
indistinguishable, in the transcript, from the note not existing.
"""

import asyncio
from pathlib import Path

import pytest

from chemclaw.agent.graph_tools import find_notes
from chemclaw.agent.subscriptions import Subscription
from chemclaw.durable.digest import _matches
from chemclaw.kg.graph import invalidate_cache, load_notes
from chemclaw.kg.note import Note
from chemclaw.kg.render import render_note
from chemclaw.kg.search import query_terms, search_text, term_coverage
from chemclaw.retrieval.retrievers import GraphRetriever

_KNOWLEDGE = Path(__file__).resolve().parents[1] / "knowledge"


def _write(directory: Path, *notes: Note) -> Path:
    """Write notes into a fresh knowledge tree and return its root."""
    root = directory / "knowledge"
    for note in notes:
        path = root / note.type / f"{note.id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_note(note), encoding="utf-8")
    invalidate_cache()
    return root


def test_the_type_and_the_structure_are_part_of_a_note_s_text() -> None:
    """The union of what the three copies searched, not the intersection.

    A chemist searches for a SMILES and for a note type; the leg that could not see them was the
    retriever, which is the leg every report is built on.
    """
    note = Note(id="c-1", type="compound", compound_smiles="CCO", tags=["alcohol"], body="ethanol")
    text = search_text(note).lower()
    assert "c-1" in text
    assert "compound" in text
    assert "cco" in text
    assert "alcohol" in text
    assert "ethanol" in text


def test_a_blank_query_asks_for_nothing_and_gets_nothing() -> None:
    """`""` is a substring of every note, so an empty query must not tokenize to one term.

    The narrow case that a naive "fall back to the whole query" rule gets wrong: it would hand a
    caller who typed nothing the entire corpus, capped at fifty, as if it were a result.
    """
    assert query_terms("") == []
    assert query_terms("   ") == []
    # A query of only stopwords is a different case and still a search.
    assert query_terms("the") == ["the"]


def test_a_note_found_by_its_smiles_is_a_note_the_retriever_can_cite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measured defect, as a regression target.

    Before the haystacks were made one, `find_notes("CCO")` returned this note and
    `GraphRetriever.retrieve("CCO", {})` returned nothing at all — the SMILES was in the agent's
    haystack and not in the retriever's. The agent could name a note it could not then support.
    """
    root = _write(
        tmp_path,
        Note(id="compound-ethanol", type="compound", compound_smiles="CCO", body="a solvent"),
    )
    monkeypatch.setattr("chemclaw.core.config.settings.note_repo_dir", str(tmp_path))

    found = asyncio.run(find_notes("CCO"))
    chunks = asyncio.run(GraphRetriever(str(root)).retrieve("CCO", {}))

    assert [ref.id for ref in found] == ["compound-ethanol"]
    assert [chunk.source_note_id for chunk in chunks] == ["compound-ethanol"]


def test_a_note_found_by_its_type_is_a_note_the_retriever_can_cite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the same defect.

    Five notes in the shipped corpus matched the term `reaction` in the agent's haystack and in no
    other reader's, because the retriever's text carried no note type at all.
    """
    root = _write(tmp_path, Note(id="rxn-1", type="reaction", body="an amidation"))
    monkeypatch.setattr("chemclaw.core.config.settings.note_repo_dir", str(tmp_path))

    found = asyncio.run(find_notes("reaction"))
    chunks = asyncio.run(GraphRetriever(str(root)).retrieve("reaction", {}))

    assert [ref.id for ref in found] == ["rxn-1"]
    assert [chunk.source_note_id for chunk in chunks] == ["rxn-1"]


def test_the_digest_matches_what_find_notes_matches() -> None:
    """The third reader, whose docstring claimed the mirror while building its own haystack.

    Whole-phrase matching meant a subscription to "biaryl coupling" was never delivered unless a
    note contained that exact run of text, while the same words in `find_notes` found it.
    """
    note = Note(id="rxn-2", type="reaction", tags=["biaryl"], body="a Suzuki coupling step")
    subscription = Subscription(id=1, owner="chemist", query="biaryl coupling")
    terms = query_terms(subscription.query)

    assert _matches(note, subscription, terms)
    assert term_coverage(note, terms) == len(terms)


def test_every_note_in_the_shipped_corpus_is_findable_by_its_own_type() -> None:
    """The corpus-level statement of the same property, over real notes rather than a fixture.

    This is the assertion that would have failed before the change: the retriever's text held no
    note type at all, so `type` was a term only one of the four readers could ever match.
    """
    invalidate_cache()
    for note in load_notes(_KNOWLEDGE):
        assert term_coverage(note, [note.type]) == 1, note.id
