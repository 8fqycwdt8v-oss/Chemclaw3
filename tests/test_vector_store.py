"""The vector-store seam: the reference store, the Qdrant adapter, and provider selection.

No database and no Qdrant server: the adapter runs against a fake client injected through its own
seam, so `qdrant-client` is not a dependency. Nothing here has run against a real Qdrant.
"""

from typing import Any

import pytest

from chemclaw.core.config import settings
from chemclaw.ingest.documents.external_index import (
    ExternalVectorDocumentIndex,
    _points_for,
    group_key,
    parse_point_id,
    point_id,
)
from chemclaw.ingest.documents.index import ChunkRecord, DocumentFilter
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


class _StubVectorStore(InMemoryVectorStore):
    """A vector database this repository ships no adapter for, standing in for a site's own.

    Deliberately nothing but a name in a module the registry has never heard of: what makes it
    reachable is `vector_store_provider` naming it, and nothing else anywhere.
    """


COLLECTION = "chunks"


def _point(reference: str, vector: list[float], group: str = "") -> VectorPoint:
    return VectorPoint(id=reference, vector=vector, group=group)


# --- the contract every store must satisfy ------------------------------------------------------


def test_the_reference_store_satisfies_the_protocol() -> None:
    """`runtime_checkable` is only worth having if something asserts it."""
    assert isinstance(InMemoryVectorStore(), VectorStore)
    assert isinstance(QdrantVectorStore(client=_FakeClient()), VectorStore)


# The chunking these fixtures are cut under. A chunk's identity is
# `(doc_id, chunking_key, ordinal)`; these fixtures are indifferent to the value, so it is one named
# constant.
_CHUNKING = "2000:200"


async def test_a_search_ranks_by_similarity_and_drops_non_matches() -> None:
    """Best first, and a vector orthogonal to the query is not a hit at all.

    Without the `> 0` floor a narrow corpus would surface unrelated documents as cited evidence.
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


async def test_an_identical_vector_scores_one_and_does_not_raise() -> None:
    """Self-similarity rounds above 1.0 about half the time, and the score is bounded `le=1.0`."""
    store = InMemoryVectorStore()
    vector = [0.37, -0.52, 0.771]
    await store.upsert(COLLECTION, [_point("a", vector)])
    hits = await store.search(COLLECTION, vector, 1)
    assert hits[0].score == 1.0


async def test_a_scope_narrows_before_the_cut_rather_than_after_it() -> None:
    """A scope filters before the top-k cut, not after it.

    Here the nearest vector is excluded, so post-filtering a k=1 search would return nothing.
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


async def test_an_empty_scope_is_not_an_unfiltered_search() -> None:
    """`set()` means "nothing is eligible" and `None` means "no restriction" — never the same.

    Collapsing the two is how an entitlement or a date window that matched nothing turns into a
    sweep of the whole corpus.
    """
    store = InMemoryVectorStore()
    await store.upsert(COLLECTION, [_point("a", [1.0, 0.0], group="g")])
    assert await store.search(COLLECTION, [1.0, 0.0], 5, set()) == []
    assert len(await store.search(COLLECTION, [1.0, 0.0], 5, None)) == 1


async def test_a_point_with_no_group_is_its_own_group() -> None:
    """The default that makes the seam usable for anything embedded whole (a note is one vector)."""
    store = InMemoryVectorStore()
    await store.upsert(COLLECTION, [_point("note-1", [1.0, 0.0])])
    assert [hit.id for hit in await store.search(COLLECTION, [1.0, 0.0], 5, {"note-1"})] == [
        "note-1"
    ]


async def test_collections_do_not_see_each_other() -> None:
    """A shared cluster is normal; one collection's points must never answer another's search."""
    store = InMemoryVectorStore()
    await store.upsert("one", [_point("a", [1.0, 0.0])])
    await store.upsert("two", [_point("b", [1.0, 0.0])])
    assert [hit.id for hit in await store.search("one", [1.0, 0.0], 5)] == ["a"]


async def test_deleting_an_absent_point_is_not_an_error() -> None:
    """The catalogue is the record; a point already gone is the state being asked for."""
    store = InMemoryVectorStore()
    await store.delete(COLLECTION, ["never-existed"])


