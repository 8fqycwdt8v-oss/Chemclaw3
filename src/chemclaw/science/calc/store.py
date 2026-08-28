"""Calculation result store — compute once, never twice (plan Phase 1b, D-011).

Results are addressed by a **versioned** `CalculationKey`: the calculator's
version is part of the key, so bumping a model or method does not silently return
a stale result — it is a cache miss and recomputes. `CALCULATION_EPOCH` is the
other half of that guarantee, covering the changes a calculator version cannot
see because they are ours rather than the underlying program's. `ResultStore` is one
interface with swappable backends (in-memory for tests, Postgres for real), and
`cached_compute` is the single lookup-before-compute path every calculator shares
(DRY) — the one place that decides hit vs. miss and persists new results.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field, model_validator

from chemclaw.core.chem import require_canonical_smiles
from chemclaw.core.ids import stable_hash
from chemclaw.core.metrics_bridge import record_metric

logger = logging.getLogger(__name__)

# A result payload is any JSON-serializable mapping. Calculators own their typed
# models; the store persists the plain dict so it stays calculator-agnostic.
ResultPayload = dict[str, Any]

# The version of **ChemClaw's own** contribution to a stored result — the half no `calc_version`
# covers.
#
# **The calculation server has a constant of the same name, and the two are composed rather than
# compared.** `remote_key` folds this value into a `params_hash` the server has *already* folded its
# own epoch into, so the stored address carries both and neither side has to know the other's
# number: a bump here re-addresses every `calc` row without the server moving, and a bump there does
# the same without this file moving. They are two independent invalidators, not one constant with
# two homes — which is why nothing here asserts equality with the constant of the same name in
# `Chemclaw3-mcp`'s calculation server, and why a test that did would go red on a legitimate
# one-sided bump. `tests/test_calc_remote.py` holds the relationship that actually exists, in
# `test_the_two_epochs_compose_rather_than_having_to_match`.
#
# **It is folded in wherever a key is assembled, and getting that wrong is silent.**
# `CalculationKey.build` is the original and folds it in; every `calc` key comes back from the
# calculation server as four parts and is assembled by `connectors.calc.remote.remote_key`, which
# folds the epoch into the params hash there instead. For one release after the physics moved it
# was folded in at *neither* place for `calc`, so a bump invalidated the DFT bundle's rows and
# nothing else while this comment, `science/calc/__init__.py` and a test's own failure message all
# prescribed bumping it as the remedy for a stored payload changing meaning. That bundle is gone
# (`D-2026-08-26-semiempirical-is-the-whole-tier`) and `remote_key` is the live path; the rule the
# episode leaves behind is that a new way of assembling a key is a new place to fold the epoch.
#
# Every calculator's `calc_version` answers one question: *would the program we shell out to
# produce a different number now?* It is built from a tblite build, an xtb/crest binary version, an
# RDKit version. Two things change what a stored row *means* that no such
# version can see, and they turned out to be the same defect reached from two directions:
#
# - **Our own arithmetic was wrong and then fixed.** `xtb_thermo._rotational` divided a linear
#   rotor's partition function by `2 * symmetry` instead of `symmetry`, so every N2/CO/CO2/HCN/
#   alkyne entropy and free energy already on disk is wrong. Nothing in an `xtb.hess` key would
#   ever move for that fix, so those rows would have kept serving the wrong S and G until tblite
#   happened to be upgraded for unrelated reasons.
# - **The payload's shape changed under a stable version.** `SolubilityResult` gained `estimate`,
#   which carries the applicability-domain flag, and nothing was bumped. The field is optional, so
#   a pre-change row validates back with `estimate=None` — an "OUT OF DOMAIN" salt silently
#   degrades to "not assessed". `durable/retention.py` deliberately never prunes
#   `calculation_results`, so such rows never self-heal.
#
# One component rather than two, because both are the single fact *what a stored result means
# changed on our side*, and a mechanism per symptom is how the second one is the one nobody
# remembers. It rides in `params_hash`, not in `calc_version`: the version string is also the
# REV-12 calibration ledger's key (`calc.calibration`), and a measured residual stays valid across
# a ChemClaw fix that a cached prediction does not.
#
# **Bump it whenever a ChemClaw-side change makes an already-written row wrong or incomplete**, and
# add a line to the log below. `tests/test_calc_payload_schemas.py` catches the shape half for you —
# it fails on any change to a persisted payload model. The arithmetic half is a judgement only the
# author of the fix can make, so the rule is written here rather than inferred.
#
#   1 — introduced. Invalidates every row written before it, deliberately: a pre-epoch cache cannot
#       be separated into "still correct" and "wrong linear-rotor thermochemistry / missing
#       applicability-domain flag", and serving the wrong half is the failure this exists to stop.
#   2 — the per-atom reactivity panel. `SiteReactivityResult` gained the conceptual-DFT global
#       descriptors (IP, EA, chemical potential, hardness, softness, electrophilicity) and four
#       local ones per site; `AtomCharge` gained its Wiberg and free valence. No stored number
#       moved — the calculation server runs the same three SCFs on the same geometry and simply
#       reads energies it used to discard — but every epoch-1 row is now *incomplete*, and the new
#       fields are required, so one cannot come back validating as a panel it never carried.
#       Bumped in `Chemclaw3-mcp/servers/calc/.../key.py` in the same change, as the rule requires.
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

    # **Constrained because `as_str()` below is the `calculation_results` primary key**, and its
    # four fields arrive verbatim from the calculation server's `calculation_key` answer — which
    # `connectors/calc/remote.py` is right to take rather than re-derive, and which means the
    # identity of every cached row was a string this process never checked. Unchecked, the flat form
    # was ambiguous: `calc_type="a@b", calc_version="c"` and `calc_type="a", calc_version="b@c"`
    # both flatten to `a@b@c:d:e`, so the second upserts over the first and `cached_compute` serves
    # the wrong payload under a key it believes it derived. `("", "", "", "")` built happily and
    # flattened to `"@::"`.
    #
    # **Only the ambiguity is closed, and `calc_version` is deliberately left free of both
    # delimiters' exclusion.** A real version carries them — `esol-delaney@2004` the `@`,
    # `cal-0.28733:-29.3116` the `:` — which is the measured fact that made the key cross the wire
    # as four parts. Barring `@` from `calc_type` fixes the parse from the left, barring `:` from
    # the two hashes fixes it from the right, and the version is then whatever lies between: the
    # encoding is a bijection without constraining the one field that needs to be free. Whitespace
    # is excluded everywhere because a newline inside a primary key would let one key's text carry
    # another's, and no producer has ever emitted one.
    #
    # A pattern here rather than at the reader alone: `kg/note.py::_CALC_REF` validates this shape
    # at the PR-gate, so the check existed only on the way *out* of the system.
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

        The single place a key is assembled, which is why `CALCULATION_EPOCH` is folded in here:
        no calculator can be keyed without it, and none has to remember to ask.
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
    # Wall time the calculation took on the miss that produced it, or None for a result that
    # arrived some other way (a measured value, a backfill). This is the cost policy
    # `durable/retention.py` says a cache needs and refuses to fake with an age cutoff: it is
    # what an artifact eviction orders by, and what tells an operator what the cache is worth.
    compute_seconds: float | None = None
    # When the row was written, for a caller that is *browsing* the store rather than addressing
    # one key. `get` leaves it None because a cache hit does not care; `find` fills it, since "what
    # do we already have on this molecule" is unanswerable without knowing when each was computed.
    created_at: datetime | None = None
    # The 3-D geometry this calculation ran *on* — never the one it produced — for the
    # structure-keyed families. The server's
    # own answer, carried on the row so that "have we already relaxed this conformer?" is a query
    # rather than an unanswerable question (D-2026-08-21). `input_hash` is a digest over the whole
    # argument payload and is not it: two calculations on one geometry in different solvents have
    # different input hashes and the same `structure_id`. Empty for a molecule-keyed calculator.
    structure_id: str = ""


# Calculators whose `input_hash` is over a 3-D structure rather than a molecule: the xTB task
# family keys on `(structure_id, charge, multiplicity)` and the geometry pointer on its whole
# subject model. A molecule alone does not determine either hash, so `smiles` cannot address
# them — and answering "nothing found" for a molecule that has an xTB result on file would be the
# one failure this tool cannot afford. Matched as prefixes, since the types are `xtb.<task>`.
#
# **A `structure_id` filter does address them**, which is what makes the refusal below a redirection
# rather than a dead end (D-2026-08-21). It used to be neither: the message named alternatives that
# were all molecule-keyed, so a chemist asking whether a conformer had already been relaxed was
# told to ask a different question instead of a workable one.
#
# `geometry.` is kept and names nothing this deployment writes: the cross-method geometry pointer
# went with the optimizer in `D-2026-08-16-the-physics-leaves-the-cache-stays`. Rows written by an
# earlier release are still on disk — `calculation_results` is never pruned — so removing the
# prefix would make a molecule filter silently answer "nothing found" about them, which is exactly
# the failure the tuple exists to prevent.
STRUCTURE_KEYED_PREFIXES = ("xtb.", "geometry.")


def molecule_hash(smiles: str) -> str:
    """The `input_hash` a molecule-keyed calculator would produce for `smiles`.

    One definition, used by the query filter in both backends: the hash is over the same
    `{"smiles": <canonical>}` mapping the calculators build their keys from, so getting this
    shape wrong in one place cannot make a molecule findable in one store and not the other.
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

    There is deliberately no filter on the result's *value*. The payload is an opaque
    calculator-owned mapping (`ResultPayload`) — the store has been calculator-agnostic since
    D-011, and a `total_energy_hartree > x` predicate would put one calculator's schema inside the
    thing that persists all of them. A caller that wants that filters the returned rows.
    """

    # Matched by hashing, never by scanning — see above.
    smiles: str | None = None
    # The geometry a calculation was about, as `optimize_geometry` and `sample_conformers` report
    # it. The filter the structure-keyed families needed and did not have (D-2026-08-21): a
    # molecule cannot address them, and until this the refusal below pointed only at calculators
    # that are not about geometries at all.
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
                "descriptors, dft)."
            )
        return self


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
        """Return results matching `query`, newest first, capped at `query.limit`."""
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

    async def known(self, keys: Sequence[str]) -> set[str]:
        """Which of `keys` the cache holds — parity with the Postgres store's existence probe."""
        return {key for key in keys if key in self._data}

    async def find(self, query: CalculationQuery) -> list[StoredResult]:
        """Return results matching `query`, newest first, capped at `query.limit`.

        Insertion order stands in for time here: this store keeps no clock, and giving it one
        would make a test's ordering depend on how fast it ran. A row with an explicit
        `created_at` still sorts by it, so a fixture that sets one gets the ordering it asked for.

        Undated rows are held out of the comparison rather than given a sentinel date. They lead,
        because in a store whose only clock is insertion order a row nobody dated is the newest
        thing it knows — but a `datetime.max` sentinel to express that was naive while every real
        `created_at` here is timezone-aware, so `find` raised `TypeError: can't compare
        offset-naive and offset-aware datetimes` the moment one store held both kinds of row.
        Partitioning states the same policy and has no sentinel to get wrong.
        """
        matched = [stored for stored in self._data.values() if _matches(stored, query)]
        matched.reverse()  # newest first, since dict order is insertion order
        undated = [stored for stored in matched if stored.created_at is None]
        dated: list[tuple[datetime, StoredResult]] = [
            (stored.created_at, stored) for stored in matched if stored.created_at is not None
        ]
        dated.sort(key=lambda pair: pair[0], reverse=True)
        return (undated + [stored for _, stored in dated])[: query.limit]


