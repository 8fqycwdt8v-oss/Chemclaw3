"""Behavioral tests for the calculation store (plan Phase 1b, D-011).

Proves the one property that matters: an identical calculation is computed once
and then served from the store, while a calculator-version bump correctly misses
and recomputes.
"""

import asyncio
import logging
from unittest import mock

import pytest

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.core.metrics import Metrics
from chemclaw.core.session_context import reset_current_session_id, set_current_session_id
from chemclaw.science.calc import store as store_module
from chemclaw.science.calc.models import Structure
from chemclaw.science.calc.postgres_store import PostgresStore
from chemclaw.science.calc.store import (
    CalculationKey,
    CalculationQuery,
    InMemoryStore,
    ResultPayload,
    StoredResult,
    cached_compute,
)
from tests.pg import migrated_db_or_skip


def test_identical_calculation_computed_once() -> None:
    """A second call with the same key hits the store; compute runs only once."""

    async def _run() -> None:
        store = InMemoryStore()
        calls = 0

        async def compute() -> dict[str, int]:
            nonlocal calls
            calls += 1
            return {"energy": 42}

        key = CalculationKey.build("xtb", "gfn2", inputs={"smiles": "CCO"})

        first, cached1 = await cached_compute(store, key, compute)
        second, cached2 = await cached_compute(store, key, compute)

        assert first == second == {"energy": 42}
        assert cached1 is False  # miss on first
        assert cached2 is True  # hit on second
        assert calls == 1  # never computed twice

    asyncio.run(_run())


def test_version_bump_invalidates_key() -> None:
    """Bumping calc_version is a miss, not a stale hit — recompute is forced."""

    async def _run() -> None:
        store = InMemoryStore()
        calls = 0

        async def compute() -> dict[str, int]:
            nonlocal calls
            calls += 1
            return {"n": calls}

        inputs = {"smiles": "CCO"}
        _, cached_v1 = await cached_compute(
            store, CalculationKey.build("solub", "v1", inputs=inputs), compute
        )
        result_v2, cached_v2 = await cached_compute(
            store, CalculationKey.build("solub", "v2", inputs=inputs), compute
        )

        assert cached_v1 is False
        assert cached_v2 is False  # different version → different key → miss
        assert result_v2 == {"n": 2}
        assert calls == 2

    asyncio.run(_run())


def test_an_earlier_epoch_cannot_be_served_to_a_later_one() -> None:
    """A ChemClaw-side fix must strand the rows it made wrong, not silently keep serving them.

    `calc_version` names the *other* programs — a tblite build, an RDKit build, a pipeline tag —
    so it does not move when our own code changes. Two changes that left rows on disk misleading:
    a corrected linear-rotor term in `xtb_thermo` (every stored N2/CO2/alkyne entropy wrong) and
    `SolubilityResult` gaining its applicability-domain flag (every stored row validating back with
    `estimate=None`). `CALCULATION_EPOCH` is what makes both a miss.
    """
    inputs = {"smiles": "CCO"}
    before = CalculationKey.build("solub", "esol@2004", inputs=inputs)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store_module, "CALCULATION_EPOCH", "next")
        after = CalculationKey.build("solub", "esol@2004", inputs=inputs)

    # The readable half is untouched, so the REV-12 calibration ledger — which keys on
    # `(calc_type, calc_version, input_hash)` — still finds its residuals.
    assert after.calc_version == before.calc_version
    assert after.input_hash == before.input_hash
    assert after.params_hash != before.params_hash


def test_the_epoch_reaches_every_calculator_not_just_the_one_that_needed_it() -> None:
    """Folded in by `build`, so no calculator has to remember to name it (D-011).

    The xTB family is the case that proves it: an `xtb.hess` version is entirely other people's
    version numbers — a tblite build, an RDKit build — so such a row would otherwise outlive any
    fix of ours. Since `D-2026-08-16-the-physics-leaves-the-cache-stays` those keys are built on
    the calculation server, which is why `CALCULATION_EPOCH` is the one constant both repositories
    must change in the same PR; what is checked here is that `build` still folds it in, for the
    keys this repository does derive (`CalculationKey.build`, which folds in `CALCULATION_EPOCH`).
    """
    structure = Structure(
        elements=[1, 1], positions=[[0.0, 0.0, 0.0], [0.0, 0.0, 0.74]], smiles="[H][H]"
    )
    inputs = {"structure": structure.structure_id, "charge": 0, "multiplicity": 1}
    before = CalculationKey.build("xtb.hess", "GFN2-xTB+tblite-0.7.0", inputs=inputs)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store_module, "CALCULATION_EPOCH", "next")
        after = CalculationKey.build("xtb.hess", "GFN2-xTB+tblite-0.7.0", inputs=inputs)
    assert after != before


