"""The pgvector HNSW recall knobs on the dense searches — off by default, transaction-local.

Three halves, for the three things that can be wrong with a knob. Offline: what the configuration
resolves to, including that the default resolves to *nothing* (no statement, no extra round trip,
and — because pgvector reserves the `hnsw.` prefix — nothing a pre-0.8 server would reject).
Server-backed: that the parameters actually land on the transaction running the search and are gone
again when it commits, which is the property that makes them safe under a shared connection pool.
And **that every dense search reads them at all** — which is where the knob was inert: it was
applied inside `PostgresNoteIndex.search_dense` only, while the residual
`settings.hnsw_ef_search` cites as its reason lives on the *document* index, whose dense path never
issued the statement. `test_the_document_dense_path_runs_under_the_configured_parameters` is that
gap, counted rather than asserted.

The server half needs a real pgvector, so it skips in the offline sandbox exactly as every other
Postgres-backed test here does.
"""

import asyncio

import psycopg
import pytest
from pydantic import ValidationError

from chemclaw.core import db
from chemclaw.core.config import Settings, settings
from chemclaw.core.db import vector_recall_settings
from chemclaw.core.embeddings import embed_texts, embedding_config_key
from chemclaw.ingest.documents.index import (
    ChunkRecord,
    DocumentFilter,
    FileRecord,
    PostgresDocumentIndex,
)
from chemclaw.retrieval.vector_index import NoteRecord, PostgresNoteIndex
from tests.pg import migrated_db_or_skip

# pgvector's own default `ef_search`. Asserted rather than read back from the server so the test
# states what "the knob is off" is supposed to leave in place, instead of agreeing with whatever it
# finds.
PGVECTOR_DEFAULT_EF_SEARCH = "40"


def test_the_recall_knobs_are_off_by_default() -> None:
    """With nothing configured, the dense path issues no session statement at all.

    The default has to be *silence*, not "the same values pgvector would have used": a deployment on
    the `pgvector >= 0.7` floor the fingerprint migrations state has no `hnsw.iterative_scan`, and
    the reserved `hnsw.` prefix makes setting an unknown parameter under it an error rather than an
    ignored placeholder.
    """
    assert settings.hnsw_ef_search == 0
    assert settings.hnsw_iterative_scan == "off"
    assert vector_recall_settings() == {}


def test_each_knob_is_emitted_only_when_it_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each configured knob contributes its parameter; the unset one contributes nothing."""
    monkeypatch.setattr(settings, "hnsw_ef_search", 200)
    assert vector_recall_settings() == {"hnsw.ef_search": "200"}

    monkeypatch.setattr(settings, "hnsw_ef_search", 0)
    monkeypatch.setattr(settings, "hnsw_iterative_scan", "relaxed_order")
    assert vector_recall_settings() == {"hnsw.iterative_scan": "relaxed_order"}

    monkeypatch.setattr(settings, "hnsw_ef_search", 100)
    assert vector_recall_settings() == {
        "hnsw.ef_search": "100",
        "hnsw.iterative_scan": "relaxed_order",
    }


def test_ef_search_has_a_documented_ceiling() -> None:
    """The setting refuses a value past the band where the planner abandons the index.

    Not pgvector's own maximum (1000): above roughly 200–400 the estimated cost of the HNSW scan
    can exceed a sequential scan, at which point a bigger `ef_search` buys the *opposite* of the
    recall it was set for. A knob whose upper range silently inverts its own purpose is a knob with
    a ceiling.
    """
    assert Settings(_env_file=None, hnsw_ef_search=400).hnsw_ef_search == 400  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, hnsw_ef_search=401)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, hnsw_ef_search=-1)  # type: ignore[call-arg]


def test_iterative_scan_only_accepts_pgvectors_own_modes() -> None:
    """`off`/`strict_order`/`relaxed_order` and nothing else — a typo must not reach the server."""
    for mode in ("off", "strict_order", "relaxed_order"):
        assert Settings(_env_file=None, hnsw_iterative_scan=mode).hnsw_iterative_scan == mode  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, hnsw_iterative_scan="strict")  # type: ignore[call-arg,arg-type]


def test_both_knobs_are_env_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator sets these the way they set every other setting — a `CHEMCLAW_` env var."""
    monkeypatch.setenv("CHEMCLAW_HNSW_EF_SEARCH", "128")
    monkeypatch.setenv("CHEMCLAW_HNSW_ITERATIVE_SCAN", "strict_order")
    configured = Settings(_env_file=None)  # type: ignore[call-arg]
    assert configured.hnsw_ef_search == 128
    assert configured.hnsw_iterative_scan == "strict_order"


