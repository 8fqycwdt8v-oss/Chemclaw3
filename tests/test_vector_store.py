"""The vector-store seam: the reference store, the Qdrant adapter, and provider selection.

No database and no Qdrant server. The adapter is exercised against a fake client injected through
its own seam — the construction `tests/test_warehouse_retriever.py` uses for Snowflake, and the
reason `qdrant-client` is not a dependency of this repository.

What is *not* covered here, stated rather than implied: nothing has run against a real Qdrant. The
fake agrees with the adapter about the calls, which is a different claim from the server agreeing
with them. `docs/planning/BACKLOG.md` carries the row.
"""

import asyncio
import functools
from typing import Any

import pytest

from chemclaw.core.config import settings
from chemclaw.ingest.documents.external_index import _points_for, parse_point_id, point_id
from chemclaw.ingest.documents.index import ChunkRecord
from chemclaw.retrieval.vectors import qdrant as qdrant_module
from chemclaw.retrieval.vectors.base import (
    VectorPoint,
    VectorStore,
    VectorStoreConfigError,
    VectorStoreError,
)
from chemclaw.retrieval.vectors.memory import InMemoryVectorStore
from chemclaw.retrieval.vectors.qdrant import QdrantVectorStore
from chemclaw.retrieval.vectors.registry import default_vector_store

COLLECTION = "chunks"


def _sync(test: Any) -> Any:
    """Run an `async def` test on its own loop, so pytest collects a plain function.

    This repository has no async pytest plugin; `tests/test_document_share.py` calls `asyncio.run`
    inline at each await. That reads badly when a test makes six calls, so the same mechanism is
    hoisted into a decorator here. One loop per test, exactly as `asyncio.run` gives.
    """

    @functools.wraps(test)
    def runner(*args: Any, **kwargs: Any) -> None:
        asyncio.run(test(*args, **kwargs))

    return runner


def _point(reference: str, vector: list[float], group: str = "") -> VectorPoint:
    return VectorPoint(id=reference, vector=vector, group=group)


# --- the contract every store must satisfy ------------------------------------------------------


def test_the_reference_store_satisfies_the_protocol() -> None:
    """`runtime_checkable` is only worth having if something asserts it."""
    assert isinstance(InMemoryVectorStore(), VectorStore)
    assert isinstance(QdrantVectorStore(client=_FakeClient()), VectorStore)


# The chunking these fixtures are cut under. `ChunkRecord` gained a required `chunking_key` in
# D-2026-08-08-a-derived-index-must-record-what-derived-it: a chunk row's identity is
# `(doc_id, chunking_key, ordinal)`, because `doc_id` is a content hash shared across shares
# while the cutting is per-share. These fixtures exercise the point-id contract, which is
# indifferent to the value — so one constant, named, rather than a literal at each site.
_CHUNKING = "2000:200"


@_sync
async def test_a_search_ranks_by_similarity_and_drops_non_matches() -> None:
    """Best first, and a vector orthogonal to the query is not a hit at all.

    The `> 0` floor every index in this repository applies: without it a nearest-neighbour search
    returns the k nearest unconditionally, so a narrow corpus surfaces unrelated documents as cited
    evidence.
    """
    store = InMemoryVectorStore()
    await store.upsert(
        COLLECTION,
        [
            _point("a", [1.0, 0.0]),
            _point("b", [0.9, 0.1]),
            _point("c", [0.0, 1.0]),  # orthogonal to the query
        ],
    )
    hits = await store.search(COLLECTION, [1.0, 0.0], 10)
    assert [hit.id for hit in hits] == ["a", "b"]
    assert hits[0].score > hits[1].score


@_sync
async def test_an_identical_vector_scores_one_and_does_not_raise() -> None:
    """Self-similarity rounds above 1.0 about half the time, and the score is bounded `le=1.0`."""
    store = InMemoryVectorStore()
    vector = [0.37, -0.52, 0.771]
    await store.upsert(COLLECTION, [_point("a", vector)])
    hits = await store.search(COLLECTION, vector, 1)
    assert hits[0].score == 1.0