def test_params_change_is_a_distinct_key() -> None:
    """Same input, different params → different key (no cross-contamination)."""
    inputs = {"smiles": "CCO"}
    k1 = CalculationKey.build("xtb", "gfn2", inputs=inputs, params={"charge": 0})
    k2 = CalculationKey.build("xtb", "gfn2", inputs=inputs, params={"charge": 1})
    assert k1.as_str() != k2.as_str()


def test_input_dict_ordering_does_not_change_key() -> None:
    """Canonical hashing makes key independent of input dict ordering."""
    k1 = CalculationKey.build("xtb", "gfn2", inputs={"a": 1, "b": 2})
    k2 = CalculationKey.build("xtb", "gfn2", inputs={"b": 2, "a": 1})
    assert k1.as_str() == k2.as_str()


def test_store_get_returns_none_on_miss() -> None:
    """An unknown key returns None rather than raising."""

    async def _run() -> None:
        store = InMemoryStore()
        key = CalculationKey.build("xtb", "gfn2", inputs={"smiles": "CCO"})
        assert await store.get(key) is None
        await store.put(StoredResult(key=key, result={"energy": 1}))
        got = await store.get(key)
        assert got is not None
        assert got.result == {"energy": 1}

    asyncio.run(_run())


def test_cache_logs_hit_and_miss(caplog: pytest.LogCaptureFixture) -> None:
    """At DEBUG the store logs miss-then-compute and a later hit — the "why recompute?" trail."""

    async def _run() -> None:
        store = InMemoryStore()

        async def compute() -> dict[str, int]:
            return {"energy": 7}

        key = CalculationKey.build("xtb", "gfn2", inputs={"smiles": "CCO"})
        await cached_compute(store, key, compute)  # miss
        await cached_compute(store, key, compute)  # hit

    with caplog.at_level(logging.DEBUG, logger="chemclaw.science.calc.store"):
        asyncio.run(_run())

    assert "calc cache miss, computing" in caplog.text
    assert "calc cache hit" in caplog.text
    assert key_str_present(caplog.text)


def key_str_present(text: str) -> bool:
    """The flat calculation key appears in the log so a specific recompute is identifiable."""
    return "xtb@gfn2" in text


def test_concurrent_misses_on_one_key_share_one_computation() -> None:
    """The in-process half of the check-then-act race, closed with a single-flight ledger.

    The measured shape this replaces: 8 concurrent misses on one key → 8 computes (CLAUDE.md's own
    number), benign while a compute was milliseconds and not once a CREST search is 19 minutes of
    CPU. The first miss computes; every concurrent second miss awaits the same future and reports
    `was_cached=True`, because from its side the answer arrived with no computation started. The
    cross-process half stays deferred with its own trigger (`docs/planning/DEFERRED.md`).
    """
    computes = 0
    release = asyncio.Event()

    async def compute() -> dict[str, int]:
        nonlocal computes
        computes += 1
        await release.wait()
        return {"energy": 7}

    async def _run() -> list[tuple[dict[str, int], bool]]:
        store = InMemoryStore()
        key = CalculationKey.build("xtb", "gfn2", inputs={"smiles": "CCO"})

        async def one() -> tuple[dict[str, int], bool]:
            return await cached_compute(store, key, compute)

        tasks = [asyncio.create_task(one()) for _ in range(8)]
        await asyncio.sleep(0)  # let every task reach its await
        release.set()
        return await asyncio.gather(*tasks)

    results = asyncio.run(_run())

    assert computes == 1, f"8 concurrent misses ran {computes} computations; the race is back"
    assert all(result == {"energy": 7} for result, _cached in results)
    assert sum(1 for _r, cached in results if not cached) == 1, (
        "exactly one caller computed; the waiters report was_cached=True"
    )


