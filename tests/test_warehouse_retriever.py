"""The retrieve half: ANN pushed into the warehouse, and the rule against double-counting.

The interesting assertions here are not "it returns rows". They are that the search is *ranked and
truncated by the warehouse* rather than locally, that a reaction which already became a reviewed
note does not also arrive as a raw row, and that an unreachable warehouse costs this leg of the
fan-out and nothing else.
"""

import asyncio
import time
from typing import Any

import pytest

import chemclaw.ingest.eln.warehouse.retriever as retriever_module
from chemclaw.core.config import settings
from chemclaw.core.embeddings import embed_texts
from chemclaw.ingest.eln.records import InMemoryReactionRecordStore, ReactionRecord
from chemclaw.ingest.eln.warehouse.binding import BindingError
from chemclaw.ingest.eln.warehouse.databricks import DatabricksVectorDialect
from chemclaw.ingest.eln.warehouse.retriever import WarehouseVectorRetriever
from tests import warehouse_fake

_DRIVER = "tests.warehouse_fake:open_fake"


def _binding(**vector: Any) -> dict[str, Any]:
    """A retrieve-only binding; `vector` overrides individual keys of its `vector:` section."""
    section: dict[str, Any] = {
        "relation": "V_EMBEDDING",
        "key": "REACTION_ID",
        "vector_column": "REACTION_VECTOR",
        "content_columns": ["REACTION_SMILES", "PROTOCOL_TEXT"],
        "filter_columns": {"tag": "PROJECT_CODE"},
    }
    section.update(vector)
    return {"connection": {"driver": _DRIVER}, "vector": section}


def _hits() -> dict[str, list[dict[str, Any]]]:
    """Two ranked rows, as the warehouse would return them."""
    return {
        "V_EMBEDDING": [
            {
                "REACTION_ID": "RX-1",
                "REACTION_SMILES": "CC(=O)O>>CCOC(C)=O",
                "PROTOCOL_TEXT": "Charge the acid, add ethanol, reflux 90 min.",
                "CHEMCLAW_SCORE": 0.91,
            },
            {
                "REACTION_ID": "RX-2",
                "REACTION_SMILES": "CCO>>CC=O",
                "PROTOCOL_TEXT": "Oxidise the alcohol.",
                "CHEMCLAW_SCORE": 0.62,
            },
        ]
    }


def _retrieve(
    binding: dict[str, Any], tables: dict[str, list[dict[str, Any]]], filters: Any = None
) -> Any:
    """Prime the fake and run one retrieval."""
    warehouse_fake.prime(**tables)
    retriever = WarehouseVectorRetriever(binding=binding, name="eln-warehouse")
    return asyncio.run(retriever.retrieve("ester formation", filters or {}))


def _ingested(monkeypatch: pytest.MonkeyPatch, *reaction_ids: str) -> None:
    """Point the retriever's suppression check at a corpus holding exactly `reaction_ids`.

    The check asks the transcription store since D-2026-08-25, not the filesystem, so seeding a
    `knowledge/reaction/*.md` file — what these tests used to do — now proves nothing at all: that
    is precisely how `suppress_ingested` became a silent no-op.
    """
    store = InMemoryReactionRecordStore()
    asyncio.run(
        store.record(
            [
                ReactionRecord(reaction_id=rid, body=f"body {rid}", source="eln:test")
                for rid in reaction_ids
            ],
            "eln-json",
        )
    )
    monkeypatch.setattr(
        "chemclaw.ingest.eln.warehouse.retriever.default_record_store", lambda: store
    )


def _primed() -> warehouse_fake.FakeWarehouse:
    """The warehouse the last retrieval actually used."""
    assert warehouse_fake.NEXT is not None
    return warehouse_fake.NEXT


def test_the_warehouse_ranks_and_truncates_rather_than_this_process() -> None:
    """The similarity, the ordering and the limit are all in the statement.

    This is the whole reason the half exists: the embedding column is already there over a corpus
    larger than what gets ingested, and pulling rows out to score them here would defeat it.
    """
    _retrieve(_binding(), _hits())
    statement, params = _primed().executed[0]

    assert "FAKE_COSINE_SIMILARITY(REACTION_VECTOR, ?) AS CHEMCLAW_SCORE" in statement
    assert "ORDER BY CHEMCLAW_SCORE DESC" in statement
    assert statement.rstrip().endswith("LIMIT ?")
    assert params[-1] == settings.retrieval_top_k
    assert isinstance(params[0], list), "the query embedding is bound, never inlined"


