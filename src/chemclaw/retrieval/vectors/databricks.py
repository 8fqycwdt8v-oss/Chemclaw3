"""Databricks Mosaic AI Vector Search as a `VectorStore`, with the vendor client late-bound.

The client package is imported at first use and is not a project dependency; CI exercises the
adapter against an injected fake.

**The score is not a cosine.** Databricks scores `1 / (1 + d²)` over Euclidean distance, while
`VectorMatch.score` is a cosine. Normalising both sides to unit length makes the L2 order equal the
cosine order and the conversion exact:

    unit vectors  ->  d² = 2 - 2cos  ->  score = 1/(3 - 2cos)  ->  cos = 1.5 - 0.5/score

**The client blocks**, so every call goes through `asyncio.to_thread` to keep the retrieval
fan-out's event loop free.

**The index is created by the operator** and must be a Direct Vector Access index with the three
columns below; a Delta Sync index cannot be upserted into.
"""

import asyncio
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

# The three columns a Direct Vector Access index must declare; constants because they must match
# what the adapter writes. `group_key` because `group` is a SQL keyword.
ID_COLUMN = "id"
VECTOR_COLUMN = "embedding"
GROUP_COLUMN = "group_key"

# Databricks' score for two orthogonal unit vectors, `1/(1 + 2)`: the `> 0` cosine floor in the
# store's own units, so it can be pushed to the server.
ORTHOGONAL_SCORE = 1.0 / 3.0


@runtime_checkable
class DatabricksIndex(Protocol):
    """The slice of a Vector Search index this adapter uses, so a fake is three methods.

    Declared rather than imported because `databricks-vectorsearch` is not a dependency.
    """

    def upsert(self, inputs: list[dict[str, Any]]) -> Any:
        """Insert or replace rows, keyed by the index's primary key column."""
        ...

    def delete(self, primary_keys: list[str]) -> Any:
        """Remove the rows with these primary keys."""
        ...

    def similarity_search(
        self,
        *,
        columns: list[str],
        query_vector: list[float],
        num_results: int,
        filters: dict[str, Any] | None = None,
        score_threshold: float | None = None,
    ) -> Any:
        """Rank rows by the index's configured metric, best first."""
        ...


@runtime_checkable
class DatabricksSearchClient(Protocol):
    """The one client method this adapter needs: resolving an index on an endpoint."""

    def get_index(self, *, endpoint_name: str, index_name: str) -> DatabricksIndex:
        """The index `index_name` served by `endpoint_name`."""
        ...


def _client_class() -> Any:
    """Import the Vector Search client, or say which package to install.

    Imported via `importlib` by string, since the package is not a declared dependency. Both the old
    (`databricks.vector_search`) and new (`databricks.ai_search`) module names are tried. A
    `VectorStoreConfigError` because no retry can install a package.
    """
    refused: list[str] = []
    for module_name, attribute in (
        ("databricks.vector_search.client", "VectorSearchClient"),
        ("databricks.ai_search.client", "AISearchClient"),
    ):
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            # Keep why each import failed: an installed package with a broken dependency also raises
            # `ImportError`, and "install X" would then point the operator at the wrong fix.
            refused.append(f"{module_name}: {exc}")
            continue
        client = getattr(module, attribute, None)
        if client is not None:
            return client
        refused.append(f"{module_name}: imported, but has no {attribute}")
    raise VectorStoreConfigError(
        "the vector store provider is 'databricks' but neither `databricks-vectorsearch` nor its "
        "successor could be loaded. It is deliberately not a runtime dependency of this "
        "repository — a store nobody configured must not weigh on every pod — so install it in "
        "the image that reaches the workspace, or set CHEMCLAW_VECTOR_STORE_PROVIDER=pgvector. "
        f"What each attempt said: {'; '.join(refused)}"
    )


def open_databricks_client() -> DatabricksSearchClient:
    """Build the Vector Search client this deployment is configured for.

    Reads the workspace URL and token from settings, the one production entry point. The token is
    registered for log redaction here, where it is read.
    """
    client_class = _client_class()
    register_secret_env("CHEMCLAW_VECTOR_STORE_API_KEY")
    client: DatabricksSearchClient = client_class(
        workspace_url=settings.vector_store_url,
        personal_access_token=settings.vector_store_api_key.get_secret_value() or None,
        # The client prints a support notice to stdout on construction. In a worker that is log
        # noise on every process start, and it is not information an operator acts on.
        disable_notice=True,
    )
    return client


def _unit(vector: list[float]) -> list[float]:
    """Scale `vector` to length 1, so Databricks' L2 ranking *is* cosine ranking.

    A zero vector is returned unchanged rather than raising, so a degenerate embedding is a bad hit
    instead of a failed sync.
    """
    magnitude = sum(component * component for component in vector) ** 0.5
    if magnitude == 0.0:
        return vector
    return [component / magnitude for component in vector]


def cosine_from_score(score: float) -> float:
    """Invert Databricks' `1/(1 + d²)` back to the cosine this seam's contract promises.

    Exact for unit vectors, which is why `_unit` is applied on both sides. Clamped into [0, 1] after
    conversion: the top absorbs rounding, and a negative cosine becomes `0.0`, which `_matches`
    drops.
    """
    if score <= 0.0:
        return 0.0
    return max(0.0, min(1.0, 1.5 - 0.5 / score))


