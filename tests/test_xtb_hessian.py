"""The Hessian as its own cached calculation (STO-2).

The property this stage exists for: a Hessian does not depend on the temperature, so asking for
thermochemistry at a second temperature must not recompute one. Before the split it did — measured
at minutes on a drug-sized substrate — because `ThermoSpec` put `temperature_k` in the key of the
calculation that produced the matrix.

These tests do not need the xtb binary. The expensive path is spied on rather than run, which is
the honest way to assert "this was not recomputed": the claim is about *how many times the
calculator was invoked*, and that is exactly what a call counter measures.
"""

import asyncio
import logging

import numpy as np
import pytest

from chemclaw.science.calc.artifacts import ArtifactRef, InMemoryArtifactStore
from chemclaw.science.calc.store import InMemoryStore
from chemclaw.science.calc.structure import Structure
from chemclaw.science.calc.xtb_hessian import (
    DIPOLE_ARTIFACT,
    HESSIAN_ARTIFACT,
    Hessian,
    HessianSpec,
    _persist,
    run_cached_hessian,
)
from chemclaw.science.calc.xtb_thermo import (
    ThermoSpec,
    run_cached_thermochemistry,
    thermochemistry_from_hessian,
)


def _water() -> Structure:
    """A water geometry near its GFN2 minimum — three atoms, so nine Cartesian coordinates."""
    return Structure(
        elements=[8, 1, 1],
        positions=[[0.0, 0.0, 0.117], [0.0, 0.757, -0.469], [0.0, -0.757, -0.469]],
        smiles="O",
    )


def _fake_hessian(structure: Structure) -> Hessian:
    """A physically meaningless but correctly shaped Hessian, for the caching contract only.

    The physics is tested against experiment in `test_xtb_thermo.py`; what is under test here is
    which calculations run and which are served from the store, so the cheapest valid matrix is
    the right fixture.
    """
    size = 3 * len(structure.elements)
    matrix = np.eye(size) * 0.5
    return Hessian(
        matrix=matrix,
        electronic_energy_hartree=-5.07,
        dipole_derivatives=np.zeros((size, 3)),
    )


class _Counted:
    """A stand-in for `compute_hessian` that records how often it was actually invoked."""

    def __init__(self, structure: Structure) -> None:
        """Count from zero, producing a fixed Hessian for `structure`."""
        self.calls = 0
        self._structure = structure

    def __call__(self, spec: HessianSpec, structure: Structure) -> tuple[Hessian, dict[str, bytes]]:
        """Produce the Hessian and its packed artifacts, counting the call."""
        from chemclaw.science.calc.xtb_hessian import _pack

        self.calls += 1
        hessian = _fake_hessian(self._structure)
        assert hessian.dipole_derivatives is not None
        return hessian, {
            HESSIAN_ARTIFACT: _pack(hessian.matrix),
            DIPOLE_ARTIFACT: _pack(hessian.dipole_derivatives),
        }