def test_the_shipped_driver_s_own_spelling_reaches_the_statement() -> None:
    """The dialect and the statement builder meet, with the vendor's real function name.

    Every other assertion in this file goes through `FakeVectorDialect`, deliberately: `sql.py`
    claims to contribute structure and no vendor's words, and a fake dialect is what can prove that.
    This one closes the other half — that the *shipped* driver's spelling and its parameter
    encoding survive the trip — so neither claim rests on the other's fixture.

    Databricks binds the query vector as one JSON scalar, because there is no array parameter type;
    the assertion on `params[0]` is what would catch a regression to a bound list, which fails only
    at the server.
    """
    warehouse_fake.prime(**_hits())
    assert warehouse_fake.NEXT is not None
    warehouse_fake.NEXT._vector_dialect = DatabricksVectorDialect()
    retriever = WarehouseVectorRetriever(binding=_binding(), name="eln-warehouse")
    asyncio.run(retriever.retrieve("ester formation", {}))
    statement, params = _primed().executed[0]

    assert "vector_cosine_similarity(REACTION_VECTOR, from_json(?, 'ARRAY<FLOAT>'))" in statement
    assert "ORDER BY CHEMCLAW_SCORE DESC" in statement
    assert isinstance(params[0], str) and params[0].startswith("["), (
        "the vector is bound as a JSON scalar, not inlined and not bound as a list"
    )


def test_a_distance_metric_sorts_the_other_way() -> None:
    """A distance and a similarity differ; pairing them wrongly inverts the whole result set."""
    _retrieve(_binding(metric="l2"), _hits())
    statement, _ = _primed().executed[0]

    assert "FAKE_L2_DISTANCE(" in statement
    assert "ORDER BY CHEMCLAW_SCORE ASC" in statement


def test_chunks_cite_the_row_because_there_is_no_note_to_cite() -> None:
    """A citation must resolve to something a reader can check — here, the warehouse row."""
    chunks = _retrieve(_binding(), _hits())

    assert [c.source_note_id for c in chunks] == ["eln-warehouse:RX-1", "eln-warehouse:RX-2"]
    assert all(c.retriever == "eln-warehouse" for c in chunks)
    assert chunks[0].score > chunks[1].score
    assert "REACTION_SMILES: CC(=O)O>>CCOC(C)=O" in chunks[0].content
    assert "PROTOCOL_TEXT: Charge the acid" in chunks[0].content


def test_a_reaction_already_ingested_is_not_surfaced_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rule that lets one ELN carry both halves without double-counting.

    A curated reaction reaching the agent once as an ingested record and again as a raw warehouse
    row would look like two independent sources agreeing.

    This asked the *filesystem* until D-2026-08-25 made a transcription a row: it stat'd
    `knowledge/reaction/reaction-<key>.md`, which ingestion stopped writing, so the check answered
    `False` forever and the suppression silently stopped happening. Asserted against the store now,
    which is where the answer lives.
    """
    _ingested(monkeypatch, "RX-1")
    chunks = _retrieve(_binding(), _hits())
    assert [c.source_note_id for c in chunks] == ["eln-warehouse:RX-2"]


def test_a_traversal_shaped_row_key_suppresses_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A warehouse-controlled key must never decide a suppression it has no record for.

    The key used to land in a path by string join, so `../../` walked out of the graph and could
    stat any file that happened to exist — hiding evidence, which is the failure that matters here
    rather than disclosure. There is no path any more, and the property is asserted against the
    shape rather than the mechanism so it survives the next storage change too: a key the slug rule
    rejects cannot be a record id, so it is filtered out before the query and suppresses nothing.
    """
    _ingested(monkeypatch)
    hits = _hits()
    hits["V_EMBEDDING"][0]["REACTION_ID"] = "../../../../outside/reaction-escaped"
    chunks = _retrieve(_binding(), hits)

    assert len(chunks) == 2, "a key that cannot be a record id has no record, so nothing is dropped"


def test_suppression_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment that ingests nothing has no duplicates to suppress and pays nothing for it."""
    _ingested(monkeypatch, "RX-1")

    chunks = _retrieve(_binding(suppress_ingested=False), _hits())
    assert len(chunks) == 2


def test_declared_filters_reach_the_statement_and_undeclared_ones_are_ignored() -> None:
    """`tag` is mapped onto the site's column; `type` is not mapped and is not guessed at."""
    _retrieve(_binding(), _hits(), {"tag": "PRJ-7", "type": "reaction"})
    statement, params = _primed().executed[0]

    assert "PROJECT_CODE = ?" in statement
    assert "PRJ-7" in params
    assert "reaction" not in params


def test_an_unreachable_warehouse_costs_this_leg_and_no_other() -> None:
    """One failed retriever must not take down a question the other sources could still answer."""
    warehouse_fake.prime(**_hits())
    _primed().fail_with = ConnectionError("warehouse down")
    retriever = WarehouseVectorRetriever(binding=_binding(), name="eln-warehouse")

    assert asyncio.run(retriever.retrieve("ester formation", {})) == []


def test_an_empty_query_asks_the_warehouse_nothing() -> None:
    """A blank query would return an arbitrary top-k and bill for the scan that produced it."""
    chunks = _retrieve(_binding(), _hits())
    assert chunks, "sanity: a real query does reach the warehouse"

    warehouse_fake.prime(**_hits())
    retriever = WarehouseVectorRetriever(binding=_binding(), name="eln-warehouse")
    assert asyncio.run(retriever.retrieve("   ", {})) == []
    assert _primed().executed == []


