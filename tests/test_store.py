"""Behavioral tests for the calculation store (plan Phase 1b, D-011).

Proves the one property that matters: an identical calculation is computed once
and then served from the store, while a calculator-version bump correctly misses
and recomputes.
"""

import asyncio

from calc.store import (
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
