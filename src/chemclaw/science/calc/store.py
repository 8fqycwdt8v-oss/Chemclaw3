"""Calculation result store — compute once, never twice (D-011).

Results are addressed by a versioned `CalculationKey`, so a calculator change is a miss, not a stale
hit; `CALCULATION_EPOCH` covers changes on our side. `cached_compute` is the single
lookup-before-compute path: concurrent misses share one computation inside a process (a future) and,
with `session_store="postgres"`, across processes (`science/calc/flight.py`).
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import datetime
from math import isfinite
from typing import Any, Protocol, runtime_checkable
from weakref import WeakKeyDictionary

from pydantic import BaseModel, Field, model_validator

from chemclaw.core.chem import require_canonical_smiles
from chemclaw.core.ids import stable_hash
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.science.calc.flight import ClaimsProvider, PeerWaitTimeout, single_flight, wait_budget

logger = logging.getLogger(__name__)

# A result payload is any JSON-serializable mapping. Calculators own their typed
# models; the store persists the plain dict so it stays calculator-agnostic.
ResultPayload = dict[str, Any]


class CorruptCacheRow(ValueError):
    """A value in or headed for `calculation_results` is not a result this cache can hand back.

    Its own type because the remedy differs: a bad row is an operator's job (delete it, let the next
    call recompute), while a calculator's own `ValidationError` is a code change.
    """


# ChemClaw's own contribution to a stored result's meaning — what no `calc_version` sees. It
# composes with the server's constant of the same name (`remote_key` folds it over the server's
# `params_hash`), so either bump alone re-addresses every row
# (`tests/test_calc_remote.py::test_the_two_epochs_compose_rather_than_having_to_match`). Every new
# place that assembles a key must fold it in.
#
# **Bump it whenever a ChemClaw-side change makes an already-written row wrong or incomplete**,
# and log it here. `tests/test_calc_payload_schemas.py` flags payload-shape changes.
#
#   1 — introduced (linear-rotor entropy fix, solubility applicability-domain flag).
#   2 — reactivity panel: global/local conceptual-DFT descriptors, Wiberg and free valence.
CALCULATION_EPOCH = "2"


class CalculationKey(BaseModel):
    """Content-addressed identity of a calculation, versioned by the calculator.

    Two calculations share a key iff they are the same calculator *version* run on
    the same input with the same parameters, under the same `CALCULATION_EPOCH`.
    `calc_version` is what prevents a model/method update from returning a pre-update
    cached result; the epoch is what prevents a *ChemClaw*-side fix or payload change
    from doing the same.

    **`build` is not the live path, and a reader verifying an epoch bump through it verifies
    nothing.** Every `calc` key in a deployment comes back from the calculation server as four
    parts and is assembled by `connectors.calc.remote.remote_key`, which folds the epoch in there;
    `build` has no caller in `src/` since the physics left. It is kept rather than deleted because
    it is the one definition of the fold the suite can exercise — hand-folding the epoch across the
    seven test files that construct keys would put the rule in the tests and leave `src/` with
    none — but the two folds are separate code and a new way of assembling a key is a new place to
    fold the epoch.

    **`calc_version` names every program whose output survives into the payload, and no program
    that does not run** (D-2026-08-01-a-key-names-what-ran) — a calculation that composes two
    programs names both, because either one moving changes the number.

    That is a *different* axis from `CALCULATION_EPOCH`, and the two are deliberately not
    merged. The epoch is a source constant: it moves once per release and invalidates every
    deployment at the same moment. A backend is configuration — two deployments running the
    identical release resolve different ones, which is why `xtb_spec.resolve_backend` refuses to
    let `auto` reach a key. Folding a backend into the epoch would make switching one a code
    change; folding the epoch into a version string would make a ChemClaw-side fix invisible
    wherever the underlying programs did not also move, which is the failure the epoch exists
    for.
    """

    # `as_str()` is the primary key: barring `@` from `calc_type` and `:` from the hashes makes it
    # unambiguous while `calc_version` may contain both. `kg/note.py`'s `_CALC_REF` matches this
    # shape (bound by a test).
    calc_type: str = Field(min_length=1, pattern=r"^[^\s@:]+$")
    calc_version: str = Field(min_length=1, pattern=r"^\S+$")
    input_hash: str = Field(min_length=1, pattern=r"^[^\s:]+$")
    params_hash: str = Field(min_length=1, pattern=r"^[^\s:]+$")

    @classmethod
    def build(
        cls,
        calc_type: str,
        calc_version: str,
        inputs: Any,
        params: Any = None,
    ) -> "CalculationKey":
        """Construct a key by hashing the inputs and parameters.

        Not the live path: deployed `calc` keys are assembled by
        `connectors.calc.remote.remote_key`, which folds `CALCULATION_EPOCH` in separately.
        """
        return cls(
            calc_type=calc_type,
            calc_version=calc_version,
            input_hash=stable_hash(inputs),
            params_hash=stable_hash({"epoch": CALCULATION_EPOCH, "params": params}),
        )

    def as_str(self) -> str:
        """Flat string form for use as a storage/index key."""
        return f"{self.calc_type}@{self.calc_version}:{self.input_hash}:{self.params_hash}"


def checked_payload(key: CalculationKey, value: object) -> ResultPayload:
    """`value` as a result payload, or `CorruptCacheRow` naming the key it is stored under.

    The store's own type, not a calculator schema: a non-empty JSON object a `jsonb` column can
    hold. Refused, each named by field: non-object or empty, non-finite floats, values with no JSON
    form, NUL in strings, non-string keys. Floats ≥ 1e16 (read back as `int`) and tuples (as lists)
    survive and are allowed.

    Raises:
        CorruptCacheRow: `value` is not a JSON object, is an empty one, or holds a value the
            `result` column would reject — each named by the field it sits on.
    """
    if not isinstance(value, dict):
        raise CorruptCacheRow(
            f"calculation_results row {key.as_str()!r} holds {type(value).__name__} where a JSON "
            "object is required. The column is bare `JSONB NOT NULL`, so this row was written by "
            "something other than this store; delete it and the next call recomputes."
        )
    if not value:
        raise CorruptCacheRow(
            f"calculation_results row {key.as_str()!r} holds an empty result. No calculator "
            "produces one — an empty object is what a truncated or failed call to the calculation "
            "server degrades into — and D-011 would make it permanent, so it is refused rather "
            "than cached or handed back."
        )
    unstorable = _unstorable(value, "result")
    if unstorable is not None:
        raise CorruptCacheRow(
            f"calculation_results row {key.as_str()!r} {unstorable}. The `result` column is "
            "`jsonb`, which cannot hold it, so persisting this would fail at the driver with a "
            "message naming a JSON token instead of the field — and D-011 would recompute it on "
            "every later call. Fix or drop the field named above."
        )
    return value


def _unstorable(value: object, path: str) -> str | None:
    """The first reason `value` could not be written to a `jsonb` column, or None.

    Iterative depth-first walk; `path` is the dotted address used in the message.
    """
    pending: list[tuple[object, str]] = [(value, path)]
    while pending:
        item, where = pending.pop()
        # One check for both, because `bool` is a subclass of `int` and JSON holds either.
        if item is None or isinstance(item, bool | int):
            continue
        if isinstance(item, float):
            if not isfinite(item):
                return f"holds the non-finite float {item!r} at {where}"
            continue
        if isinstance(item, str):
            if "\x00" in item:
                return f"holds a string containing a NUL character at {where}"
            continue
        if isinstance(item, dict):
            for name, nested in item.items():
                if not isinstance(name, str):
                    return (
                        f"is keyed at {where} by {type(name).__name__} {name!r}, which is not the "
                        "string a JSON object's member name has to be"
                    )
                pending.append((nested, f"{where}.{name}"))
            continue
        if isinstance(item, list | tuple):
            pending.extend((nested, f"{where}[{index}]") for index, nested in enumerate(item))
            continue
        return f"holds {type(item).__name__} {item!r} at {where}, which has no JSON form"
    return None


class StoredResult(BaseModel):
    """A persisted calculation result plus its provenance.

    `provenance` records how the value came to be. For this compute cache it is always
    "computed" (the system ran the calculator) — retained as audit metadata on every
    persisted row, and the seam by which an externally *measured* value could be stored
    under the same key with `provenance="measured"`. It is audit trail, not a control
    signal: no code branches on it, so it is written and available to an auditor/query,
    not read back into logic.
    """

    key: CalculationKey
    result: ResultPayload
    provenance: str = "computed"
    # Wall time of the miss that produced this, or None for a result that arrived otherwise (a
    # measured value, a backfill). Artifact eviction orders by it.
    compute_seconds: float | None = None
    # When the row was written; filled by `find` (browsing), left None by `get`.
    created_at: datetime | None = None
    # The 3-D geometry this calculation ran *on* (never the one it produced), as the server reported
    # it, so "have we relaxed this conformer?" is a query. Not `input_hash`, which also covers
    # solvent and other arguments. Empty for a molecule-keyed calculator.
    structure_id: str = ""
    # The `CALCULATION_EPOCH` this row was written under, for `find` to drop superseded rows.
    # Empty means written before migration 090 (returned, not hidden). Proves "superseded", not
    # "current", since the server's epoch is not recorded.
    epoch: str = ""


# Calculators keyed on a 3-D structure, which a `smiles` filter cannot address (use
# `structure_id`). `geometry.` is no longer written, but old rows remain.
STRUCTURE_KEYED_PREFIXES = ("xtb.", "geometry.")


def molecule_hash(smiles: str) -> str:
    """The `input_hash` a molecule-keyed calculator would produce for `smiles`.

    Re-derives the server's hash, so both sides' RDKit canonicalisation must agree;
    `tests/test_ids.py` checks it against the sibling's key functions.
    """
    return stable_hash({"smiles": require_canonical_smiles(smiles)})


class CalculationQuery(BaseModel):
    """A search over stored results — the browse half of a store built for exact lookup.

    Every field is a filter and every one is optional, so an empty query is "the most recent
    results" rather than an error. `smiles` is matched by hashing it the way a key is built:
    `input_hash` is not reversible, so a molecule is found by computing its hash, never by
    scanning rows and un-hashing them.

    **A molecule filter reaches the molecule-keyed calculators only** — see
    `STRUCTURE_KEYED_PREFIXES`. Combining it with a structure-keyed `calc_type` raises rather than
    returning an empty list, because the empty list would read as "nothing has been computed" when
    the truth is "that family cannot be looked up this way".

    A `structure_id` filter and a `smiles` filter compose rather than conflict — the first narrows
    to one geometry, the second to one molecule, and a geometry belongs to a molecule — but only
    the second is refused against a structure-keyed type, because only the second cannot address
    one.

    **A superseded epoch is excluded and is not a filter.** `CALCULATION_EPOCH` rides inside
    `params_hash`, so a row it invalidated is unreachable by `get` and was fully reachable by this
    — two rows for one molecule under one `calc_version`, indistinguishable, with the wrong one
    offered as evidence to cite. `_matches` drops a row whose recorded epoch is not the current
    one, with no way to ask for it back, because such a row is wrong rather than merely old. A row
    written before migration 090 records no epoch and is returned; `find_calculations` marks it.

    There is deliberately no filter on the result's *value*. The payload is an opaque
    calculator-owned mapping (`ResultPayload`) — the store has been calculator-agnostic since
    D-011, and a `total_energy_hartree > x` predicate would put one calculator's schema inside the
    thing that persists all of them. A caller that wants that filters the returned rows.
    """

    # Matched by hashing, never by scanning — see above.
    smiles: str | None = None
    # The geometry a calculation was about, as `optimize_geometry` and `sample_conformers` report
    # it; the filter for structure-keyed calculators.
    structure_id: str | None = None
    calc_type: str | None = None
    calc_version: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = 20

    @model_validator(mode="after")
    def _molecule_filter_addresses_the_type(self) -> "CalculationQuery":
        """Refuse a molecule filter on a family a molecule cannot address."""
        if self.smiles is None or self.calc_type is None:
            return self
        if self.calc_type.startswith(STRUCTURE_KEYED_PREFIXES):
            raise ValueError(
                f"{self.calc_type!r} is keyed by 3-D structure, not by molecule, so it cannot be "
                "found by SMILES. Give a `structure_id` instead — the st_... address a geometry "
                "calculation reports — or ask for a molecule-keyed calculation (pka, solubility, "
                "descriptors)."
            )
        return self


class CalculationPage(list[StoredResult]):
    """One page of a browse: the rows, **and the two things the rows cannot say**.

    `total_matched` and `unreadable`, so a capped or filtered page is not read as everything. A
    `list` subclass so sequence callers are unchanged; slicing drops the counts.
    """

    def __init__(
        self,
        rows: Iterable[StoredResult],
        *,
        total_matched: int,
        truncated: bool,
        unreadable: int = 0,
    ) -> None:
        """Hold `rows` together with what the query found beyond them."""
        super().__init__(rows)
        self.total_matched = total_matched
        self.truncated = truncated
        self.unreadable = unreadable


def as_page(rows: list[StoredResult], limit: int) -> CalculationPage:
    """`rows` as a page, conservatively, when the store did not report one.

    A third-party store returning a plain list has its full page reported as truncated, the reading
    that cannot claim unmeasured completeness.
    """
    if isinstance(rows, CalculationPage):
        return rows
    return CalculationPage(rows, total_matched=len(rows), truncated=len(rows) >= limit)


@runtime_checkable
class ResultStore(Protocol):
    """Persistence contract for calculation results. Backends implement this."""

    async def get(self, key: CalculationKey) -> StoredResult | None:
        """Return the stored result for `key`, or None on a miss."""
        ...

    async def put(self, stored: StoredResult) -> None:
        """Persist `stored`, overwriting any existing result for its key."""
        ...

    async def find(self, query: CalculationQuery) -> list[StoredResult]:
        """Return results matching `query`, newest first, capped at `query.limit`.

        Declared as a list so wrapping stores that forward the inner answer still satisfy the
        protocol; callers needing counts go through `as_page`.
        """
        ...


class InMemoryStore:
    """Process-local `ResultStore` — the reference the Postgres one is written to match.

    A differential test oracle, not a deployment backend: no configuration returns it.
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

    async def known(self, keys: Sequence[str]) -> set[str]:
        """Which of `keys` the cache holds — parity with the Postgres store's existence probe."""
        return {key for key in keys if key in self._data}

    async def find(self, query: CalculationQuery) -> CalculationPage:
        """Return results matching `query`, newest first, capped at `query.limit`.

        Insertion order stands in for time; dated rows sort by `created_at`, undated rows lead.
        """
        matched = [stored for stored in self._data.values() if _matches(stored, query)]
        matched.reverse()  # newest first, since dict order is insertion order
        undated = [stored for stored in matched if stored.created_at is None]
        dated: list[tuple[datetime, StoredResult]] = [
            (stored.created_at, stored) for stored in matched if stored.created_at is not None
        ]
        dated.sort(key=lambda pair: pair[0], reverse=True)
        ordered = undated + [stored for _, stored in dated]
        # This store holds parsed rows, so `unreadable` is structurally zero here — a payload that
        # is not a mapping cannot be `put`. Its Postgres sibling reads jsonb and can meet one.
        return CalculationPage(
            ordered[: query.limit],
            total_matched=len(ordered),
            truncated=len(ordered) > query.limit,
        )


