"""Behavioral tests for the GFN2-xTB calculator (plan step 1c.2).

Runs real semiempirical calculations (tblite is a pip dependency, no HPC), and
proves the store integration computes once and reuses thereafter.
"""

import asyncio
from importlib.metadata import version

import pytest

from calc.store import InMemoryStore
from calc.xtb import XtbInput, _calc_version, run_cached_xtb, run_xtb


def test_water_energy_is_physical() -> None:
    """A GFN2-xTB single point on water gives its known ballpark energy."""
    result = run_xtb(XtbInput(smiles="O"))
    assert result.method == "GFN2-xTB"
    # GFN2-xTB water total energy is ~ -5.07 Hartree; assert a tight-ish window.
    assert -5.2 < result.total_energy_hartree < -4.9


def test_invalid_smiles_raises() -> None:
    """An unparseable SMILES fails fast, not with a bogus energy (gate G4)."""
    with pytest.raises(ValueError, match="invalid SMILES"):
        run_xtb(XtbInput(smiles="not_a_molecule)("))


def test_cached_xtb_computes_once() -> None:
    """The second identical request is served from the store, not recomputed."""

    async def _run() -> None:
        store = InMemoryStore()
        job = XtbInput(smiles="CCO")

        first, cached1 = await run_cached_xtb(store, job)
        second, cached2 = await run_cached_xtb(store, job)

        assert cached1 is False
        assert cached2 is True
        assert first.total_energy_hartree == second.total_energy_hartree

    asyncio.run(_run())


def test_charge_mismatch_raises() -> None:
    """A declared charge that contradicts the SMILES formal charge fails fast (G4).

    Acetate at the default charge=0 would be a neutral radical at the wrong
    electron count — tblite converges it silently ~195 kcal/mol off, so the
    mismatch must be rejected, never computed and cached.
    """
    with pytest.raises(ValueError, match="formal charge"):
        run_xtb(XtbInput(smiles="CC(=O)[O-]"))


def test_anion_with_matching_charge_computes() -> None:
    """Acetate declared at its true charge -1 gives the correct anion energy."""
    result = run_xtb(XtbInput(smiles="CC(=O)[O-]", charge=-1))
    # GFN2-xTB acetate anion total energy is ~ -14.14 Hartree.
    assert -14.3 < result.total_energy_hartree < -14.0


def test_open_shell_raises() -> None:
    """An odd-electron species is rejected instead of silently converged (G4)."""
    with pytest.raises(ValueError, match="open-shell"):
        run_xtb(XtbInput(smiles="[CH3]"))


def test_cached_xtb_rejects_charge_mismatch() -> None:
    """The cached entry point never persists a wrong-charge energy."""

    async def _run() -> None:
        store = InMemoryStore()
        with pytest.raises(ValueError, match="formal charge"):
            await run_cached_xtb(store, XtbInput(smiles="CC(=O)[O-]", charge=0))

    asyncio.run(_run())


def test_energy_is_independent_of_smiles_spelling() -> None:
    """Equivalent spellings compute the same energy (D-011 determinism).

    The cache key canonicalizes, so the computation must run on the canonical
    form too — before the fix, `CCO` vs `OCC` differed by ~1.2 kcal/mol because
    atom order steers the seeded embedding. Fresh stores force both spellings to
    actually compute. tblite's SCF carries ~1e-12 run-to-run numerical noise, so
    assert agreement far below chemical significance rather than bitwise equality.
    """

    async def _run() -> None:
        first, _ = await run_cached_xtb(InMemoryStore(), XtbInput(smiles="CCO"))
        second, _ = await run_cached_xtb(InMemoryStore(), XtbInput(smiles="OCC"))
        assert first.total_energy_hartree == pytest.approx(second.total_energy_hartree, abs=1e-10)
        assert first.smiles == second.smiles  # both report the canonical form

    asyncio.run(_run())


def test_calc_version_embeds_rdkit_build() -> None:
    """The cache key carries the RDKit build (D-011).

    Embedding changes across RDKit releases, so an upgrade must be a cache
    miss, not a silent stale hit.
    """
    assert version("rdkit") in _calc_version()
