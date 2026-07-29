"""Behavioral tests for the content-addressed `Structure` (xTB plan X1).

The three properties the rest of the xTB layer depends on: identity is the chemical
content and nothing else, coordinates are normalized so float noise cannot fork the
cache, and a physically impossible structure is rejected at construction rather than
converged by tblite into a meaningless number (gate G4).
"""

import pytest

from calc.structure import Structure, structure_from_smiles


def _water() -> Structure:
    return Structure(
        elements=[8, 1, 1],
        positions=[[0.0, 0.0, 0.0], [0.0, 0.0, 0.9572], [0.9266, 0.0, -0.2400]],
    )


def test_identity_is_the_chemical_content() -> None:
    """Two structures with the same chemistry share an id; a moved atom does not."""
    assert _water().structure_id == _water().structure_id
    moved = _water().model_copy(
        update={"positions": [[0.5, 0.0, 0.0], [0.0, 0.0, 0.9572], [0.9266, 0.0, -0.24]]}
    )
    assert moved.structure_id != _water().structure_id


def test_identity_ignores_provenance() -> None:
    """A geometry is the same structure however it was produced.

    This is what lets a downstream task hit the cache whether its input was embedded
    from a SMILES or produced by an optimizer.
    """
    labelled = _water().model_copy(update={"smiles": "O", "origin": "xtb.opt@v1:abc:def"})
    assert labelled.structure_id == _water().structure_id


def test_coordinates_are_normalized_below_chemical_significance() -> None:
    """Float noise far below chemical significance cannot fork the cache."""
    noisy = _water().model_copy(
        update={"positions": [[1e-9, 0.0, 0.0], [0.0, 0.0, 0.9572], [0.9266, 0.0, -0.24]]}
    )
    assert Structure(**noisy.model_dump()).structure_id == _water().structure_id


def test_negative_zero_does_not_change_identity() -> None:
    """A sign bit on a zero coordinate is not a different molecule."""
    signed = Structure(
        elements=[8, 1, 1],
        positions=[[-0.0, 0.0, -0.0], [0.0, 0.0, 0.9572], [0.9266, 0.0, -0.24]],
    )
    assert signed.structure_id == _water().structure_id


def test_charge_and_multiplicity_are_part_of_identity() -> None:
    """The same nuclei in a different electronic state are a different calculation."""
    triplet = Structure(
        elements=[8, 8], positions=[[0.0, 0.0, 0.0], [0.0, 0.0, 1.2]], multiplicity=3
    )
    singlet_like = triplet.model_copy(update={"multiplicity": 1})
    assert triplet.structure_id != singlet_like.structure_id


def test_impossible_multiplicity_is_rejected() -> None:
    """An electron count that cannot produce the declared multiplicity fails fast (G4)."""
    with pytest.raises(ValueError, match="cannot form multiplicity"):
        Structure(elements=[8, 1, 1], positions=[[0.0, 0.0, 0.0]] * 3, multiplicity=2)


def test_odd_electron_count_names_the_open_shell_problem() -> None:
    """The common accident — a radical at the default multiplicity — gets the clear message."""
    with pytest.raises(ValueError, match="open-shell"):
        Structure(elements=[6, 1, 1, 1], positions=[[0.0, 0.0, 0.0]] * 4)


def test_declared_open_shell_is_allowed() -> None:
    """A methyl radical is computable once its multiplicity is stated, not rejected outright.

    This is the generalization the Fukui ions depend on: the old check refused every
    odd-electron system, which would have made the N-1/N+1 single points impossible.
    """
    radical = Structure(elements=[6, 1, 1, 1], positions=[[0.0, 0.0, 0.0]] * 4, multiplicity=2)
    assert radical.uhf == 1


def test_mismatched_arrays_are_rejected() -> None:
    """Parallel arrays that are not parallel are a programming error, caught here."""
    with pytest.raises(ValueError, match="positions for"):
        Structure(elements=[8, 1, 1], positions=[[0.0, 0.0, 0.0]])


def test_smiles_spellings_produce_one_structure() -> None:
    """Canonicalizing before embedding is what makes two spellings one cache entry (D-011)."""
    assert structure_from_smiles("CCO").structure_id == structure_from_smiles("OCC").structure_id


def test_declared_charge_must_match_the_smiles() -> None:
    """A charge contradicting the SMILES is rejected, never computed (G4)."""
    with pytest.raises(ValueError, match="formal charge"):
        structure_from_smiles("CC(=O)[O-]", charge=0)


def test_charge_defaults_to_the_smiles_formal_charge() -> None:
    """Omitting the charge takes the molecule's own, rather than assuming neutral."""
    assert structure_from_smiles("CC(=O)[O-]").charge == -1


def test_symbols_index_the_heavy_atoms_of_the_canonical_smiles() -> None:
    """Heavy atoms keep their canonical-SMILES order; hydrogens follow (the tools' contract)."""
    symbols = structure_from_smiles("CCO").symbols
    assert symbols[:3] == ["C", "C", "O"]
    assert set(symbols[3:]) == {"H"}