class DatabricksVectorStore:
    """A `VectorStore` over a Databricks Direct Vector Access index."""

    def __init__(
        self, client: DatabricksSearchClient | None = None, endpoint: str | None = None
    ) -> None:
        """Bind to a client, or resolve the configured one lazily on first use.

        Lazy so an unreachable workspace is a failure to search, not a failure to boot.
        """
        self._client = client
        self._endpoint = endpoint if endpoint is not None else settings.vector_store_endpoint_name

    def _index(self, collection: str) -> DatabricksIndex:
        """The index object for `collection`, resolving the client on first use.

        `collection` is a Unity Catalog name (`catalog.schema.index`); the endpoint comes from
        settings.
        """
        if self._client is None:
            self._client = open_databricks_client()
        try:
            return self._client.get_index(endpoint_name=self._endpoint, index_name=collection)
        except Exception as exc:  # the client raises its own hierarchy; the caller wants one type
            raise VectorStoreError(
                f"databricks index {collection!r} on endpoint {self._endpoint!r} "
                f"could not be resolved: {exc}"
            ) from exc

    async def upsert(self, collection: str, points: list[VectorPoint]) -> None:
        """Insert or replace each point by id, storing unit vectors."""
        if not points:
            return
        index = self._index(collection)
        rows = [
            {
                ID_COLUMN: point.id,
                VECTOR_COLUMN: _unit(point.vector),
                GROUP_COLUMN: point.group_key,
            }
            for point in points
        ]
        try:
            await asyncio.to_thread(index.upsert, rows)
        except Exception as exc:
            raise VectorStoreError(f"databricks upsert into {collection!r} failed: {exc}") from exc

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
        index = self._index(collection)
        filters = None if groups is None else {GROUP_COLUMN: sorted(groups)}
        try:
            response = await asyncio.to_thread(
                lambda: index.similarity_search(
                    columns=[ID_COLUMN, GROUP_COLUMN],
                    query_vector=_unit(embedding),
                    num_results=top_k,
                    filters=filters,
                    # The `> 0` cosine floor, in the store's own units so a threshold never costs a
                    # slot in the top-k. Pushed to the server exactly as Qdrant's `0.0` is.
                    score_threshold=ORTHOGONAL_SCORE,
                )
            )
        except Exception as exc:
            raise VectorStoreError(f"databricks search of {collection!r} failed: {exc}") from exc
        return _matches(response)

    async def delete(self, collection: str, ids: list[str]) -> None:
        """Remove these points; absent ids are the state being asked for, not an error."""
        if not ids:
            return
        index = self._index(collection)
        try:
            await asyncio.to_thread(index.delete, ids)
        except Exception as exc:
            raise VectorStoreError(f"databricks delete from {collection!r} failed: {exc}") from exc


def _matches(response: Any) -> list[VectorMatch]:
    """Read a `similarity_search` response into `VectorMatch`es, dropping anything unusable.

    Tolerant of shape because the format is not published API. Reads the documented envelope
    (`{"result": {"data_array": [[id, group, score], ...]}, "manifest": {"columns": [{"name": ...},
    ...]}}`) and a plain sequence of mappings. A row without an id cannot be rejoined to the
    catalogue and is dropped.
    """
    rows = _rows(response)
    matches: list[VectorMatch] = []
    for row in rows:
        reference = row.get(ID_COLUMN)
        raw = row.get("score")
        if not reference or raw is None:
            logger.warning("databricks returned a row with no %r or score; skipping it", ID_COLUMN)
            continue
        score = cosine_from_score(float(raw))
        # The floor the seam requires. Applied here as well as pushed to the server, because a
        # threshold the server ignored would otherwise surface an unrelated document as evidence.
        if score <= 0.0:
            continue
        matches.append(VectorMatch(id=str(reference), score=score))
    return matches


def _rows(response: Any) -> list[dict[str, Any]]:
    """Normalise either response form into column-keyed rows.

    Values come back positionally with names alongside; the score is a trailing, unnamed column.
    """
    if isinstance(response, dict):
        result = response.get("result") or {}
        data = result.get("data_array") or []
        names = [
            column.get("name") for column in (response.get("manifest") or {}).get("columns", [])
        ]
        if not names:
            # A recognised envelope whose column metadata moved: `data_array` cannot be read without
            # names,
            # and an empty result would be indistinguishable from an empty corpus.
            logger.warning(
                "databricks returned %d row(s) with no readable column names; the manifest shape "
                "has moved and `_rows` in this module is what needs teaching",
                len(data),
            )
            return []
        return [dict(zip(names, values, strict=False)) for values in data]
    if isinstance(response, list):
        rows = [dict(row) for row in response if isinstance(row, dict)]
        if len(rows) != len(response):
            logger.warning(
                "databricks returned %d entries of which %d were not mappings; dropping them",
                len(response),
                len(response) - len(rows),
            )
        return rows
    # Never silently: an unrecognised response would otherwise read as "no matches" on every search.
    logger.warning(
        "databricks returned a response shape this adapter does not read (%s); treating it as no "
        "matches. If the client was upgraded, `_rows` in this module is what needs teaching",
        type(response).__name__,
    )
    return []