def _matches(stored: StoredResult, query: CalculationQuery) -> bool:
    """Whether one stored result satisfies every filter set on `query`.

    Postgres expresses the same predicate in SQL; tests pin the two agreeing.
    """
    key = stored.key
    if query.calc_type is not None and key.calc_type != query.calc_type:
        return False
    if query.calc_version is not None and key.calc_version != query.calc_version:
        return False
    if query.smiles is not None and key.input_hash != molecule_hash(query.smiles):
        return False
    if query.structure_id is not None and stored.structure_id != query.structure_id:
        return False
    if query.since is not None and (stored.created_at is None or stored.created_at < query.since):
        return False
    if query.until is not None and (stored.created_at is None or stored.created_at > query.until):
        return False
    # Unconditional: a row from another epoch is wrong or incomplete by definition, not an older
    # version a chemist might want (that is `calc_version`). `get` and `known` still reach it by
    # key.
    if stored.epoch and stored.epoch != CALCULATION_EPOCH:
        return False
    return True


#: Computations in flight by key, per event loop (an `asyncio.Future` belongs to one): a second
#: miss in the process awaits the first. Across processes the claim table coordinates
#: (`science/calc/flight.py`).
_Ledger = dict[str, "asyncio.Future[tuple[ResultPayload, float]]"]
_IN_FLIGHT: "WeakKeyDictionary[asyncio.AbstractEventLoop, _Ledger]" = WeakKeyDictionary()