def _matches(stored: StoredResult, query: CalculationQuery) -> bool:
    """Whether one stored result satisfies every filter set on `query`.

    Shared by the in-memory store and by the tests that pin the two backends agreeing; the
    Postgres store expresses the same predicate as SQL because it must filter before it fetches.
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
    return True


#: Computations currently in flight in this process, by key. The single-flight ledger: a second
#: miss on a key someone is already computing awaits the first computation instead of starting a
#: duplicate. `api/routes/ops._shared_probe` is the same shape for the readiness probes, and
#: `docs/planning/DEFERRED.md` keeps the *cross-process* half of the dedup — an advisory lock or
#: an in-flight row — which this deliberately does not attempt.
_IN_FLIGHT: dict[str, "asyncio.Future[tuple[ResultPayload, bool]]"] = {}


async def cached_compute(
    store: ResultStore,
    key: CalculationKey,
    compute: Callable[[], Awaitable[ResultPayload]],
    *,
    structure_id: str = "",
) -> tuple[ResultPayload, bool]:
    """Return a result for `key`, computing and persisting it only on a miss.

    This is the single lookup-before-compute path (plan step 1b.4): every
    calculator goes through it, so caching behavior is defined in exactly one
    place. `compute` is called only when the store has no entry for `key`.

    **Concurrent misses on one key in one process now share one computation.** The check-then-act
    was measured at 8 concurrent misses → 8 computes, benign only while every compute was
    milliseconds; a CREST search is 19 minutes of CPU, and two composites sharing a primitive — a
    reaction-energy job and a solvent screen relaxing the same species — could each pay it. The
    first miss computes under a future in `_IN_FLIGHT`; every later caller of the same key awaits
    that future and reports `was_cached=True`, because from its side the answer arrived without a
    computation being started. Cross-process misses still race — that half is deferred with its
    own trigger (`docs/planning/DEFERRED.md`), and identical *jobs* were already collapsed by
    Temporal's workflow-id reuse before either.

    A failed computation fails every waiter with the same exception and clears the slot, so the
    next attempt starts fresh rather than awaiting a corpse.

    Args:
        store: The backend to read from and write to.
        key: The versioned identity of this calculation.
        compute: Zero-arg coroutine that produces the result on a miss.
        structure_id: The geometry this calculation is *about*, when it is about one, so the
            stored row can be found by it (D-2026-08-21). Recorded, never used to look up: the
            key is still the identity, and a second calculation on the same geometry is a
            different row. Empty for a molecule-keyed calculator, which is not about a geometry.

    **Metered here, and only here.** `was_cached` reaches `connectors/calc/compose.py` as a
    per-job field and never became a number, so D-011 — "a persisted result is never recomputed",
    the largest cost lever in this system — could be observed only by turning DEBUG on over the
    hottest read there is. `chemclaw_calc_cache_total{outcome}` separates three states the boolean
    collapses into two: a store `hit`, a `miss` this caller computed, and a `shared` miss that
    another caller in this process was already computing. The third is the single-flight working,
    and it reports `was_cached=True` to its caller, so on the boolean it was indistinguishable from
    a hit — which is exactly the distinction anyone asking "is the cache earning its keep" needs.

    Returns:
        `(result, was_cached)` — `was_cached` is True on a store hit *and* on a miss this call
        joined to another caller's in-flight computation, because from this caller's side the
        answer arrived without a computation being started.
    """
    hit = await store.get(key)
    if hit is not None:
        # DEBUG, not INFO: on the hot path (every calculator call), but it is the one place
        # that answers the recurring troubleshooting question "why did this recompute?".
        logger.debug("calc cache hit: %s", key.as_str())
        record_metric(lambda m: m.increment("chemclaw_calc_cache_total", labels={"outcome": "hit"}))
        return hit.result, True
    slot = key.as_str()
    waiting = _IN_FLIGHT.get(slot)
    if waiting is not None:
        logger.debug("calc cache miss already computing elsewhere, awaiting: %s", slot)
        # Counted before the await, not after: a waiter whose computer is cancelled raises here,
        # and the fact worth counting is that a caller *joined* an in-flight computation instead of
        # starting a second one — which happened whether or not that computation went on to
        # succeed.
        record_metric(
            lambda m: m.increment("chemclaw_calc_cache_total", labels={"outcome": "shared"})
        )
        result, _ = await asyncio.shield(waiting)
        return result, True
    future: asyncio.Future[tuple[ResultPayload, bool]] = asyncio.get_running_loop().create_future()
    _IN_FLIGHT[slot] = future
    try:
        logger.debug("calc cache miss, computing: %s", slot)
        record_metric(
            lambda m: m.increment("chemclaw_calc_cache_total", labels={"outcome": "miss"})
        )
        # Monotonic, so a clock adjustment mid-calculation cannot record a negative or absurd cost.
        started = time.perf_counter()
        result = await compute()
        elapsed = time.perf_counter() - started
        await store.put(
            StoredResult(key=key, result=result, compute_seconds=elapsed, structure_id=structure_id)
        )
        # On the miss branch only. A cache *hit* returns above without touching this, which keeps
        # the write off the hottest read in the system — the same reason `calculation_results`
        # deliberately carries no `last_access_at`. A repeat call costs what it always cost.
        await publish_stored_result(key, result, compute_seconds=elapsed, structure_id=structure_id)
        future.set_result((result, False))
        return result, False
    except BaseException as exc:
        # Cancellation included: a waiter must never hang on a future its computer abandoned.
        if not future.done():
            future.set_exception(exc if isinstance(exc, Exception) else _Abandoned(slot))
            # A future nobody ends up awaiting must not warn on teardown.
            future.exception()
        raise
    finally:
        _IN_FLIGHT.pop(slot, None)


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

    **Public, and paired with `put` rather than with `cached_compute`.** Every writer to the
    calculation store is a producer of publishable science, and this used to be private to the one
    writer that goes through `cached_compute` below. The second writer — the removed DFT bundle's
    `persist_qm_result`, which could not use `cached_compute` because its computation happened on a
    cluster rather than behind a callable — was therefore missed, and DFT published on backfill and
    never live. It stays public and stays paired with the write rather than with `cached_compute`,
    because that is what makes "persisted implies offered" checkable for the *next* writer that
    does not come through the cache.

    **Imported inside the function, and that is load-bearing rather than stylistic.** `science` may
    not import a capability layer at module scope (`tests/test_layering.py`), and the publish path
    pulls in the projection machinery and RDKit canonicalization — which a deployment with no sink
    configured should never load at all. `publishing_enabled()` is a list lookup, so the whole
    subsystem costs one comparison when it is off.

    Never raises. The calculation succeeded and is already persisted; a results store that cannot
    be queued to is strictly less important than returning the science.
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