async def test_a_zero_query_vector_matches_nothing_rather_than_ordering_over_nan() -> None:
    """A token-less query under the hash embedder is a zero vector; cosine is 0 to everything."""
    store = InMemoryVectorStore()
    await store.upsert(COLLECTION, [_point("a", [1.0, 0.0])])
    assert await store.search(COLLECTION, [0.0, 0.0], 5) == []


# --- point ids round-trip -----------------------------------------------------------------------


def test_a_point_id_round_trips_through_the_whole_catalogue_key() -> None:
    """A point id round-trips the whole key `(doc_id, chunking_key, ordinal)`.

    Without the chunking, two cuttings of one document would collide on a single point.
    """
    assert parse_point_id(point_id("doc-abc", "c1800o200", 3)) == ("doc-abc", "c1800o200", 3)
    # `rpartition` on `#`, so a doc id carrying one still parses.
    assert parse_point_id(point_id("doc#weird", "c900o100", 12)) == ("doc#weird", "c900o100", 12)
    # Two cuttings of one document are two distinct points, which is the whole point.
    assert point_id("doc-a", "c1800o200", 3) != point_id("doc-a", "c900o100", 3)


@pytest.mark.parametrize(
    "bad", ["", "no-separator", "#3", "doc-abc@ck#notanumber", "doc-abc#3", "@ck#3"]
)
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

    The adapter reaches the vendor namespace through `_models()`, so patching it exercises the
    adapter's payloads, filters and errors without `qdrant-client` installed.
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


async def test_the_adapter_reads_the_reference_back_out_of_the_payload() -> None:
    """A hit is useless unless it rejoins the catalogue, and `ref` is what does that."""
    client = _FakeClient(matches=[("doc-a#0", 0.9), ("doc-b#2", 0.4)])
    hits = await QdrantVectorStore(client=client).search(COLLECTION, [1.0, 0.0], 5)
    assert [hit.id for hit in hits] == ["doc-a#0", "doc-b#2"]
    assert hits[0].score == pytest.approx(0.9)


async def test_the_adapter_sends_the_scope_to_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """The filter must reach Qdrant, or the top-k is taken before eligibility and recall is lost."""
    client = _FakeClient()
    await QdrantVectorStore(client=client).search(COLLECTION, [1.0, 0.0], 5, {"doc-a"})
    assert client.queries[0]["filter"] is not None, "the scope was not sent to the server"


async def test_the_adapter_answers_an_empty_scope_without_a_round_trip() -> None:
    """Nothing is eligible, so there is nothing to ask — and asking would ask for everything."""
    client = _FakeClient(matches=[("doc-a#0", 0.9)])
    assert await QdrantVectorStore(client=client).search(COLLECTION, [1.0, 0.0], 5, set()) == []
    assert client.queries == []


async def test_a_point_with_no_ref_is_dropped_rather_than_guessed_at() -> None:
    """It cannot be rejoined to the catalogue, so it is not evidence anyone could check."""

    class _Nameless(_FakeClient):
        async def query_points(self, **kwargs: Any) -> Any:
            return _FakeResponse([_FakePoint({}, 0.9)])

    assert await QdrantVectorStore(client=_Nameless()).search(COLLECTION, [1.0, 0.0], 5) == []


async def test_a_client_failure_becomes_one_error_type() -> None:
    """The caller handles `VectorStoreError`; a vendor hierarchy leaking through defeats that."""

    class _Broken(_FakeClient):
        async def query_points(self, **kwargs: Any) -> Any:
            raise RuntimeError("connection reset")

    with pytest.raises(VectorStoreError):
        await QdrantVectorStore(client=_Broken()).search(COLLECTION, [1.0, 0.0], 5)


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


def test_the_databricks_provider_builds_the_databricks_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second name on the token, resolved in the same one place."""
    from chemclaw.retrieval.vectors.databricks import DatabricksVectorStore

    monkeypatch.setattr(settings, "vector_store_provider", "databricks")
    assert isinstance(default_vector_store(), DatabricksVectorStore)


def test_databricks_will_not_accept_the_shipped_qdrant_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Databricks will not accept the shipped Qdrant default URL.

    `vector_store_url` has a non-empty default, so an emptiness check cannot catch a forgotten URL.
    """
    from chemclaw.core.config.store import StoreSettings

    with pytest.raises(ValueError, match="shipped default"):
        StoreSettings(vector_store_provider="databricks", vector_store_endpoint_name="ep")