@_sync
async def test_a_scope_narrows_before_the_cut_rather_than_after_it() -> None:
    """The property the whole design rests on: filter first, *then* take the top k.

    Post-filtering returns nothing here — the single nearest vector belongs to the excluded group,
    so a k=1 search followed by a filter yields an empty list while the correct answer is the best
    point that is actually eligible.
    """
    store = InMemoryVectorStore()
    await store.upsert(
        COLLECTION,
        [
            _point("wanted#0", [0.8, 0.6], group="wanted"),
            _point("other#0", [1.0, 0.0], group="other"),  # the nearest, and ineligible
        ],
    )
    assert [hit.id for hit in await store.search(COLLECTION, [1.0, 0.0], 1)] == ["other#0"]
    scoped = await store.search(COLLECTION, [1.0, 0.0], 1, {"wanted"})
    assert [hit.id for hit in scoped] == ["wanted#0"]


@_sync
async def test_an_empty_scope_is_not_an_unfiltered_search() -> None:
    """`set()` means "nothing is eligible" and `None` means "no restriction" — never the same.

    Collapsing the two is how an entitlement or a date window that matched nothing turns into a
    sweep of the whole corpus.
    """
    store = InMemoryVectorStore()
    await store.upsert(COLLECTION, [_point("a", [1.0, 0.0], group="g")])
    assert await store.search(COLLECTION, [1.0, 0.0], 5, set()) == []
    assert len(await store.search(COLLECTION, [1.0, 0.0], 5, None)) == 1


@_sync
async def test_a_point_with_no_group_is_its_own_group() -> None:
    """The default that makes the seam usable for anything embedded whole (a note is one vector)."""
    store = InMemoryVectorStore()
    await store.upsert(COLLECTION, [_point("note-1", [1.0, 0.0])])
    assert [hit.id for hit in await store.search(COLLECTION, [1.0, 0.0], 5, {"note-1"})] == [
        "note-1"
    ]


@_sync
async def test_collections_do_not_see_each_other() -> None:
    """A shared cluster is normal; one collection's points must never answer another's search."""
    store = InMemoryVectorStore()
    await store.upsert("one", [_point("a", [1.0, 0.0])])
    await store.upsert("two", [_point("b", [1.0, 0.0])])
    assert [hit.id for hit in await store.search("one", [1.0, 0.0], 5)] == ["a"]


@_sync
async def test_deleting_an_absent_point_is_not_an_error() -> None:
    """The catalogue is the record; a point already gone is the state being asked for."""
    store = InMemoryVectorStore()
    await store.delete(COLLECTION, ["never-existed"])


@_sync
async def test_a_zero_query_vector_matches_nothing_rather_than_ordering_over_nan() -> None:
    """A token-less query under the hash embedder is a zero vector; cosine is 0 to everything."""
    store = InMemoryVectorStore()
    await store.upsert(COLLECTION, [_point("a", [1.0, 0.0])])
    assert await store.search(COLLECTION, [0.0, 0.0], 5) == []


# --- point ids round-trip -----------------------------------------------------------------------


def test_a_point_id_round_trips_through_the_catalogue_key() -> None:
    """The write and the read must agree; a doc id containing `#` must not break the parse."""
    assert parse_point_id(point_id("doc-abc", 3)) == ("doc-abc", 3)
    # `rpartition`, so only the final `#` separates the ordinal.
    assert parse_point_id(point_id("doc#weird", 12)) == ("doc#weird", 12)


@pytest.mark.parametrize("bad", ["", "no-separator", "#3", "doc-abc#notanumber"])
def test_an_unreadable_point_id_is_none_rather_than_an_exception(bad: str) -> None:
    """A store may hold points this catalogue no longer knows about; one must not fail a search."""
    assert parse_point_id(bad) is None


# --- the Qdrant adapter, against a fake client ---------------------------------------------------


class _FakeClient:
    """The three methods the adapter uses, recording what it was asked."""

    def __init__(self, matches: list[tuple[str, float]] | None = None) -> None:
        self.upserted: list[Any] = []
        self.deleted: list[Any] = []
        self.queries: list[dict[str, Any]] = []
        self._matches = matches or []

    async def upsert(self, *, collection_name: str, points: list[Any]) -> None:
        self.upserted.append((collection_name, points))

    async def query_points(
        self,
        *,
        collection_name: str,
        query: list[float],
        limit: int,
        query_filter: Any | None = None,
        score_threshold: float | None = None,
    ) -> Any:
        self.queries.append(
            {
                "collection": collection_name,
                "limit": limit,
                "filter": query_filter,
                "threshold": score_threshold,
            }
        )
        return _FakeResponse(
            [_FakePoint({"ref": ref, "group": "g"}, score) for ref, score in self._matches]
        )

    async def delete(self, *, collection_name: str, points_selector: Any) -> None:
        self.deleted.append((collection_name, points_selector))


