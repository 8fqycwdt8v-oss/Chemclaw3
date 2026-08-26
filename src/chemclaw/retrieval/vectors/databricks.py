"""Databricks Mosaic AI Vector Search as a `VectorStore`, with the vendor client late-bound.

The second adapter on the seam `D-2026-08-08-a-vector-store-is-not-a-catalogue` opened, and built
the way the first one is: the client package is imported at first use rather than at import time, it
is **not** in `pyproject.toml` (a store nobody has configured must not weigh on every pod), and the
whole adapter is exercised in CI against a fake injected through the constructor.

Two things here are *not* copied from `qdrant.py`, because getting either wrong is a silent wrong
answer rather than a crash.

**The score is not a cosine, and this seam's contract says it is.** Databricks ranks by
`1 / (1 + d²)` over *Euclidean* distance — not by cosine — while `VectorMatch.score` is documented
as a cosine in [0, 1] and `retrieval/hybrid.py` fuses on it. Passing the raw number through would
mis-rank exactly as a Qdrant collection built with `Distance.DOT` would. The conversion is exact
**iff both sides are unit length**, so this adapter normalises on write *and* on query and then
inverts the transform:

    unit vectors  ->  d² = 2 - 2cos  ->  score = 1/(3 - 2cos)  ->  cos = 1.5 - 0.5/score

which checks at all three boundaries: identical (`d=0`, `score=1`) gives 1.0, orthogonal (`d²=2`,
`score=1/3`) gives 0.0, and opposing (`d²=4`, `score=0.2`) gives -1.0 and is dropped by the `> 0`
floor `base.py` requires. Normalising is also what makes Databricks' L2 ranking *order* the same as
cosine ranking in the first place; without it this store would disagree with pgvector on which
document is nearest, and nothing would fail.

**The client blocks.** `databricks-vectorsearch` is synchronous, so every call crosses
`asyncio.to_thread` — the reason `ingest/eln/warehouse/snowflake.py` gives for the same treatment:
a retriever runs inside a `gather`, and a blocking call on the event loop stalls every other leg of
the fan-out for the length of a network round trip. `tests/test_event_loop_offload.py` is the guard.

**The index is created by the operator, not from here**, and it must be a *Direct Vector Access*
index: a Delta Sync index computes its own embeddings from a source table and cannot be upserted or
deleted into, which is the whole of what this seam writes. Its required schema is the three columns
below. `retrieval/vectors/README.md` states this as an operator requirement, the way it states
Qdrant's cosine-distance requirement.
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

# The three columns a Direct Vector Access index must declare. Constants rather than settings: they
# have to match what the adapter writes, so an operator who could change one here would only be able
# to break it. `group_key` rather than `group` because `GROUP` is a SQL keyword and this column is
# queryable from Databricks SQL, where an unquoted `group` is a syntax error.
ID_COLUMN = "id"
VECTOR_COLUMN = "embedding"
GROUP_COLUMN = "group_key"

# The score Databricks returns for two orthogonal unit vectors: `1/(1 + 2)`. The `> 0` cosine floor
# every index on this seam applies, expressed in the store's own units so it can be pushed to the
# server instead of costing a slot in the top-k.
ORTHOGONAL_SCORE = 1.0 / 3.0


@runtime_checkable
class DatabricksIndex(Protocol):
    """The slice of a Vector Search index this adapter uses, so a fake is three methods.

    Declared rather than imported, which is the point: `databricks-vectorsearch` is not a dependency
    of this repository, and a Protocol is how the adapter is type-checked and tested without one.
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

    Imported through `importlib` with a string rather than an `import` statement, which is the
    construction `qdrant.py` uses and it is load-bearing twice over: the package is genuinely not
    installed here, and `tests/test_third_party_layering.py` walks the AST for third-party imports —
    a name it cannot resolve is a dependency this package does not declare, which is the truth.

    The SDK is mid-rename (`databricks.vector_search` -> `databricks.ai_search`), so both are tried
    before giving up. A `VectorStoreConfigError` because no retry can install a package: the
    operator has to act, and the message is the action.
    """
    for module_name, attribute in (
        ("databricks.vector_search.client", "VectorSearchClient"),
        ("databricks.ai_search.client", "AISearchClient"),
    ):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        client = getattr(module, attribute, None)
        if client is not None:
            return client
    raise VectorStoreConfigError(
        "the vector store provider is 'databricks' but neither `databricks-vectorsearch` nor its "
        "successor is installed. It is deliberately not a runtime dependency of this repository — "
        "a store nobody configured must not weigh on every pod — so install it in the image that "
        "reaches the workspace, or set CHEMCLAW_VECTOR_STORE_PROVIDER=pgvector"
    )


def open_databricks_client() -> DatabricksSearchClient:
    """Build the Vector Search client this deployment is configured for.

    Reads the workspace URL and the token from settings rather than taking them as arguments,
    because this is the one production entry point and the alternative is a second place that
    decides what "the vector store" means.

    The token is registered with the log-redaction inventory **here, where it is read** — the
    placement `open_qdrant_client` uses and the one that cannot drift from the value it protects.
    """
    client_class = _client_class()
    register_secret_env("CHEMCLAW_VECTOR_STORE_API_KEY")
    client: DatabricksSearchClient = client_class(
        workspace_url=settings.vector_store_url,
        personal_access_token=settings.vector_store_api_key or None,
        # The client prints a support notice to stdout on construction. In a worker that is log
        # noise on every process start, and it is not information an operator acts on.
        disable_notice=True,
    )
    return client


def _unit(vector: list[float]) -> list[float]:
    """Scale `vector` to length 1, so Databricks' L2 ranking *is* cosine ranking.

    A zero vector has no direction to preserve and is returned unchanged; `search` short-circuits on
    one before it ever reaches here, and an all-zero *point* is meaningless to rank against either
    way. Returning it rather than raising keeps a degenerate embedding a bad hit instead of a failed
    sync.
    """
    magnitude = sum(component * component for component in vector) ** 0.5
    if magnitude == 0.0:
        return vector
    return [component / magnitude for component in vector]


def cosine_from_score(score: float) -> float:
    """Invert Databricks' `1/(1 + d²)` back to the cosine this seam's contract promises.

    Exact for unit vectors, which is why `_unit` is applied on both sides.

    Clamped into [0, 1] after the conversion rather than instead of it. At the top end the clamp
    absorbs floating-point rounding, where `VectorMatch` would otherwise reject a self-similarity a
    hair over 1.0. At the bottom it floors a negative cosine to exactly `0.0` — so what reaches the
    caller is not the negative number itself but a zero, which `_matches` then drops on its `> 0`
    test. The outcome is the one `base.py` specifies (non-positive similarity is not a hit); this
    docstring used to describe the mechanism as a negative surviving the conversion, which its own
    clamp made false.
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

        Lazy for the reason `QdrantVectorStore` is: the data-source registry builds retrieve halves
        in the chat pod at startup, and a store that dialled out from its constructor would make an
        unreachable workspace a failure to *boot* rather than a failure to search.
        """
        self._client = client
        self._endpoint = endpoint if endpoint is not None else settings.vector_store_endpoint_name

    def _index(self, collection: str) -> DatabricksIndex:
        """The index object for `collection`, resolving the client on first use.

        `collection` is a three-level Unity Catalog name (`catalog.schema.index`); the endpoint it
        is served by is a deployment fact and comes from settings.
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

    Tolerant of shape on purpose, the way `qdrant._matches` is tolerant of two client generations —
    and with more reason, because this response format is not published as API. Two forms are read:
    the documented envelope (`{"result": {"data_array": [[id, group, score], ...]},
    "manifest": {"columns": [{"name": ...}, ...]}}`) and a plain sequence of mappings. A row whose
    id did not come back cannot be rejoined to the catalogue and is dropped rather than guessed at.
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

    `similarity_search` returns the column values positionally with the names alongside, so the two
    have to be zipped back together; the score is appended as a trailing column that the `columns`
    request does not name.
    """
    if isinstance(response, dict):
        result = response.get("result") or {}
        data = result.get("data_array") or []
        names = [
            column.get("name") for column in (response.get("manifest") or {}).get("columns", [])
        ]
        if not names:
            # The likelier client-version change of the two, and until now the quietest: an envelope
            # this adapter recognises whose *column* metadata moved. `data_array` cannot be read
            # without names, and an empty result here is indistinguishable from an empty corpus.
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
    # Never silently: tolerant parsing is here to absorb a client-version change, and returning an
    # empty list without a word would turn one into "this corpus has no matches" on every search —
    # the failure the tolerance exists to prevent, inverted. `qdrant._matches` warns per dropped
    # point for the same reason.
    logger.warning(
        "databricks returned a response shape this adapter does not read (%s); treating it as no "
        "matches. If the client was upgraded, `_rows` in this module is what needs teaching",
        type(response).__name__,
    )
    return []
