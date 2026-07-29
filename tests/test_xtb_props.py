"""Behavioral tests for the xTB electronic properties and Fukui indices (plan X2).

Real GFN2-xTB calculations throughout (tblite is a pip dependency, no HPC). The
assertions are physics rather than golden numbers wherever possible: definitional
identities that must hold exactly, normalization sums, and — for site reactivity —
textbook regioselectivity that a descriptor merely correlating with something else
would get wrong in at least one direction.
"""

import asyncio

import pytest
from rdkit import Chem

from chemclaw.science.calc.store import InMemoryStore
from chemclaw.science.calc.structure import structure_from_smiles
from chemclaw.science.calc.xtb_props import (
    compute_fukui,
    compute_properties,
    run_cached_fukui,
    run_cached_properties,
)
from chemclaw.science.calc.xtb_spec import XtbSpec


def _ring_positions(smiles: str) -> dict[str, list[int]]:
    """Map ortho/meta/para to atom indices of a monosubstituted benzene, from the graph.

    Derived rather than hardcoded: the indices depend on RDKit's canonical atom order,
    so writing them down would leave the regioselectivity tests silently checking the
    wrong atoms if that order ever shifted. Positions come from the topological
    distance to the substituted (ipso) carbon within the ring.
    """
    mol = Chem.MolFromSmiles(smiles)
    ring = next(r for r in mol.GetRingInfo().AtomRings() if len(r) == 6)
    ipso = next(
        index
        for index in ring
        if any(
            neighbor.GetIdx() not in ring for neighbor in mol.GetAtomWithIdx(index).GetNeighbors()
        )
    )
    distances = Chem.GetDistanceMatrix(mol)
    positions: dict[str, list[int]] = {"ortho": [], "meta": [], "para": []}
    for name, distance in (("ortho", 1), ("meta", 2), ("para", 3)):
        positions[name] = [index for index in ring if distances[ipso][index] == distance]
    assert [len(positions[name]) for name in ("ortho", "meta", "para")] == [2, 2, 1]
    return positions


def _fukui_by_index(smiles: str) -> dict[int, float]:
    """Electrophilic-attack Fukui index per atom index, for one molecule."""
    result = compute_fukui(
        XtbSpec(task="fukui"), structure_from_smiles(smiles, optimize=True), "electrophilic"
    )
    return {site.index: site.f_minus for site in result.sites}


def test_water_properties_are_physical() -> None:
    """Water's frontier orbitals, dipole and charges come out in their known ranges."""
    properties = compute_properties(XtbSpec(task="properties"), structure_from_smiles("O"))
    assert properties.homo_ev < 0 < properties.lumo_ev  # type: ignore[operator]
    assert properties.gap_ev is not None and 10.0 < properties.gap_ev < 16.0
    # GFN2 overestimates water's 1.85 D experimental dipole somewhat, as expected.
    assert 1.8 < properties.dipole_debye < 2.5
    oxygen, *hydrogens = properties.atom_charges
    assert oxygen.element == "O" and oxygen.charge < -0.3
    assert all(hydrogen.charge > 0 for hydrogen in hydrogens)
    # Two O-H bonds and nothing else above the threshold.
    assert len(properties.bond_orders) == 2


def test_benzene_is_symmetric_and_aromatic() -> None:
    """Benzene has no dipole and six equivalent aromatic C-C bonds of order ~1.4."""
    properties = compute_properties(
        XtbSpec(task="properties"), structure_from_smiles("c1ccccc1", optimize=True)
    )
    assert properties.dipole_debye < 0.1
    carbon_carbon = [bond for bond in properties.bond_orders if bond.order > 1.2]
    assert len(carbon_carbon) == 6
    assert all(1.25 < bond.order < 1.6 for bond in carbon_carbon)


def test_conjugation_narrows_the_gap() -> None:
    """A more conjugated system has a smaller HOMO-LUMO gap — the comparison the tool is for."""
    spec = XtbSpec(task="properties")
    benzene = compute_properties(spec, structure_from_smiles("c1ccccc1", optimize=True))
    naphthalene = compute_properties(spec, structure_from_smiles("c1ccc2ccccc2c1", optimize=True))
    assert naphthalene.gap_ev is not None and benzene.gap_ev is not None
    assert naphthalene.gap_ev < benzene.gap_ev


def test_solvation_changes_the_result() -> None:
    """An ALPB solvent is actually applied, not silently ignored."""
    structure = structure_from_smiles("CC(=O)O")
    gas = compute_properties(XtbSpec(task="properties"), structure)
    water = compute_properties(XtbSpec(task="properties", solvent="water"), structure)
    assert gas.total_energy_hartree != water.total_energy_hartree
    assert water.solvent == "water"