class _FakePoint:
    def __init__(self, payload: dict[str, Any], score: float) -> None:
        self.payload = payload
        self.score = score


class _FakeResponse:
    def __init__(self, points: list[_FakePoint]) -> None:
        self.points = points


class _FakeModels:
    """The `qdrant_client.models` names this adapter builds with, as plain recording objects.

    The other half of the fake, and the reason these tests *run* rather than skip: the adapter
    reaches for the vendor namespace through `_models()`, so patching that one function is enough
    to exercise every line of it on a machine with no `qdrant-client` installed. Skipping instead
    would leave the adapter's own logic — the payload it writes, the filter it builds, the error
    type it raises — asserted by nothing, which is the state the seam exists to avoid.
    """

    class PointStruct:
        def __init__(self, id: str, vector: list[float], payload: dict[str, Any]) -> None:
            self.id, self.vector, self.payload = id, vector, payload

    class MatchAny:
        def __init__(self, any: list[str]) -> None:
            self.any = any

    class FieldCondition:
        def __init__(self, key: str, match: Any) -> None:
            self.key, self.match = key, match

    class Filter:
        def __init__(self, must: list[Any]) -> None:
            self.must = must

    class PointIdsList:
        def __init__(self, points: list[str]) -> None:
            self.points = points


@pytest.fixture(autouse=True)
def _fake_qdrant_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this module builds Qdrant structs from the fake namespace, never the vendor."""
    monkeypatch.setattr(qdrant_module, "_models", lambda: _FakeModels)


@_sync
async def test_the_adapter_reads_the_reference_back_out_of_the_payload() -> None:
    """A hit is useless unless it rejoins the catalogue, and `ref` is what does that."""
    client = _FakeClient(matches=[("doc-a#0", 0.9), ("doc-b#2", 0.4)])
    hits = await QdrantVectorStore(client=client).search(COLLECTION, [1.0, 0.0], 5)
    assert [hit.id for hit in hits] == ["doc-a#0", "doc-b#2"]
    assert hits[0].score == pytest.approx(0.9)


@_sync
async def test_the_adapter_sends_the_scope_to_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """The filter must reach Qdrant, or the top-k is taken before eligibility and recall is lost."""
    client = _FakeClient()
    await QdrantVectorStore(client=client).search(COLLECTION, [1.0, 0.0], 5, {"doc-a"})
    assert client.queries[0]["filter"] is not None, "the scope was not sent to the server"


@_sync
async def test_the_adapter_answers_an_empty_scope_without_a_round_trip() -> None:
    """Nothing is eligible, so there is nothing to ask — and asking would ask for everything."""
    client = _FakeClient(matches=[("doc-a#0", 0.9)])
    assert await QdrantVectorStore(client=client).search(COLLECTION, [1.0, 0.0], 5, set()) == []
    assert client.queries == []


@_sync
async def test_a_point_with_no_ref_is_dropped_rather_than_guessed_at() -> None:
    """It cannot be rejoined to the catalogue, so it is not evidence anyone could check."""

    class _Nameless(_FakeClient):
        async def query_points(self, **kwargs: Any) -> Any:
            return _FakeResponse([_FakePoint({}, 0.9)])

    assert await QdrantVectorStore(client=_Nameless()).search(COLLECTION, [1.0, 0.0], 5) == []


@_sync
async def test_a_client_failure_becomes_one_error_type() -> None:
    """The caller handles `VectorStoreError`; a vendor hierarchy leaking through defeats that."""

    class _Broken(_FakeClient):
        async def query_points(self, **kwargs: Any) -> Any:
            raise RuntimeError("connection reset")

    with pytest.raises(VectorStoreError):
        await QdrantVectorStore(client=_Broken()).search(COLLECTION, [1.0, 0.0], 5)


@_sync
async def test_an_empty_upsert_touches_no_client() -> None:
    """A crawl chunk where nothing changed must not cost a round trip."""
    client = _FakeClient()
    await QdrantVectorStore(client=client).upsert(COLLECTION, [])
    await QdrantVectorStore(client=client).delete(COLLECTION, [])
    assert client.upserted == [] and client.deleted == []


def test_a_missing_client_package_names_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one failure an operator can act on, so the message is the action.

    Not an `ImportError` surfacing from inside a worker: a `VectorStoreConfigError` is non-retryable
    by class name in `durable.publish`, because no retry installs a package.
    """

    def _absent(name: str) -> Any:
        raise ImportError(f"no module named {name}")

    monkeypatch.setattr("importlib.import_module", _absent)
    with pytest.raises(VectorStoreConfigError, match="qdrant-client"):
        qdrant_module.open_qdrant_client()


