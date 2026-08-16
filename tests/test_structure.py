"""Behavioral tests for the content-addressed `Structure` — now a cross-repository contract.

The three properties everything keyed on a geometry depends on: identity is the chemical content
and nothing else, coordinates are normalized so float noise cannot fork the cache, and a physically
impossible structure is rejected at construction rather than converged into a meaningless number
(gate G4).

**They matter more since the physics left** (`D-2026-08-16-the-physics-leaves-the-cache-stays`).
`structure_id` is half of every key `relax_structure`, `compute_hessian`, `scan_point` and the two
CREST searches are cached under, and it is derived on *both* sides of the wire — measured identical
for `CCO` (`st_739a222f45be0c3a`). A divergence in the rounding or the hash payload below would
raise nowhere: every lookup would simply miss, forever, while `calculator_trust` reported a
confident `UNCALIBRATED`.

The embedding itself is no longer here — `structure_from_smiles` went with the engines, because a
geometry built by a different RDKit build is a different id and every result keyed on it would miss.
`connectors/calc/compose.py::embed` asks the server for it, which keeps the two sides in agreement
by construction rather than by a version comparison nobody runs.
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

    This is the generalization the Fukui ions depend on: the old check refused every odd-electron
    system, which would have made the N-1/N+1 single points impossible. It is also why
    `connectors/calc/compose.py::radical_multiplicity` derives the value from the SMILES' own
    radical electrons before embedding — the server reads an unstated multiplicity as closed-shell
    singlet and refuses this species outright, which would take homolysis reactions with it.
    """
    radical = Structure(elements=[6, 1, 1, 1], positions=[[0.0, 0.0, 0.0]] * 4, multiplicity=2)
    assert radical.multiplicity == 2
    assert radical.structure_id.startswith("st_")


def test_mismatched_arrays_are_rejected() -> None:
    """Parallel arrays that are not parallel are a programming error, caught here."""
    with pytest.raises(ValueError, match="positions for"):
        Structure(elements=[8, 1, 1], positions=[[0.0, 0.0, 0.0]])


def test_symbols_index_the_atoms_in_order() -> None:
    """Element symbols pair with `elements` positionally — the per-atom results' contract.

    Heavy atoms keep their canonical-SMILES order and hydrogens follow, because that is the order
    the server embeds in; what this pins is that the mapping from atomic number to symbol does not
    reorder anything on the way to a chemist reading an atom index.
    """
    ethanol = Structure(
        elements=[6, 6, 8, 1, 1, 1, 1, 1, 1], positions=[[0.0, 0.0, float(i)] for i in range(9)]
    )
    assert ethanol.symbols[:3] == ["C", "C", "O"]
    assert set(ethanol.symbols[3:]) == {"H"}
