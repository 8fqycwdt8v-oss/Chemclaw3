"""Behavioral tests for the calculation store.

An identical calculation is computed once and then served from the store, while a version or
epoch bump misses and recomputes.
"""

import asyncio
import logging
import threading

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
    """An earlier `CALCULATION_EPOCH` cannot be served to a later one.

    `calc_version` names other programs' builds, so it does not move when this code's own fixes
    change what a stored row means; the epoch does.
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
    """`CalculationKey.build` folds the epoch into every key it derives, so no calculator must name
    it.

    Remote keys are built on the calculation server, whose epoch composes with this one.
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


async def test_store_get_returns_none_on_miss() -> None:
    """An unknown key returns None rather than raising."""
    store = InMemoryStore()
    key = CalculationKey.build("xtb", "gfn2", inputs={"smiles": "CCO"})
    assert await store.get(key) is None
    await store.put(StoredResult(key=key, result={"energy": 1}))
    got = await store.get(key)
    assert got is not None
    assert got.result == {"energy": 1}


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
    """Concurrent misses on one key in one process share one computation.

    The first miss computes; concurrent misses await the same future and report `was_cached=True`.
    The cross-process half is deferred (`docs/planning/BACKLOG.md`).
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


def test_a_second_event_loop_computes_rather_than_awaiting_the_first_loops_future() -> None:
    """A second event loop computes rather than awaiting the first loop's future.

    The single-flight ledger is per loop, because an `asyncio.Future` is bound to one. The first
    loop is held inside its computation until the second has finished, so the test is
    deterministic; the two computations are separate callables so neither waits on the other's
    latch.
    """
    holding = threading.Event()
    release = threading.Event()
    store = InMemoryStore()
    key = CalculationKey.build("xtb", "gfn2", inputs={"smiles": "CCO"})

    async def hold() -> dict[str, int]:
        holding.set()
        await asyncio.get_running_loop().run_in_executor(None, release.wait)
        return {"energy": 7}

    computed_on_the_second_loop = 0

    async def quick() -> dict[str, int]:
        nonlocal computed_on_the_second_loop
        computed_on_the_second_loop += 1
        return {"energy": 7}

    holder = threading.Thread(
        target=lambda: asyncio.run(cached_compute(store, key, hold)), daemon=True
    )
    holder.start()
    try:
        assert holding.wait(5), "the first loop never reached its computation"
        result, cached = asyncio.run(cached_compute(store, key, quick))
    finally:
        release.set()
        holder.join(10)

    assert result == {"energy": 7}
    assert cached is False, "a second loop cannot join the first loop's future; it computes"
    assert computed_on_the_second_loop == 1


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
    """Two calculations cannot flatten to one cache key.

    `as_str()` is the primary key, `f"{calc_type}@{calc_version}:{input_hash}:{params_hash}"`, built
    from fields taken verbatim from the server. `@` is barred from `calc_type` and `:` from the two
    hashes, which makes the encoding a bijection; `calc_version` may contain both, as real versions
    do.
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


