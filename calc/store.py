"""Calculation result store — compute once, never twice (plan Phase 1b, D-011).

Results are addressed by a **versioned** `CalculationKey`: the calculator's
version is part of the key, so bumping a model or method does not silently return
a stale result — it is a cache miss and recomputes. `ResultStore` is one
interface with swappable backends (in-memory for tests, Postgres for real), and
`cached_compute` is the single lookup-before-compute path every calculator shares
(DRY) — the one place that decides hit vs. miss and persists new results.
"""

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# A result payload is any JSON-serializable mapping. Calculators own their typed
# models; the store persists the plain dict so it stays calculator-agnostic.
ResultPayload = dict[str, Any]


def _hash(obj: Any) -> str:
    """Stable short hash of any JSON-serializable value (canonical form).

    Sorted keys + tight separators make the hash independent of dict ordering and
    whitespace, so semantically identical inputs collapse to the same key.
    """
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


class CalculationKey(BaseModel):
    """Content-addressed identity of a calculation, versioned by the calculator.

    Two calculations share a key iff they are the same calculator *version* run on
    the same input with the same parameters. `calc_version` in the key is what
    prevents a model/method update from returning a pre-update cached result.
    """

    calc_type: str
    calc_version: str
    input_hash: str
    params_hash: str

    @classmethod
    def build(
        cls,
        calc_type: str,
        calc_version: str,
        inputs: Any,
        params: Any = None,
    ) -> "CalculationKey":
        """Construct a key by hashing the inputs and parameters."""
        return cls(
            calc_type=calc_type,
            calc_version=calc_version,
            input_hash=_hash(inputs),
            params_hash=_hash(params),
        )

    def as_str(self) -> str:
        """Flat string form for use as a storage/index key."""
        return f"{self.calc_type}@{self.calc_version}:{self.input_hash}:{self.params_hash}"


class StoredResult(BaseModel):
    """A persisted calculation result plus its provenance.

    `provenance` records how the value came to be. For this compute cache it is always
    "computed" (the system ran the calculator) — retained as GxP audit metadata on every
    persisted row, and the seam by which an externally *measured* value could be stored
    under the same key with `provenance="measured"`. It is audit trail, not a control
    signal: no code branches on it, so it is written and available to an auditor/query,
    not read back into logic.
    """

    key: CalculationKey
    result: ResultPayload
    provenance: str = "computed"


@runtime_checkable
class ResultStore(Protocol):
    """Persistence contract for calculation results. Backends implement this."""

    async def get(self, key: CalculationKey) -> StoredResult | None:
        """Return the stored result for `key`, or None on a miss."""
        ...

    async def put(self, stored: StoredResult) -> None:
        """Persist `stored`, overwriting any existing result for its key."""
        ...


class InMemoryStore:
    """Process-local `ResultStore` for tests and single-run use.

    Proves the compute-once logic without a database; the Postgres backend
    implements the same interface for durable, cross-process caching.
    """

    def __init__(self) -> None:
        """Start with an empty cache."""
        self._data: dict[str, StoredResult] = {}

    async def get(self, key: CalculationKey) -> StoredResult | None:
        """Return the stored result for `key`, or None on a miss."""
        return self._data.get(key.as_str())

    async def put(self, stored: StoredResult) -> None:
        """Persist `stored`, overwriting any existing result for its key."""
        self._data[stored.key.as_str()] = stored


async def cached_compute(
    store: ResultStore,
    key: CalculationKey,
    compute: Callable[[], Awaitable[ResultPayload]],
) -> tuple[ResultPayload, bool]:
    """Return a result for `key`, computing and persisting it only on a miss.

    This is the single lookup-before-compute path (plan step 1b.4): every
    calculator goes through it, so caching behavior is defined in exactly one
    place. `compute` is called only when the store has no entry for `key`.

    Args:
        store: The backend to read from and write to.
        key: The versioned identity of this calculation.
        compute: Zero-arg coroutine that produces the result on a miss.

    Returns:
        `(result, was_cached)` — `was_cached` is True on a store hit, so callers
        can count hits vs. misses for the metrics layer (Phase 2b).
    """
    hit = await store.get(key)
    if hit is not None:
        # DEBUG, not INFO: on the hot path (every calculator call), but it is the one place
        # that answers the recurring troubleshooting question "why did this recompute?".
        logger.debug("calc cache hit: %s", key.as_str())
        return hit.result, True
    logger.debug("calc cache miss, computing: %s", key.as_str())
    result = await compute()
    await store.put(StoredResult(key=key, result=result))
    return result, False


# A calculator's typed result model — the payload the cache stores and reconstructs.
ResultT = TypeVar("ResultT", bound=BaseModel)


async def run_cached(
    store: ResultStore,
    key: CalculationKey,
    compute: Callable[[], ResultT],
    result_type: type[ResultT],
) -> tuple[ResultT, bool]:
    """The calculator contract: run a blocking calculator once, cached (plan 1c.1).

    Every fast calculator repeats the same skeleton — build a versioned key, run
    synchronous CPU-bound work (RDKit/tblite), persist the dict, reconstruct the typed
    model. This captures it once (DRY, Rule of Three across xTB/solubility/pKa): the
    blocking `compute` is offloaded so the event loop stays free, its result is stored
    as a plain dict (the store stays calculator-agnostic), and the dict is validated
    back into `result_type` on both hit and miss. Two *concurrent* misses on the same
    key both compute and last-writer-wins on the upsert — benign duplicate work for
    deterministic calculators; per-key in-flight dedup is deliberately not built.

    Args:
        store: The backend to read from and write to.
        key: The versioned identity of this calculation.
        compute: Zero-arg *synchronous* callable producing the typed result on a miss.
        result_type: The pydantic model to reconstruct from the stored payload.

    Returns:
        `(result, was_cached)` — `was_cached` is True on a store hit.
    """

    async def _compute() -> ResultPayload:
        return (await asyncio.to_thread(compute)).model_dump()

    payload, was_cached = await cached_compute(store, key, _compute)
    return result_type.model_validate(payload), was_cached
