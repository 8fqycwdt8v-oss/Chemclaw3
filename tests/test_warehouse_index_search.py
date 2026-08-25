"""An index-ranked warehouse source: rank in the store, resolve the keys in the warehouse.

The path Pistachio takes, and the reason it exists is arithmetic rather than taste. The scanned path
evaluates a similarity function per row, which is right for an ELN and a full scan of the corpus for
anything at patent scale. So the vectors move to a vector index and the relation is queried only to
turn the winning keys into text — the same division `ingest/documents/external_index.py` makes
between a store and a catalogue, with Databricks SQL standing in for Postgres.

Three properties carry that split, and each of them is a silent wrong answer when it breaks:

* the **store's ordering** is the ranking, and the resolve query has none of its own;
* **eligibility reaches the index before its top-k**, because filtering afterwards makes a narrow
  filter over a wide corpus return nothing at all; and
* a scope too large to send is **refused rather than truncated**, because a cut eligibility set is a
  wrong answer that reads as a thin corpus.
"""

import asyncio
import functools
from typing import Any

import pytest

from chemclaw.core.config import settings
from chemclaw.core.embeddings import embed_texts
from chemclaw.ingest.eln.warehouse.binding import BindingError, load_binding
from chemclaw.ingest.eln.warehouse.driver import WarehouseQueryError
from chemclaw.ingest.eln.warehouse.retriever import WarehouseVectorRetriever
from chemclaw.retrieval.vectors.base import VectorPoint
from chemclaw.retrieval.vectors.memory import InMemoryVectorStore
from tests import warehouse_fake

_DRIVER = "tests.warehouse_fake:open_fake"
_INDEX = "pistachio.public.reaction_index"


def _sync(test: Any) -> Any:
    """Run an `async def` test on its own loop; this repository has no async pytest plugin."""

    @functools.wraps(test)
    def runner(*args: Any, **kwargs: Any) -> None:
        asyncio.run(test(*args, **kwargs))

    return runner


def _binding(**vector: Any) -> dict[str, Any]:
    section: dict[str, Any] = {
        "index": _INDEX,
        "relation": "V_REACTION",
        "key": "REACTION_ID",
        "content_columns": ["REACTION_SMILES", "TITLE"],
        "filter_columns": {"since": "PUBLICATION_DATE"},
        "suppress_ingested": False,
    }
    section.update(vector)
    return {"connection": {"driver": _DRIVER}, "vector": section}


def _rows() -> dict[str, list[dict[str, Any]]]:
    """What the relation holds — deliberately in an order the ranking does not agree with."""
    return {
        "V_REACTION": [
            {"REACTION_ID": "RX-2", "REACTION_SMILES": "CCO>>CC=O", "TITLE": "Oxidation"},
            {"REACTION_ID": "RX-1", "REACTION_SMILES": "CC(=O)O>>CCOC(C)=O", "TITLE": "Ester"},
        ]
    }


async def _store_ranked(query: str, *ids: str) -> InMemoryVectorStore:
    """A store whose points rank in the order given, nearest first, for this exact query.

    Built from the query's own embedding rather than from hand-written coordinates: the retriever
    embeds the query with the configured provider, so a two-element fixture is not even the right
    width. Each successive point gets a larger alternating perturbation, which lowers its cosine
    monotonically while keeping it comfortably positive — so the expected order is a property of the
    construction rather than of whatever the hash embedder happened to produce.
    """
    base = embed_texts([query])[0]
    store = InMemoryVectorStore()
    await store.upsert(
        _INDEX,
        [
            VectorPoint(
                id=note_id,
                vector=[c + n * 0.01 * (1 if i % 2 else -1) for i, c in enumerate(base)],
            )
            for n, note_id in enumerate(ids)
        ],
    )
    return store


def _retriever(store: InMemoryVectorStore, **vector: Any) -> WarehouseVectorRetriever:
    warehouse_fake.prime(**_rows())
    retriever = WarehouseVectorRetriever(binding=_binding(**vector), name="pistachio")
    # Assigned rather than passed: the registry splats a manifest's `config:` into the constructor,
    # so every parameter there is something a manifest could set, and `store:` is not.
    retriever._store = store
    return retriever


# --- the binding refuses a half-declared shape ---------------------------------------------------


def test_an_index_and_a_vector_column_together_are_refused() -> None:
    """Two shapes half-declared would silently take one of them."""
    with pytest.raises(BindingError, match="must not also name a `vector_column`"):
        load_binding(_binding(vector_column="REACTION_VECTOR"))