def _in_flight() -> _Ledger:
    """This loop's slice of the single-flight ledger, created on first use."""
    return _IN_FLIGHT.setdefault(asyncio.get_running_loop(), {})


async def cached_compute(
    store: ResultStore,
    key: CalculationKey,
    compute: Callable[[], Awaitable[ResultPayload]],
    *,
    structure_id: str = "",
    wait_seconds: float | None = None,
) -> tuple[ResultPayload, bool]:
    """Return a result for `key`, computing and persisting it only on a miss.

    Concurrent misses on one key share one computation: in this process through a future and, when
    the store can coordinate (`session_store="postgres"`), across processes through a claim — the
    other processes wait for the holder's result instead of computing it. A failure fails every
    waiter and clears the slot; a holder killed mid-computation hands the key to exactly one
    waiter once its lease lapses. The result is shape-checked (`checked_payload`) before it
    becomes permanent. A hit costs one read and no claim. Metered by
    `chemclaw_calc_cache_total{outcome}` (`hit`, `miss`, `shared`) and, across processes,
    `chemclaw_calc_claims_total`.

    Args:
        store: The backend to read from and write to.
        key: The versioned identity of this calculation.
        compute: Zero-arg coroutine that produces the result on a miss.
        structure_id: The geometry this calculation is about, recorded for lookup by geometry; empty
            for molecule-keyed calculators.
        wait_seconds: The longest this caller waits on someone else's computation — what the
            computer itself would have been allowed (the calculation's request timeout); default
            `calc_server_timeout_seconds`. Unused when nobody else is computing.

    Returns:
        `(result, was_cached)`; `was_cached` is True on a hit and on a joined computation.

    Raises:
        PeerComputationFailed: another process's computation of this key failed while awaited.
        PeerWaitTimeout: another computation outlasted `wait_seconds`; it continues unharmed.
    """
    hit = await store.get(key)
    if hit is not None:
        # DEBUG, not INFO: on the hot path (every calculator call), but it is the one place
        # that answers the recurring troubleshooting question "why did this recompute?".
        logger.debug("calc cache hit: %s", key.as_str())
        record_metric(lambda m: m.increment("chemclaw_calc_cache_total", labels={"outcome": "hit"}))
        _credit_saved_seconds(hit.compute_seconds)
        return hit.result, True
    slot = key.as_str()
    ledger = store.claims() if isinstance(store, ClaimsProvider) else None
    budget = wait_budget(wait_seconds)
    in_flight = _in_flight()
    waiting = in_flight.get(slot)
    if waiting is not None:
        logger.debug("calc cache miss already computing elsewhere, awaiting: %s", slot)
        # Counted before the await: the join happened even if the computation is later cancelled.
        record_metric(
            lambda m: m.increment("chemclaw_calc_cache_total", labels={"outcome": "shared"})
        )
        result, saved = await _join(waiting, slot, budget if ledger else None)
        # Credited once the joined computation finishes and its cost is known; never if it was
        # cancelled.
        _credit_saved_seconds(saved)
        return result, True
    # The future carries the computation's wall clock, which waiters need to credit what their join
    # saved.
    future: asyncio.Future[tuple[ResultPayload, float]] = asyncio.get_running_loop().create_future()
    in_flight[slot] = future

    async def _produce() -> tuple[ResultPayload, float]:
        logger.debug("calc cache miss, computing: %s", slot)
        record_metric(
            lambda m: m.increment("chemclaw_calc_cache_total", labels={"outcome": "miss"})
        )
        # Monotonic, so a clock adjustment mid-calculation cannot record a negative or absurd cost.
        started = time.perf_counter()
        result = checked_payload(key, await compute())
        elapsed = time.perf_counter() - started
        # Offered before it is persisted, on misses only: a crash between the two then costs one
        # recompute (the outbox de-duplicates), whereas persist-first would make every later call a
        # hit that never publishes.
        await publish_stored_result(key, result, compute_seconds=elapsed, structure_id=structure_id)
        await store.put(
            StoredResult(
                key=key,
                result=result,
                compute_seconds=elapsed,
                structure_id=structure_id,
                # The epoch folded into `key` from the same constant. Stamped here rather than
                # defaulted on the model, so rows read back from before migration 090 stay
                # "unrecorded".
                epoch=CALCULATION_EPOCH,
            )
        )
        return result, elapsed

    async def _persisted() -> tuple[ResultPayload, float] | None:
        stored = await store.get(key)
        return None if stored is None else (stored.result, stored.compute_seconds or 0.0)

    try:
        if ledger is not None:
            (result, elapsed), computed = await single_flight(
                ledger, slot, lookup=_persisted, produce=_produce, wait_seconds=budget
            )
            if not computed:
                record_metric(
                    lambda m: m.increment("chemclaw_calc_cache_total", labels={"outcome": "shared"})
                )
                _credit_saved_seconds(elapsed)
        else:
            result, elapsed = await _produce()
            computed = True
        future.set_result((result, elapsed))
        return result, not computed
    except BaseException as exc:
        # Cancellation included: a waiter must never hang on a future its computer abandoned.
        if not future.done():
            future.set_exception(exc if isinstance(exc, Exception) else _Abandoned(slot))
            # A future nobody ends up awaiting must not warn on teardown.
            future.exception()
        raise
    finally:
        in_flight.pop(slot, None)