def test_no_provider_but_qdrant_may_keep_qdrants_default_url() -> None:
    """No provider but Qdrant may keep Qdrant's default URL.

    A `module:callable` provider selected without a URL would otherwise validate and query a server
    it was never pointed at.
    """
    from chemclaw.core.config.store import StoreSettings

    with pytest.raises(ValueError, match="shipped default"):
        StoreSettings(vector_store_provider="acme.vectors:MilvusVectorStore")
    # Qdrant is the one provider that default belongs to, so it stays legitimate there.
    assert StoreSettings(vector_store_provider="qdrant").vector_store_url


def test_databricks_needs_the_endpoint_that_serves_its_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Databricks needs the endpoint that serves its index.

    An index is addressed by a pair the client cannot derive, so a missing half is caught at deploy
    time rather than as "index not found" in a worker.
    """
    from chemclaw.core.config.store import StoreSettings

    with pytest.raises(ValueError, match="vector_store_endpoint_name"):
        StoreSettings(
            vector_store_provider="databricks",
            vector_store_url="https://example.cloud.databricks.com",
            vector_store_endpoint_name="",
        )


def test_a_vector_database_this_repository_never_heard_of_attaches_with_no_core_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vector database this repository never heard of attaches with no core edit.

    A `module:callable` provider is late-bound through `chemclaw.core.connect`, like the warehouse
    ELN and the result store. The adapter is this test module's own attribute, a name the repository
    has never seen.
    """
    from chemclaw.core.config.store import StoreSettings

    reference = f"{__name__}:_StubVectorStore"
    # Validated as a setting first: the config must accept the reference without resolving it,
    # because resolving at startup would import a client in every process that reads settings.
    assert (
        StoreSettings(
            vector_store_provider=reference, vector_store_url="acme://vectors:9000"
        ).vector_store_provider
        == reference
    )
    monkeypatch.setattr(settings, "vector_store_provider", reference)
    assert isinstance(default_vector_store(), _StubVectorStore)


def test_a_provider_that_is_neither_shipped_nor_a_reference_is_refused() -> None:
    """Dropping the `Literal` must not drop the typo check that came with it."""
    from chemclaw.core.config.store import StoreSettings

    with pytest.raises(ValueError, match="module:callable"):
        StoreSettings(vector_store_provider="qdrnat", vector_store_url="http://localhost:6333")


def test_every_shipped_provider_name_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every shipped provider name accepted by `core` resolves in `retrieval`.

    `core` imports no sibling, so the two declarations are held in step here rather than failing at
    the first search.
    """
    from chemclaw.core.config.store import _SHIPPED_VECTOR_STORES
    from chemclaw.retrieval.vectors.registry import SHIPPED

    assert set(_SHIPPED_VECTOR_STORES) == set(SHIPPED)
    for name, reference in SHIPPED.items():
        monkeypatch.setattr(settings, "vector_store_provider", name)
        assert isinstance(default_vector_store(), VectorStore), reference


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

    `VectorPoint.group` defaults to the point's own id, which would make a chunk vanish from every
    filtered search. One builder serves both the crawl and the re-embedding drain.
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
    assert [point.id for point in points] == [
        f"doc-abc@{_CHUNKING}#0",
        f"doc-abc@{_CHUNKING}#3",
    ]
    # Grouped by the *cutting* of the document, because that is what eligibility joins on — a
    # share must never be served another share's cutting of the same text.
    assert {point.group_key for point in points} == {f"doc-abc@{_CHUNKING}"}


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
    assert written[0].payload == {
        "ref": f"doc-a@{_CHUNKING}#2",
        "group": f"doc-a@{_CHUNKING}",
    }


# --- the scope always carries the source ----------------------------------------------------------


class _RecordingStore:
    """A `VectorStore` that records the scope it was handed and returns nothing."""

    def __init__(self) -> None:
        self.scopes: list[set[str] | None] = []

    async def upsert(self, collection: str, points: list[VectorPoint]) -> None: ...

    async def delete(self, collection: str, ids: list[str]) -> None: ...

    async def search(
        self,
        collection: str,
        embedding: list[float],
        top_k: int,
        groups: set[str] | None = None,
    ) -> list[Any]:
        self.scopes.append(groups)
        return []  # returning nothing short-circuits before `_resolve` touches a database


async def test_an_unfiltered_search_is_still_scoped_to_its_own_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A search is always scoped to its own source, even with no tag and no date window.

    Every share writes into one collection, so an unscoped top-k would be filled with other sources'
    hits that `_resolve` then drops, returning fewer than `top_k`.
    """
    store = _RecordingStore()
    index = ExternalVectorDocumentIndex(store)

    # The catalogue lookup is the part that needs a database; stub it, since what is under test is
    # whether a scope is passed at all.
    async def _eligible(source: str, filters: DocumentFilter) -> set[str]:
        return {group_key("doc-a", "400:40"), group_key("doc-b", "400:40")}

    monkeypatch.setattr(index, "_eligible_cuttings", _eligible)
    await index.search_dense("share-A", [1.0, 0.0], 8, DocumentFilter())
    assert store.scopes == [{"doc-a@400:40", "doc-b@400:40"}], (
        "an unfiltered search reached the store unscoped"
    )


