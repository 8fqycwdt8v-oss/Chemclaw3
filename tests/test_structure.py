"""Behavioral tests for the content-addressed `Structure`, a cross-repository contract.

Identity is the chemical content only, coordinates are normalized so float noise cannot fork the
cache, and an impossible structure is rejected at construction. `structure_id` is derived on both
sides of the wire, so any divergence here would make every lookup miss silently. Embedding is the
server's (`connectors/calc/compose.py::embed`).
"""

import pytest

from chemclaw.science.calc.models import Structure


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

    Built through the constructor with provenance read back before comparing ids, because
    `Structure` ignores undeclared fields and the property needs provenance actually present.
    """
    water = _water()
    labelled = Structure(
        elements=water.elements,
        positions=water.positions,
        smiles="O",
        origin="xtb.opt@v1:abc:def",
    )
    assert (labelled.smiles, labelled.origin) == ("O", "xtb.opt@v1:abc:def"), (
        f"the provenance did not survive construction ({labelled!r}), so the comparison below "
        "would be between two structures that carry none — which is what this test used to assert"
    )
    assert labelled.structure_id == water.structure_id


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
    """A declared open-shell structure such as a methyl radical is allowed.

    Fukui N-1/N+1 single points need it. `radical_multiplicity` states the multiplicity before
    embedding, since the server reads an unstated one as a closed-shell singlet.
    """
    radical = Structure(elements=[6, 1, 1, 1], positions=[[0.0, 0.0, 0.0]] * 4, multiplicity=2)
    assert radical.multiplicity == 2
    assert radical.structure_id.startswith("st_")


def test_mismatched_arrays_are_rejected() -> None:
    """Parallel arrays that are not parallel are a programming error, caught here."""
    with pytest.raises(ValueError, match="positions for"):
        Structure(elements=[8, 1, 1], positions=[[0.0, 0.0, 0.0]])


def test_symbols_index_the_atoms_in_order() -> None:
    """Element symbols pair with `elements` positionally, heavy atoms first and hydrogens after."""
    ethanol = Structure(
        elements=[6, 6, 8, 1, 1, 1, 1, 1, 1], positions=[[0.0, 0.0, float(i)] for i in range(9)]
    )
    assert ethanol.symbols[:3] == ["C", "C", "O"]
    assert set(ethanol.symbols[3:]) == {"H"}
