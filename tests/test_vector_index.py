"""The derived note index: in-memory ranking (offline) + a Postgres round-trip (skips offline).

Offline proves the reference ranking both backends share — dense by cosine, lexical by term
overlap — and that `reindex_notes` embeds notes so a query with no id/substring overlap still finds
the semantically-related note. The server-backed test proves `PostgresNoteIndex` upserts and ranks
the same way over real pgvector + full-text search.
"""

import asyncio
from pathlib import Path

from agents.embedding_provider import embed_texts
from chemclaw.config import settings
from report.vector_index import (
    InMemoryNoteIndex,
    NoteRecord,
    PostgresNoteIndex,
    reindex_notes,
)
from tests.pg import migrated_db_or_skip


def _write_note(directory: Path, note_id: str, body: str) -> None:
    """Write a minimal note file under `directory`."""
    (directory / f"{note_id}.md").write_text(
        f"---\nid: {note_id}\ntype: reaction\n---\n{body}\n", encoding="utf-8"
    )


def test_inmemory_dense_ranks_by_cosine() -> None:
    """Dense search orders notes by cosine similarity and drops zero-similarity ones."""

    async def _run() -> None:
        index = InMemoryNoteIndex()
        await index.upsert(
            [
                NoteRecord(note_id="aligned", text="x", embedding=[1.0, 0.0]),
                NoteRecord(note_id="partial", text="y", embedding=[0.7, 0.7]),
                NoteRecord(note_id="orthogonal", text="z", embedding=[0.0, 1.0]),
            ]
        )
        hits = await index.search_dense([1.0, 0.0], top_k=5)
        assert [h.note_id for h in hits] == ["aligned", "partial"]  # orthogonal dropped (cosine 0)

    asyncio.run(_run())


def test_inmemory_lexical_ranks_by_term_overlap() -> None:
    """Lexical search ranks a term-overlap note above one with fewer terms; non-matches dropped."""

    async def _run() -> None:
        index = InMemoryNoteIndex()
        await index.upsert(
            [
                NoteRecord(note_id="both", text="amide coupling epimerization", embedding=[0.0]),
                NoteRecord(note_id="one", text="amide only here", embedding=[0.0]),
                NoteRecord(note_id="none", text="distillation reflux", embedding=[0.0]),
            ]
        )
        hits = await index.search_lexical("amide coupling", top_k=5)
        assert [h.note_id for h in hits] == ["both", "one"]  # 'none' shares no terms

    asyncio.run(_run())


def test_reindex_then_dense_search_finds_the_semantic_note(tmp_path: Path) -> None:
    """A query sharing no id/substring with a note still retrieves it via the embedded body."""

    async def _run() -> None:
        _write_note(tmp_path, "note-001", "amide coupling with HATU gave epimerization")
        _write_note(tmp_path, "note-002", "distillation column reflux ratio study")
        index = InMemoryNoteIndex()
        indexed = await reindex_notes(index, notes_dir=str(tmp_path))
        assert indexed == 2
        (query_embedding,) = await asyncio.to_thread(
            embed_texts, ["epimerization observed during an amide coupling"]
        )
        hits = await index.search_dense(query_embedding, top_k=1)
        assert hits and hits[0].note_id == "note-001"  # found without any id/substring overlap

    asyncio.run(_run())


def test_reindex_empty_dir_is_a_noop(tmp_path: Path) -> None:
    """Reindexing an empty knowledge dir indexes nothing (no crash, no rows)."""
    index = InMemoryNoteIndex()
    assert asyncio.run(reindex_notes(index, notes_dir=str(tmp_path))) == 0


def test_postgres_index_within_restricts_before_top_k() -> None:
    """`within` scopes the SQL query itself, so a filtered search keeps full top-k recall."""

    async def _run() -> None:
        await migrated_db_or_skip()
        import psycopg

        async with await psycopg.AsyncConnection.connect(settings.postgres_dsn) as conn:
            await conn.execute("TRUNCATE note_index")
            await conn.commit()

        index = PostgresNoteIndex()
        close, far = await asyncio.to_thread(
            embed_texts, ["amide coupling epimerization", "amide coupling workup"]
        )
        await index.upsert(
            [
                NoteRecord(note_id="rxn-1", text="amide coupling epimerization", embedding=close),
                NoteRecord(note_id="play-1", text="amide coupling workup", embedding=far),
            ]
        )
        (query_embedding,) = await asyncio.to_thread(embed_texts, ["amide coupling epimerization"])
        # Unrestricted, the single top slot goes to the nearest note (rxn-1)...
        dense = await index.search_dense(query_embedding, top_k=1)
        assert [h.note_id for h in dense] == ["rxn-1"]
        # ...but a `within` scope still finds the eligible note past that global rank.
        dense = await index.search_dense(query_embedding, top_k=1, within={"play-1"})
        assert [h.note_id for h in dense] == ["play-1"]
        lexical = await index.search_lexical("amide coupling", top_k=1, within={"play-1"})
        assert [h.note_id for h in lexical] == ["play-1"]

    asyncio.run(_run())


def test_postgres_note_index_round_trip() -> None:
    """The real pgvector/FTS backend upserts and ranks the indexed note under both search modes."""

    async def _run() -> None:
        await migrated_db_or_skip()
        import psycopg

        async with await psycopg.AsyncConnection.connect(settings.postgres_dsn) as conn:
            await conn.execute("TRUNCATE note_index")
            await conn.commit()

        index = PostgresNoteIndex()
        (embedding,) = await asyncio.to_thread(embed_texts, ["amide coupling epimerization"])
        await index.upsert(
            [
                NoteRecord(
                    note_id="note-001", text="amide coupling epimerization", embedding=embedding
                )
            ]
        )
        (query_embedding,) = await asyncio.to_thread(embed_texts, ["epimerization amide coupling"])
        dense = await index.search_dense(query_embedding, top_k=5)
        assert any(h.note_id == "note-001" for h in dense)
        lexical = await index.search_lexical("amide coupling", top_k=5)
        assert any(h.note_id == "note-001" for h in lexical)

    asyncio.run(_run())
