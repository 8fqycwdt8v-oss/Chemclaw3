"""Behavioral tests for the artifact store (D-124).

Proves the properties the design rests on: bytes survive the calculation that produced them, two
runs that produced identical bytes store one copy, an artifact too large to keep is skipped rather
than raised, and a cache hit does not re-store what is already there.
"""

import asyncio
import logging
import random
from typing import Any

import pytest

from chemclaw.core.config import settings
from chemclaw.science.calc.artifacts import (
    ArtifactRef,
    InMemoryArtifactStore,
    content_address,
    decode,
    encode,
    media_type_for,
    put_all,
)
from chemclaw.science.calc.store import (
    CalculationKey,
    InMemoryStore,
    StoredResult,
    cached_compute,
)

# A Turbomole-shaped Hessian: highly repetitive numeric text, which is what the real artifacts are
# and therefore what the compression claim has to hold for.
_HESSIAN = b"$hessian\n" + b"   0.1234567890   -0.0987654321    0.0000000000\n" * 400


def test_put_then_open_round_trips_the_exact_bytes() -> None:
    """An artifact read back is byte-identical to what was stored, compression notwithstanding."""

    async def _run() -> None:
        store = InMemoryArtifactStore()
        ref = await store.put("xtb.hess@gfn2:a:b", "hessian", _HESSIAN)
        assert ref is not None
        assert ref.content_hash == content_address(_HESSIAN)
        assert ref.byte_size == len(_HESSIAN)
        assert await store.open(ref.content_hash) == _HESSIAN

    asyncio.run(_run())


def test_identical_bytes_from_two_calculations_store_one_blob() -> None:
    """Content addressing dedups: two runs, two link rows, one blob — reachable from both."""

    async def _run() -> None:
        store = InMemoryArtifactStore()
        first = await store.put("xtb.opt@gfn2:aaa:p", "xtbopt.xyz", b"2\n\nH 0 0 0\nH 0 0 0.74\n")
        second = await store.put("xtb.opt@gfnff:bbb:p", "xtbopt.xyz", b"2\n\nH 0 0 0\nH 0 0 0.74\n")
        assert first is not None and second is not None
        # One content address, so one copy of the bytes...
        assert first.content_hash == second.content_hash
        # ...but each calculation still reaches it under its own key.
        assert [ref.name for ref in await store.list_for("xtb.opt@gfn2:aaa:p")] == ["xtbopt.xyz"]
        assert [ref.name for ref in await store.list_for("xtb.opt@gfnff:bbb:p")] == ["xtbopt.xyz"]

    asyncio.run(_run())


def test_compression_shrinks_a_hessian_and_decodes_identically() -> None:
    """The codec is worth having on the artifacts this actually stores, and is lossless."""
    codec, payload = encode(_HESSIAN)
    assert codec == "zlib"
    assert len(payload) < len(_HESSIAN)
    assert decode(codec, payload) == _HESSIAN


def test_incompressible_payload_is_stored_raw() -> None:
    """Compression that does not shrink is not applied — no larger row, no pointless decode."""
    # Seeded rather than `os.urandom`, so the assertion is reproducible. `bytes(range(256)) * 4`
    # looks high-entropy and is not — deflate finds the repeat and shrinks it — which is exactly
    # the mistake this fixture has to avoid making.
    noise = random.Random(0).randbytes(4096)
    codec, payload = encode(noise)
    assert codec == "none"
    assert payload == noise
    assert decode(codec, payload) == noise


def test_unknown_codec_raises_rather_than_returning_deflate_bytes() -> None:
    """A mislabelled row must fail loudly, not hand back bytes that parse as garbage later."""
    with pytest.raises(ValueError, match="unknown artifact codec"):
        decode("brotli", b"whatever")


