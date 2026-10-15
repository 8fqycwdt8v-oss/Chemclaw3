"""The embedding width is configuration: columns built from a setting, a corpus that can be moved.

Against a real Postgres with pgvector, in a schema of its own per test so a database can be taken to
the state an existing deployment is in (every migration before the width one applied, rows written)
and migrated from there. What is proved:

* a deployment that keeps its width migrates to nothing and loses nothing;
* a deployment that changes width gets a column and an HNSW index beside the data, never a rewrite;
* a fresh database at a new width stores and ranks vectors;
* the HNSW index is what answers the dense query at the new width;
* a row stamped with another configuration is never ranked and is reported as a mixed corpus;
* the re-embed moves the corpus to the new column, empties the old one, and is idempotent, claimed
  (two real processes never embed one chunk twice) and resumable (a killed process changes nothing).
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
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Worker

import chemclaw.core.migrate as migrate_module
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
    ChunkRecord,
    DocumentFilter,
    FileRecord,
    PostgresDocumentIndex,
)
from chemclaw.ingest.documents.sync import reembed_claimed
from chemclaw.retrieval.vector_index import (
    NoteRecord,
    PostgresNoteIndex,
    note_embedding_key,
    reindex_notes,
)
from tests.pg import TEST_SCHEMA, create_test_schema, drop_test_schema, schema_dsn
from tests.temporal_env import pydantic_client, start_env_or_skip

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


async def _write_chunks(count: int, *, key: str | None = None) -> None:
    """Index `count` single-chunk documents under the live configuration (or `key`)."""
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
        key or embedding_config_key(),
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


async def test_the_re_embed_moves_a_corpus_to_a_new_width_and_empties_the_old_vectors(
    scratch: Scratch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 1536 deployment moves to 768: rows end under the new key and column, the old emptied."""
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
    batches = []
    while True:
        batch = await run(reembed.reembed_document_batch)
        batches.append(batch.embedded)
        if not batch.has_more:
            break
    assert batches == [4, 2]
    assert await reindex_notes(PostgresNoteIndex(), str(notes_dir)) == 6

    assert (await run(reembed.count_unembedded)).model_dump() == {"documents": 0, "notes": 0}
    assert await scratch.afetch(
        "SELECT count(*) FROM document_chunks WHERE embedding_768 IS NOT NULL"
    ) == [(6,)]
    # The original column still holds the old vectors until they are reclaimed, in batches.
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
    again = await run(reembed.reembed_document_batch)
    assert (again.embedded, again.has_more) == (0, False)
    assert await run(reembed.clear_superseded_vectors, plan) == 0


async def test_the_workflow_moves_a_corpus_across_continue_as_new_and_reports_what_is_left(
    scratch: Scratch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run as a real workflow with one activity per run, so it continues as new between batches."""
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
                id="reembed-test",
                task_queue="test-reembed",
            )
            outcome = await started.result()
            last_run = (await client.get_workflow_handle("reembed-test").describe()).run_id

    assert last_run != started.result_run_id, "the run never continued as new"
    assert outcome.complete
    assert (outcome.documents_embedded, outcome.notes_indexed) == (5, 3)
    assert outcome.vectors_cleared == 8
    assert outcome.remaining.model_dump() == {"documents": 0, "notes": 0}
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


def _claimed(log: Path) -> list[list[str]]:
    """The batches a probe has claimed and begun to embed."""
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


async def test_two_processes_re_embedding_at_once_never_embed_a_chunk_twice(
    scratch: Scratch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two real processes against one table take disjoint batches that together cover it."""
    await _stale_corpus(scratch, 48, monkeypatch)
    logs = [tmp_path / "a.log", tmp_path / "b.log"]
    # Two seconds a batch over twelve batches: far longer than the processes' start-up skew, so
    # both are working together for most of the run.
    first = _spawn(scratch, logs[0], hold=2.0, batch=4)
    second = _spawn(scratch, logs[1], hold=2.0, batch=4)
    outputs = [await asyncio.to_thread(p.communicate, None, 180) for p in (first, second)]
    assert (first.returncode, second.returncode) == (0, 0), outputs

    embedded = [text for log in logs for batch in _claimed(log) for text in batch]
    assert len(embedded) == len(set(embedded)) == 48, (
        "a chunk was embedded twice, or one was missed"
    )
    assert all(_claimed(log) for log in logs), "one process did all the work, so nothing overlapped"
    assert await scratch.afetch(
        "SELECT count(*) FROM document_chunks "
        "WHERE embedding_768 IS NOT NULL AND embedding_key = %s",
        (embedding_config_key(),),
    ) == [(48,)]


async def test_a_process_killed_mid_batch_changes_nothing_and_the_next_one_finishes(
    scratch: Scratch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGKILL mid-batch: no row moved, no lock left, and the next process finishes the work."""
    await _stale_corpus(scratch, 12, monkeypatch)
    doomed_log = tmp_path / "doomed.log"
    doomed = _spawn(scratch, doomed_log, hold=600.0, batch=5)

    async def _claim_is_held() -> bool:
        return bool(_claimed(doomed_log))

    await _wait_for(_claim_is_held)
    held = _claimed(doomed_log)[0]
    assert len(held) == 5

    # While it holds the claim, a peer skips those five rather than waiting for them.
    free = await scratch.afetch(
        "SELECT count(*) FROM (SELECT 1 FROM document_chunks WHERE embedding_key = %s "
        "FOR UPDATE SKIP LOCKED) AS unlocked",
        (embedding_config_key().replace("d768", "d1536"),),
    )
    assert free == [(12 - 5,)]

    doomed.kill()
    doomed.communicate(timeout=30)

    async def _locks_released() -> bool:
        rows = await scratch.afetch(
            "SELECT count(*) FROM (SELECT 1 FROM document_chunks FOR UPDATE SKIP LOCKED) AS u"
        )
        return rows == [(12,)]

    await _wait_for(_locks_released)
    assert await scratch.afetch(
        "SELECT count(*) FROM document_chunks WHERE embedding_768 IS NOT NULL"
    ) == [(0,)]

    survivor_log = tmp_path / "survivor.log"
    survivor = _spawn(scratch, survivor_log, hold=0.0, batch=5)
    output, _ = await asyncio.to_thread(survivor.communicate, None, 180)
    assert survivor.returncode == 0, output
    embedded = [text for batch in _claimed(survivor_log) for text in batch]
    assert len(embedded) == len(set(embedded)) == 12, "the survivor did not embed each chunk once"
    assert await scratch.afetch(
        "SELECT count(*) FROM document_chunks "
        "WHERE embedding_768 IS NOT NULL AND embedding_key = %s",
        (embedding_config_key(),),
    ) == [(12,)]


async def test_reembed_claimed_in_process_is_idempotent(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run to exhaustion twice: the second pass embeds nothing and says nothing is left."""
    await _stale_corpus(scratch, 5, monkeypatch)
    index = PostgresDocumentIndex()
    first = await reembed_claimed(index, {_CHUNKING}, 10)
    second = await reembed_claimed(index, {_CHUNKING}, 10)
    assert (first.embedded, first.has_more, second.embedded, second.has_more) == (
        5,
        False,
        0,
        False,
    )
