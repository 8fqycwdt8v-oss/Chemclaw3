"""Qdrant as a `VectorStore`, with the vendor client late-bound and never a hard dependency.

The client is imported when a connection is first needed and is not a runtime dependency; CI
exercises the adapter against an injected fake. The three operations map onto `upsert_points`,
`query_points` and `delete_points`.

The group scope is a server-side filter applied before Qdrant's top-k (filterable HNSW), never a
post-filter. The collection must be configured for cosine distance, since `VectorMatch.score` is a
cosine in [0, 1]; the operator creates it, so this is a documented requirement rather than enforced
here.
"""

import importlib
import logging
from typing import Any, Protocol, runtime_checkable

from chemclaw.core.config import settings
from chemclaw.core.logging import register_secret_env
from chemclaw.retrieval.vectors.base import (
    VectorMatch,
    VectorPoint,
    VectorStoreConfigError,
    VectorStoreError,
)

logger = logging.getLogger(__name__)


@runtime_checkable
class QdrantClient(Protocol):
    """The slice of the Qdrant async client this adapter uses, so a fake is three methods.

    Declared rather than imported because `qdrant-client` is not a dependency. Signatures are
    keyword-only where the real client's are, so a fake cannot accept a call the real client would
    reject.
    """

    async def upsert(self, *, collection_name: str, points: list[Any]) -> Any:
        """Insert or replace points in a collection."""
        ...

    async def query_points(
        self,
        *,
        collection_name: str,
        query: list[float],
        limit: int,
        query_filter: Any | None = None,
        score_threshold: float | None = None,
    ) -> Any:
        """Rank points by the collection's configured distance, best first."""
        ...

    async def delete(self, *, collection_name: str, points_selector: Any) -> Any:
        """Remove the selected points."""
        ...


def _client_module() -> Any:
    """Import `qdrant_client`, or say which package to install rather than raising `ImportError`.

    A `VectorStoreConfigError` because no retry can install a package.
    """
    try:
        return importlib.import_module("qdrant_client")
    except ImportError as exc:
        raise VectorStoreConfigError(
            "the vector store provider is 'qdrant' but the `qdrant-client` package is not "
            "installed. It is deliberately not a runtime dependency of this repository — a store "
            "nobody configured must not weigh on every pod — so install it in the image that "
            "reaches Qdrant, or set CHEMCLAW_VECTOR_STORE_PROVIDER=pgvector"
        ) from exc


def _models() -> Any:
    """The client's `models` namespace: point structs, filters and distances."""
    return importlib.import_module("qdrant_client.models")


def open_qdrant_client() -> QdrantClient:
    """Build the async Qdrant client this deployment is configured for.

    Reads URL and API key from settings, the one production entry point. The key is registered for
    log redaction here, where it is read; it is also a `SecretStr` in `_SECRET_SETTINGS`, which
    covers it whatever source supplied it.
    """
    module = _client_module()
    register_secret_env("CHEMCLAW_VECTOR_STORE_API_KEY")
    options: dict[str, Any] = {
        "url": settings.vector_store_url,
        "api_key": settings.vector_store_api_key.get_secret_value() or None,
        "timeout": int(settings.vector_store_timeout_seconds),
        # Unconditional: extra keywords are forwarded into the client's own `httpx.AsyncClient` (a
        # caller-supplied `http_client` is refused). Without it a proxy variable on the pod would
        # carry embedded note text and query vectors off-address, past a guard that sees only the
        # dial to the proxy.
        "trust_env": False,
    }
    # The private-CA bundle the other transports honour, passed only when configured: the unset
    # value is the empty string, which is not a path.
    if settings.llm_tls_ca_bundle:
        options["verify"] = settings.llm_tls_ca_bundle
    client: QdrantClient = module.AsyncQdrantClient(**options)
    return client