def test_artifact_over_the_cap_is_skipped_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The "optional by construction" contract: over-cap returns None and stores nothing."""

    async def _run() -> None:
        monkeypatch.setattr(settings, "artifact_max_bytes", 16)
        store = InMemoryArtifactStore()
        with caplog.at_level(logging.DEBUG, logger="chemclaw.science.calc.artifacts"):
            stored = await put_all(store, "k", {"hessian": _HESSIAN})
        assert stored == []
        assert await store.list_for("k") == []

    asyncio.run(_run())


def test_disabled_store_keeps_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment that wants no artifacts gets none, from one config token."""

    async def _run() -> None:
        monkeypatch.setattr(settings, "artifact_store_enabled", False)
        store = InMemoryArtifactStore()
        assert await store.put("k", "hessian", _HESSIAN) is None

    asyncio.run(_run())


def test_list_for_is_ordered_and_scoped_to_one_calculation() -> None:
    """Every by-product of one run, name-ordered; nothing from another run."""

    async def _run() -> None:
        store = InMemoryArtifactStore()
        for name in ("vibspectrum", "hessian", "xtbopt.xyz"):
            await store.put("mine", name, f"{name}-body".encode())
        await store.put("theirs", "hessian", b"other")
        assert [ref.name for ref in await store.list_for("mine")] == [
            "hessian",
            "vibspectrum",
            "xtbopt.xyz",
        ]
        assert [ref.name for ref in await store.list_for("theirs")] == ["hessian"]

    asyncio.run(_run())


def test_media_types_are_named_for_known_artifacts_and_opaque_otherwise() -> None:
    """A reader is told what a Hessian is; an unrecognised name is not guessed at."""
    assert media_type_for("hessian") == "application/x-turbomole-hessian"
    assert media_type_for("xtbopt.xyz") == "chemical/x-xyz"
    assert media_type_for("something.unknown") == "application/octet-stream"


def test_ref_as_str_addresses_one_artifact_of_one_calculation() -> None:
    """The flat form a knowledge-graph note cites."""
    ref = ArtifactRef(
        calc_key="xtb.hess@gfn2:in:pa", name="hessian", content_hash="ab", byte_size=1
    )
    assert ref.as_str() == "xtb.hess@gfn2:in:pa#hessian"


def test_cached_compute_records_what_a_miss_cost() -> None:
    """The cost policy `durable/retention.py` asked for: a miss is timed, a hit keeps the time."""

    async def _run() -> None:
        store = InMemoryStore()

        async def compute() -> dict[str, int]:
            return {"energy": 1}

        key = CalculationKey.build("xtb", "gfn2", inputs={"smiles": "CCO"})
        await cached_compute(store, key, compute)
        stored = await store.get(key)
        assert stored is not None
        assert stored.compute_seconds is not None
        assert stored.compute_seconds >= 0.0
        # A hit must not overwrite the measurement with a near-zero lookup time.
        await cached_compute(store, key, compute)
        again = await store.get(key)
        assert again is not None
        assert again.compute_seconds == stored.compute_seconds

    asyncio.run(_run())


# --- the offloading store: the matrix stays out of the row it cannot be pruned from -------------


def _packed(size: int) -> str:
    """A base64 `.npy` array of `size`x`size`, the shape a Hessian payload carries."""
    import base64
    import io

    import numpy as np

    buffer = io.BytesIO()
    np.save(buffer, np.eye(size, dtype=float))
    return base64.b64encode(buffer.getvalue()).decode("ascii")


_PAYLOAD = {
    "structure_id": "st_1",
    "method": "GFN2-xTB",
    "solvent": "water",
    "atom_count": 3,
    "electronic_energy_hartree": -76.4,
    "hessian_npy": _packed(9),
    "dipole_derivatives_npy": _packed(3),
    "ir_intensities": None,
}
_KEY = CalculationKey.build("xtb.hess", "v1", {"structure": "st_1"}, {"solvent": "water"})


def _offloading() -> tuple[Any, InMemoryStore, InMemoryArtifactStore]:
    """An `ArrayOffloadingStore` over fresh in-memory backends, with both backends visible."""
    from chemclaw.science.calc.artifacts import HESSIAN_ARRAYS, ArrayOffloadingStore

    results, blobs = InMemoryStore(), InMemoryArtifactStore()
    return ArrayOffloadingStore(results, blobs, HESSIAN_ARRAYS), results, blobs


