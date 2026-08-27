"""The note index with its vectors outside Postgres, and the prune that made it safe to have one.

Two subjects, and the second is the reason the first is here at all.

`ExternalVectorNoteIndex` is the second consumer of the `VectorStore` seam. D-2026-08-08 declined
one on the grounds that generalizing the seam before the first consumer had run for real would be
designing against a guess — so the assertions that matter are the ones showing the interface did not
have to move: the same three store methods, used the same way, with the catalogue keeping everything
a vector database has no `ts_rank` for.

The prune is the part that genuinely was missing. `NoteIndex` had no delete and `reindex_notes`
never removed anything, which is harmless while a stale row is a row in a Postgres table and is not
harmless once a deleted note leaves a vector behind in a store that bills for it and that no other
sweep reaches.
"""

import asyncio
import functools
from pathlib import Path
from typing import Any

import pytest

from chemclaw.core.config import settings
from chemclaw.retrieval.external_note_index import ExternalVectorNoteIndex
from chemclaw.retrieval.vector_index import (
    InMemoryNoteIndex,
    NoteIndex,
    NoteRecord,
    PostgresNoteIndex,
    default_note_index,
    reindex_notes,
)
from chemclaw.retrieval.vectors.memory import InMemoryVectorStore
from tests.pg import migrated_db_or_skip

COLLECTION = "notes"


def _sync(test: Any) -> Any:
    """Run an `async def` test on its own loop; this repository has no async pytest plugin."""

    @functools.wraps(test)
    def runner(*args: Any, **kwargs: Any) -> None:
        asyncio.run(test(*args, **kwargs))

    return runner


def _record(note_id: str, vector: list[float], text: str = "ester formation") -> NoteRecord:
    return NoteRecord(note_id=note_id, text=text, embedding=vector, fingerprint=f"fp-{note_id}")


def _write_note(directory: Path, note_id: str, title: str) -> None:
    """A minimal note the graph indexer will load, so `reindex_notes` sees a real corpus.

    The title lives in the body: `Note` has no `title` field, and since `extra="forbid"` a
    frontmatter key the schema does not know is a refused note rather than silently dropped
    metadata — this helper used to write one, which is the finding in miniature.
    """
    (directory / "reaction").mkdir(parents=True, exist_ok=True)
    (directory / "reaction" / f"{note_id}.md").write_text(
        f"---\nid: {note_id}\ntype: reaction\n---\n\n# {title}\n\n{title} notes.\n",
        encoding="utf-8",
    )


# --- the prune, on the reference backend (no database needed) -----------------------------------


@_sync
async def test_retire_absent_removes_what_is_no_longer_on_disk() -> None:
    """The gap D-2026-08-25 closed: nothing in this system ever deleted a note vector."""
    index = InMemoryNoteIndex()
    await index.upsert([_record("a", [1.0, 0.0]), _record("b", [0.0, 1.0])], "key-1")

    assert await index.retire_absent({"a"}) == 1
    assert set(await index.fingerprints("key-1")) == {"a"}


@_sync
async def test_an_empty_keep_set_retires_nothing() -> None:
    """A mis-pointed notes directory must not wipe an index that costs one call per note to rebuild.

    Guarded here as well as by `reindex_notes`' own early return, because two cheap noes are worth
    more than one: this method is public, and the failure it prevents is silent and expensive.
    """
    index = InMemoryNoteIndex()
    await index.upsert([_record("a", [1.0, 0.0])], "key-1")

    assert await index.retire_absent(set()) == 0
    assert set(await index.fingerprints("key-1")) == {"a"}