class QdrantVectorStore:
    """A `VectorStore` over a Qdrant collection. One per process; the client pools internally."""

    def __init__(self, client: QdrantClient | None = None) -> None:
        """Bind to a client, or resolve the configured one lazily on first use.

        Lazy so an unreachable Qdrant is a failure to search, not a failure to boot.
        """
        self._client = client

    def _backend(self) -> QdrantClient:
        """The client, resolved on first use."""
        if self._client is None:
            self._client = open_qdrant_client()
        return self._client

    async def upsert(self, collection: str, points: list[VectorPoint]) -> None:
        """Insert or replace each point by id."""
        if not points:
            return
        models = _models()
        structs = [
            models.PointStruct(
                id=_point_id(point.id),
                vector=point.vector,
                # `ref` is what comes back out and rejoins the catalogue; `group` is what the scope
                # filter matches. Both are indexed payload fields on the collection.
                payload={"ref": point.id, "group": point.group_key},
            )
            for point in points
        ]
        try:
            await self._backend().upsert(collection_name=collection, points=structs)
        except Exception as exc:  # the client raises its own hierarchy; the caller wants one type
            raise VectorStoreError(f"qdrant upsert into {collection!r} failed: {exc}") from exc

    async def search(
        self,
        collection: str,
        embedding: list[float],
        top_k: int,
        groups: set[str] | None = None,
    ) -> list[VectorMatch]:
        """Rank by cosine, filtered to `groups` server-side so the cut follows the filter."""
        if not any(embedding):
            return []
        # An empty scope is "nothing is eligible", which is a different statement from `None` and
        # must not be sent as an unfiltered search. Answering it locally also saves the round trip.
        if groups is not None and not groups:
            return []
        models = _models()
        query_filter = None
        if groups is not None:
            query_filter = models.Filter(
                must=[models.FieldCondition(key="group", match=models.MatchAny(any=sorted(groups)))]
            )
        try:
            response = await self._backend().query_points(
                collection_name=collection,
                query=embedding,
                limit=top_k,
                query_filter=query_filter,
                # The `> 0` floor every other index here applies. Pushed to the server rather than
                # filtered afterwards, so a threshold never costs a slot in the top-k.
                score_threshold=0.0,
            )
        except Exception as exc:
            raise VectorStoreError(f"qdrant search of {collection!r} failed: {exc}") from exc
        return _matches(response)

    async def delete(self, collection: str, ids: list[str]) -> None:
        """Remove these points; absent ids are the state being asked for, not an error."""
        if not ids:
            return
        models = _models()
        try:
            await self._backend().delete(
                collection_name=collection,
                points_selector=models.PointIdsList(points=[_point_id(i) for i in ids]),
            )
        except Exception as exc:
            raise VectorStoreError(f"qdrant delete from {collection!r} failed: {exc}") from exc


def _point_id(reference: str) -> str:
    """Qdrant's own id for a catalogue reference.

    Qdrant accepts only unsigned integers or UUIDs, so a UUIDv5 over the reference: deterministic
    (re-embedding replaces the point) and collision-free. The readable reference is kept in the
    payload as `ref`, which the scope filter matches.
    """
    import uuid

    return str(uuid.uuid5(uuid.NAMESPACE_URL, reference))


def _matches(response: Any) -> list[VectorMatch]:
    """Read a `query_points` response into `VectorMatch`es, dropping anything unusable.

    Accepts `.points` (newer clients) or a bare iterable (older). A point whose payload lost its
    `ref` cannot be rejoined to the catalogue and is dropped.
    """
    points = getattr(response, "points", response)
    matches: list[VectorMatch] = []
    for point in points:
        reference = (getattr(point, "payload", None) or {}).get("ref")
        if not reference:
            logger.warning("qdrant returned a point with no 'ref' payload; skipping it")
            continue
        # The `> 0` floor of the base contract: the clamp alone would keep a negative cosine as a
        # `0.0` hit, and the server's `score_threshold=0.0` admits zero.
        score = float(point.score)
        if score <= 0.0:
            continue
        matches.append(VectorMatch(id=str(reference), score=min(1.0, score)))
    return matches
