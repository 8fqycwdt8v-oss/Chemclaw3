"""Reaching a calculation's by-products: `list_artifacts` and `fetch_artifact` (D-165).

The artifact store has been complete on the write side since D-124 — content-addressed, deduped,
eviction-managed — and unreachable from the agent. A note may cite an `artifact_ref` in exactly the
form `ArtifactRef.as_str()` writes, and nothing could open it. `find_calculations` (D-163) made the
gap sharper by handing the model calculation keys it then had no way to look behind.

What is pinned here is the pair of refusals, because both replace a plausible wrong answer: a
binary artifact is refused rather than returned as mojibake, and a truncated read says so rather
than reading as the whole file.

**And, since `D-2026-08-27-a-tool-that-can-only-refuse-is-not-a-capability`, what the pair may
*say*.** Measured on the real write path, `list_artifacts` returns one `application/x-npy` entry
and `fetch_artifact` on it raises "is binary" — every time, because `ArrayOffloadingStore` is the
only artifact writer left and it writes nothing but packed arrays. The text by-products these
docstrings were written for went with the engines. So the last three tests below hold the surface
to what the code can do: the refusal is *derived* from the writer rather than transcribed, no
agent-facing surface names an artifact nothing produces, and the spectrum the docstrings now
redirect to is one the code actually returns.

The text fixtures here are deliberately hypothetical, and `test_no_agent_facing_surface_names_an_
artifact_without_a_producer` is what stops that from becoming a claim: they exercise a real code
path (`fetch_artifact`'s decode, clamp and truncate) that would serve any text artifact a future
writer produced, and they are not evidence that one exists.
"""

import ast
import asyncio
import pathlib
import re

import pytest

from chemclaw.connectors.calc.server import tools
from chemclaw.core.config import settings
from chemclaw.science.calc.artifacts import (
    _MEDIA_TYPES,
    HESSIAN_ARRAYS,
    InMemoryArtifactStore,
    media_type_for,
    put_all,
)
from chemclaw.science.calc.models import ThermochemistryResult, VibrationalMode

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