async def test_a_source_with_no_eligible_documents_returns_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty means empty — it must not degrade into a search of every other share's points."""
    store = _RecordingStore()
    index = ExternalVectorDocumentIndex(store)

    async def _none_eligible(source: str, filters: DocumentFilter) -> set[str]:
        return set()

    monkeypatch.setattr(index, "_eligible_cuttings", _none_eligible)
    assert await index.search_dense("share-A", [1.0, 0.0], 8, DocumentFilter()) == []
    assert store.scopes == [], "an empty scope still reached the store"


def test_the_api_key_is_registered_for_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """The claim two docstrings used to make and nothing implemented.

    A client that echoes its own configuration into a traceback must not be able to put the key in
    a log. Asserted against the inventory rather than trusted as prose.
    """
    registered: list[str] = []
    monkeypatch.setattr(qdrant_module, "register_secret_env", registered.append)
    monkeypatch.setattr(qdrant_module, "_client_module", lambda: _StubModule())
    qdrant_module.open_qdrant_client()
    assert "CHEMCLAW_VECTOR_STORE_API_KEY" in registered


def test_the_qdrant_client_refuses_the_ambient_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """`trust_env=False` reaches the httpx client `AsyncQdrantClient` builds for itself.

    Embedded note text and query vectors travel on this client, and an ambient proxy would carry
    them past `core/netguard.py`. `AsyncQdrantClient` forwards extra keywords into its
    `httpx.AsyncClient`. `qdrant_client` is not installed here, so this asserts the keyword is sent,
    unconditionally.
    """
    stub = _StubModule()
    monkeypatch.setattr(qdrant_module, "register_secret_env", lambda name: None)
    monkeypatch.setattr(qdrant_module, "_client_module", lambda: stub)
    monkeypatch.setattr(settings, "llm_tls_ca_bundle", "")
    qdrant_module.open_qdrant_client()
    assert stub.kwargs.get("trust_env") is False, (
        "the Qdrant client would read a proxy variable and carry note text and query vectors to a "
        "host of the setter's choosing"
    )

    monkeypatch.setattr(settings, "llm_tls_ca_bundle", "/etc/ssl/internal.pem")
    qdrant_module.open_qdrant_client()
    assert stub.kwargs.get("trust_env") is False, "a private CA is not what decides this"


def test_no_private_ca_means_no_verify_keyword(monkeypatch: pytest.MonkeyPatch) -> None:
    """No private CA means no `verify` keyword.

    `verify` is forwarded to httpx rather than being in the constructor's own signature, so the
    default path passes only keywords the client certainly accepts.
    """
    stub = _StubModule()
    monkeypatch.setattr(qdrant_module, "register_secret_env", lambda name: None)
    monkeypatch.setattr(qdrant_module, "_client_module", lambda: stub)
    monkeypatch.setattr(settings, "llm_tls_ca_bundle", "")
    qdrant_module.open_qdrant_client()
    assert "verify" not in stub.kwargs

    monkeypatch.setattr(settings, "llm_tls_ca_bundle", "/etc/ssl/internal.pem")
    qdrant_module.open_qdrant_client()
    assert stub.kwargs["verify"] == "/etc/ssl/internal.pem"


