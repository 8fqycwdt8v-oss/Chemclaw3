"""The document index's two backends answer a lexical question the same way.

`PostgresDocumentIndex._lexical` must OR terms like `InMemoryDocumentIndex.search_lexical` (the
reference the share tests stand on); `websearch_to_tsquery` alone ANDs, which would silence the
share's lexical leg on ordinary multi-word questions and leave the RRF fusion one-legged.

The tests pin agreement, not numbers: `ts_rank` is not a coverage fraction, but the set of hits
and a complete match outranking a partial one must agree. Deliberately not closed: the reference
has no stop-word list, so "the and of" is a query to it and none to Postgres; matching would mean
a second text-search configuration. The server-backed half skips offline.
"""

import asyncio

import pytest

from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.core.embeddings import embed_texts, embedding_config_key
from chemclaw.ingest.documents.index import (
    ChunkRecord,
    DocumentFilter,
    DocumentIndex,
    FileRecord,
    InMemoryDocumentIndex,
    PostgresDocumentIndex,
)
from tests.pg import migrated_db_or_skip

_SOURCE = "share"
_CHUNKING = "chars-400"
# Built so the query's terms are split across documents, exactly as the note corpus is: `complete`
# holds all four, the `partial-*` pair holds two each, `unrelated` none. `solvent-guide` is the
# negation fixture — it carries the excluded term and nothing else the query asks for.
_CORPUS = {
    "doc-complete": "amide coupling solvent screen",
    "doc-partial-solvent": "solvent screen for the Suzuki step",
    "doc-partial-amide": "amide coupling with HATU",
    "doc-unrelated": "distillation reflux ratio study",
    "doc-solvent-guide": "solvent selection guide",
}
_QUERY = "amide coupling solvent screen"
_EXCLUDING_QUERY = "amide coupling -solvent"


async def _load(index: DocumentIndex) -> None:
    """Fill `index` with one single-chunk document per corpus entry, cited by its own path."""
    texts = list(_CORPUS.values())
    vectors = await asyncio.to_thread(embed_texts, texts)
    files = [
        FileRecord(
            path=f"{doc_id}.txt",
            source=_SOURCE,
            doc_id=doc_id,
            fingerprint="1:1",
            chunking_key=_CHUNKING,
        )
        for doc_id in _CORPUS
    ]
    chunks = [
        ChunkRecord(
            doc_id=doc_id, chunking_key=_CHUNKING, ordinal=0, content=text, embedding=vector
        )
        for (doc_id, text), vector in zip(_CORPUS.items(), vectors, strict=True)
    ]
    await index.upsert(files, chunks, embedding_config_key())


async def _durable() -> PostgresDocumentIndex:
    """A migrated, empty `document_files`/`document_chunks` and an index over them."""
    await migrated_db_or_skip()
    async with await connect(settings.postgres_dsn) as conn:
        await conn.execute("TRUNCATE document_chunks, document_files")
        await conn.commit()
    return PostgresDocumentIndex()


async def _hits(index: DocumentIndex, query: str) -> list[str]:
    """The document ids this backend returns for `query`, in rank order."""
    return [h.doc_id for h in await index.search_lexical(_SOURCE, query, 10, DocumentFilter())]


async def test_the_reference_matches_any_term_and_ranks_a_complete_match_first() -> None:
    """Every term first, then partial matches — and a partial match is still a hit, not nothing."""
    index = InMemoryDocumentIndex()
    await _load(index)
    hits = await _hits(index, _QUERY)
    assert hits[0] == "doc-complete"
    assert set(hits) == {
        "doc-complete",
        "doc-partial-solvent",
        "doc-partial-amide",
        "doc-solvent-guide",
    }
    assert "doc-unrelated" not in hits


async def test_the_two_document_backends_state_the_same_boolean_rule() -> None:
    """The durable backend and the reference return the same chunks, complete match first.

    Asserted on the set and the top position, since scores legitimately differ.
    """
    durable = await _durable()
    reference = InMemoryDocumentIndex()
    await _load(durable)
    await _load(reference)

    durable_hits = await _hits(durable, _QUERY)
    assert durable_hits, "the durable leg must contribute a ranking at all"
    assert durable_hits[0] == "doc-complete"
    assert set(durable_hits) == set(await _hits(reference, _QUERY))
    assert "doc-unrelated" not in durable_hits


@pytest.mark.parametrize("backend", ["reference", "durable"])
async def test_a_negated_term_is_excluded_by_both_document_backends(backend: str) -> None:
    """`-solvent` removes the solvent documents from the share instead of asking for them.

    Both backends must honour the exclusion, independently of how they combine other terms.
    """
    index: DocumentIndex = InMemoryDocumentIndex() if backend == "reference" else await _durable()
    await _load(index)
    assert await _hits(index, _EXCLUDING_QUERY) == ["doc-partial-amide"]
    # The exclusion must not undo the widening: the same question without the `-` still
    # returns every document sharing any term.
    assert set(await _hits(index, "amide coupling solvent")) == {
        "doc-complete",
        "doc-partial-solvent",
        "doc-partial-amide",
        "doc-solvent-guide",
    }


@pytest.mark.parametrize("backend", ["reference", "durable"])
async def test_a_query_that_only_excludes_returns_what_is_left(backend: str) -> None:
    """`-solvent` on its own is a query, and both backends answer it with the rest of the corpus.

    The durable backend returns those rows with `ts_rank` zero, so a reference dropping zero scores
    would diverge here only.
    """
    index: DocumentIndex = InMemoryDocumentIndex() if backend == "reference" else await _durable()
    await _load(index)
    assert set(await _hits(index, "-solvent")) == {"doc-partial-amide", "doc-unrelated"}


async def test_a_termless_query_is_not_a_query_to_the_durable_backend() -> None:
    """Widening changes which chunks match, not what counts as a question.

    A stop-word-only query has no lexemes to widen to and must return nothing rather than the whole
    share — the one way "match any term" could have become "match anything".
    """
    durable = await _durable()
    await _load(durable)
    assert await _hits(durable, "the and of") == []
    assert await _hits(durable, "   ") == []
