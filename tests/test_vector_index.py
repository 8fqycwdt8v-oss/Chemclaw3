"""The derived note index: in-memory ranking (offline) + a Postgres round-trip (skips offline).

Offline proves the reference ranking both backends share — dense by cosine, lexical by term
overlap — and that `reindex_notes` embeds notes so a query with no id/substring overlap still finds
the semantically-related note. The server-backed test proves `PostgresNoteIndex` upserts and ranks
the same way over real pgvector + full-text search.
"""

import asyncio
import time
import types
from collections.abc import Callable
from pathlib import Path

import pytest

import chemclaw.retrieval.vector_index as vector_index_module
from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.core.embeddings import embed_texts
from chemclaw.retrieval.vector_index import (
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


# --- incremental reindex: re-embed only what changed (R4.2) -------------------------------------


def _count_embed_calls(monkeypatch: pytest.MonkeyPatch, module: types.ModuleType) -> dict[str, int]:
    """Wrap `module.embed_texts` to count texts sent through it, without touching the provider."""
    calls = {"texts": 0}
    real: Callable[[list[str]], list[list[float]]] = module.embed_texts

    def _counting(texts: list[str]) -> list[list[float]]:
        calls["texts"] += len(texts)
        return real(texts)

    monkeypatch.setattr(module, "embed_texts", _counting)
    return calls


def test_reindex_a_second_time_unchanged_embeds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect this fixes: a re-run over an unchanged corpus must issue zero embedding calls."""
    calls = _count_embed_calls(monkeypatch, vector_index_module)
    _write_note(tmp_path, "note-a", "first note body")
    _write_note(tmp_path, "note-b", "second note body")
    index = InMemoryNoteIndex()

    first = asyncio.run(reindex_notes(index, notes_dir=str(tmp_path)))
    assert first == 2
    assert calls["texts"] == 2  # both notes are new: both embedded

    calls["texts"] = 0
    second = asyncio.run(reindex_notes(index, notes_dir=str(tmp_path)))
    assert second == 0  # nothing changed on disk
    assert calls["texts"] == 0  # so nothing was re-embedded


def test_reindex_only_embeds_the_note_that_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Editing one note out of several re-embeds exactly that one, not the whole corpus."""
    calls = _count_embed_calls(monkeypatch, vector_index_module)
    _write_note(tmp_path, "note-a", "first note body")
    _write_note(tmp_path, "note-b", "second note body")
    _write_note(tmp_path, "note-c", "third note body")
    index = InMemoryNoteIndex()
    asyncio.run(reindex_notes(index, notes_dir=str(tmp_path)))

    time.sleep(0.01)  # a distinct mtime on coarse filesystems
    _write_note(tmp_path, "note-b", "second note body, edited")
    calls["texts"] = 0
    changed = asyncio.run(reindex_notes(index, notes_dir=str(tmp_path)))
    assert changed == 1
    assert calls["texts"] == 1

    # A dense search for the new content finds the edited note through its fresh embedding.
    (query_embedding,) = embed_texts(["second note body, edited"])
    hits = asyncio.run(index.search_dense(query_embedding, top_k=1))
    assert hits and hits[0].note_id == "note-b"


def test_reindex_full_re_embeds_every_note_regardless_of_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`full=True` is the escape hatch for recovery: it ignores stored fingerprints entirely."""
    calls = _count_embed_calls(monkeypatch, vector_index_module)
    _write_note(tmp_path, "note-a", "first note body")
    _write_note(tmp_path, "note-b", "second note body")
    index = InMemoryNoteIndex()
    asyncio.run(reindex_notes(index, notes_dir=str(tmp_path)))

    calls["texts"] = 0
    full = asyncio.run(reindex_notes(index, notes_dir=str(tmp_path), full=True))
    assert full == 2  # both re-embedded even though neither changed on disk
    assert calls["texts"] == 2


def test_postgres_index_within_restricts_before_top_k() -> None:
    """`within` scopes the SQL query itself, so a filtered search keeps full top-k recall."""

    async def _run() -> None:
        await migrated_db_or_skip()
        async with await connect(settings.postgres_dsn) as conn:
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
        async with await connect(settings.postgres_dsn) as conn:
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


def test_postgres_fingerprints_round_trip_and_omit_unset_rows() -> None:
    """The durable `fingerprints()` read matches what was upserted; an unset one is left out."""

    async def _run() -> None:
        await migrated_db_or_skip()
        async with await connect(settings.postgres_dsn) as conn:
            await conn.execute("TRUNCATE note_index")
            await conn.commit()

        index = PostgresNoteIndex()
        set_emb, unset_emb = await asyncio.to_thread(embed_texts, ["set text", "unset text"])
        await index.upsert(
            [
                NoteRecord(note_id="fp-set", text="t", embedding=set_emb, fingerprint="123:45"),
                NoteRecord(note_id="fp-unset", text="t", embedding=unset_emb),  # no fingerprint
            ]
        )
        stored = await index.fingerprints()
        assert stored == {"fp-set": "123:45"}  # the unset row is absent, not an empty string

    asyncio.run(_run())


def test_reindex_incremental_against_postgres_embeds_only_the_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real durable backend, not just the in-memory reference.

    A second run over an unchanged corpus embeds nothing, and editing one note re-embeds exactly
    that note.
    """
    calls = _count_embed_calls(monkeypatch, vector_index_module)

    async def _run() -> None:
        await migrated_db_or_skip()
        async with await connect(settings.postgres_dsn) as conn:
            await conn.execute("TRUNCATE note_index")
            await conn.commit()

        _write_note(tmp_path, "pg-note-a", "alpha body")
        _write_note(tmp_path, "pg-note-b", "beta body")
        index = PostgresNoteIndex()

        first = await reindex_notes(index, notes_dir=str(tmp_path))
        assert first == 2
        assert calls["texts"] == 2

        calls["texts"] = 0
        second = await reindex_notes(index, notes_dir=str(tmp_path))
        assert second == 0
        assert calls["texts"] == 0

        time.sleep(0.01)
        _write_note(tmp_path, "pg-note-a", "alpha body, edited")
        calls["texts"] = 0
        third = await reindex_notes(index, notes_dir=str(tmp_path))
        assert third == 1
        assert calls["texts"] == 1

    asyncio.run(_run())