@_sync
async def test_reindex_retires_a_deleted_note_even_when_nothing_changed(tmp_path: Path) -> None:
    """A run whose only news is a deletion has nothing to embed and must still remove it.

    The ordering that makes this work: the prune runs *before* the "nothing changed" exit. With it
    after, the deletion would be invisible on exactly the runs where it is the only thing to do.
    """
    _write_note(tmp_path, "reaction-a", "Ester A")
    _write_note(tmp_path, "reaction-b", "Ester B")
    index = InMemoryNoteIndex()
    assert await reindex_notes(index, notes_dir=str(tmp_path)) == 2

    (tmp_path / "reaction" / "reaction-b.md").unlink()
    # Nothing to embed: `reaction-a`'s fingerprint has not moved.
    assert await reindex_notes(index, notes_dir=str(tmp_path)) == 0
    assert set(await index.fingerprints(await _key())) == {"reaction-a"}


@_sync
async def test_an_empty_notes_directory_does_not_empty_the_index(tmp_path: Path) -> None:
    """`reindex_notes` returns before the prune when it loaded no notes at all."""
    _write_note(tmp_path, "reaction-a", "Ester A")
    index = InMemoryNoteIndex()
    await reindex_notes(index, notes_dir=str(tmp_path))

    assert await reindex_notes(index, notes_dir=str(tmp_path / "nowhere")) == 0
    assert set(await index.fingerprints(await _key())) == {"reaction-a"}


async def _key() -> str:
    from chemclaw.core.embeddings import embedding_config_key

    return embedding_config_key()


async def _fresh_index(store: InMemoryVectorStore) -> ExternalVectorNoteIndex:
    """A migrated database with an empty `note_index`, and an index bound to `store`.

    Emptied per test because every test in this file shares one schema (`conftest.py` redirects
    `postgres_dsn` at `TEST_SCHEMA` for the session), and `retire_absent` counts what it deleted —
    a row another test left behind would be counted here.
    """
    await migrated_db_or_skip()
    from chemclaw.core import db

    async with db.connection(settings.postgres_dsn) as conn:
        await conn.execute("DELETE FROM note_index")
        await conn.commit()
    return ExternalVectorNoteIndex(store, collection=COLLECTION)


# --- provider selection --------------------------------------------------------------------------


def test_the_default_deployment_keeps_notes_in_postgres() -> None:
    """`pgvector` answers ranking, eligibility and citation in one statement — nothing to split."""
    assert isinstance(default_note_index(), PostgresNoteIndex)


