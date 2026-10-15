"""The embedding width is configuration: columns built from a setting, a corpus that can be moved.

Against a real Postgres with pgvector, in a schema of its own per test so a database can be taken to
the state an existing deployment is in (every migration before the width one applied, rows written)
and migrated from there. What is proved:

* a deployment that keeps its width migrates to nothing and loses nothing;
* a deployment that changes width gets a column and an HNSW index beside the data, never a rewrite;
* a fresh database at a new width stores and ranks vectors;
* the HNSW index is what answers the dense query at the new width;
* a row stamped with another configuration is never ranked and is reported as a mixed corpus;
* the re-embed moves the corpus to the new column and is idempotent; it holds no lock while the
  gateway answers, so the scheduled pass, the crawl's upsert and its prune proceed beside it and its
  own late write skips whatever moved; a killed process changes nothing; a reclaim is a run apart;
* a chunk that will not embed is passed once and counted, and a dead endpoint stops the run;
* the CLI rejoins a running run and reads its verdict; `/readyz` and the job agree on "mixed" and a
  lock on the vector tables does not turn into `database unreachable`.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from temporalio import activity
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Worker

import chemclaw.core.migrate as migrate_module
import chemclaw.ingest.documents.sync as sync_module
from chemclaw.api.routes import ops
from chemclaw.cli import reembed as cli_reembed
from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.core.embeddings import clear_embedding_cache, embed_texts, embedding_config_key
from chemclaw.core.migrate import migrate
from chemclaw.core.vector_schema import (
    embedding_column,
    embedding_columns,
    inspect_tables,
    live_embedding_keys,
    parse_embedding_key,
)
from chemclaw.durable import reembed
from chemclaw.durable.note_index import reindex_notes_activity
from chemclaw.ingest.documents.index import (
    ChunkCursor,
    ChunkRecord,
    DocumentFilter,
    FileRecord,
    PostgresDocumentIndex,
)
from chemclaw.ingest.documents.sync import ReembedReport, reembed_after, reembed_stale
from chemclaw.retrieval.vector_index import (
    NoteRecord,
    PostgresNoteIndex,
    note_embedding_key,
    reindex_notes,
)
from tests.pg import TEST_SCHEMA, create_test_schema, drop_test_schema, schema_dsn
from tests.temporal_env import pydantic_client, start_env_or_skip, start_local_env_or_skip

_SQL_DIR = Path(settings.sql_migrations_dir)
_CHUNKING = "2000:200"
_SOURCE = "share"


def _width_migration() -> str:
    """The migration that defines `ensure_embedding_width`, wherever it is numbered."""
    return next(
        path.name
        for path in sorted(_SQL_DIR.glob("*.sql"))
        if "ensure_embedding_width" in path.read_text()
    )


def _before_width_migration(tmp_path: Path) -> Path:
    """A copy of the migrations a deployment had before this one: every file sorted ahead of it."""
    target = tmp_path / "before"
    target.mkdir()
    for path in sorted(_SQL_DIR.glob("*.sql")):
        if path.name < _width_migration():
            (target / path.name).write_text(path.read_text())
    return target


class Scratch:
    """A schema of this test's own, with every Postgres DSN setting pointed into it."""

    def __init__(self, dsn: str, schema: str) -> None:
        """Remember the DSN (carrying the schema on its `search_path`) and the schema's name."""
        self.dsn = dsn
        self.schema = schema

    async def afetch(self, statement: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        """Run one statement in the schema and return its rows."""
        async with await connect(self.dsn) as conn:
            cursor = await conn.execute(statement, params)
            rows = await cursor.fetchall() if cursor.description else []
            await conn.commit()
            return [tuple(row) for row in rows]


@pytest.fixture
def scratch(monkeypatch: pytest.MonkeyPatch) -> Iterator[Scratch]:
    """An empty schema reachable through every DSN setting, dropped afterwards."""
    base = settings.postgres_dsn.split("?")[0]
    schema = f"{TEST_SCHEMA}_w{os.getpid()}{int(time.monotonic_ns()) % 100000}"
    try:
        asyncio.run(create_test_schema(base, schema))
    except (psycopg.Error, ConnectionError) as exc:  # pragma: no cover - env-dependent
        pytest.skip(f"Postgres unavailable (start it: sudo dockerd; make up): {exc}")
    dsn = schema_dsn(base, schema)
    for name in ("postgres_dsn", "postgres_migration_dsn", "session_store_dsn"):
        monkeypatch.setattr(settings, name, dsn)
    monkeypatch.setattr(settings, "embedding_provider", "hash")
    monkeypatch.setattr(settings, "embedding_dim", 1536)
    clear_embedding_cache()
    try:
        yield Scratch(dsn, schema)
    finally:
        asyncio.run(drop_test_schema(base, schema))


async def _deployed_before(
    scratch: Scratch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Take the schema to what an existing deployment holds: every earlier migration, no width one.

    The width step is switched off for this one run, as the image that wrote these rows had none.
    """

    async def _not_yet(_conn: Any) -> None:
        return None

    with monkeypatch.context() as patch:
        patch.setattr(settings, "sql_migrations_dir", str(_before_width_migration(tmp_path)))
        patch.setattr(migrate_module, "_ensure_vector_width", _not_yet)
        await migrate(scratch.dsn)


async def _write_notes(count: int) -> list[str]:
    """Index `count` notes under the live configuration, as the running deployment does."""
    index = PostgresNoteIndex()
    texts = [f"solvent screen {i} heptane" for i in range(count)]
    vectors = embed_texts(texts, cache=False)
    await index.upsert(
        [
            NoteRecord(note_id=f"n{i}", text=text, embedding=vector, fingerprint=f"fp{i}")
            for i, (text, vector) in enumerate(zip(texts, vectors, strict=True))
        ],
        note_embedding_key(),
    )
    return texts


async def _write_chunks(count: int) -> None:
    """Index `count` single-chunk documents under the live configuration."""
    index = PostgresDocumentIndex()
    texts = [f"crystallisation report {i} ethyl acetate" for i in range(count)]
    vectors = embed_texts(texts, cache=False)
    await index.upsert(
        [
            FileRecord(
                path=f"p{i}.txt",
                source=_SOURCE,
                doc_id=f"d{i}",
                fingerprint="1:1",
                chunking_key=_CHUNKING,
            )
            for i in range(count)
        ],
        [
            ChunkRecord(
                doc_id=f"d{i}",
                chunking_key=_CHUNKING,
                ordinal=0,
                content=text,
                embedding=vector,
            )
            for i, (text, vector) in enumerate(zip(texts, vectors, strict=True))
        ],
        embedding_config_key(),
    )


async def _columns(scratch: Scratch, table: str) -> dict[str, int]:
    async with await connect(scratch.dsn) as conn:
        return await embedding_columns(conn, table)


# --- a deployment that keeps its width ----------------------------------------------------------


async def test_a_deployment_that_keeps_its_width_migrates_nothing_and_loses_nothing(
    scratch: Scratch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At 1536 the width migration adds no column and no index, and every row is as it was."""
    await _deployed_before(scratch, tmp_path, monkeypatch)
    await _write_notes(4)
    await _write_chunks(4)
    before = await scratch.afetch("SELECT note_id, embedding::text FROM note_index ORDER BY 1")
    objects = await scratch.afetch(
        "SELECT relname FROM pg_class WHERE relnamespace = %s::regnamespace ORDER BY 1",
        (scratch.schema,),
    )

    assert await migrate(scratch.dsn) == [_width_migration()]

    assert (
        await scratch.afetch("SELECT note_id, embedding::text FROM note_index ORDER BY 1") == before
    )
    after = await scratch.afetch(
        "SELECT relname FROM pg_class WHERE relnamespace = %s::regnamespace ORDER BY 1",
        (scratch.schema,),
    )
    assert after == objects, "the width migration created an object for a width that was not asked"
    assert set(await _columns(scratch, "note_index")) == {"embedding"}
    assert [
        hit.note_id
        for hit in await PostgresNoteIndex().search_dense(
            embed_texts(["solvent screen 2 heptane"])[0], 3
        )
    ][0] == "n2"


# --- a deployment that changes width ------------------------------------------------------------


async def test_a_new_width_adds_a_column_and_an_hnsw_index_beside_the_data(
    scratch: Scratch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Moving to 768 adds `embedding_768` and its index to both tables; old rows are untouched."""
    await _deployed_before(scratch, tmp_path, monkeypatch)
    await _write_notes(5)
    await _write_chunks(5)
    notes = await scratch.afetch(
        "SELECT note_id, embedding::text, embedding_key FROM note_index ORDER BY 1"
    )
    chunks = await scratch.afetch("SELECT doc_id, embedding::text FROM document_chunks ORDER BY 1")

    monkeypatch.setattr(settings, "embedding_dim", 768)
    assert await migrate(scratch.dsn) == [_width_migration()]

    for table in ("note_index", "document_chunks"):
        assert await _columns(scratch, table) == {"embedding": 1536, "embedding_768": 768}
        (definition,) = (
            row[0]
            for row in await scratch.afetch(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = %s AND indexname = %s",
                (scratch.schema, f"{table}_embedding_768_idx"),
            )
        )
        assert "USING hnsw (embedding_768 vector_cosine_ops)" in definition
    assert (
        await scratch.afetch(
            "SELECT note_id, embedding::text, embedding_key FROM note_index ORDER BY 1"
        )
        == notes
    )
    assert (
        await scratch.afetch("SELECT doc_id, embedding::text FROM document_chunks ORDER BY 1")
        == chunks
    )
    assert await scratch.afetch(
        "SELECT count(*) FROM note_index WHERE embedding_768 IS NOT NULL"
    ) == [(0,)]

    # Running again changes nothing and applies nothing, so the pre-upgrade hook is safe each time.
    objects = await scratch.afetch(
        "SELECT oid FROM pg_class WHERE relnamespace = %s::regnamespace ORDER BY 1",
        (scratch.schema,),
    )
    assert await migrate(scratch.dsn) == []
    assert (
        await scratch.afetch(
            "SELECT oid FROM pg_class WHERE relnamespace = %s::regnamespace ORDER BY 1",
            (scratch.schema,),
        )
        == objects
    )


async def test_a_fresh_database_at_a_new_width_stores_and_ranks_vectors(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """From nothing at 768: notes and chunks are written to `embedding_768` and found there."""
    monkeypatch.setattr(settings, "embedding_dim", 768)
    clear_embedding_cache()
    await migrate(scratch.dsn)
    texts = await _write_notes(4)
    await _write_chunks(4)

    assert await scratch.afetch(
        "SELECT count(*) FROM note_index WHERE embedding_768 IS NOT NULL"
    ) == [(4,)]
    assert await scratch.afetch("SELECT count(*) FROM note_index WHERE embedding IS NOT NULL") == [
        (0,)
    ]
    query = embed_texts([texts[3]])[0]
    assert len(query) == 768
    assert (await PostgresNoteIndex().search_dense(query, 2))[0].note_id == "n3"
    documents = await PostgresDocumentIndex().search_dense(
        _SOURCE, embed_texts(["crystallisation report 1 ethyl acetate"])[0], 2, DocumentFilter()
    )
    assert documents[0].doc_id == "d1"


async def test_the_hnsw_index_answers_the_dense_query_at_the_new_width(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The planner orders by `*_embedding_768_idx`; with that index gone it plainly does not.

    The note statement is the production one. The document statement joins file rows before it
    ranks, which on a table this small the planner answers without the vector index at either
    width, so the chunk table is asked the bare nearest-neighbour question its index exists for.
    """
    monkeypatch.setattr(settings, "embedding_dim", 768)
    clear_embedding_cache()
    await migrate(scratch.dsn)
    await _write_notes(60)
    await _write_chunks(60)
    query = "[" + ",".join(str(x) for x in embed_texts(["solvent screen 7 heptane"])[0]) + "]"
    notes = (
        PostgresNoteIndex()._dense,
        {"q": query, "k": 3, "ids": None, "key": note_embedding_key()},
    )
    chunks = (
        "SELECT doc_id FROM document_chunks WHERE embedding_768 IS NOT NULL "
        "AND embedding_key = %(key)s ORDER BY embedding_768 <=> %(q)s::vector(768) LIMIT %(k)s",
        {"q": query, "k": 3, "key": embedding_config_key()},
    )

    async def _plan(
        conn: psycopg.AsyncConnection[Any], statement: str, params: dict[str, Any]
    ) -> str:
        cursor = await conn.execute(f"EXPLAIN (FORMAT JSON) {statement}", params)
        row = await cursor.fetchone()
        assert row is not None
        return json.dumps(row[0])

    async with await connect(scratch.dsn) as conn:
        await conn.execute("ANALYZE note_index")
        await conn.execute("ANALYZE document_chunks")
        # Without it the key's btree is the planner's other way to the same rows.
        await conn.execute("DROP INDEX document_chunks_embedding_key_idx")
        await conn.execute("SET enable_seqscan = off")
        assert "note_index_embedding_768_idx" in await _plan(conn, *notes)
        assert "document_chunks_embedding_768_idx" in await _plan(conn, *chunks)
        await conn.execute("DROP INDEX note_index_embedding_768_idx")
        await conn.execute("DROP INDEX document_chunks_embedding_768_idx")
        assert "embedding_768_idx" not in await _plan(conn, *notes)
        assert "embedding_768_idx" not in await _plan(conn, *chunks)
        await conn.rollback()


async def test_a_migration_that_cannot_get_both_table_locks_creates_nothing_and_retries_clean(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A writer holding `document_chunks` times the run out on its lock, leaving nothing half-built.

    Both tables are taken in one statement before anything is built, so the wait is one wait, the
    timeout aborts before an index is started, and a rerun once the writer is gone finishes.
    """
    await migrate(scratch.dsn)
    monkeypatch.setattr(settings, "embedding_dim", 768)
    monkeypatch.setattr(settings, "pg_migration_lock_timeout_seconds", 0.5)
    async with await connect(scratch.dsn) as writer:
        await writer.execute("LOCK TABLE document_chunks IN ROW EXCLUSIVE MODE")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            await migrate(scratch.dsn)
        await writer.rollback()
    for table in ("note_index", "document_chunks"):
        assert set(await _columns(scratch, table)) == {"embedding"}
    await migrate(scratch.dsn)
    assert set(await _columns(scratch, "document_chunks")) == {"embedding", "embedding_768"}


# --- a width pgvector cannot index ----------------------------------------------------------------


async def test_a_width_pgvector_cannot_index_is_refused_by_the_function_and_skipped_by_the_migrator(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The function names the limit; the migrator skips it and still migrates the rest."""
    monkeypatch.setattr(settings, "embedding_dim", 3072)
    assert _width_migration() in await migrate(scratch.dsn)
    assert set(await _columns(scratch, "note_index")) == {"embedding"}
    with pytest.raises(psycopg.errors.RaiseException, match="1 to 2000 dimensions"):
        await scratch.afetch("SELECT ensure_embedding_width(3072)")


# --- a corpus holding more than one configuration -------------------------------------------


async def test_a_row_under_another_configuration_is_never_ranked_and_is_reported_as_mixed(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows under a foreign key are invisible to dense search and named by `inspect_tables`."""
    await migrate(scratch.dsn)
    await _write_notes(3)
    foreign = "openai_compatible:ep-abc123:d1536:text-embed-v1|ntv3"
    await scratch.afetch(
        "UPDATE note_index SET embedding_key = %s WHERE note_id = 'n1'", (foreign,)
    )
    await scratch.afetch("UPDATE note_index SET embedding_key = NULL WHERE note_id = 'n2'")
    await _write_chunks(2)

    live = live_embedding_keys()
    async with await connect(scratch.dsn) as conn:
        notes, documents = await inspect_tables(conn, 1536, live)

    assert notes.width_ok(1536) and notes.mixed
    assert set(notes.foreign_keys) == {foreign, None}
    assert "openai_compatible/text-embed-v1 d1536" in notes.describe_foreign()
    assert "(unrecorded)" in notes.describe_foreign()
    assert not documents.mixed
    ranked = await PostgresNoteIndex().search_dense(embed_texts(["solvent screen 1 heptane"])[0], 5)
    assert {hit.note_id for hit in ranked} == {"n0"}, "a row from another model was ranked"


async def test_a_column_missing_for_the_configured_width_is_reported(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configured for 768 against a database migrated at 1536, `width_ok` is false and says why."""
    await migrate(scratch.dsn)
    monkeypatch.setattr(settings, "embedding_dim", 768)
    live = live_embedding_keys()
    async with await connect(scratch.dsn) as conn:
        states = await inspect_tables(conn, 768, live)
    assert [(s.table, s.column, s.column_width) for s in states] == [
        ("note_index", "embedding_768", None),
        ("document_chunks", "embedding_768", None),
    ]
    assert not any(s.width_ok(768) for s in states)


def test_a_stored_key_is_decoded_into_provider_model_and_width() -> None:
    """The per-row record is the key; its width and model read back, suffixes tolerated."""
    key = "openai_compatible:ep-0123456789ab:d3072:org/embed-large:latest|ntv3"
    identity = parse_embedding_key(key)
    assert identity is not None
    assert (identity.provider, identity.endpoint, identity.dim) == (
        "openai_compatible",
        "ep-0123456789ab",
        3072,
    )
    assert identity.model == "org/embed-large:latest"
    assert parse_embedding_key(None) is None
    assert parse_embedding_key("not a key") is None
    assert embedding_column(1536) == "embedding" and embedding_column(768) == "embedding_768"


# --- moving the corpus ----------------------------------------------------------------------


def _use_shares(monkeypatch: pytest.MonkeyPatch) -> None:
    """Name one enabled share cut under `_CHUNKING`, as the job scopes its work to."""
    share = SimpleNamespace(share_binding=lambda: SimpleNamespace(chunking_key=_CHUNKING))
    monkeypatch.setattr(reembed, "share_sources", lambda: {_SOURCE: share})
    monkeypatch.setattr(cli_reembed, "share_sources", lambda: {_SOURCE: share})
    monkeypatch.setattr(ops, "share_sources", lambda: {_SOURCE: share})


async def _walk(run: Callable[..., Awaitable[Any]]) -> list[int]:
    """The job's document phase as the workflow drives it: each batch resumes after the last."""
    cursor = None
    embedded = []
    while True:
        batch = await run(reembed.reembed_document_batch, cursor)
        embedded.append(batch.embedded)
        cursor = batch.cursor or cursor
        if not batch.has_more:
            return embedded


async def test_the_re_embed_moves_a_corpus_to_a_new_width_and_a_reclaim_empties_the_old_vectors(
    scratch: Scratch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 1536 deployment moves to 768: rows end under the new key and column; reclaim is apart."""
    await _deployed_before(scratch, tmp_path, monkeypatch)
    await _write_notes(6)
    await _write_chunks(6)
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    for i in range(6):
        (notes_dir / f"n{i}.md").write_text(
            f"---\nid: n{i}\ntype: reaction\n---\nsolvent screen {i} heptane\n", encoding="utf-8"
        )
    monkeypatch.setattr(settings, "embedding_dim", 768)
    clear_embedding_cache()
    await migrate(scratch.dsn)
    _use_shares(monkeypatch)
    monkeypatch.setattr(settings, "document_reembed_batch_size", 4)
    run = ActivityEnvironment().run

    # Mixed until moved: nothing ranks, and the job's own count says how much is left.
    assert (await run(reembed.count_unembedded)).documents == 6
    assert (
        await PostgresDocumentIndex().search_dense(
            _SOURCE, embed_texts(["crystallisation report 1 ethyl acetate"])[0], 3, DocumentFilter()
        )
        == []
    )

    plan = await run(reembed.plan_embedding_reembed)
    assert (plan.column, plan.dim) == ("embedding_768", 768)
    assert await _walk(run) == [4, 2]
    assert await reindex_notes(PostgresNoteIndex(), str(notes_dir)) == 6

    assert (await run(reembed.count_unembedded)).model_dump() == {"documents": 0, "notes": 0}
    assert await scratch.afetch(
        "SELECT count(*) FROM document_chunks WHERE embedding_768 IS NOT NULL"
    ) == [(6,)]
    # The original column still holds the old vectors: the previous image reads them, and only an
    # explicit reclaim, which is the end of the rollback path, empties them.
    assert await scratch.afetch(
        "SELECT count(*) FROM document_chunks WHERE embedding IS NOT NULL"
    ) == [(6,)]
    cleared = [await run(reembed.clear_superseded_vectors, plan) for _ in range(4)]
    assert cleared == [4, 4, 4, 0], "6 chunks and 6 notes cleared 4 at a time, then nothing"
    for table in ("note_index", "document_chunks"):
        assert await scratch.afetch(
            f"SELECT count(*) FROM {table} WHERE embedding IS NOT NULL"
        ) == [(0,)]
    ranked = await PostgresDocumentIndex().search_dense(
        _SOURCE, embed_texts(["crystallisation report 1 ethyl acetate"])[0], 3, DocumentFilter()
    )
    assert ranked[0].doc_id == "d1"

    # Idempotent: a second run finds nothing to do.
    assert await _walk(run) == [0]
    assert await run(reembed.clear_superseded_vectors, plan) == 0


async def _run_workflow(
    client: Any, workflow_id: str, request: reembed.ReembedRequest | None = None
) -> tuple[reembed.ReembedOutcome, bool]:
    """Run the real workflow on a worker of this test's own: verdict, and whether it continued."""
    async with Worker(
        client,
        task_queue="test-reembed",
        workflows=[reembed.EmbeddingReembedWorkflow],
        activities=[
            reembed.plan_embedding_reembed,
            reembed.reembed_document_batch,
            reindex_notes_activity,
            reembed.clear_superseded_vectors,
            reembed.count_unembedded,
        ],
    ):
        started = await client.start_workflow(
            reembed.EmbeddingReembedWorkflow.run,
            request,
            id=workflow_id,
            task_queue="test-reembed",
        )
        outcome: reembed.ReembedOutcome = await started.result()
        latest = (await client.get_workflow_handle(workflow_id).describe()).run_id
        return outcome, latest != started.result_run_id


async def test_the_workflow_moves_a_corpus_across_continue_as_new_and_leaves_reclaim_apart(
    scratch: Scratch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One activity per run forces `continue_as_new`; reclaim is a second request."""
    await _deployed_before(scratch, tmp_path, monkeypatch)
    await _write_chunks(5)
    await _write_notes(3)
    notes_dir = tmp_path / "knowledge"
    notes_dir.mkdir()
    for i in range(3):
        (notes_dir / f"n{i}.md").write_text(
            f"---\nid: n{i}\ntype: reaction\n---\nsolvent screen {i} heptane\n", encoding="utf-8"
        )
    monkeypatch.setattr(settings, "embedding_dim", 768)
    monkeypatch.setattr(settings, "note_repo_dir", str(tmp_path))
    monkeypatch.setattr(settings, "knowledge_dir", "knowledge")
    monkeypatch.setattr(settings, "document_reembed_batch_size", 2)
    monkeypatch.setattr(settings, "document_sync_max_iterations", 1)
    clear_embedding_cache()
    await migrate(scratch.dsn)
    _use_shares(monkeypatch)

    async with await start_env_or_skip() as env:
        client = pydantic_client(env)
        outcome, continued = await _run_workflow(client, "reembed-test")
        assert continued, "the run never continued as new"
        assert outcome.complete
        assert (outcome.documents_embedded, outcome.notes_indexed) == (5, 3)
        assert not outcome.notes_skipped
        assert outcome.vectors_cleared == 0, "an ordinary run emptied the vectors a rollback reads"
        assert outcome.remaining.model_dump() == {"documents": 0, "notes": 0}
        assert await scratch.afetch(
            "SELECT count(*) FROM document_chunks WHERE embedding IS NOT NULL"
        ) == [(5,)]

        reclaimed, continued = await _run_workflow(
            client, "reclaim-test", reembed.ReembedRequest(reclaim=True)
        )
        assert continued and reclaimed.complete
        assert (reclaimed.documents_embedded, reclaimed.vectors_cleared) == (0, 8)
        assert await scratch.afetch(
            "SELECT count(*) FROM document_chunks WHERE embedding IS NOT NULL"
        ) == [(0,)]


async def test_the_plan_refuses_a_run_whose_column_the_migrator_has_not_built(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing column would fail every batch alike, so the plan says so once, with the remedy."""
    await migrate(scratch.dsn)
    monkeypatch.setattr(settings, "embedding_dim", 768)
    with pytest.raises(
        Exception, match="embedding_768 does not exist at width 768.*make db-migrate"
    ):
        await ActivityEnvironment().run(reembed.plan_embedding_reembed)


# --- a chunk that will not embed -----------------------------------------------------------------


def _refuse_poison(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the embedder refuse any text mentioning `POISON`, one at a time as in a batch."""
    real = embed_texts

    def refusing(texts: list[str], *, cache: bool = True) -> list[list[float]]:
        if any("POISON" in text for text in texts):
            raise ValueError("the gateway refused this text")
        return real(texts, cache=cache)

    monkeypatch.setattr(sync_module, "embed_texts", refusing)


async def _write_text_chunks(texts: list[str]) -> None:
    """Index one single-chunk document per text under the live configuration."""
    vectors = embed_texts(texts, cache=False)
    await PostgresDocumentIndex().upsert(
        [
            FileRecord(
                path=f"q{i}.txt",
                source=_SOURCE,
                doc_id=f"q{i:03d}",
                fingerprint="1:1",
                chunking_key=_CHUNKING,
            )
            for i in range(len(texts))
        ],
        [
            ChunkRecord(
                doc_id=f"q{i:03d}",
                chunking_key=_CHUNKING,
                ordinal=0,
                content=text,
                embedding=vector,
            )
            for i, (text, vector) in enumerate(zip(texts, vectors, strict=True))
        ],
        embedding_config_key(),
    )


async def test_poison_chunks_wider_than_a_batch_are_passed_once_and_counted(
    scratch: Scratch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Five unembeddable chunks sort first and fill more than two batches; the rest still move.

    A scan that started from the first stale row each time would meet them forever. The walk passes
    each once, reports them as failed and ends with them still counted as not under the live key.
    """
    await migrate(scratch.dsn)
    await _write_text_chunks([f"POISON {i}" for i in range(5)] + [f"good {i}" for i in range(4)])
    monkeypatch.setattr(settings, "embedding_dim", 768)
    monkeypatch.setattr(settings, "note_repo_dir", str(tmp_path))
    monkeypatch.setattr(settings, "knowledge_dir", "knowledge")
    (tmp_path / "knowledge").mkdir()
    monkeypatch.setattr(settings, "document_reembed_batch_size", 2)
    clear_embedding_cache()
    await migrate(scratch.dsn)
    _use_shares(monkeypatch)
    _refuse_poison(monkeypatch)

    async with await start_env_or_skip() as env:
        outcome, _ = await _run_workflow(pydantic_client(env), "poison-test")

    assert (outcome.documents_embedded, outcome.documents_failed) == (4, 5)
    assert not outcome.stalled, "two failed batches of three were read as the endpoint being down"
    assert outcome.remaining.documents == 5 and not outcome.complete
    assert "5 chunk(s) would not embed" in cli_reembed.summarise(outcome)


async def test_batches_that_all_fail_in_a_row_stop_the_run_as_stalled(
    scratch: Scratch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the endpoint refusing everything the run stops after its failure budget."""
    await migrate(scratch.dsn)
    await _write_text_chunks([f"POISON {i}" for i in range(12)])
    monkeypatch.setattr(settings, "embedding_dim", 768)
    monkeypatch.setattr(settings, "note_repo_dir", str(tmp_path))
    monkeypatch.setattr(settings, "knowledge_dir", "knowledge")
    (tmp_path / "knowledge").mkdir()
    monkeypatch.setattr(settings, "document_reembed_batch_size", 2)
    monkeypatch.setattr(settings, "document_reembed_max_failed_batches", 3)
    clear_embedding_cache()
    await migrate(scratch.dsn)
    _use_shares(monkeypatch)
    _refuse_poison(monkeypatch)

    async with await start_env_or_skip() as env:
        outcome, _ = await _run_workflow(pydantic_client(env), "stall-test")

    assert outcome.stalled and (outcome.documents_embedded, outcome.documents_failed) == (0, 6)
    assert outcome.remaining.documents == 12
    assert "stopped early" in cli_reembed.summarise(outcome)


# --- the CLI ---------------------------------------------------------------------------------


async def test_the_cli_rejoins_a_running_re_embed_and_reads_its_verdict(
    scratch: Scratch,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A second start finds the first running and waits for it, reading the verdict as a model.

    Without the handle's `result_type` the converter returns a dict and the verdict raises after
    the CLI has waited the whole run out.
    """
    await migrate(scratch.dsn)
    monkeypatch.setattr(settings, "note_repo_dir", str(tmp_path))
    monkeypatch.setattr(settings, "knowledge_dir", "knowledge")
    (tmp_path / "knowledge").mkdir()
    _use_shares(monkeypatch)
    gate = asyncio.Event()

    @activity.defn(name="reembed_document_batch")
    async def held_batch(after: ChunkCursor | None = None) -> ReembedReport:
        await gate.wait()
        return ReembedReport(embedded=3)

    async with await start_local_env_or_skip() as env:
        client = pydantic_client(env)

        async def _connect() -> Any:
            return client

        monkeypatch.setattr(cli_reembed, "connect", _connect)
        async with Worker(
            client,
            task_queue=settings.background_task_queue,
            workflows=[reembed.EmbeddingReembedWorkflow],
            activities=[
                reembed.plan_embedding_reembed,
                held_batch,
                reindex_notes_activity,
                reembed.clear_superseded_vectors,
                reembed.count_unembedded,
            ],
        ):
            first = await client.start_workflow(
                reembed.EmbeddingReembedWorkflow.run,
                reembed.ReembedRequest(),
                id=cli_reembed.WORKFLOW_ID,
                task_queue=settings.background_task_queue,
            )
            second = asyncio.create_task(cli_reembed.run())
            await asyncio.sleep(1.0)
            assert not second.done(), "the second start did not wait for the first"
            gate.set()
            summary, code = await asyncio.wait_for(second, 60)
            await first.result()

    assert "rejoining embedding-reembed" in capsys.readouterr().out
    assert code == 0, summary
    assert "documents embedded 3" in summary


async def test_the_cli_reports_a_refusal_and_a_missing_column_as_exit_two(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan refuses a width with no column; the CLI names the cause and exits 2."""
    await migrate(scratch.dsn)
    monkeypatch.setattr(settings, "embedding_dim", 768)
    _use_shares(monkeypatch)
    async with await start_env_or_skip() as env:
        client = pydantic_client(env)

        async def _connect() -> Any:
            return client

        monkeypatch.setattr(cli_reembed, "connect", _connect)
        async with Worker(
            client,
            task_queue=settings.background_task_queue,
            workflows=[reembed.EmbeddingReembedWorkflow],
            activities=[reembed.plan_embedding_reembed],
        ):
            summary, code = await cli_reembed.run()
    assert code == 2 and "make db-migrate" in summary


async def test_the_cli_status_names_the_columns_and_the_other_configurations(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--status` needs no Temporal, and inspects nothing where pgvector holds no vectors."""
    await migrate(scratch.dsn)
    await _write_notes(2)
    await scratch.afetch(
        "UPDATE note_index SET embedding_key = 'openai_compatible:ep-abc:d768:other|ntv3' "
        "WHERE note_id = 'n0'"
    )
    _use_shares(monkeypatch)
    report = await cli_reembed.status()
    assert report.splitlines()[0].startswith("configured: hash:")
    assert (
        "note_index: embedding vector(1536); other configurations: openai_compatible/other d768"
        in report
    )
    assert "document_chunks: embedding vector(1536); other configurations: none" in report

    monkeypatch.setattr(settings, "vector_store_provider", "qdrant")
    elsewhere = await cli_reembed.status()
    assert "not in Postgres" in elsewhere and "note_index" not in elsewhere


async def test_mixed_means_the_same_to_readyz_and_the_job(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows of a share nobody enables keep their old key and are not a mixed corpus.

    The job converges only the cuttings the enabled shares use, so the probe must not report the
    others as pending forever.
    """
    await migrate(scratch.dsn)
    await _write_chunks(3)
    foreign = "openai_compatible:ep-abc123:d1536:text-embed-v1"
    await scratch.afetch(
        "UPDATE document_chunks SET chunking_key = 'disabled:1', embedding_key = %s "
        "WHERE doc_id = 'd1'",
        (foreign,),
    )
    live = live_embedding_keys()
    async with await connect(scratch.dsn) as conn:
        everything = {s.table: s for s in await inspect_tables(conn, 1536, live)}
        enabled = {s.table: s for s in await inspect_tables(conn, 1536, live, {_CHUNKING})}
        disabled_too = {
            s.table: s for s in await inspect_tables(conn, 1536, live, {_CHUNKING, "disabled:1"})
        }
    assert everything["document_chunks"].mixed, "the unscoped reading sees the disabled share's row"
    assert not enabled["document_chunks"].mixed
    assert disabled_too["document_chunks"].mixed

    _use_shares(monkeypatch)
    count = await PostgresDocumentIndex().count_stale(embedding_config_key(), {_CHUNKING})
    assert count == 0, "the job's own count disagrees with the probe"


# --- the probe beside a migration's lock ----------------------------------------------------


async def test_a_lock_on_the_vector_tables_does_not_make_the_database_unreachable(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With `document_chunks` held `ACCESS EXCLUSIVE`, the probe answers ready, verdict kept.

    A migration building an index on a large table holds exactly this lock; a probe that waited on
    it would time out and drain every new pod as `database unreachable`.
    """
    await migrate(scratch.dsn)
    monkeypatch.setattr(settings, "service_readiness_db_timeout_seconds", 2.0)
    front: Any = SimpleNamespace(
        database_reachable=False,
        schema_current=False,
        database_probed_at=0.0,
        vector_width_problem="",
        embedding_corpus_mixed=True,
    )
    async with await connect(scratch.dsn) as holder:
        await holder.execute("LOCK TABLE document_chunks IN ACCESS EXCLUSIVE MODE")
        started = time.monotonic()
        ready = await ops._probe_database(front)
        elapsed = time.monotonic() - started
        await holder.rollback()

    assert ready, "a locked vector table made the pod unready"
    assert front.database_reachable and front.schema_current
    assert front.embedding_corpus_mixed, (
        "the last verdict was lost when the question went unanswered"
    )
    assert elapsed < 2.0, f"the probe waited {elapsed:.1f}s on a lock"

    ready = await ops._probe_database(front)
    assert ready and not front.embedding_corpus_mixed, "an answerable probe kept a stale verdict"


# --- real processes against one database -----------------------------------------------------


def _spawn(scratch: Scratch, log: Path, *, hold: float, batch: int) -> subprocess.Popen[str]:
    """One re-embed pass in its own process, logging each batch it embeds *before* it writes it."""
    env = {
        **os.environ,
        "CHEMCLAW_POSTGRES_DSN": scratch.dsn,
        "CHEMCLAW_POSTGRES_MIGRATION_DSN": scratch.dsn,
        "CHEMCLAW_SESSION_STORE_DSN": scratch.dsn,
        "CHEMCLAW_EMBEDDING_PROVIDER": "hash",
        "CHEMCLAW_EMBEDDING_DIM": "768",
    }
    return subprocess.Popen(
        [sys.executable, "-m", "tests.reembed_probe", str(log), str(hold), str(batch), _CHUNKING],
        env=env,
        cwd=str(Path(__file__).resolve().parents[1]),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _embedding(log: Path) -> list[list[str]]:
    """The batches a probe has read and begun to embed."""
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line]


async def _stale_corpus(scratch: Scratch, count: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """`count` chunks embedded at 1536, then the deployment moved to 768 and migrated."""
    await migrate(scratch.dsn)
    await _write_chunks(count)
    monkeypatch.setattr(settings, "embedding_dim", 768)
    clear_embedding_cache()
    await migrate(scratch.dsn)


async def _wait_for(condition: Callable[[], Awaitable[bool]], seconds: float = 90.0) -> None:
    """Poll `await condition()` until true; the probes need a few seconds to import and connect."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if await condition():
            return
        await asyncio.sleep(0.2)
    raise AssertionError("timed out waiting")


async def _no_row_is_locked(scratch: Scratch) -> bool:
    """Whether a `NOWAIT` lock on every chunk row is granted, i.e. nothing holds one."""
    try:
        async with await connect(scratch.dsn) as conn:
            await conn.execute("SELECT 1 FROM document_chunks FOR UPDATE NOWAIT")
            await conn.rollback()
    except psycopg.errors.LockNotAvailable:
        return False
    return True


async def _live_rows(scratch: Scratch) -> list[tuple[Any, ...]]:
    return await scratch.afetch(
        "SELECT doc_id, content FROM document_chunks "
        "WHERE embedding_768 IS NOT NULL AND embedding_key = %s ORDER BY 1",
        (embedding_config_key(),),
    )


async def test_the_job_holds_no_lock_while_the_gateway_answers_so_every_other_writer_proceeds(
    scratch: Scratch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a re-embed mid-call, the scheduled pass, the crawl's upsert and its prune all finish.

    The probe process holds a batch in its embedding call for twelve seconds. Meanwhile, here:
    the scheduled sync's own pass re-embeds other chunks, a re-chunked document is upserted over a
    chunk the probe read, and another is pruned. None waits; and once the probe's write lands it
    skips the rows that moved under it, so the upserted text keeps its own vector and the pruned
    document stays gone.
    """
    await _stale_corpus(scratch, 24, monkeypatch)
    index = PostgresDocumentIndex()
    log = tmp_path / "job.log"
    job = _spawn(scratch, log, hold=12.0, batch=8)

    async def _reading() -> bool:
        return bool(_embedding(log))

    await _wait_for(_reading)
    read = _embedding(log)[0]
    assert len(read) == 8
    assert await _no_row_is_locked(scratch), "the job holds row locks across the embedding call"

    # The scheduled pass: the other chunks, re-embedded without waiting for the job.
    started = time.monotonic()
    scheduled = await reembed_stale(index, {_CHUNKING}, 100)
    assert time.monotonic() - started < 6, "the scheduled pass waited on the job"
    assert scheduled.embedded == 24, "the scheduled pass takes whatever is stale, as designed"

    # The crawl: d0 is re-cut with new text (under the live key), d3 is pruned.
    new_text = "crystallisation report REVISED isopropanol"
    vector = embed_texts([new_text], cache=False)[0]
    started = time.monotonic()
    await index.upsert(
        [
            FileRecord(
                path="p0.txt",
                source=_SOURCE,
                doc_id="d0",
                fingerprint="2:2",
                chunking_key=_CHUNKING,
            )
        ],
        [
            ChunkRecord(
                doc_id="d0", chunking_key=_CHUNKING, ordinal=0, content=new_text, embedding=vector
            )
        ],
        embedding_config_key(),
    )
    mark = await index.clock()
    await index.touch(_SOURCE, [f"p{i}.txt" for i in range(24) if i != 3])
    assert await index.prune_stale(_SOURCE, mark) == 1
    assert time.monotonic() - started < 6, "the crawl waited on the job"

    # The job's embedding call returns: it writes what it read, and every write finds its row moved.
    output, _ = await asyncio.to_thread(job.communicate, None, 120)
    assert job.returncode == 0, output

    rows = dict(await _live_rows(scratch))
    assert rows["d0"] == new_text
    literal = "[" + ",".join(repr(x) for x in vector) + "]"
    assert await scratch.afetch(
        "SELECT embedding_768 = %s::vector(768) FROM document_chunks WHERE doc_id = 'd0'",
        (literal,),
    ) == [(True,)], "the old text's vector was written over the re-cut text's"
    assert "d3" not in rows, "a pruned document came back"
    assert len(rows) == 23
    assert await scratch.afetch(
        "SELECT count(*) FROM document_chunks WHERE embedding_key IS DISTINCT FROM %s",
        (embedding_config_key(),),
    ) == [(0,)]


async def test_a_write_that_lost_the_race_skips_the_row_and_is_not_counted_against_it(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`store_embeddings` is conditional on the row still being stale, in the same statement."""
    await _stale_corpus(scratch, 3, monkeypatch)
    index = PostgresDocumentIndex()
    stale = await index.stale_chunks(embedding_config_key(), 10, {_CHUNKING})
    assert len(stale) == 3
    winner = embed_texts(["the winner"], cache=False)[0]
    await index.store_embeddings(
        [
            ChunkRecord(
                doc_id="d1", chunking_key=_CHUNKING, ordinal=0, content="x", embedding=winner
            )
        ],
        embedding_config_key(),
    )
    loser = embed_texts(["the loser"], cache=False)[0]
    await index.store_embeddings(
        [
            ChunkRecord(
                doc_id=c.doc_id,
                chunking_key=c.chunking_key,
                ordinal=c.ordinal,
                content=c.content,
                embedding=loser,
            )
            for c in stale
        ],
        embedding_config_key(),
    )
    vectors = {
        row[0]: row[1]
        for row in await scratch.afetch("SELECT doc_id, embedding_768::text FROM document_chunks")
    }
    assert vectors["d0"] == vectors["d2"], "the rows that were still stale did not take the write"
    assert vectors["d1"] != vectors["d0"], "the loser overwrote the winner's row"


async def test_a_process_killed_mid_batch_changes_nothing_and_the_next_one_finishes(
    scratch: Scratch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGKILL mid-embed: no row moved, no lock left, and the next process finishes the work."""
    await _stale_corpus(scratch, 12, monkeypatch)
    doomed_log = tmp_path / "doomed.log"
    doomed = _spawn(scratch, doomed_log, hold=600.0, batch=5)

    async def _reading() -> bool:
        return bool(_embedding(doomed_log))

    await _wait_for(_reading)
    assert len(_embedding(doomed_log)[0]) == 5
    assert await _no_row_is_locked(scratch), "the process holds row locks while it waits"

    doomed.kill()
    doomed.communicate(timeout=30)
    assert await _no_row_is_locked(scratch)
    assert await scratch.afetch(
        "SELECT count(*) FROM document_chunks WHERE embedding_768 IS NOT NULL"
    ) == [(0,)]

    survivor_log = tmp_path / "survivor.log"
    survivor = _spawn(scratch, survivor_log, hold=0.0, batch=5)
    output, _ = await asyncio.to_thread(survivor.communicate, None, 180)
    assert survivor.returncode == 0, output
    embedded = [text for batch in _embedding(survivor_log) for text in batch]
    assert len(embedded) == len(set(embedded)) == 12, "the survivor did not embed each chunk once"
    assert len(await _live_rows(scratch)) == 12


async def test_two_walks_at_once_both_finish_and_the_corpus_is_whole(
    scratch: Scratch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two processes walking one table may embed a chunk twice; neither blocks, none is lost."""
    await _stale_corpus(scratch, 36, monkeypatch)
    first = _spawn(scratch, tmp_path / "a.log", hold=0.5, batch=4)
    second = _spawn(scratch, tmp_path / "b.log", hold=0.5, batch=4)
    outputs = [await asyncio.to_thread(p.communicate, None, 180) for p in (first, second)]
    assert (first.returncode, second.returncode) == (0, 0), outputs
    assert len(await _live_rows(scratch)) == 36
    seen = {t for log in ("a.log", "b.log") for batch in _embedding(tmp_path / log) for t in batch}
    assert len(seen) == 36, "a chunk was never embedded"


async def test_reembed_after_in_process_is_idempotent_and_walks_past_what_it_has_read(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run to exhaustion twice: the second walk embeds nothing and says nothing is left."""
    await _stale_corpus(scratch, 5, monkeypatch)
    index = PostgresDocumentIndex()
    first = await reembed_after(index, {_CHUNKING}, 3, None)
    assert (first.embedded, first.has_more) == (3, True) and first.cursor is not None
    rest = await reembed_after(index, {_CHUNKING}, 3, first.cursor)
    assert (rest.embedded, rest.has_more) == (2, False)
    again = await reembed_after(index, {_CHUNKING}, 3, None)
    assert (again.embedded, again.has_more, again.cursor) == (0, False, None)