def test_a_second_temperature_reuses_the_hessian_instead_of_recomputing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of STO-2, asserted end to end as a call count on the expensive half.

    Two thermochemistry requests differing only in temperature: two genuinely different free
    energies, and exactly **one** Hessian between them. Before the split this ran the
    second-derivative calculation twice — measured at 26 s (binary) or 218 s (finite difference)
    per run on a 76-atom substrate, to answer a question the first matrix already contained.
    """

    async def _run() -> None:
        structure = _water()
        counted = _Counted(structure)
        monkeypatch.setattr("chemclaw.science.calc.xtb_hessian.compute_hessian", counted)
        results = InMemoryStore()
        artifacts = InMemoryArtifactStore()

        warm, warm_cached = await run_cached_thermochemistry(
            results, structure, ThermoSpec(temperature_k=298.15, symmetry_number=2), artifacts
        )
        hot, hot_cached = await run_cached_thermochemistry(
            results, structure, ThermoSpec(temperature_k=350.0, symmetry_number=2), artifacts
        )

        # Both were computed — the thermochemistry genuinely differs with temperature...
        assert (warm_cached, hot_cached) == (False, False)
        assert warm.gibbs_correction_kcal != hot.gibbs_correction_kcal
        assert (warm.temperature_k, hot.temperature_k) == (298.15, 350.0)
        # ...but the matrix underneath them was produced once.
        assert counted.calls == 1

    asyncio.run(_run())


def test_the_same_hessian_asked_for_twice_is_computed_once() -> None:
    """The plain cache contract, and that a reused matrix is the stored one rather than a redo."""

    async def _run() -> None:
        structure = _water()
        counted = _Counted(structure)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr("chemclaw.science.calc.xtb_hessian.compute_hessian", counted)
            results = InMemoryStore()
            artifacts = InMemoryArtifactStore()
            first, first_cached = await run_cached_hessian(
                results, artifacts, structure, HessianSpec()
            )
            second, second_cached = await run_cached_hessian(
                results, artifacts, structure, HessianSpec()
            )

        assert (first_cached, second_cached) == (False, True)
        assert counted.calls == 1
        assert np.array_equal(first.matrix, second.matrix)
        assert second.dipole_derivatives is not None

    asyncio.run(_run())


def test_thermo_specs_differing_only_in_temperature_share_one_hessian_spec() -> None:
    """The projection that makes the reuse possible, asserted on the keys themselves.

    Every state variable is varied at once. If any of them leaked into `hessian_spec`, the two
    keys would differ and the expensive calculation would run twice for one matrix.
    """
    structure = _water()
    warm = ThermoSpec(temperature_k=298.15, pressure_pa=101325.0, symmetry_number=2)
    hot = ThermoSpec(
        temperature_k=350.0, pressure_pa=200000.0, symmetry_number=4, rrho_cutoff_cm=150.0
    )

    # The thermochemistry itself is a different calculation — the free energy really does depend
    # on all of these — so those keys must differ.
    assert warm.cache_key(structure) != hot.cache_key(structure)
    # ...while the Hessian underneath is the same calculation, and is keyed as one.
    assert warm.hessian_spec().cache_key(structure) == hot.hessian_spec().cache_key(structure)


def test_the_displacement_still_changes_the_hessian_key() -> None:
    """The negative control: a setting that *does* move the matrix must still recompute it.

    A split that dropped too much would be indistinguishable from a working one until it served a
    Hessian computed at the wrong step size.
    """
    structure = _water()
    fine = ThermoSpec(displacement_angstrom=0.005)
    coarse = ThermoSpec(displacement_angstrom=0.02)
    assert fine.hessian_spec().cache_key(structure) != coarse.hessian_spec().cache_key(structure)


def test_a_cached_hessian_whose_blob_is_gone_recomputes_rather_than_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Artifacts are optional by construction (D-124), so the read path must survive their loss.

    This is what the eviction sweep depends on: reclaiming a blob has to cost a recomputation and
    nothing else. A row that pointed at a missing blob and was still served as a hit would turn
    eviction into data loss.
    """

    async def _run() -> None:
        structure = _water()
        counted = _Counted(structure)
        monkeypatch.setattr("chemclaw.science.calc.xtb_hessian.compute_hessian", counted)
        results = InMemoryStore()
        artifacts = InMemoryArtifactStore()

        await run_cached_hessian(results, artifacts, structure, HessianSpec())
        assert counted.calls == 1

        # Evict everything, leaving the result row pointing at nothing.
        artifacts._blobs.clear()

        recovered, was_cached = await run_cached_hessian(
            results, artifacts, structure, HessianSpec()
        )
        assert was_cached is False
        assert counted.calls == 2
        assert recovered.matrix.shape == (9, 9)

    asyncio.run(_run())