def test_a_crash_between_the_two_writes_costs_a_recompute_and_never_a_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash between the two writes costs a recompute, never a publication.

    Publishing before persisting means a kill leaves a queued publication and no cache row, so the
    retry recomputes and re-enqueues idempotently; the reverse order would turn every later call
    into a hit that never publishes. Driven with a `BaseException` between the two.
    """

    async def _run() -> None:
        store = InMemoryStore()
        key = CalculationKey.build("xtb", "gfn2", inputs={"smiles": "CCO"})
        computes = 0
        published: list[str] = []

        async def compute() -> dict[str, int]:
            nonlocal computes
            computes += 1
            return {"energy": 42}

        async def dies(*_args: object, **_kwargs: object) -> None:
            raise KeyboardInterrupt("the pod was evicted between the two writes")

        monkeypatch.setattr(store_module, "publish_stored_result", dies)
        with pytest.raises(KeyboardInterrupt):
            await cached_compute(store, key, compute)

        async def records(published_key: CalculationKey, *_a: object, **_k: object) -> None:
            published.append(published_key.as_str())

        monkeypatch.setattr(store_module, "publish_stored_result", records)
        _result, was_cached = await cached_compute(store, key, compute)

        assert was_cached is False, (
            "nothing may be cached that was not offered first: a hit here is the state that makes "
            "the lost publication permanent under D-011"
        )
        assert computes == 2, "the recompute is the price, and it is paid exactly once"
        assert published == [key.as_str()]

    asyncio.run(_run())


def test_the_publish_is_offered_before_the_row_is_persisted() -> None:
    """The publish is offered before the row is persisted: "persisted implies offered"."""

    async def _run() -> None:
        events: list[str] = []

        class _RecordingStore(InMemoryStore):
            async def put(self, stored: StoredResult) -> None:
                events.append("put")
                await super().put(stored)

        async def compute() -> dict[str, int]:
            return {"energy": 42}

        async def publish(*_a: object, **_k: object) -> None:
            events.append("publish")

        original = store_module.publish_stored_result
        store_module.publish_stored_result = publish
        try:
            await cached_compute(
                _RecordingStore(),
                CalculationKey.build("xtb", "gfn2", inputs={"smiles": "CCO"}),
                compute,
            )
        finally:
            store_module.publish_stored_result = original

        assert events == ["publish", "put"]

    asyncio.run(_run())


def test_a_value_postgres_cannot_store_is_refused_by_name_before_the_write() -> None:
    """A value Postgres `jsonb` cannot store is refused by name before the write.

    NaN, `Decimal`, `datetime` and NUL would otherwise fail inside `cached_compute`, after the
    single-flight future exists, with a driver message naming neither the calculation nor the field.
    `json.loads` accepts `NaN`, so a diverged calculation can return one.
    """
    from datetime import UTC, datetime
    from decimal import Decimal

    from chemclaw.science.calc.store import CorruptCacheRow, checked_payload

    key = CalculationKey.build("probe.unstorable", "1", inputs={"n": 1})
    unstorable: list[tuple[str, dict[str, object]]] = [
        ("max_gradient", {"energy": -1.0, "max_gradient": float("nan")}),
        ("max_gradient", {"max_gradient": float("inf")}),
        ("converged", {"nested": [{"converged": float("-inf")}]}),
        ("energy", {"energy": Decimal("1.5")}),
        ("when", {"when": datetime.now(UTC)}),
        ("note", {"note": "a\x00b"}),
    ]
    for field, payload in unstorable:
        with pytest.raises(CorruptCacheRow) as caught:
            checked_payload(key, payload)
        message = str(caught.value)
        assert field in message, f"the refusal does not name the offending field: {message!r}"
        assert key.as_str() in message, f"the refusal does not name the calculation: {message!r}"


def test_a_storable_payload_is_still_returned_unchanged() -> None:
    """A storable payload is returned unchanged: ordinary unicode and any finite float pass.

    Only NUL is refused among strings; `tests/test_postgres_store.py` pins large floats against the
    real column.
    """
    from chemclaw.science.calc.store import checked_payload

    key = CalculationKey.build("probe.storable", "1", inputs={"n": 1})
    payload: dict[str, object] = {
        "note": "α-pinene · Δ 25 °C — ünïcode 中文 🧪",
        "avogadro": 6.02214076e23,
        "tiny": 5e-324,
        "counts": [1, 2, 3],
        "nested": {"ok": True, "absent": None},
    }
    assert checked_payload(key, payload) is payload


def test_the_browse_does_not_serve_a_row_the_epoch_invalidated() -> None:
    """`find` does not serve a row the epoch invalidated.

    The epoch rides in `params_hash`, so superseded and current rows are otherwise indistinguishable
    in a browse, and `find_calculations` tells the model to reuse and cite what it returns.
    """
    inputs = {"smiles": "CCO"}

    async def _run() -> tuple[list[str], bool]:
        store = InMemoryStore()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(store_module, "CALCULATION_EPOCH", "1")
            old = CalculationKey.build("thermo", "xtb-6.7", inputs=inputs)

            async def _old() -> dict[str, float]:
                return {"g": -40.111}

            await cached_compute(store, old, _old)
        new = CalculationKey.build("thermo", "xtb-6.7", inputs=inputs)

        async def _new() -> dict[str, float]:
            return {"g": -40.222}

        _, was_cached = await cached_compute(store, new, _new)
        found = await store.find(
            store_module.CalculationQuery(smiles="CCO", calc_type="thermo", limit=10)
        )
        return [stored.key.params_hash for stored in found], was_cached

    hashes, was_cached = asyncio.run(_run())
    assert not was_cached, "the epoch stopped re-addressing `get`, which is the other half"
    assert hashes == [CalculationKey.build("thermo", "xtb-6.7", inputs=inputs).params_hash], (
        f"the browse served a row a later epoch invalidated: {hashes}"
    )