def test_recall_parameters_are_transaction_local_on_a_shared_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The configured parameters hold for the search's transaction and are gone after it commits.

    Pinned to a one-connection pool so the second block is guaranteed the *same* backend as the
    first — otherwise "the parameters are gone" could be satisfied by simply getting a different
    connection, which proves nothing. This is the property that makes the knob safe to set at all:
    a session-level `SET` would leak one scoped query's widened candidate list onto every later
    borrower of that connection.
    """
    monkeypatch.setattr(settings, "pg_pool_min_size", 1)
    monkeypatch.setattr(settings, "pg_pool_max_size", 1)
    monkeypatch.setattr(settings, "hnsw_ef_search", 200)
    monkeypatch.setattr(settings, "hnsw_iterative_scan", "strict_order")

    async def _run() -> None:
        await migrated_db_or_skip()
        async with db.pooling():
            async with db.connection(settings.postgres_dsn) as conn:
                # Touch a vector value first: pgvector registers `hnsw.ef_search` when its library
                # is loaded into the backend, and until then the name is an empty placeholder — so
                # without this the "gone afterwards" read below would pass for the wrong reason.
                await conn.execute("SELECT '[1,0]'::vector <=> '[0,1]'::vector")
            async with db.connection(settings.postgres_dsn) as conn:
                async with conn.cursor() as cur:
                    await db.apply_vector_recall_settings(cur)
                    await cur.execute(
                        "SELECT current_setting('hnsw.ef_search'), "
                        "current_setting('hnsw.iterative_scan'), pg_backend_pid()"
                    )
                    applied = await cur.fetchone()
            assert applied is not None
            assert applied[0] == "200"
            assert applied[1] == "strict_order"
            async with db.connection(settings.postgres_dsn) as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT current_setting('hnsw.ef_search'), pg_backend_pid()")
                    after = await cur.fetchone()
            assert after is not None
            assert after[1] == applied[2], "not the same backend; the leak check proves nothing"
            assert after[0] == PGVECTOR_DEFAULT_EF_SEARCH

    asyncio.run(_run())


def test_dense_search_runs_under_the_configured_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scoped dense search still finds its note with both knobs turned on.

    The assertion that matters is not the hit — it is that the statement ran: pgvector reserves the
    `hnsw.` prefix, so a misspelled parameter name or an invalid mode raises rather than being
    ignored. This is therefore the test that would catch `hnsw.iterative_search`, a value of
    `strict`, or a `set_config` call that never made it into the search's own transaction.
    """
    monkeypatch.setattr(settings, "hnsw_ef_search", 100)
    monkeypatch.setattr(settings, "hnsw_iterative_scan", "relaxed_order")

    async def _run() -> None:
        await migrated_db_or_skip()
        async with db.connection(settings.postgres_dsn) as conn:
            await conn.execute("TRUNCATE note_index")
            await conn.commit()

        index = PostgresNoteIndex()
        near, far = await asyncio.to_thread(
            embed_texts, ["amide coupling epimerization", "amide coupling workup"]
        )
        await index.upsert(
            [
                NoteRecord(note_id="rxn-1", text="amide coupling epimerization", embedding=near),
                NoteRecord(note_id="rxn-2", text="amide coupling workup", embedding=far),
            ],
            embedding_config_key(),
        )
        (query,) = await asyncio.to_thread(embed_texts, ["amide coupling epimerization"])
        assert [h.note_id for h in await index.search_dense(query, top_k=1)] == ["rxn-1"]
        # The scoped shape is the one the knobs exist for: `within=` is a post-filter over the
        # candidate list, so this is the query that can come back short.
        scoped = await index.search_dense(query, top_k=1, within={"rxn-2"})
        assert [h.note_id for h in scoped] == ["rxn-2"]

    asyncio.run(_run())


def test_the_document_dense_path_runs_under_the_configured_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The document index's dense leg issues the recall statement; its lexical leg does not.

    **Counted, because the defect this pins was an omission and an omission has no wrong answer.**
    Both knobs were applied inside `PostgresNoteIndex.search_dense` alone, so the document index —
    the path `settings.hnsw_ef_search`'s own documentation names as the reason the knobs exist —
    ran every dense search under pgvector's defaults, and every possible assertion about its *hits*
    passed. What separates "wired up" from "inert" is whether the statement is sent at all, so that
    is what is counted: one on the dense leg, none on the lexical leg (whose `ts_rank` over a GIN
    index is exact and has no such parameter), and none anywhere when the knobs are off.
    """
    issued: list[str] = []
    real_execute = psycopg.AsyncCursor.execute

    async def _spy(self, query, params=None, **kwargs):  # type: ignore[no-untyped-def]
        issued.append(str(query))
        return await real_execute(self, query, params, **kwargs)

    monkeypatch.setattr(psycopg.AsyncCursor, "execute", _spy)

    async def _run() -> None:
        await migrated_db_or_skip()
        async with db.connection(settings.postgres_dsn) as conn:
            await conn.execute("TRUNCATE document_chunks, document_files")
            await conn.commit()

        index = PostgresDocumentIndex()
        (vector,) = await asyncio.to_thread(embed_texts, ["amide coupling with HATU"])
        await index.upsert(
            [
                FileRecord(
                    path="a.txt",
                    source="share",
                    doc_id="doc-a",
                    fingerprint="1:1",
                    chunking_key="chars-400",
                )
            ],
            [
                ChunkRecord(
                    doc_id="doc-a",
                    chunking_key="chars-400",
                    ordinal=0,
                    content="amide coupling with HATU",
                    embedding=vector,
                )
            ],
            embedding_config_key(),
        )

        def recall_statements() -> int:
            return sum(1 for statement in issued if "set_config" in statement)

        # Off (the shipped default): not one extra round trip, on either leg.
        issued.clear()
        assert await index.search_dense("share", vector, 5, DocumentFilter())
        assert await index.search_lexical("share", "amide coupling", 5, DocumentFilter())
        assert recall_statements() == 0

        monkeypatch.setattr(settings, "hnsw_ef_search", 200)
        monkeypatch.setattr(settings, "hnsw_iterative_scan", "strict_order")
        issued.clear()
        assert await index.search_dense("share", vector, 5, DocumentFilter())
        assert recall_statements() == 1, "the document dense path never read the knobs"
        issued.clear()
        assert await index.search_lexical("share", "amide coupling", 5, DocumentFilter())
        assert recall_statements() == 0, "an exact GIN scan has no recall parameter to set"

    asyncio.run(_run())