class _StubModule:
    """Stands in for the `qdrant_client` module, capturing the constructor keywords."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def AsyncQdrantClient(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return _FakeClient()


class _FakeCursor:
    """Records the SQL a catalogue lookup issues, and returns two rows."""

    def __init__(self, executed: list[str]) -> None:
        self._executed = executed

    async def execute(self, sql: str, params: Any = None) -> None:
        self._executed.append(sql)

    async def fetchall(self) -> list[tuple[str, str]]:
        return [("doc-a", "400:40"), ("doc-b", "4000:400")]

    async def __aenter__(self) -> "_FakeCursor":
        return self

    async def __aexit__(self, *exc: Any) -> None: ...


class _FakeConnection:
    def __init__(self, executed: list[str]) -> None:
        self._executed = executed

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._executed)

    async def __aenter__(self) -> "_FakeConnection":
        return self

    async def __aexit__(self, *exc: Any) -> None: ...


async def test_the_catalogue_is_consulted_even_when_nothing_is_filtered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_eligible_cuttings` is consulted even when nothing is filtered.

    The source is always a restriction, so there is no fast path. The scope is spelled in
    `group_key` terms and carries the chunking, because that is what points are filed under; a
    stubbed scope could not show that.
    """
    executed: list[str] = []
    index = ExternalVectorDocumentIndex(_RecordingStore())
    monkeypatch.setattr(index, "_connection", lambda: _FakeConnection(executed))

    eligible = await index._eligible_cuttings("share-A", DocumentFilter())

    assert executed, "an unfiltered query returned a scope without asking the catalogue"
    assert "source = %(src)s" in executed[0]
    assert "chunking_key" in executed[0], "a scope that cannot see the cutting cannot match a point"
    assert eligible == {"doc-a@400:40", "doc-b@4000:400"}


async def test_a_re_chunk_reclaims_the_superseded_cutting_s_vectors() -> None:
    """A re-chunk reclaims the superseded cutting's vectors.

    The catalogue drops the old cutting's rows; without deleting their points the vector database
    would keep a second full copy of the corpus.
    """

    class _Deleting(_RecordingStore):
        def __init__(self) -> None:
            super().__init__()
            self.deleted: list[str] = []

        async def delete(self, collection: str, ids: list[str]) -> None:
            self.deleted.extend(ids)

    store = _Deleting()
    index = ExternalVectorDocumentIndex(store)
    await index._forget_vectors([("doc-a", "old-cut", 0), ("doc-a", "old-cut", 1)])
    assert store.deleted == ["doc-a@old-cut#0", "doc-a@old-cut#1"]


def test_the_base_index_forgets_nothing_because_its_vectors_were_in_the_rows() -> None:
    """The hook is a no-op for pgvector, which is why it can live on the base at all."""
    from chemclaw.ingest.documents.index import PostgresDocumentIndex

    assert PostgresDocumentIndex._forget_vectors is not ExternalVectorDocumentIndex._forget_vectors


def test_the_store_is_built_once_per_process_and_not_once_per_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The store is built once per process, not once per tool call.

    The clients hold connection pools and have no `close` to call, and a retrieve half is built for
    every `gather_evidence`. 100 resolutions of the configured store must construct one.
    """
    built: list[object] = []

    def counting() -> VectorStore:
        store = _StubVectorStore()
        built.append(store)
        return store

    monkeypatch.setattr(settings, "vector_store_provider", f"{__name__}:_counting_store")
    monkeypatch.setitem(globals(), "_counting_store", counting)

    stores = [default_vector_store() for _ in range(100)]

    assert len(built) == 1, (
        f"100 tool calls built {len(built)} vector-store client(s); none of them can be closed"
    )
    assert len({id(store) for store in stores}) == 1


def test_a_reconfigured_store_is_rebuilt_rather_than_served_from_the_last_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reconfigured store is rebuilt rather than served from the last configuration.

    The memo is keyed by configuration, so a swapped `Settings` gets a client for what is configured
    now.
    """
    built: list[object] = []

    def counting() -> VectorStore:
        store = _StubVectorStore()
        built.append(store)
        return store

    monkeypatch.setattr(settings, "vector_store_provider", f"{__name__}:_counting_store")
    monkeypatch.setitem(globals(), "_counting_store", counting)

    first = default_vector_store()
    monkeypatch.setattr(settings, "vector_store_url", "acme://vectors:9001")
    second = default_vector_store()

    assert len(built) == 2 and first is not second