def test_a_block_with_neither_a_column_nor_an_index_is_refused() -> None:
    """There has to be something to rank on, or something to rank in."""
    binding = _binding()
    del binding["vector"]["index"]
    with pytest.raises(BindingError, match="rank on"):
        load_binding(binding)


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [("metric", "l2", "cosine"), ("embedding", "server", "embeds the query here")],
)
def test_an_index_ranked_source_pins_the_two_settings_it_cannot_honour(
    field: str, value: str, match: str
) -> None:
    """`VectorMatch` is a cosine, and there is no statement for a server embedder to appear in."""
    with pytest.raises(BindingError, match=match):
        load_binding(_binding(**{field: value, "server_embed_function": "EMBED"}))


# --- the search itself ----------------------------------------------------------------------------


@_sync
async def test_the_store_ranks_and_the_warehouse_only_resolves() -> None:
    """One statement reaches the warehouse, and it is a keyed lookup with no similarity in it."""
    retriever = _retriever(await _store_ranked("ester formation", "RX-1", "RX-2"))
    chunks = await retriever.retrieve("ester formation", {})

    assert [chunk.source_note_id for chunk in chunks] == ["pistachio:RX-1", "pistachio:RX-2"]
    statements = [sql for sql, _ in warehouse_fake.NEXT.executed]  # type: ignore[union-attr]
    assert len(statements) == 1
    assert "IN (" in statements[0]
    assert "SIMILARITY" not in statements[0].upper()


@_sync
async def test_the_stores_order_survives_the_resolve() -> None:
    """The relation returns rows in its own order; the ranking is the store's and must win."""
    retriever = _retriever(await _store_ranked("oxidation", "RX-2", "RX-1"))
    chunks = await retriever.retrieve("oxidation", {})

    assert [chunk.source_note_id for chunk in chunks] == ["pistachio:RX-2", "pistachio:RX-1"]
    assert chunks[0].score > chunks[1].score


@_sync
async def test_a_key_the_relation_no_longer_holds_is_dropped_not_guessed_at() -> None:
    """An index outlives a deleted row; a hit nobody can resolve is not a citation."""
    retriever = _retriever(await _store_ranked("ester formation", "RX-1", "RX-GONE"))
    chunks = await retriever.retrieve("ester formation", {})

    assert [chunk.source_note_id for chunk in chunks] == ["pistachio:RX-1"]


@_sync
async def test_an_unfiltered_search_costs_no_scope_query() -> None:
    """`None` means the whole index and must cost nothing extra — one statement, not two."""
    retriever = _retriever(await _store_ranked("ester formation", "RX-1"))
    await retriever.retrieve("ester formation", {})

    assert len(warehouse_fake.NEXT.executed) == 1  # type: ignore[union-attr]


@_sync
async def test_a_filtered_search_sends_its_eligibility_before_the_top_k() -> None:
    """Filter after the cut and a narrow filter over a wide corpus returns nothing at all.

    The fake resolves both statements from the same primed relation, so what this asserts is the
    *shape*: a scope query runs first, and the store is asked with a bounded group set.
    """
    store = await _store_ranked("ester formation", "RX-1", "RX-2")
    retriever = _retriever(store)
    await retriever.retrieve("ester formation", {"since": "2020-01-01"})

    statements = [sql for sql, _ in warehouse_fake.NEXT.executed]  # type: ignore[union-attr]
    assert len(statements) == 2, "a scope query, then a resolve"
    assert statements[0].startswith("SELECT REACTION_ID FROM V_REACTION WHERE")
    assert "PUBLICATION_DATE" in statements[0]


@_sync
async def test_a_scope_too_broad_to_send_is_refused_rather_than_truncated() -> None:
    """A silently cut eligibility set is a wrong answer that reads as a thin corpus.

    `retrieve` swallows the failure into an empty leg, as it does for every warehouse error, so the
    refusal is asserted on `_search` where it is raised.
    """
    retriever = _retriever(await _store_ranked("ester formation", "RX-1", "RX-2"))
    settings_cap = settings.vector_store_max_scope_keys
    try:
        object.__setattr__(settings, "vector_store_max_scope_keys", 1)
        with pytest.raises(WarehouseQueryError, match="more eligibility"):
            await retriever._search("ester formation", {"since": "2020-01-01"})
    finally:
        object.__setattr__(settings, "vector_store_max_scope_keys", settings_cap)


@_sync
async def test_an_empty_eligibility_set_returns_nothing_without_asking_the_store() -> None:
    """An empty scope means nothing is eligible, which is not the same as no filter."""
    warehouse_fake.prime(V_REACTION=[])
    retriever = WarehouseVectorRetriever(binding=_binding(), name="pistachio")
    retriever._store = await _store_ranked("ester formation", "RX-1")

    assert await retriever._search("ester formation", {"since": "2020-01-01"}) == []
