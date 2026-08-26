"""The Databricks Vector Search adapter, against a fake index client.

Its own file rather than a section in `tests/test_vector_store.py`, because that module installs an
**autouse** fixture patching the Qdrant adapter's `_models` namespace: anything added beside it
would silently inherit that patch. `tests/test_warehouse_retriever.py` is its own file for the same
reason.

What is worth testing here is not "does the fake echo what it was handed" — it is the three places
this adapter can be wrong in a way no exception reports:

* the score arithmetic, because Databricks ranks by a rescaled *Euclidean* distance while this
  seam's contract is a cosine, and a mis-scaled score mis-ranks every fusion above it silently;
* the scope, because an empty one means "nothing is eligible" and sending it as an unfiltered
  search returns the whole corpus; and
* the normalisation, because Databricks' L2 ordering only equals cosine ordering for unit vectors.
"""

import asyncio
import functools
import math
from typing import Any

import pytest

from chemclaw.retrieval.vectors.base import VectorPoint, VectorStore, VectorStoreError
from chemclaw.retrieval.vectors.databricks import (
    GROUP_COLUMN,
    ID_COLUMN,
    ORTHOGONAL_SCORE,
    VECTOR_COLUMN,
    DatabricksVectorStore,
    cosine_from_score,
)

COLLECTION = "main.chemclaw.document_chunks"


def _sync(test: Any) -> Any:
    """Run an `async def` test on its own loop, so pytest collects a plain function.

    This repository has no async pytest plugin; the same decorator sits at the top of
    `tests/test_vector_store.py` for the same reason.
    """

    @functools.wraps(test)
    def runner(*args: Any, **kwargs: Any) -> None:
        asyncio.run(test(*args, **kwargs))

    return runner


def _score_for(cosine: float) -> float:
    """What Databricks would return for two unit vectors this far apart: `1/(1 + d²)`."""
    return 1.0 / (1.0 + (2.0 - 2.0 * cosine))


class _FakeIndex:
    """Records every call and replays a canned response, so the request shape is assertable."""

    def __init__(self, rows: list[list[Any]] | None = None) -> None:
        self.upserted: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.searches: list[dict[str, Any]] = []
        self._rows = rows or []

    def upsert(self, inputs: list[dict[str, Any]]) -> Any:
        self.upserted.extend(inputs)
        return {"status": "SUCCESS"}

    def delete(self, primary_keys: list[str]) -> Any:
        self.deleted.extend(primary_keys)
        return {"status": "SUCCESS"}

    def similarity_search(self, **kwargs: Any) -> Any:
        self.searches.append(kwargs)
        return {
            "manifest": {
                "columns": [{"name": ID_COLUMN}, {"name": GROUP_COLUMN}, {"name": "score"}]
            },
            "result": {"data_array": self._rows},
        }


class _FakeClient:
    """A client that hands back one index, and records what was asked for."""

    def __init__(self, index: _FakeIndex) -> None:
        self.index = index
        self.requested: list[tuple[str, str]] = []

    def get_index(self, *, endpoint_name: str, index_name: str) -> _FakeIndex:
        self.requested.append((endpoint_name, index_name))
        return self.index


def _store(index: _FakeIndex) -> DatabricksVectorStore:
    return DatabricksVectorStore(client=_FakeClient(index), endpoint="chemclaw-endpoint")


def test_the_adapter_satisfies_the_protocol() -> None:
    """It is a `VectorStore` — the same check the reference and the Qdrant adapter get."""
    assert isinstance(_store(_FakeIndex()), VectorStore)


# --- the score arithmetic: the one thing that fails silently ------------------------------------


@pytest.mark.parametrize(
    ("cosine", "expected"),
    [(1.0, 1.0), (0.5, 0.5), (0.0, 0.0), (-0.5, 0.0), (-1.0, 0.0)],
)
def test_the_databricks_score_converts_back_to_the_cosine_it_came_from(
    cosine: float, expected: float
) -> None:
    """`cos = 1.5 - 0.5/score` inverts `score = 1/(1 + d²)` exactly, for unit vectors.

    The whole reason this adapter normalises. A negative cosine converts to a negative number and
    is floored at 0 here, which is what makes it *not a hit* to the caller — the `> 0` rule
    `retrieval/vectors/base.py` states and both Postgres indexes apply.
    """
    assert cosine_from_score(_score_for(cosine)) == pytest.approx(max(0.0, expected), abs=1e-9)


def test_the_orthogonal_score_is_the_floor_pushed_to_the_server() -> None:
    """`ORTHOGONAL_SCORE` is exactly the score of a zero cosine, so the threshold *is* the floor."""
    assert ORTHOGONAL_SCORE == pytest.approx(_score_for(0.0))
    assert cosine_from_score(ORTHOGONAL_SCORE) == pytest.approx(0.0)


@_sync
async def test_a_negative_cosine_is_not_a_hit() -> None:
    """A row the server let through below the floor is still dropped, not surfaced as evidence."""
    index = _FakeIndex(rows=[["doc-a#0", "doc-a", _score_for(0.8)], ["doc-b#0", "doc-b", 0.2]])
    hits = await _store(index).search(COLLECTION, [1.0, 0.0], 5)
    assert [hit.id for hit in hits] == ["doc-a#0"]
    assert hits[0].score == pytest.approx(0.8)


# --- normalisation ------------------------------------------------------------------------------


@_sync
async def test_points_are_stored_as_unit_vectors() -> None:
    """Databricks' L2 ranking only equals cosine ranking when both sides have length 1."""
    index = _FakeIndex()
    await _store(index).upsert(COLLECTION, [VectorPoint(id="n-1", vector=[3.0, 4.0])])
    stored = index.upserted[0][VECTOR_COLUMN]
    assert math.isclose(sum(c * c for c in stored) ** 0.5, 1.0)
    assert stored == pytest.approx([0.6, 0.8])