def test_a_failed_shared_computation_fails_every_waiter_and_clears_the_slot() -> None:
    """A corpse in the in-flight ledger must not wedge the key forever."""
    attempts = 0
    release = asyncio.Event()

    async def compute() -> dict[str, int]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            await release.wait()
            raise RuntimeError("SCF did not converge")
        return {"energy": 7}

    async def _run() -> dict[str, int]:
        store = InMemoryStore()
        key = CalculationKey.build("xtb", "gfn2", inputs={"smiles": "CCO"})

        async def one() -> tuple[dict[str, int], bool]:
            return await cached_compute(store, key, compute)

        first = asyncio.create_task(one())
        second = asyncio.create_task(one())
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(RuntimeError):
            await first
        with pytest.raises(RuntimeError):
            await second
        # The slot is clear: a fresh call computes anew rather than awaiting the corpse.
        result, cached = await cached_compute(store, key, compute)
        assert not cached
        return result

    assert asyncio.run(_run()) == {"energy": 7}
    assert attempts == 2


def test_two_calculations_cannot_flatten_to_one_cache_key() -> None:
    """`as_str()` is the `calculation_results` primary key, so its encoding has to be a bijection.

    It is `f"{calc_type}@{calc_version}:{input_hash}:{params_hash}"`, and all four fields were free
    text taken verbatim off the calculation server's `calculation_key` answer — which
    `connectors/calc/remote.py` is right to do, since deriving a key on this side would build one
    that matches nothing. The consequence is that the *identity* of every cached row was a string
    this process never checked, and two distinct calculations flattened to one:

        calc_type="a@b", calc_version="c"  ->  a@b@c:d:e
        calc_type="a",   calc_version="b@c" ->  a@b@c:d:e

    The second upserts over the first, and `cached_compute` then serves the wrong payload for a key
    it believes it derived — with correct-looking provenance, into the RRHO arithmetic, the
    calibration ledger and any note citing the `calc_ref`. The table is also the one the retention
    sweep deliberately never prunes.

    **Only the fields that create the ambiguity are constrained, and `calc_version` is deliberately
    not one of them.** A real version carries both delimiters — `esol-delaney@2004` carries the
    `@`, `cal-0.28733:-29.3116` carries the `:` — which is the measured fact
    `connectors/calc/remote.py` records as the reason the key crosses the wire as four parts rather
    than as one string. Barring `@` from `calc_type` fixes the left-hand parse; barring `:` from the
    two hashes fixes the right-hand one; between them the middle is whatever is left, so the version
    may contain anything.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CalculationKey(calc_type="a@b", calc_version="c", input_hash="d", params_hash="e")
    with pytest.raises(ValidationError):
        CalculationKey(calc_type="a:b", calc_version="c", input_hash="d", params_hash="e")
    with pytest.raises(ValidationError):
        CalculationKey(calc_type="a", calc_version="b", input_hash="d:e", params_hash="f")

    # And a version carrying both delimiters still round-trips, because the parse does not need it
    # to be free of them.
    key = CalculationKey(
        calc_type="solubility",
        calc_version="esol-delaney@2004:cal-0.28",
        input_hash="ab",
        params_hash="cd",
    )
    flat = key.as_str()
    assert flat == "solubility@esol-delaney@2004:cal-0.28:ab:cd"
    # The parse the encoding now guarantees: type up to the first `@`, the two hashes off the last
    # two `:`, version whatever is between. Written out because "unambiguous" is only a claim until
    # somebody recovers all four.
    calc_type, _, rest = flat.partition("@")
    rest, _, params_hash = rest.rpartition(":")
    calc_version, _, input_hash = rest.rpartition(":")
    assert (calc_type, calc_version, input_hash, params_hash) == (
        key.calc_type,
        key.calc_version,
        key.input_hash,
        key.params_hash,
    )


def test_an_empty_key_is_not_a_key() -> None:
    """`CalculationKey(calc_type="", ...)` used to build, and `as_str()` returned `"@::"`.

    A primary key that four empty strings can produce is one every miswired producer collides on.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CalculationKey(calc_type="", calc_version="", input_hash="", params_hash="")


