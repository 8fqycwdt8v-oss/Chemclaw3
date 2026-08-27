"""Behavioral tests for the calculation store (plan Phase 1b, D-011).

Proves the one property that matters: an identical calculation is computed once
and then served from the store, while a calculator-version bump correctly misses
and recomputes.
"""

import asyncio
import logging

import pytest

from chemclaw.science.calc import store as store_module
from chemclaw.science.calc.models import Structure
from chemclaw.science.calc.store import (
    CalculationKey,
    InMemoryStore,
    StoredResult,
    cached_compute,
)


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