@_sync
async def test_the_query_vector_is_normalised_too() -> None:
    """Both sides, or the conversion is not the transform it claims to invert."""
    index = _FakeIndex()
    await _store(index).search(COLLECTION, [0.0, 2.0], 3)
    assert index.searches[0]["query_vector"] == pytest.approx([0.0, 1.0])


@_sync
async def test_a_zero_vector_never_reaches_the_store() -> None:
    """It has cosine 0 to everything, so it is no hit — and no round trip."""
    index = _FakeIndex()
    assert await _store(index).search(COLLECTION, [0.0, 0.0], 3) == []
    assert index.searches == []


# --- the scope: pre-filter, and the empty-set rule from both ends -------------------------------


@_sync
async def test_a_group_scope_is_sent_as_a_server_side_filter() -> None:
    """Applied before the top-k, which is the whole reason an external store is worth attaching."""
    index = _FakeIndex()
    await _store(index).search(COLLECTION, [1.0, 0.0], 5, {"doc-b", "doc-a"})
    assert index.searches[0]["filters"] == {GROUP_COLUMN: ["doc-a", "doc-b"]}


@_sync
async def test_an_unfiltered_search_sends_no_filter() -> None:
    """`None` means the whole collection and must cost nothing extra."""
    index = _FakeIndex()
    await _store(index).search(COLLECTION, [1.0, 0.0], 5, None)
    assert index.searches[0]["filters"] is None


@_sync
async def test_an_empty_scope_returns_nothing_without_a_round_trip() -> None:
    """An empty scope means nothing is eligible — never send that as an unfiltered search."""
    index = _FakeIndex()
    assert await _store(index).search(COLLECTION, [1.0, 0.0], 5, set()) == []
    assert index.searches == []


@_sync
async def test_the_floor_is_pushed_to_the_server() -> None:
    """A threshold applied afterwards would cost slots in the top-k."""
    index = _FakeIndex()
    await _store(index).search(COLLECTION, [1.0, 0.0], 7)
    assert index.searches[0]["score_threshold"] == pytest.approx(ORTHOGONAL_SCORE)
    assert index.searches[0]["num_results"] == 7


# --- addressing, empty calls, and error translation ---------------------------------------------


@_sync
async def test_the_index_is_addressed_by_endpoint_and_catalogue_name() -> None:
    """An index is a pair; resolving it from the name alone is not possible."""
    index = _FakeIndex()
    client = _FakeClient(index)
    await DatabricksVectorStore(client=client, endpoint="ep-1").search(COLLECTION, [1.0], 1)
    assert client.requested == [("ep-1", COLLECTION)]


@_sync
async def test_empty_writes_are_no_round_trip() -> None:
    """The sync calls both with nothing to do on every quiet pass."""
    index = _FakeIndex()
    store = _store(index)
    await store.upsert(COLLECTION, [])
    await store.delete(COLLECTION, [])
    assert index.upserted == [] and index.deleted == []


@_sync
async def test_delete_passes_the_catalogue_keys_through() -> None:
    """The id this seam addresses a point by *is* the index's primary key — no re-encoding."""
    index = _FakeIndex()
    await _store(index).delete(COLLECTION, ["doc-a#chunks#0", "doc-a#chunks#1"])
    assert index.deleted == ["doc-a#chunks#0", "doc-a#chunks#1"]


@_sync
async def test_a_row_without_an_id_is_dropped_rather_than_guessed_at() -> None:
    """It cannot be rejoined to the catalogue, so it is not a citation anybody could check."""
    index = _FakeIndex(rows=[[None, "doc-a", 0.9], ["doc-b#0", "doc-b", _score_for(0.7)]])
    assert [hit.id for hit in await _store(index).search(COLLECTION, [1.0], 5)] == ["doc-b#0"]


@pytest.mark.parametrize("method", ["upsert", "search", "delete"])
@_sync
async def test_a_client_failure_becomes_one_retryable_error_type(method: str) -> None:
    """The client raises its own hierarchy; a caller deciding retry wants `VectorStoreError`.

    `VectorStoreError` is a `SubsystemUnavailableError` and deliberately not a `ChemclawError`, so
    a durable activity rides out a blip rather than registering it as bad data.
    """

    class _Broken(_FakeIndex):
        def upsert(self, inputs: list[dict[str, Any]]) -> Any:
            raise RuntimeError("workspace unreachable")

        def delete(self, primary_keys: list[str]) -> Any:
            raise RuntimeError("workspace unreachable")

        def similarity_search(self, **kwargs: Any) -> Any:
            raise RuntimeError("workspace unreachable")

    store = _store(_Broken())
    with pytest.raises(VectorStoreError):
        if method == "upsert":
            await store.upsert(COLLECTION, [VectorPoint(id="n-1", vector=[1.0])])
        elif method == "search":
            await store.search(COLLECTION, [1.0], 3)
        else:
            await store.delete(COLLECTION, ["n-1"])


@_sync
async def test_an_unreadable_response_shape_warns_rather_than_reading_as_no_matches(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tolerant parsing must not turn a client-version change into a silently empty corpus.

    `_rows` reads two shapes and falls through for anything else. Falling through *quietly* would
    make every search return nothing with no trace — the failure the tolerance exists to absorb,
    inverted, and the one an operator has no way to notice.
    """
    import logging

    class _Odd(_FakeIndex):
        def similarity_search(self, **kwargs: Any) -> Any:
            return "an unexpected envelope"

    with caplog.at_level(logging.WARNING):
        assert await _store(_Odd()).search(COLLECTION, [1.0, 0.0], 5) == []

    assert any("does not read" in record.getMessage() for record in caplog.records)
