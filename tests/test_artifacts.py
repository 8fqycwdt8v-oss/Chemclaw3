"""Behavioral tests for the artifact store (D-124).

Proves the properties the design rests on: bytes survive the calculation that produced them, two
runs that produced identical bytes store one copy, an artifact too large to keep is skipped rather
than raised, and a cache hit does not re-store what is already there.
"""

import asyncio
import logging
import random

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
