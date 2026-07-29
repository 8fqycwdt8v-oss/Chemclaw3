"""Relaxed scans reproduce a textbook torsion profile (xTB plan X3)."""

import asyncio

import pytest
from pydantic import ValidationError

from chemclaw.core.config import settings
from chemclaw.science.calc.store import InMemoryStore
from chemclaw.science.calc.structure import structure_from_smiles
from chemclaw.science.calc.xtb_scan import ScanSpec, run_cached_scan, run_scan


def test_butane_torsion_profile_has_anti_and_gauche_in_the_right_places() -> None:
    """n-Butane's C-C-C-C profile is the canonical conformational analysis figure.

    Anti (180°) is the global minimum; gauche (60°) sits ~0.6-0.9 kcal/mol above it;
    the eclipsed maxima are at 0° (syn, methyl against methyl, ~4.5-6.0) and 120°
    (~3.4). Asserting the *shape*: which points are minima, which are maxima, and that
    the syn barrier is the tallest — the things a barrier or atropisomer question turns
    on, and none of which survive a mistake in the constraint.
    """
    structure = structure_from_smiles("CCCC", optimize=True)
    spec = ScanSpec(atoms=(0, 1, 2, 3), values=(0.0, 60.0, 120.0, 180.0))
    result = run_scan(spec, structure)

    syn, gauche, eclipsed, anti = (point.relative_kcal for point in result.points)
    assert result.minimum_value == 180.0
    assert anti == 0.0
    assert 0.0 < gauche < 2.0
    assert eclipsed > gauche
    assert syn == max(syn, gauche, eclipsed)
    assert 3.0 < syn < 8.0
    assert result.maximum_relative_kcal == pytest.approx(syn)


def test_the_scanned_coordinate_is_actually_held() -> None:
    """The minimum geometry really has the coordinate it was asked for.

    If the constraint leaked, every point would relax to the same conformer and the
    "profile" would be flat noise — a failure that produces plausible-looking output.
    """
    from rdkit.Chem import rdMolTransforms

    from chemclaw.science.calc.xtb_scan import _mol_with_conformer

    structure = structure_from_smiles("CCCC", optimize=True)
    result = run_scan(ScanSpec(atoms=(0, 1, 2, 3), values=(60.0, 180.0)), structure)
    held = _mol_with_conformer(result.minimum_structure).GetConformer()
    assert abs(rdMolTransforms.GetDihedralDeg(held, 0, 1, 2, 3)) == pytest.approx(180.0, abs=1.0)


def test_a_bond_scan_reports_angstrom() -> None:
    """Two atoms mean a bond length, and the unit says so."""
    structure = structure_from_smiles("CCO", optimize=True)
    result = run_scan(ScanSpec(atoms=(1, 2), values=(1.35, 1.43, 1.55)), structure)
    assert (result.coordinate, result.unit) == ("bond", "angstrom")
    assert result.minimum_value == pytest.approx(1.43, abs=0.09)


def test_an_out_of_range_atom_index_is_rejected() -> None:
    """A scan over an atom that does not exist fails fast (gate G4)."""
    structure = structure_from_smiles("O", optimize=True)
    with pytest.raises(ValueError, match="out of range"):
        run_scan(ScanSpec(atoms=(0, 9), values=(1.0,)), structure)


def test_a_scan_longer_than_the_cap_is_rejected() -> None:
    """`xtb_scan_max_points` is a real bound, not a documented intention (D-117).

    Every point is a full constrained geometry optimization and the values come from the model,
    so the length of `values` *is* the cost of the call. The setting has described itself as
    bounding that "the way `xtb_hessian_max_atoms` bounds a Hessian" since it was added, but
    nothing enforced it: the field carried `min_length=1` and no maximum, leaving an unbounded
    compute request the agent could issue simply by naming more values.
    """
    over = tuple(float(i) for i in range(settings.xtb_scan_max_points + 1))
    with pytest.raises(ValidationError, match="capped at"):
        ScanSpec(atoms=(0, 1, 2, 3), values=over)

    # Exactly at the cap is still allowed — the bound is inclusive.
    at_limit = tuple(float(i) for i in range(settings.xtb_scan_max_points))
    assert len(ScanSpec(atoms=(0, 1, 2, 3), values=at_limit).values) == settings.xtb_scan_max_points


def test_scan_spec_freezes_exactly_the_scanned_atoms() -> None:
    """The constraint is derived from the coordinate, so the two cannot drift apart."""
    spec = ScanSpec(atoms=(0, 1, 2, 3), values=(0.0,))
    assert spec.frozen_atoms == (0, 1, 2, 3)
    assert spec.coordinate == "dihedral"


def test_cached_scan_computes_once() -> None:
    """A whole profile is one cache entry; the repeat is free."""

    async def _run() -> None:
        store = InMemoryStore()
        structure = structure_from_smiles("CCO", optimize=True)
        spec = ScanSpec(atoms=(1, 2), values=(1.40, 1.46))
        _, first = await run_cached_scan(store, structure, spec)
        _, second = await run_cached_scan(store, structure, spec)
        assert (first, second) == (False, True)

    asyncio.run(_run())