def test_fukui_indices_satisfy_their_definition() -> None:
    """f0 is exactly the mean of f- and f+, and each function sums to one over the molecule.

    The normalization is the strong check: Fukui functions are normalized by
    construction, so a sum far from 1 would mean the charge differences were taken
    between the wrong electronic states.
    """
    result = compute_fukui(
        XtbSpec(task="fukui"), structure_from_smiles("Oc1ccccc1", optimize=True), "electrophilic"
    )
    for site in result.sites:
        assert site.f_zero == pytest.approx((site.f_minus + site.f_plus) / 2, abs=1e-3)
    assert sum(site.f_minus for site in result.sites) == pytest.approx(1.0, abs=1e-2)
    assert sum(site.f_plus for site in result.sites) == pytest.approx(1.0, abs=1e-2)


@pytest.mark.parametrize("smiles", ["Oc1ccccc1", "Cc1ccccc1"], ids=["phenol", "toluene"])
def test_activating_substituents_direct_ortho_para(smiles: str) -> None:
    """-OH and -CH3 rank the ortho and para carbons above both meta carbons."""
    fukui = _fukui_by_index(smiles)
    ring = _ring_positions(smiles)
    directed = [fukui[index] for index in ring["ortho"] + ring["para"]]
    assert min(directed) > max(fukui[index] for index in ring["meta"])


def test_deactivating_substituent_directs_meta() -> None:
    """-NO2 inverts the ranking to meta, which a merely-correlated descriptor would miss.

    This is the discriminating case: any descriptor that just tracks ring position
    passes the ortho/para tests above; only one that responds to the substituent's
    electronic effect also flips here.
    """
    smiles = "O=[N+]([O-])c1ccccc1"
    fukui = _fukui_by_index(smiles)
    ring = _ring_positions(smiles)
    assert min(fukui[index] for index in ring["meta"]) > max(
        fukui[index] for index in ring["ortho"] + ring["para"]
    )


def test_symmetry_equivalent_positions_agree() -> None:
    """Toluene's two ortho carbons are chemically equivalent and must score alike.

    They only do so on a relaxed geometry — the check that the property tasks' MMFF
    pre-optimization is doing its job rather than being decoration.
    """
    fukui = _fukui_by_index("Cc1ccccc1")
    left, right = _ring_positions("Cc1ccccc1")["ortho"]
    assert fukui[left] == pytest.approx(fukui[right], abs=5e-3)


def test_open_shell_fukui_is_rejected() -> None:
    """A radical parent leaves its ions' spin state ambiguous, so it fails fast (G4)."""
    radical = structure_from_smiles("[CH3]", multiplicity=2)
    with pytest.raises(ValueError, match="closed-shell"):
        compute_fukui(XtbSpec(task="fukui"), radical, "electrophilic")


def test_cached_properties_compute_once() -> None:
    """The second identical request is served from the store, not recomputed."""

    async def _run() -> None:
        store = InMemoryStore()
        first, cached_first = await run_cached_properties(store, "CCO")
        second, cached_second = await run_cached_properties(store, "CCO")
        assert (cached_first, cached_second) == (False, True)
        assert first.total_energy_hartree == second.total_energy_hartree

    asyncio.run(_run())


def test_second_fukui_mode_reuses_the_calculation() -> None:
    """Mode only chooses the sort, so asking for another one is a cache hit, not three more SCFs."""

    async def _run() -> None:
        store = InMemoryStore()
        electrophilic, cached_first = await run_cached_fukui(store, "Oc1ccccc1", "electrophilic")
        nucleophilic, cached_second = await run_cached_fukui(store, "Oc1ccccc1", "nucleophilic")
        assert (cached_first, cached_second) == (False, True)
        assert nucleophilic.ranked_by == "f_plus"
        assert nucleophilic.sites == sorted(
            electrophilic.sites, key=lambda site: site.f_plus, reverse=True
        )

    asyncio.run(_run())


def test_properties_and_fukui_have_separate_cache_entries() -> None:
    """Two tasks on one geometry are two calculations, distinguished by `calc_type`."""
    structure = structure_from_smiles("CCO", optimize=True)
    assert XtbSpec(task="properties").cache_key(structure).calc_type == "xtb.properties"
    assert XtbSpec(task="fukui").cache_key(structure).calc_type == "xtb.fukui"


def test_solvent_is_part_of_the_cache_key() -> None:
    """A solvated result must never be served for a gas-phase request (D-011)."""
    structure = structure_from_smiles("CCO", optimize=True)
    gas = XtbSpec(task="properties").cache_key(structure)
    water = XtbSpec(task="properties", solvent="water").cache_key(structure)
    assert gas.params_hash != water.params_hash