def test_a_raising_artifact_store_leaves_no_row_behind(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`_persist` refuses to write a row whose blob did not land — asserted where it is visible.

    This path had no test until `store.run_cached_with_artifacts` was deleted: that dead wrapper
    carried the only coverage of a raising artifact store, and it exercised the **opposite** policy
    (keep the row, lose the by-product, warn).

    The first version of this test drove `run_cached_hessian` and asserted the second call
    recomputed. **It passed with the policy deliberately broken**, twice, and that is why it is
    written against `_persist` instead: the read path independently treats a missing blob as a miss
    (`_load` returns `None`), so a dangling row is invisible from outside — the recompute happens
    either way and the assertion could not fail. What the refusal actually buys is that no
    unreadable row is written at all, and `_persist`'s return value is the only place that shows.

    So the module docstring's "rejected on every read" is not quite what happens; the read path
    recomputes silently. The write-side refusal keeps the store from accumulating rows that can
    never be served, which is a smaller claim and the true one.
    """

    class _Broken(InMemoryArtifactStore):
        async def put(self, *args: object, **kwargs: object) -> ArtifactRef | None:
            raise ConnectionError("Postgres unreachable at <postgres>")

    async def _run() -> None:
        results = InMemoryStore()
        key = HessianSpec().cache_key(_water())
        hessian = Hessian(
            matrix=np.eye(9),
            electronic_energy_hartree=-5.07,
            dipole_derivatives=None,
            ir_intensities=None,
        )
        with caplog.at_level(logging.WARNING, logger="chemclaw.science.calc.xtb_hessian"):
            cached = await _persist(results, _Broken(), key, hessian, {"hessian.npy": b"x"}, 1.0)

        assert cached is False
        assert await results.get(key) is None, "a row was written that no read can ever satisfy"
        assert "could not store hessian artifacts" in caplog.text  # and the loss is loud

    asyncio.run(_run())


def test_a_disabled_artifact_store_caches_no_hessian_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With artifacts off there is nowhere to keep the matrix, so nothing is cached.

    Not a silent degradation but the documented consequence: such a deployment behaves exactly as
    it did before the split, recomputing every Hessian, rather than caching a row it could never
    honour.
    """

    async def _run() -> None:
        from chemclaw.core.config import settings

        monkeypatch.setattr(settings, "artifact_store_enabled", False)
        structure = _water()
        counted = _Counted(structure)
        monkeypatch.setattr("chemclaw.science.calc.xtb_hessian.compute_hessian", counted)
        results = InMemoryStore()
        artifacts = InMemoryArtifactStore()

        await run_cached_hessian(results, artifacts, structure, HessianSpec())
        await run_cached_hessian(results, artifacts, structure, HessianSpec())
        assert counted.calls == 2
        assert await results.get(HessianSpec().cache_key(structure)) is None

    asyncio.run(_run())


def test_a_stored_matrix_of_the_wrong_shape_is_rejected_not_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`atom_count` is checked against the loaded array, so a mismatched blob cannot be believed.

    Wrong frequencies from a plausible-looking matrix are the worst failure available here: they
    are not obviously wrong to any reader. Recomputing costs minutes; this costs nothing.
    """

    async def _run() -> None:
        from chemclaw.science.calc.xtb_hessian import HessianResult, _load, _pack

        artifacts = InMemoryArtifactStore()
        ref = await artifacts.put("k", HESSIAN_ARTIFACT, _pack(np.eye(6)))
        assert ref is not None
        row = HessianResult(
            electronic_energy_hartree=-1.0,
            atom_count=3,  # implies a 9x9 matrix; the blob is 6x6
            hessian_artifact=ref.content_hash,
        )
        assert await _load(artifacts, row) is None

    asyncio.run(_run())


def test_thermochemistry_from_a_hessian_needs_a_spectrum_source() -> None:
    """A Hessian with neither intensities nor dipole derivatives fails loudly.

    Unreachable through `compute_hessian`, which always populates one. Asserted anyway because the
    alternative failure mode is a full IR spectrum of zero-intensity bands, which reads as a real
    result.
    """
    structure = _water()
    bare = Hessian(matrix=np.eye(9) * 0.5, electronic_energy_hartree=-5.0)
    with pytest.raises(ValueError, match="neither IR intensities nor dipole derivatives"):
        thermochemistry_from_hessian(ThermoSpec(), structure, bare)
