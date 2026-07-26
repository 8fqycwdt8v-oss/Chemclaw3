"""Geometry optimization behaves like an optimization (xTB plan X3).

Real GFN2-xTB runs — tblite and scipy are pip dependencies, no HPC — so these assert
physics, not mocks: the energy goes down, the gradient goes to zero, a stretched bond
comes back, and a converged structure is a fixed point.
"""

import asyncio

import numpy as np
import pytest

from calc.store import InMemoryStore
from calc.structure import Structure, structure_from_smiles
from calc.xtb_opt import OptimizationSummary, OptSpec, optimize_structure, run_cached_optimization


def test_optimization_lowers_the_energy_and_flattens_the_gradient() -> None:
    """The defining property: a minimum is lower, and its forces have gone."""
    result = optimize_structure(OptSpec(), structure_from_smiles("CCO", optimize=True))
    assert result.energy_hartree < result.initial_energy_hartree
    assert result.relaxation_kcal > 0
    assert result.max_gradient <= OptSpec().gradient_tolerance
    assert result.steps > 0


def test_a_converged_structure_is_a_fixed_point() -> None:
    """Re-optimizing a minimum changes nothing — which is what makes its id stable.

    The structure id is a hash of coordinates, so an optimizer that drifted on a
    converged input would fork the cache on every pass and the "compute once" guarantee
    would quietly stop holding for every task downstream of a geometry.
    """
    first = optimize_structure(OptSpec(), structure_from_smiles("O", optimize=True))
    second = optimize_structure(OptSpec(), first.structure)
    assert second.structure.structure_id == first.structure.structure_id
    assert second.relaxation_kcal == pytest.approx(0.0, abs=1e-3)


def test_a_stretched_bond_is_pulled_back() -> None:
    """Given a deliberately wrong geometry, the optimizer repairs it.

    Water's O-H is ~0.96 Angstrom; this starts one at 1.6 and asserts it comes back.
    A test that only optimized an already-good geometry would pass on an optimizer that
    did nothing at all.
    """
    water = structure_from_smiles("O", optimize=True)
    positions = np.array(water.positions)
    bond = positions[1] - positions[0]
    positions[1] = positions[0] + bond / np.linalg.norm(bond) * 1.6
    stretched = Structure(elements=water.elements, positions=positions.tolist(), smiles="O")

    result = optimize_structure(OptSpec(), stretched)
    relaxed = np.array(result.structure.positions)
    assert np.linalg.norm(relaxed[1] - relaxed[0]) == pytest.approx(0.96, abs=0.05)
    assert result.relaxation_kcal > 5  # a 0.64 Angstrom stretch is worth real energy


def test_frozen_atoms_do_not_move() -> None:
    """A frozen atom stays exactly where it was — the constraint the scan depends on."""
    start = structure_from_smiles("CCO", optimize=True)
    result = optimize_structure(OptSpec(frozen_atoms=(0, 1)), start)
    for index in (0, 1):
        assert result.structure.positions[index] == start.positions[index]
    assert result.structure.positions[2] != start.positions[2]


def test_non_convergence_raises_rather_than_returning_a_geometry() -> None:
    """An unconverged optimization is an error, not a result with a flag (gate G4).

    Everything downstream — frequencies, thermochemistry, reaction energies — is
    meaningless on a non-stationary geometry, and each of those would look entirely
    ordinary. Holding an `OptimizationResult` must therefore guarantee convergence.
    """
    with pytest.raises(ValueError, match="did not converge"):
        optimize_structure(
            OptSpec(max_steps=1, gradient_tolerance=1e-8),
            structure_from_smiles("CCCCO", optimize=False),
        )


def test_optimized_structure_records_its_origin() -> None:
    """The output geometry carries the key of the calculation that produced it (GxP)."""
    result = optimize_structure(OptSpec(), structure_from_smiles("O", optimize=True))
    assert (
        result.structure.origin
        == OptSpec().cache_key(structure_from_smiles("O", optimize=True)).as_str()
    )
    assert result.structure.smiles == "O"


def test_cached_optimization_computes_once() -> None:
    """The second identical request is served from the store."""

    async def _run() -> None:
        store = InMemoryStore()
        structure = structure_from_smiles("CO", optimize=True)
        first, cached_first = await run_cached_optimization(store, structure)
        second, cached_second = await run_cached_optimization(store, structure)
        assert (cached_first, cached_second) == (False, True)
        assert first.structure.structure_id == second.structure.structure_id

    asyncio.run(_run())


def test_optimization_key_carries_the_convergence_criterion() -> None:
    """A tighter tolerance is a different calculation, so it is a different key (D-011).

    This is the invariant the spec *subclasses* exist to protect: a task-specific
    setting reaches the key through `model_dump()` without anyone remembering to add it.
    """
    structure = structure_from_smiles("O", optimize=True)
    loose = OptSpec(gradient_tolerance=1e-3).cache_key(structure)
    tight = OptSpec(gradient_tolerance=1e-5).cache_key(structure)
    assert loose.params_hash != tight.params_hash
    assert loose.calc_type == "xtb.opt"


def test_summary_drops_the_coordinates_but_keeps_the_address() -> None:
    """What the agent sees is the numbers, not 3N floats it cannot read."""
    result = optimize_structure(OptSpec(), structure_from_smiles("O", optimize=True))
    summary = OptimizationSummary.of(result)
    assert summary.structure_id == result.structure.structure_id
    assert "positions" not in summary.model_dump()