# --- provider selection --------------------------------------------------------------------------


def test_pgvector_is_not_a_vector_store_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """Its vectors live in the statement resolving the citation; there is nothing to delegate."""
    monkeypatch.setattr(settings, "vector_store_provider", "pgvector")
    with pytest.raises(VectorStoreConfigError, match="pgvector"):
        default_vector_store()


def test_the_qdrant_provider_builds_the_qdrant_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of the config token: a name becomes an implementation, in one place."""
    monkeypatch.setattr(settings, "vector_store_provider", "qdrant")
    assert isinstance(default_vector_store(), QdrantVectorStore)


def test_the_default_deployment_selects_pgvector() -> None:
    """A shipped default that quietly required an external service would be a bad default."""
    assert settings.vector_store_provider == "pgvector"


def test_an_external_provider_needs_an_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caught while somebody is still looking at the deploy, not from inside a worker later."""
    from chemclaw.core.config.store import StoreSettings

    with pytest.raises(ValueError, match="vector_store_url"):
        StoreSettings(vector_store_provider="qdrant", vector_store_url="")


def test_the_pgvector_width_check_is_inert_for_an_external_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An external store's deployment may legitimately run a model the column was never sized for.

    Refusing it would be the check inventing a constraint rather than reporting one: nothing writes
    that column when the vectors live elsewhere.
    """
    from chemclaw.ingest.documents.index import require_schema_vector_width

    monkeypatch.setattr(settings, "embedding_dim", 768)
    monkeypatch.setattr(settings, "vector_store_provider", "pgvector")
    with pytest.raises(Exception, match="768"):
        require_schema_vector_width()
    monkeypatch.setattr(settings, "vector_store_provider", "qdrant")
    require_schema_vector_width()


# --- the composition's point building -------------------------------------------------------------


def test_every_chunk_is_filed_under_its_document_not_under_itself() -> None:
    """A chunk's group is its `doc_id`, because eligibility is decided per document.

    **The bug this exists for.** `VectorPoint.group` defaults to the point's own id, which is right
    for anything embedded whole and silently wrong for a chunk. Two places built these points — the
    crawl's `upsert` and the re-embedding drain's `store_embeddings` — and only the first passed the
    group, so a re-embedded chunk was filed under `doc-abc#3` instead of `doc-abc`. It would still
    answer unfiltered questions and would vanish from every *filtered* one, with nothing raised
    anywhere. One builder now, so a second caller has nowhere to differ.
    """
    chunks = [
        ChunkRecord(
            doc_id="doc-abc",
            chunking_key=_CHUNKING,
            ordinal=0,
            content="first",
            embedding=[1.0, 0.0],
        ),
        ChunkRecord(
            doc_id="doc-abc",
            chunking_key=_CHUNKING,
            ordinal=3,
            content="fourth",
            embedding=[0.0, 1.0],
        ),
    ]
    points = _points_for(chunks)
    assert [point.id for point in points] == ["doc-abc#0", "doc-abc#3"]
    assert {point.group_key for point in points} == {"doc-abc"}


@_sync
async def test_the_adapter_writes_both_the_reference_and_the_group() -> None:
    """`ref` is what rejoins the catalogue; `group` is what the scope filter matches on.

    Losing either is silent: without `ref` a hit cannot be resolved, and without `group` the point
    is invisible to every filtered search.
    """
    client = _FakeClient()
    await QdrantVectorStore(client=client).upsert(
        COLLECTION,
        _points_for(
            [
                ChunkRecord(
                    doc_id="doc-a", chunking_key=_CHUNKING, ordinal=2, content="x", embedding=[1.0]
                )
            ]
        ),
    )
    (_, written) = client.upserted[0]
    assert written[0].payload == {"ref": "doc-a#2", "group": "doc-a"}