def test_a_negative_read_size_does_not_slice_from_the_wrong_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`text[:-5]` is all but the last five characters — a near-complete read reported as bounded.

    The clamp is not defensive tidiness: `max_chars` arrives from a model, and this is the one
    argument whose misuse produces content that looks right and is not what was asked for.
    """

    async def _run() -> None:
        _use(monkeypatch, await _populated())
        content = await tools.fetch_artifact(f"{_CALC_KEY}#xtbopt.xyz", max_chars=-5)
        assert len(content.text) == 1
        assert content.truncated is True

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


# --- what the surface is allowed to say -------------------------------------------------------
#
# `ArrayOffloadingStore` is the only artifact writer left in this repository, and `HESSIAN_ARRAYS`
# is the map it offloads through — so the set of names anything here can ever store is exactly its
# values. Deriving both the refusal and the dead-name set from that constant, rather than listing
# them, is what makes these tests fail the day a *text* writer is added: the name drops out of the
# dead set on its own and the refusal below stops holding, which is the moment the docstrings need
# revisiting. An invariant, not a transcription.

_PRODUCED: frozenset[str] = frozenset(HESSIAN_ARRAYS.values())
# Every name the media-type table knows that nothing writes. `vibspectrum`, `xtbopt.xyz` and the
# two CREST ensembles are here because their producers left with the engines
# (`D-2026-08-16-the-physics-leaves-the-cache-stays`); `density.restart` and `orbitals.molden`
# because the tier they were reserved for was retracted outright
# (`D-2026-08-26-semiempirical-is-the-whole-tier`).
_WITHOUT_A_PRODUCER: frozenset[str] = frozenset(_MEDIA_TYPES) - _PRODUCED


def _docstring(name: str) -> str:
    """The tool docstring exactly as the model is shown it, read off the source.

    Parsed rather than imported through `__doc__` so this reads the *declared* prose even if a
    decorator ever rewrapped it — the docstring is the prompt, and the prompt is what is on trial.
    """
    source = pathlib.Path(tools.__file__).read_text()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == name:
            return ast.get_docstring(node) or ""
    raise AssertionError(f"{name} is not defined in {tools.__file__}")


def _agent_facing_surfaces() -> dict[str, str]:
    """Every place this repository tells an agent what these two tools are for."""
    root = pathlib.Path(tools.__file__).parents[5]
    return {
        "list_artifacts docstring": _docstring("list_artifacts"),
        "fetch_artifact docstring": _docstring("fetch_artifact"),
        "computation profile": (root / "data/profiles/computation.yaml").read_text(),
        "evidence profile": (root / "data/profiles/evidence.yaml").read_text(),
    }


def test_every_artifact_this_release_can_write_is_refused_by_fetch_artifact() -> None:
    """The measurement behind the decision, derived from the writer rather than transcribed.

    `ArrayOffloadingStore` offloads exactly `HESSIAN_ARRAYS`, and every one of those is a packed
    `.npy` whose magic byte is not valid UTF-8 — so `fetch_artifact` refuses the complete set of
    what this repository stores. That is what makes "it can only refuse" a fact about the code and
    not a remark about today's data.
    """

    async def _run(store: InMemoryArtifactStore) -> None:
        await put_all(store, _CALC_KEY, dict.fromkeys(_PRODUCED, _NPY))
        listed = await tools.list_artifacts(_CALC_KEY)
        assert {entry.name for entry in listed} == _PRODUCED
        assert {entry.media_type for entry in listed} == {"application/x-npy"}
        for entry in listed:
            with pytest.raises(ValueError, match="is binary"):
                await tools.fetch_artifact(entry.artifact_ref)

    monkey = pytest.MonkeyPatch()
    try:
        store = InMemoryArtifactStore()
        monkey.setattr(tools, "default_artifact_store", lambda: store)
        asyncio.run(_run(store))
    finally:
        monkey.undo()


def test_no_agent_facing_surface_names_an_artifact_without_a_producer() -> None:
    """A docstring is the prompt, so a filename in one is an offer the model will take up.

    `vibspectrum` and `xtbopt.xyz` were named in both places this checks, and neither has had a
    writer since the engines moved. Naming a file nobody can obtain does not merely waste prompt:
    it sends the model to `fetch_artifact` for a spectrum, which answers with an error, and the
    turn that follows reports a gap where there is a band list.
    """
    for where, text in _agent_facing_surfaces().items():
        for name in sorted(_WITHOUT_A_PRODUCER):
            # Word boundaries that do not treat `.` as one, so `hessian` cannot match inside
            # `hessian.npy` — the live name must stay sayable while the dead one does not.
            found = re.search(rf"(?<![\w.]){re.escape(name)}(?![\w])", text)
            assert found is None, (
                f"{where} names {name!r}, which nothing in this repository writes "
                f"(the only artifacts stored are {sorted(_PRODUCED)}). Either give it a producer "
                "or stop offering it."
            )


def test_the_spectrum_the_docstrings_redirect_to_is_one_the_code_returns() -> None:
    """The other half of the fix: a removed promise must leave a reachable answer behind.

    Deleting "a `vibspectrum`" from the two docstrings would be a regression on its own if the
    spectrum then had nowhere to come from — a chemist asking for band positions would get a
    refusal and no route. It has one, and this drives it rather than reading it: a
    `ThermochemistryResult` carries every mode as a wavenumber with an IR intensity, and
    `strongest_bands` is the truncation `compute_thermochemistry`'s `top_bands` applies.
    """
    for where in ("list_artifacts docstring", "fetch_artifact docstring"):
        text = _agent_facing_surfaces()[where]
        assert "compute_thermochemistry" in text, f"{where} names no route to a spectrum"
        assert "modes" in text, f"{where} does not name the field the bands arrive in"

    result = ThermochemistryResult(
        smiles="O",
        structure_id="st_0123456789abcdef",
        method="gfn2",
        solvent=None,
        temperature_k=298.15,
        pressure_pa=101325.0,
        symmetry_number=2,
        is_minimum=True,
        imaginary_frequencies_cm=[],
        modes=[
            VibrationalMode(wavenumber_cm=1595.0, ir_intensity_km_per_mol=70.0),
            VibrationalMode(wavenumber_cm=3657.0, ir_intensity_km_per_mol=5.0),
            VibrationalMode(wavenumber_cm=3756.0, ir_intensity_km_per_mol=45.0),
        ],
        mode_count=3,
        lowest_wavenumbers_cm=[1595.0],
        electronic_energy_hartree=-5.07,
        zero_point_energy_kcal=12.9,
        thermal_enthalpy_correction_kcal=14.7,
        entropy_cal_per_mol_k=45.1,
        gibbs_correction_kcal=1.2,
        enthalpy_hartree=-5.04,
        gibbs_free_energy_hartree=-5.06,
        uncertainty_kcal=1.0,
    )
    # The band list a chemist would have gone to `fetch_artifact` for — positions *and*
    # intensities, strongest first, which is what a measured spectrum is compared on.
    bands = result.strongest_bands(2)
    assert [band.wavenumber_cm for band in bands] == [1595.0, 3756.0]
    assert [band.ir_intensity_km_per_mol for band in bands] == [70.0, 45.0]
    assert result.mode_count == 3  # the truncation says how many there were
