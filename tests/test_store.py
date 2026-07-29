"""Behavioral tests for the calculation store (plan Phase 1b, D-011).

Proves the one property that matters: an identical calculation is computed once
and then served from the store, while a calculator-version bump correctly misses
and recomputes.
"""

import asyncio
import logging

import pytest
from pydantic import BaseModel

from calc.store import (
    CalculationKey,
    InMemoryStore,
    StoredResult,
    cached_compute,
    run_cached,
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


def test_run_cached_contract_offloads_and_reconstructs_typed_model() -> None:
    """The calculator contract computes once and returns the typed model on a hit."""

    class _Res(BaseModel):
        energy: float

    async def _run() -> None:
        store = InMemoryStore()
        calls = 0

        def compute() -> _Res:  # synchronous, as a real calculator's inner fn is
            nonlocal calls
            calls += 1
            return _Res(energy=1.5)

        key = CalculationKey.build("demo", "v1", inputs={"smiles": "CCO"})
        first, cached1 = await run_cached(store, key, compute, _Res)
        second, cached2 = await run_cached(store, key, compute, _Res)

        assert isinstance(first, _Res) and isinstance(second, _Res)  # reconstructed on hit
        assert first.energy == second.energy == 1.5
        assert (cached1, cached2) == (False, True)
        assert calls == 1  # never computed twice

    asyncio.run(_run())


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

    with caplog.at_level(logging.DEBUG, logger="calc.store"):
        asyncio.run(_run())

    assert "calc cache miss, computing" in caplog.text
    assert "calc cache hit" in caplog.text
    assert key_str_present(caplog.text)


def key_str_present(text: str) -> bool:
    """The flat calculation key appears in the log so a specific recompute is identifiable."""
    return "xtb@gfn2" in text