async def _join(
    waiting: "asyncio.Future[tuple[ResultPayload, float]]", slot: str, budget: float | None
) -> tuple[ResultPayload, float]:
    """Await another task's computation of `slot`, for at most `budget` seconds when one is set.

    The shield keeps this caller's cancellation or timeout from cancelling the computation.
    """
    if budget is None:
        return await asyncio.shield(waiting)
    try:
        async with asyncio.timeout(budget) as scope:
            return await asyncio.shield(waiting)
    except TimeoutError:
        if not scope.expired():
            raise
        raise PeerWaitTimeout(
            f"{slot} is still being computed and the {budget:g} s allowed for this call ran out. "
            "The computation continues and its result is cached when it ends; ask again."
        ) from None


def _credit_saved_seconds(compute_seconds: float | None) -> None:
    """Count the wall clock a lookup did not have to spend; `None` books nothing."""
    if compute_seconds:
        record_metric(
            lambda m: m.increment("chemclaw_calc_cache_seconds_saved_total", float(compute_seconds))
        )


class _Abandoned(Exception):
    """The computing caller was cancelled, so this waiter's shared computation never finished."""

    def __init__(self, slot: str) -> None:
        super().__init__(
            f"the in-flight computation for {slot} was cancelled by the caller running it; "
            "retry to start a fresh one"
        )


async def publish_stored_result(
    key: CalculationKey,
    result: ResultPayload,
    *,
    compute_seconds: float | None = None,
    structure_id: str = "",
    payload_kind: str = "",
) -> None:
    """Offer a just-persisted primitive to the external results store, if one is configured.

    Public so writers that bypass the cache can call it, before their `put`. Imported lazily
    (layering, and no projection machinery without a sink). Never raises.
    """
    from chemclaw.publish.outbox import enqueue_payload

    await enqueue_payload(
        calc_ref=key.as_str(),
        calc_type=key.calc_type,
        payload=result,
        payload_kind=payload_kind,
        calc_version=key.calc_version,
        input_hash=key.input_hash,
        params_hash=key.params_hash,
        structure_id=structure_id,
        compute_seconds=compute_seconds,
    )
