"""Qdrant as a `VectorStore`, with the vendor client late-bound and never a hard dependency.

The same construction `chemclaw.ingest.eln.warehouse.databricks` uses, for the same reasons: the
client package is imported the moment a connection is first needed rather than at import time, so a
deployment that never points at Qdrant never loads it, an image that does not carry it still starts,
and the whole adapter is exercised in CI against a fake client injected through the same seam. The
package is **not** in `pyproject.toml`'s runtime dependencies — a store nobody has configured must
not weigh on every pod.

**Why an adapter is small here.** Everything a vector database is bad at stayed in the catalogue
(`chemclaw.retrieval.vectors.base` says which and why), so what is left is three operations that map
onto Qdrant's own vocabulary almost directly: `upsert` → `upsert_points`, `search` → `query_points`
with an optional payload filter, `delete` → `delete_points`. There is no join to emulate and no
clock to invent.

**Two details this adapter must get right, because they are where the semantics live:**

*The scope is a filter on each point's group, applied by Qdrant before its own top-k.* Passing it
as a post-filter would reproduce exactly the recall defect `base.py` describes and that
`docs/planning/BACKLOG.md` already records against pgvector's post-filtering. Qdrant's filterable
HNSW is the reason this store is worth attaching at all, so the filter goes to the server.

*Cosine similarity is what the collection must be configured for.* Qdrant returns the configured
distance's score, and `VectorMatch.score` is documented as a cosine bounded to [0, 1]. A collection
created with `Distance.DOT` or `Distance.EUCLID` would return numbers in another range entirely and
every fusion above would silently mis-rank. The collection is created by the operator, so this is
stated in `retrieval/vectors/README.md` as a requirement rather than enforced from here.
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

    Declared here rather than imported, which is the point: `qdrant-client` is not a dependency of
    this repository, and a Protocol is how the adapter is type-checked and tested without one. The
    signatures are keyword-only where the real client's are, so a fake that satisfies this cannot
    pass a call the real client would reject.
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

    Late, and only from the two places that genuinely need a connection. A `VectorStoreConfigError`
    because no retry can install a package: the operator has to act, and the message is the action.
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

    Reads the URL and the API key from settings rather than taking them as arguments, because this
    is the one production entry point and the alternative is a second place that decides what
    "the vector store" means.

    **The key is registered with the log-redaction inventory here, at build time**, where the secret
    is read — the warehouse seam's placement, and the one that cannot drift from the read.

    It is no longer the *only* thing covering this key, and for one commit it covered nothing at
    all: `register_secret_env` stores a variable *name* and `_secret_values` resolved it against
    `os.environ`, while `Settings` reads `.env` without exporting anything — so on the documented
    `.env` posture the registration resolved to the empty string. `logging._configured_by` closes
    that, and the field is now a `SecretStr` in `_SECRET_SETTINGS` besides, which is the protection
    that cannot depend on where the value came from.
    """
    module = _client_module()
    register_secret_env("CHEMCLAW_VECTOR_STORE_API_KEY")
    options: dict[str, Any] = {
        "url": settings.vector_store_url,
        "api_key": settings.vector_store_api_key.get_secret_value() or None,
        "timeout": int(settings.vector_store_timeout_seconds),
    }
    # The private-CA bundle the rest of this system's transports honour, passed **only** when one is
    # configured. Not unconditionally: `verify` is forwarded to the underlying httpx client rather
    # than being part of the constructor's own documented signature, and nothing here has run
    # against a real client, so an unrecognised keyword would fail every deployment — including the
    # ones that never needed a private CA. Narrowing it means the default path uses only the three
    # keywords the client certainly accepts, and the risk is carried by the deployments that opt in.
    if settings.llm_tls_ca_bundle:
        options["verify"] = settings.llm_tls_ca_bundle
    client: QdrantClient = module.AsyncQdrantClient(**options)
    return client


class QdrantVectorStore:
    """A `VectorStore` over a Qdrant collection. One per process; the client pools internally."""

    def __init__(self, client: QdrantClient | None = None) -> None:
        """Bind to a client, or resolve the configured one lazily on first use.

        Lazy so that constructing this opens no connection: the data-source registry builds retrieve
        halves in the chat pod at startup, and a store that dialled out from its constructor would
        make an unreachable Qdrant a failure to *boot* rather than a failure to search.
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

    Qdrant accepts an unsigned integer or a UUID as a point id and nothing else, while this system's
    references are readable strings (`doc-9f2a1c…#3`). A UUIDv5 over the reference is the standard
    resolution: deterministic, so re-embedding the same chunk replaces its point rather than adding
    one, and collision-free in the way a hash truncation would not be. The readable form is kept in
    the payload as `ref`, which is what the scope filter matches and what comes back out.
    """
    import uuid

    return str(uuid.uuid5(uuid.NAMESPACE_URL, reference))


def _matches(response: Any) -> list[VectorMatch]:
    """Read a `query_points` response into `VectorMatch`es, dropping anything unusable.

    Tolerant of the two response shapes the client has carried — `.points` on newer versions, a bare
    iterable on older ones — because pinning this adapter to one client minor would make a routine
    dependency bump a code change. A point whose payload lost its `ref` cannot be rejoined to the
    catalogue and is dropped rather than guessed at.
    """
    points = getattr(response, "points", response)
    matches: list[VectorMatch] = []
    for point in points:
        reference = (getattr(point, "payload", None) or {}).get("ref")
        if not reference:
            logger.warning("qdrant returned a point with no 'ref' payload; skipping it")
            continue
        matches.append(VectorMatch(id=str(reference), score=min(1.0, max(0.0, float(point.score)))))
    return matches