def test_server_side_embedding_binds_the_query_text_instead_of_a_vector() -> None:
    """When the warehouse owns the model, the text goes over and the vector never does."""
    binding = _binding(
        embedding="server",
        server_embed_function="ai_query",
        server_embed_model="e5-base-v2",
    )
    _retrieve(binding, _hits())
    statement, params = _primed().executed[0]

    assert "ai_query(?, ?)" in statement
    assert params[0] == "e5-base-v2"
    assert params[1] == "ester formation"
    assert not any(isinstance(p, list) for p in params), "no local embedding was computed"


def test_a_misconfigured_source_costs_this_leg_and_no_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A driver the image does not carry must not fail every question in the process.

    `gather_evidence` fans the retrievers out with a plain `asyncio.gather`, so a raising leg does
    not degrade an answer — it loses it. The deployment error still has to be loud, but in the log,
    not in every chemist's next question.
    """
    binding = _binding()
    binding["connection"] = {"driver": "chemclaw.ingest.eln.warehouse.no_such_driver:Nope"}
    retriever = WarehouseVectorRetriever(binding=binding, name="eln-warehouse")

    assert asyncio.run(retriever.retrieve("ester formation", {})) == []


class _ProviderError(Exception):
    """What an embedding client raises — none of the types this retriever used to catch."""


def test_an_embedding_provider_failure_costs_this_leg_and_no_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The query is embedded *inside* this leg, so the provider's own errors are this leg's too.

    They are not `WarehouseQueryError`, `ConnectionError` or `OSError`, so before this they escaped
    into `gather_evidence`'s `gather` — which has no `return_exceptions` — and a rate-limited
    embedding endpoint failed the whole turn, including the answer the knowledge graph had already
    produced.
    """
    monkeypatch.setattr(
        retriever_module, "embed_texts", lambda texts: (_ for _ in ()).throw(_ProviderError("429"))
    )
    warehouse_fake.prime(**_hits())
    retriever = WarehouseVectorRetriever(binding=_binding(), name="eln-warehouse")

    assert asyncio.run(retriever.retrieve("ester formation", {})) == []


def test_the_query_is_embedded_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blocking provider call must not freeze the loop that serves every other SSE stream.

    Counted rather than asserted qualitatively: a free loop ticks continuously through a 0.4 s
    provider call, and an inline call yields zero ticks in the same window.
    """
    real = embed_texts

    def _slow(texts: list[str]) -> list[list[float]]:
        time.sleep(0.4)
        return real(texts)

    monkeypatch.setattr(retriever_module, "embed_texts", _slow)
    warehouse_fake.prime(**_hits())
    retriever = WarehouseVectorRetriever(binding=_binding(), name="eln-warehouse")

    async def _run() -> int:
        ticks = 0

        async def _heartbeat() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        beat = asyncio.create_task(_heartbeat())
        await retriever.retrieve("ester formation", {})
        beat.cancel()
        return ticks

    assert asyncio.run(_run()) > 5, "the loop kept running while the provider was blocking"


def test_a_key_the_store_cannot_hold_costs_its_own_row_and_no_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The filter must not be stricter than the question it answers.

    Under the old path form, `resolve()` raised `ValueError` on an embedded NUL where `is_file()`
    merely answered `False`, so one unusable row raised into `retrieve()`'s backstop and returned
    `[]` for the **whole leg** — discarding every other legitimate hit. That is the same "hide
    evidence" outcome the check exists to prevent, reached from the other side.

    The failure mode survived the move to a store rather than being retired by it: Postgres text
    cannot hold a NUL, so *asking* about such a key raises out of the driver in exactly the same
    way. Pre-filtering by the slug rule is what keeps one bad row costing one row.
    """
    _ingested(monkeypatch)
    hits = _hits()
    hits["V_EMBEDDING"][0]["REACTION_ID"] = "RX-\x00-nul"
    chunks = _retrieve(_binding(), hits)

    assert len(chunks) == 2, "one unusable key must not zero the other rows in the same result"


def test_a_driver_with_no_similarity_dialect_refuses_the_vector_block() -> None:
    """A `vector:` block against a driver that cannot serve one fails loudly, not as bad SQL.

    How a warehouse spells a similarity search is a dialect fact and lives on the driver — see
    `VectorDialect` in `chemclaw.ingest.eln.warehouse.driver`. A driver answering `None` has no
    verified function to call, so the honest outcome is a `BindingError` naming the problem, not a
    statement built from another vendor's function name for the server to reject on first query.

    Checked at first use rather than at construction on purpose: resolving the driver means
    importing the vendor client, and a chat pod that builds retrieve halves at startup must not pay
    that import for a warehouse it may never query.
    """
    warehouse_fake.prime(**_hits())
    primed = _primed()
    primed._vector_dialect = None
    retriever = WarehouseVectorRetriever(binding=_binding(), name="eln-warehouse")

    with pytest.raises(BindingError, match="similarity-search dialect"):
        asyncio.run(retriever._search("ester formation", {}))
