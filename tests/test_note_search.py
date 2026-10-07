"""One searchable text for a note (D-2026-08-05).

`find_notes`, `GraphRetriever`, the dense embedding, the lexical index and the digest all read
`note_text`, so a note the model finds with `find_notes` is one `gather_evidence` can cite.
"""

import asyncio
from pathlib import Path

import pytest

from chemclaw.agent.graph_tools import find_notes
from chemclaw.agent.subscriptions import Subscription
from chemclaw.core.config import settings
from chemclaw.durable.digest import _matches
from chemclaw.kg.graph import invalidate_cache, load_notes
from chemclaw.kg.note import Note
from chemclaw.kg.render import render_note
from chemclaw.kg.search import query_terms, search_text, term_coverage
from chemclaw.retrieval.retrievers import GraphRetriever
from chemclaw.retrieval.vector_index import NoteRecord, PostgresNoteIndex
from tests.pg import migrated_db_or_skip

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
    """A note found by its SMILES through `find_notes` is also returned by `GraphRetriever`."""
    root = _write(
        tmp_path,
        Note(id="compound-ethanol", type="compound", compound_smiles="CCO", body="a solvent"),
    )
    monkeypatch.setattr("chemclaw.core.config.settings.note_repo_dir", str(tmp_path))

    found = asyncio.run(find_notes("CCO")).matches
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

    found = asyncio.run(find_notes("reaction")).matches
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


def test_a_question_s_own_grammar_is_not_a_term_the_record_must_contain() -> None:
    """A question's function words are not terms a record must contain.

    Under the all-terms rule each one removes real hits, and once search widens it adds every note
    containing it — substring matching makes that severe (`so` is inside `solvent`, `he` inside
    `ether`).
    """
    terms = query_terms("Has anyone here run that before, and what conditions did they end up on?")

    assert "conditions" in terms
    assert "anyone" in terms  # not a function word; the list is closed-class only
    for framing in ("has", "here", "that", "what", "did", "they", "up"):
        assert framing not in terms, framing


def test_a_stopword_list_only_grows_by_words_a_note_cannot_be_about() -> None:
    """`_STOPWORDS` holds only closed-class English function words.

    Each entry is a word a query can no longer require, so open-class verbs (`give`, `use`, `need`)
    stay out.
    """
    from chemclaw.kg.search import _STOPWORDS

    for open_class in ("give", "given", "use", "used", "using", "get", "got", "need", "yield"):
        assert open_class not in _STOPWORDS, open_class


def test_the_two_lexical_rules_over_one_corpus_are_not_one_rule() -> None:
    """Neither lexical leg subsumes the other, so neither can be deleted.

    `GraphRetriever` matches substrings in process; `LexicalRetriever` uses Postgres `ts_rank`,
    which stems and stop-words by a text-search configuration. Over the two notes below each finds
    words the other cannot. The graph leg is also the only one that answers where the derived index
    is not built.
    """
    asyncio.run(migrated_db_or_skip())
    corpus = {
        "n-coupling": "The Suzuki coupling was run in toluene.",
        "n-polyester": "The polyester film was dried overnight.",
    }
    durable = PostgresNoteIndex()
    asyncio.run(
        durable.upsert(
            [
                NoteRecord(note_id=note_id, text=text, embedding=[0.0] * settings.embedding_dim)
                for note_id, text in corpus.items()
            ],
            "probe",
        )
    )
    scope = set(corpus)

    def stemmed(query: str) -> set[str]:
        hits = asyncio.run(durable.search_lexical(query, 50, within=scope))
        return {hit.note_id for hit in hits}

    def substrings(query: str) -> set[str]:
        terms = query_terms(query)
        return {
            note_id
            for note_id, text in corpus.items()
            if term_coverage(Note(id=note_id, type="reaction", body=text), terms) == len(terms)
        }

    # Inflections the server stems and a substring test cannot see at all.
    for inflected, note_id in (
        ("couplings", "n-coupling"),
        ("coupled", "n-coupling"),
        ("dry", "n-polyester"),
        ("films", "n-polyester"),
    ):
        assert stemmed(inflected) == {note_id}, inflected
        assert substrings(inflected) == set(), inflected

    # And the coarseness that goes the other way: `ester` inside `polyester` is a hit for the
    # substring rule and no lexeme at all for the server.
    assert substrings("ester") == {"n-polyester"}
    assert stemmed("ester") == set()