def test_an_external_provider_moves_only_the_dense_half(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same chooser shape `default_document_index()` has, one layer over."""
    monkeypatch.setattr(settings, "vector_store_provider", "qdrant")
    index = default_note_index()
    assert isinstance(index, ExternalVectorNoteIndex)
    # A subclass, which is the claim: the catalogue half is inherited rather than reimplemented.
    assert isinstance(index, PostgresNoteIndex)


def test_the_external_index_satisfies_the_protocol() -> None:
    """Five methods, three of them inherited untouched."""
    assert isinstance(ExternalVectorNoteIndex(InMemoryVectorStore()), NoteIndex)


# --- the composition, against a real `note_index` table -----------------------------------------


@_sync
async def test_the_vectors_leave_and_the_catalogue_stays(tmp_path: Path) -> None:
    """The whole design in one assertion: `note_index.embedding` is NULL, the store has it.

    The lexical column, the fingerprint and the embedding key stay in Postgres, because they are the
    parts a vector database has nothing to offer for.
    """
    store = InMemoryVectorStore()
    index = await _fresh_index(store)

    await index.upsert([_record("reaction-a", [1.0, 0.0])], "key-1")

    assert await index.fingerprints("key-1") == {"reaction-a": "fp-reaction-a"}
    assert [hit.note_id for hit in await index.search_lexical("ester", 5)] == ["reaction-a"]
    assert [match.id for match in await store.search(COLLECTION, [1.0, 0.0], 5)] == [
        "reaction-a"
    ], "the vector went to the store"

    from chemclaw.core import db

    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT embedding FROM note_index WHERE note_id = 'reaction-a'")
            row = await cur.fetchone()
    assert row is not None and row[0] is None, "the pgvector column is written NULL, not dropped"


@_sync
async def test_dense_search_ranks_in_the_store_and_returns_note_ids(tmp_path: Path) -> None:
    """A note is embedded whole, so the point id *is* the note id — no encoding, no resolve step."""
    store = InMemoryVectorStore()
    index = await _fresh_index(store)
    await index.upsert(
        [_record("reaction-a", [1.0, 0.0]), _record("reaction-b", [0.0, 1.0])], "key-1"
    )

    hits = await index.search_dense([1.0, 0.0], 5)
    assert [hit.note_id for hit in hits] == ["reaction-a"], "orthogonal notes are not hits"


@_sync
async def test_a_scope_narrows_before_the_cut_rather_than_after_it(tmp_path: Path) -> None:
    """Filtering after the top-k is how a narrow scope over a wide corpus returns nothing at all."""
    store = InMemoryVectorStore()
    index = await _fresh_index(store)
    await index.upsert(
        [_record(f"reaction-{n}", [1.0 - n / 100, n / 100]) for n in range(20)], "key-1"
    )

    hits = await index.search_dense([0.0, 1.0], 1, within={"reaction-1"})
    assert [hit.note_id for hit in hits] == ["reaction-1"]


@_sync
async def test_a_zero_query_vector_costs_no_round_trip(tmp_path: Path) -> None:
    """It has cosine 0 to everything, which is what the reference backend returns too."""
    index = await _fresh_index(InMemoryVectorStore())
    assert await index.search_dense([0.0, 0.0], 5) == []


@_sync
async def test_retiring_a_note_removes_its_point_too(tmp_path: Path) -> None:
    """The reason this class needed a prune at all: an orphaned vector has no other sweep."""
    store = InMemoryVectorStore()
    index = await _fresh_index(store)
    await index.upsert(
        [_record("reaction-a", [1.0, 0.0]), _record("reaction-b", [0.0, 1.0])], "key-1"
    )

    assert await index.retire_absent({"reaction-a"}) == 1
    assert set(await index.fingerprints("key-1")) == {"reaction-a"}
    assert [match.id for match in await store.search(COLLECTION, [0.0, 1.0], 5)] == [], (
        "the retired note's vector is gone from the store, not just from the catalogue"
    )


@_sync
async def test_switching_provider_re_embeds_rather_than_returning_nothing(tmp_path: Path) -> None:
    """A deployment whose notes were in Postgres must not silently lose its dense leg on the move.

    The defect this pins is the one `infra/sql/039` was written about, one backend over. A cluster
    already running an external store for its *documents* gets its notes moved here too, because
    `vector_store_provider` is one switch. Every `note_index` row is still present with a matching
    `embedding_key`, so a fingerprint diff keyed only on the model sees nothing to do — while the
    store holds no note vector at all. `reindex_notes` returns 0, and `search_dense` answers from an
    empty collection: no hits, no error, until somebody runs `--full` by hand.

    Namespacing the stored key by the collection is what makes the move self-healing.
    """
    _write_note(tmp_path, "reaction-a", "Ester A")
    store = InMemoryVectorStore()
    postgres = await _fresh_index(store)  # empties note_index, then hands back the external index

    # Stage the "before" state: rows written by the *Postgres* index, i.e. the bare embedding key.
    plain = PostgresNoteIndex()
    assert await reindex_notes(plain, notes_dir=str(tmp_path)) == 1
    assert set(await plain.fingerprints(await _key())) == {"reaction-a"}

    # Now the same corpus through the external index, as an upgrade would.
    assert await postgres.fingerprints(await _key()) == {}, (
        "the catalogue's rows must not claim a vector this store has never held"
    )
    assert await reindex_notes(postgres, notes_dir=str(tmp_path)) == 1, "the note is re-embedded"

    # And the vector landed where the search will look for it, which is the whole point: before the
    # fix this returned nothing at all, from an empty collection, with no error anywhere.
    hits = await postgres.search_dense(await _embed("Ester A"), 5)
    assert [hit.note_id for hit in hits] == ["reaction-a"]
    assert set(await postgres.fingerprints(await _key())) == {"reaction-a"}


async def _embed(text: str) -> list[float]:
    from chemclaw.core.embeddings import embed_texts

    return embed_texts([text])[0]
