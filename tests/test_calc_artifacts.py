"""Reaching a calculation's by-products: `list_artifacts` and `fetch_artifact` (D-165).

The artifact store has been complete on the write side since D-124 — content-addressed, deduped,
eviction-managed — and unreachable from the agent. A note may cite an `artifact_ref` in exactly the
form `ArtifactRef.as_str()` writes, and nothing could open it. `find_calculations` (D-163) made the
gap sharper by handing the model calculation keys it then had no way to look behind.

What is pinned here is the pair of refusals, because both replace a plausible wrong answer: a
binary artifact is refused rather than returned as mojibake, and a truncated read says so rather
than reading as the whole file.
"""

import asyncio

import pytest

from chemclaw.connectors.calc.server import tools
from chemclaw.core.config import settings
from chemclaw.science.calc.artifacts import InMemoryArtifactStore, media_type_for

_CALC_KEY = "xtb.thermo@gfn2-v1:0123456789abcdef:fedcba9876543210"

_XYZ = "3\nwater\nO 0.0 0.0 0.0\nH 0.0 0.0 0.96\nH 0.93 0.0 -0.24\n"
# A `.npy` header starts with a magic byte that is not a valid UTF-8 start byte, which is what the
# readability check actually keys on — no media-type table involved.
_NPY = b"\x93NUMPY\x01\x00v\x00{'descr': '<f8'}\x00\x00\x00\x00\x00\x00\x00\x00"


async def _populated() -> InMemoryArtifactStore:
    """One calculation with a readable geometry and an unreadable packed array."""
    store = InMemoryArtifactStore()
    await store.put(_CALC_KEY, "xtbopt.xyz", _XYZ.encode(), media_type=media_type_for("xtbopt.xyz"))
    await store.put(_CALC_KEY, "hessian.npy", _NPY, media_type=media_type_for("hessian.npy"))
    return store


def _use(monkeypatch: pytest.MonkeyPatch, store: InMemoryArtifactStore) -> None:
    """Point both tools at `store` — the `default_artifact_store` seam, swapped at the importer."""
    monkeypatch.setattr(tools, "default_artifact_store", lambda: store)


def test_a_calculations_by_products_are_listed_by_the_reference_a_note_cites(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The listing's `artifact_ref` is what `fetch_artifact` takes and what a note's cites."""

    async def _run() -> None:
        _use(monkeypatch, await _populated())
        found = await tools.list_artifacts(_CALC_KEY)
        assert [a.name for a in found] == ["hessian.npy", "xtbopt.xyz"]  # ordered by name
        assert found[1].artifact_ref == f"{_CALC_KEY}#xtbopt.xyz"
        assert found[1].media_type == "chemical/x-xyz"
        assert found[1].byte_size == len(_XYZ.encode())

    asyncio.run(_run())


def test_a_calculation_with_no_by_products_lists_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most calculations keep nothing, so empty is the common answer and not an error."""

    async def _run() -> None:
        _use(monkeypatch, InMemoryArtifactStore())
        assert await tools.list_artifacts(_CALC_KEY) == []

    asyncio.run(_run())


def test_a_readable_artifact_comes_back_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of the tool: the exact coordinates, not a recollection of them."""

    async def _run() -> None:
        _use(monkeypatch, await _populated())
        content = await tools.fetch_artifact(f"{_CALC_KEY}#xtbopt.xyz")
        assert content.text == _XYZ
        assert content.truncated is False
        assert content.byte_size == len(_XYZ.encode())

    asyncio.run(_run())


def test_a_binary_artifact_is_refused_rather_than_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A packed array decoded as text is noise that costs context and answers nothing.

    Refusing names what the thing is for. Returning it — or returning empty text with a flag —
    invites the model to report an artifact as unreadable or absent when it is neither.
    """

    async def _run() -> None:
        _use(monkeypatch, await _populated())
        with pytest.raises(ValueError, match="is binary"):
            await tools.fetch_artifact(f"{_CALC_KEY}#hessian.npy")

    asyncio.run(_run())


def test_a_large_artifact_is_truncated_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """A partial read that does not announce itself would be quoted as the whole file.

    This is the case a media-type rule cannot catch: a Hessian is text, so it is readable, and
    megabytes of it is still not something an answer is built by reading.
    """

    async def _run() -> None:
        store = InMemoryArtifactStore()
        body = "1.0 2.0 3.0\n" * 5_000
        await store.put(_CALC_KEY, "hessian", body.encode())
        _use(monkeypatch, store)

        content = await tools.fetch_artifact(f"{_CALC_KEY}#hessian")
        assert content.truncated is True
        assert len(content.text) == settings.calc_artifact_max_chars
        assert content.byte_size == len(body.encode())  # the *full* size, not the read size

    asyncio.run(_run())


def test_a_smaller_read_is_honoured_and_a_larger_one_clamped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`max_chars` is a request to spend less context; it can never be a request to spend more."""

    async def _run() -> None:
        _use(monkeypatch, await _populated())
        ref = f"{_CALC_KEY}#xtbopt.xyz"
        assert len((await tools.fetch_artifact(ref, max_chars=10)).text) == 10
        assert (await tools.fetch_artifact(ref, max_chars=10_000_000)).text == _XYZ

    asyncio.run(_run())


def test_a_missing_artifact_names_what_is_stored_instead(monkeypatch: pytest.MonkeyPatch) -> None:
    """Eviction is real, so "gone" must be distinguishable from "you asked for the wrong name"."""

    async def _run() -> None:
        _use(monkeypatch, await _populated())
        with pytest.raises(ValueError, match="xtbopt.xyz"):
            await tools.fetch_artifact(f"{_CALC_KEY}#vibspectrum")

    asyncio.run(_run())


def test_a_reference_without_a_name_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """`rpartition` on a bare calculation key would otherwise ask for the artifact named ''."""

    async def _run() -> None:
        _use(monkeypatch, await _populated())
        with pytest.raises(ValueError, match="not an artifact reference"):
            await tools.fetch_artifact(_CALC_KEY)

    asyncio.run(_run())
