"""Behavioral tests for the GFN2-xTB calculator (plan step 1c.2).

Runs real semiempirical calculations (tblite is a pip dependency, no HPC), and
proves the store integration computes once and reuses thereafter.
"""

import asyncio
from importlib.metadata import version

import pytest

from calc.store import InMemoryStore
from calc.structure import structure_from_smiles
from calc.xtb import XtbInput, run_cached_xtb, run_xtb
from calc.xtb_spec import XtbSpec


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
    key = XtbSpec(task="sp").cache_key(structure_from_smiles("CCO"))
    assert version("rdkit") in key.calc_version


def test_energy_key_is_addressed_by_geometry_not_by_seed() -> None:
    """The single-point key names the structure, so it has no free parameters (X1).

    The embedding seed used to appear in `params`; it is now inside the geometry the
    key already names, so `params` is empty — the honest statement that a single point
    is fully determined by its structure and method.
    """
    key = XtbSpec(task="sp").cache_key(structure_from_smiles("CCO"))
    assert key.calc_type == "xtb.sp"
    assert key.params_hash == XtbSpec(task="sp").cache_key(structure_from_smiles("OCC")).params_hash


# Textbook relative stabilities (kcal/mol, more-stable species first). Chosen to span the
# range where the comparison is easy (alkene geometry) to where it is large (ethanol vs its
# ether isomer): all five are orderings any chemist would call uncontroversial.
_ISOMER_PAIRS = [
    ("C/C=C/C", "C/C=C\\C", "trans- vs cis-2-butene"),
    ("CC(C)C", "CCCC", "isobutane vs n-butane"),
    ("CC(=O)O", "COC=O", "acetic acid vs methyl formate"),
    ("Cc1ccc(C)cc1", "Cc1ccccc1C", "p- vs o-xylene"),
    ("CCO", "COC", "ethanol vs dimethyl ether"),
]


@pytest.mark.parametrize(
    ("stable", "less_stable", "label"),
    _ISOMER_PAIRS,
    ids=[label for *_, label in _ISOMER_PAIRS],
)
def test_relative_isomer_energies_have_the_right_ordering(
    stable: str, less_stable: str, label: str
) -> None:
    """The energy calculator ranks isomer stability correctly — the only use it has.

    An absolute GFN2 energy answers nothing on its own; the whole point of the tool is
    comparing related structures. This pins that behaviour across five textbook pairs.

    It is also a regression guard with teeth. Before the geometry policy was fixed, the
    single point ran on a raw ETKDG embedding whose residual strain exceeded the energy
    difference being asked about, and two of these five pairs came out **inverted**. A
    change that reverts the relaxation would fail here rather than quietly returning
    confident, backwards chemistry.
    """
    assert (
        run_xtb(XtbInput(smiles=stable)).total_energy_hartree
        < run_xtb(XtbInput(smiles=less_stable)).total_energy_hartree
    )