def test_a_store_that_cannot_write_does_not_destroy_the_computation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed `put` costs a future cache hit, never the calculation in hand.

    The write used to be unguarded, so a Postgres restart mid-`put` propagated out of
    `cached_compute` and failed the leader *and* every waiter it had collected — measured, one
    leader plus one waiter both got `RuntimeError - postgres is down`, one computation ran, and its
    payload reached nobody. For a CREST search that is nineteen minutes of CPU discarded because a
    cache row could not be written, which is the opposite of what a cache is for.

    Both halves are asserted, because the waiter is the half that is easy to leave broken: it never
    touches the store itself and only ever sees what the leader's future carries.
    """
    metrics = Metrics()

    async def _run() -> tuple[tuple[ResultPayload, bool], tuple[ResultPayload, bool]]:
        store = _BrokenWrites()
        key = CalculationKey.build("xtb.sp", "gfn2", inputs={"smiles": "CCO"})
        started = asyncio.Event()

        async def compute() -> dict[str, float]:
            started.set()
            await asyncio.sleep(0.05)
            return {"energy": -1.5}

        async def waiter() -> tuple[ResultPayload, bool]:
            await started.wait()
            await asyncio.sleep(0.01)
            return await cached_compute(store, key, compute)

        leader = asyncio.create_task(cached_compute(store, key, compute))
        joined = asyncio.create_task(waiter())
        first, second = await asyncio.gather(leader, joined)
        assert store.computes == 1
        return first, second

    with (
        caplog.at_level(logging.WARNING),
        mock.patch("chemclaw.core.metrics_bridge.METRICS", metrics),
    ):
        (payload, was_cached), (shared_payload, shared_cached) = asyncio.run(_run())

    assert payload == shared_payload == {"energy": -1.5}
    assert was_cached is False  # this caller computed it
    assert shared_cached is True  # this one joined an in-flight computation
    assert 'chemclaw_calc_cache_total{outcome="unstored"} 1' in metrics.render()
    assert "could not be stored" in caplog.text


class _BrokenWrites(InMemoryStore):
    """A store whose `put` always fails, as a database being restarted does."""

    def __init__(self) -> None:
        """Count the computations that reached it, so a shared miss is distinguishable."""
        super().__init__()
        self.computes = 0

    async def put(self, stored: StoredResult) -> None:
        """Refuse every write."""
        self.computes += 1
        raise RuntimeError("postgres is down")


def test_a_waiter_on_a_different_store_is_never_told_the_row_is_cached() -> None:
    """The single-flight ledger is keyed by store as well as key, because a key is not a row.

    Keyed by the flat key alone, a caller of store B joined a computation running against store A,
    was told `was_cached=True`, and left B without the row — measured, `A: ({'e': 1}, False)`,
    `B: ({'e': 1}, True)`, and B held nothing. Every later call on B was a miss again, so the join
    saved no computation and bought a false statement about caching.
    """

    async def _run() -> None:
        first, second = InMemoryStore(), InMemoryStore()
        key = CalculationKey.build("xtb.sp", "gfn2", inputs={"smiles": "CCO"})
        started = asyncio.Event()
        computes = 0

        async def compute() -> dict[str, int]:
            nonlocal computes
            computes += 1
            started.set()
            await asyncio.sleep(0.05)
            return {"energy": 1}

        async def other() -> tuple[ResultPayload, bool]:
            await started.wait()
            await asyncio.sleep(0.01)
            return await cached_compute(second, key, compute)

        (_, leader_cached), (_, other_cached) = await asyncio.gather(
            cached_compute(first, key, compute), other()
        )
        assert leader_cached is False
        assert other_cached is False, "a different store's miss is its own miss"
        assert computes == 2
        assert await second.get(key) is not None, "the second store must end up holding the row"

    asyncio.run(_run())


def test_a_stored_row_records_the_epoch_it_was_written_under() -> None:
    """The epoch is on the row, not only inside the opaque `params_hash`.

    Exact-key `get` was always protected by the fold into the key; the *record* was not, so a
    browse surface served rows from two epochs side by side with nothing to tell them apart.
    """

    async def _run() -> None:
        store = InMemoryStore()
        key = CalculationKey.build("pka", "v3", inputs={"smiles": "CCO"})
        await cached_compute(store, key, _one)
        stored = await store.get(key)
        assert stored is not None
        assert stored.epoch == store_module.CALCULATION_EPOCH

    asyncio.run(_run())


async def _one() -> dict[str, int]:
    """One trivial payload, for tests that care about the envelope rather than the science."""
    return {"n": 1}


def test_a_family_prefix_without_its_dot_is_refused_like_the_family() -> None:
    """`"xtb"` is not a `calc_type`, and it must not slip past the molecule-filter guard.

    Matching is exact equality and the real types are `xtb.sp`, `xtb.hess`, … — so `"xtb"` found
    nothing *and* passed a `startswith(("xtb.", ...))` refusal, which made the exact combination
    the validator exists to refuse the one combination it accepted. Measured before the fix:
    `calc_type='xtb.sp' -> 1`, `calc_type='xtb' -> 0`, and `'xtb'` with a molecule accepted.
    """
    for family in ("xtb", "geometry"):
        with pytest.raises(ValueError, match="keyed by 3-D structure"):
            CalculationQuery(smiles="CCO", calc_type=family)
    # And the dotted members it was always meant to catch still are.
    with pytest.raises(ValueError, match="keyed by 3-D structure"):
        CalculationQuery(smiles="CCO", calc_type="xtb.hess")
    # A molecule-keyed type is unaffected.
    assert CalculationQuery(smiles="CCO", calc_type="pka").calc_type == "pka"


def test_two_chemists_sharing_one_cached_result_both_reach_the_results_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cache hit is the *second* chemist asking, and the store must learn both names.

    Driven end to end because the defect had two independent causes, and fixing either alone
    measures as success while the other stays open:

    * `cached_compute` returned on a hit *before* `publish_stored_result`, so the second chemist
      never enqueued at all;
    * the outbox's `ON CONFLICT (sink, calc_ref, schema_version) DO NOTHING` dropped the second
      row *including its `publications`*, so even an enqueue that did happen was discarded.

    And underneath both, the hook named nobody — it passed no `Publication` at all, so the actor
    index on `calculation_publication` held no row for any primitive this system had ever computed.
    That is why this asserts on the *set of actors* rather than on a row count: a count of 1 was the
    old bug, and a count of 2 carrying one name would be the same bug wearing a different number.

    `settings.result_sinks` is the knob rather than `publishing_enabled`, for the reason
    `tests/test_publish_reaches_the_hooks.py::_publishing` gives — two modules read that function,
    and patching one is how a hook comes to look wired without being. It names the shipped
    `postgres` sink because `enabled()` refuses a name no manifest declares; nothing is delivered
    here, only queued, which is where the publication lives.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        monkeypatch.setattr(settings, "result_sinks", "postgres")
        store = PostgresStore()
        key = CalculationKey(
            calc_type="pka", calc_version="probe-prov", input_hash="prov-1", params_hash="p"
        )

        async def _compute() -> ResultPayload:
            # A real projectable payload: `_pka` builds a `Subject` from `smiles`, and a member
            # that names no compound is refused. The point of this test is the publication, so the
            # chemistry has to be valid enough to reach one.
            return {"smiles": "CCO", "pka": 4.76, "method": "gfn2"}

        for actor, session in (("alice", "s-alice"), ("bob", "s-bob")):
            identity = set_current_identity(actor, frozenset())
            session_token = set_current_session_id(session)
            try:
                _, was_cached = await cached_compute(store, key, _compute)
            finally:
                reset_current_session_id(session_token)
                reset_current_identity(identity)
            # bob's call is the cache hit — the branch that used to publish nothing at all.
            assert was_cached is (actor == "bob")

        async with db.connection(settings.postgres_dsn) as conn:
            cursor = await conn.execute(
                "SELECT document->'publications' FROM result_publications "
                "WHERE sink = 'postgres' AND calc_ref = %s",
                (key.as_str(),),
            )
            rows = await cursor.fetchall()
        assert len(rows) == 1, "one calculation is one record, however many people asked for it"
        actors = {entry["actor"] for entry in rows[0][0]}
        assert actors == {"alice", "bob"}, (
            "both requesters must survive: the sink keys publications on "
            "(calc_ref, tenant_id, session_id, job_id) precisely to hold several"
        )

    asyncio.run(_run())