def test_the_matrix_does_not_land_in_the_row_it_can_never_be_pruned_from() -> None:
    """The whole point: `calculation_results` keeps an address, the artifact store keeps the bytes.

    `durable/retention.py` refuses to prune `calculation_results` because D-011 says a persisted
    result is never recomputed. A megabyte-scale matrix stored inline is therefore a row nothing can
    ever reclaim, in the one table with no reclaim path — which is exactly what
    `durable/artifact_eviction.py` and D-124 exist to prevent. So the assertion is on what the
    *underlying* row contains, not on what the caller gets back.
    """

    async def _run() -> None:
        store, results, blobs = _offloading()
        await store.put(StoredResult(key=_KEY, result=dict(_PAYLOAD), compute_seconds=12.0))

        row = await results.get(_KEY)
        assert row is not None
        assert "hessian_npy" not in row.result, "the matrix was stored in the unprunable row"
        assert "dipole_derivatives_npy" not in row.result
        assert row.result["hessian_artifact"], "the row does not address the matrix"
        # And the bytes really are in the store the eviction sweep walks.
        assert await blobs.open(str(row.result["hessian_artifact"])) is not None

    asyncio.run(_run())


def test_a_hit_comes_back_byte_identical_to_what_was_stored() -> None:
    """Offloading is invisible to the caller, or it is not a cache."""

    async def _run() -> None:
        store, _, _ = _offloading()
        await store.put(StoredResult(key=_KEY, result=dict(_PAYLOAD), compute_seconds=12.0))

        hit = await store.get(_KEY)
        assert hit is not None
        assert hit.result == _PAYLOAD

    asyncio.run(_run())


def test_an_evicted_matrix_is_a_miss_to_recompute_from_and_never_an_error() -> None:
    """The trade D-124 makes: a cold matrix is reclaimed, and the next caller pays a recompute.

    Every reason a blob is absent is ordinary — the store disabled, the sweep reclaimed it, a
    database restored without its artifact table. Raising here would turn a routine eviction into a
    failed calculation; returning the row without its matrix would be worse still, because the
    caller would validate a payload with no arrays in it.
    """

    async def _run() -> None:
        store, results, blobs = _offloading()
        await store.put(StoredResult(key=_KEY, result=dict(_PAYLOAD), compute_seconds=12.0))
        blobs._blobs.clear()  # the eviction sweep, or a restore without the artifact table

        assert await store.get(_KEY) is None, "an evicted matrix must read as a miss"
        assert await results.get(_KEY) is not None, "the row itself is untouched by eviction"

    asyncio.run(_run())


def test_a_row_is_never_written_addressing_an_artifact_that_did_not_land() -> None:
    """Ordering is the design, not an implementation detail.

    A row whose `hessian_artifact` points at nothing would be served as a hit forever and rejected
    on every read — strictly worse than not caching, because it converts one recomputation into a
    permanent one. So the blobs go first and the row is written only once they are retrievable.
    Losing a by-product costs a future recomputation and never the calculation in hand.
    """

    class _Refusing(InMemoryArtifactStore):
        async def put(self, *args: Any, **kwargs: Any) -> None:
            return None  # over the cap, or the store is disabled

    from chemclaw.science.calc.artifacts import HESSIAN_ARRAYS, ArrayOffloadingStore

    async def _run() -> None:
        results = InMemoryStore()
        store = ArrayOffloadingStore(results, _Refusing(), HESSIAN_ARRAYS)
        await store.put(StoredResult(key=_KEY, result=dict(_PAYLOAD), compute_seconds=12.0))

        assert await results.get(_KEY) is None, "a row was written addressing nothing"
        assert await store.get(_KEY) is None

    asyncio.run(_run())


def test_a_payload_carrying_no_arrays_is_stored_unchanged() -> None:
    """Wrapping a store must never be lossy for a result that has nothing to offload."""

    async def _run() -> None:
        store, results, _ = _offloading()
        plain = {"structure_id": "st_1", "electronic_energy_hartree": -76.4}
        await store.put(StoredResult(key=_KEY, result=dict(plain), compute_seconds=1.0))

        row = await results.get(_KEY)
        assert row is not None and row.result == plain
        hit = await store.get(_KEY)
        assert hit is not None and hit.result == plain

    asyncio.run(_run())
