"""GFN2-xTB geometry optimization (xTB plan X3).

The first task whose *output* is a geometry. Everything before it — the single point,
the electronic properties, the Fukui indices, the pKa — described whatever conformer
RDKit happened to embed and MMFF happened to relax; this module produces a stationary
point of the surface those numbers are actually computed on, which is the precondition
for a Hessian (`calc.xtb_thermo`), a torsion profile (`calc.xtb_scan`), and a reaction
free energy (`calc.reaction`).

The optimizer is `scipy.optimize.minimize(method="L-BFGS-B")` driven by tblite's
**analytic** gradient. It works directly in Cartesian coordinates, which is the simple
choice rather than the fast one: an internal-coordinate optimizer converges in fewer
steps, but each step here costs ~2 ms, and a dependency-free Cartesian L-BFGS is far
easier to reason about than a redundant-internal one. Atoms can be frozen by pinning
their coordinates with equal bounds — an exact constrained minimization over the free
subspace, which is what makes the relaxed scan a scan and not a suggestion.

D-011 note: the optimized `Structure` is a field of the cached result rather than a row
in a second store. The result store already persists it, content-addressed by the
optimization's own key, and the geometry's `origin` records which calculation produced
it — so the "structure store" X1 deferred turns out to be one field, not a subsystem.
"""

from typing import Literal

import numpy as np
from pydantic import BaseModel, Field
from scipy.optimize import minimize

from calc.store import ResultStore, run_cached
from calc.structure import Structure
from calc.xtb_engine import evaluate_point, make_calculator
from calc.xtb_spec import XtbSpec
from chemclaw.config import settings

_HARTREE_TO_KCAL = 627.5094740631


class OptSpec(XtbSpec):
    """Settings of one geometry optimization.

    Every field moves the result and therefore belongs in the cache key, which it
    reaches automatically — `XtbSpec.cache_key` derives from `model_dump()`, so a
    subclass field is keyed by construction exactly as a base field is. That is the
    whole reason the per-task settings live in subclasses instead of widening the base
    model: a single point's key has no business carrying a gradient tolerance.
    """

    task: Literal["opt"] = "opt"
    # Convergence criterion: the largest absolute gradient component, in
    # Hartree/Angstrom, over the atoms that are free to move.
    gradient_tolerance: float = Field(
        default_factory=lambda: settings.xtb_opt_gradient_tolerance, gt=0
    )
    max_steps: int = Field(default_factory=lambda: settings.xtb_opt_max_steps, gt=0)
    # Indices of atoms held at their input positions. Used by the relaxed scan; empty
    # for a free optimization.
    frozen_atoms: tuple[int, ...] = ()


class OptimizationResult(BaseModel):
    """A converged GFN2-xTB minimum, with what it took to get there.

    `structure` is the optimized geometry and is the value downstream tasks consume;
    it carries `origin`, the key of the calculation that produced it, so a
    thermochemistry or reaction result computed from it has its lineage recorded
    rather than implied (GxP).

    A *non*-converged optimization is never returned: it raises. A geometry that is
    not a stationary point produces frequencies, thermochemistry and reaction energies
    that all look ordinary and mean nothing, so the honest contract is that holding an
    `OptimizationResult` guarantees convergence (gate G4).
    """

    smiles: str | None
    input_structure_id: str
    structure: Structure
    method: str
    solvent: str | None
    initial_energy_hartree: float
    energy_hartree: float
    # How much the relaxation was worth, in the unit a chemist reads. A large value on
    # a supposedly relaxed input means the starting geometry was misleading.
    relaxation_kcal: float
    steps: int
    # Largest absolute gradient component (Hartree/Angstrom) at the final geometry,
    # over the free atoms — the quantity `OptSpec.gradient_tolerance` bounds.
    max_gradient: float
    # Root-mean-square coordinate displacement, in Angstrom. Not Kabsch-aligned: the
    # forces of a molecule sum to zero, so an optimization introduces no net
    # translation and this is a movement measure, not a superposition.
    displacement_rms_angstrom: float
    frozen_atoms: list[int]


class OptimizationSummary(BaseModel):
    """An optimization without its coordinates — what an agent can actually use.

    A model cannot read 3N Cartesians, and pasting them into a conversation is the
    unbounded-context failure the retrieval layer was already audited for. The
    geometry keeps flowing in-process (`calc.reaction` consumes `OptimizationResult`
    directly); `structure_id` is what makes the geometry referable from a transcript.
    """

    smiles: str | None
    structure_id: str
    method: str
    solvent: str | None
    energy_hartree: float
    relaxation_kcal: float
    steps: int
    max_gradient: float
    displacement_rms_angstrom: float

    @classmethod
    def of(cls, result: OptimizationResult) -> "OptimizationSummary":
        """Drop the geometry from a full result, keeping its address."""
        return cls(
            smiles=result.smiles,
            structure_id=result.structure.structure_id,
            method=result.method,
            solvent=result.solvent,
            energy_hartree=result.energy_hartree,
            relaxation_kcal=result.relaxation_kcal,
            steps=result.steps,
            max_gradient=result.max_gradient,
            displacement_rms_angstrom=result.displacement_rms_angstrom,
        )


def optimize_structure(spec: OptSpec, structure: Structure) -> OptimizationResult:
    """Relax `structure` to a GFN2-xTB minimum, or raise if it does not converge.

    Raises `ValueError` if the gradient is still above `spec.gradient_tolerance` after
    `spec.max_steps` — with the numbers, so the caller can tell "nearly there" from
    "this geometry is falling apart".
    """
    numbers, positions = structure.arrays()
    frozen = np.zeros(len(numbers), dtype=bool)
    if spec.frozen_atoms:
        if max(spec.frozen_atoms) >= len(numbers) or min(spec.frozen_atoms) < 0:
            raise ValueError(f"frozen atom index out of range for {len(numbers)} atoms")
        frozen[list(spec.frozen_atoms)] = True
    if frozen.all():
        raise ValueError("every atom is frozen: there is nothing to optimize")

    calc = make_calculator(
        spec.method,
        numbers,
        positions,
        charge=structure.charge,
        uhf=structure.uhf,
        solvent=spec.solvent,
    )
    # The gradient of a frozen coordinate is zeroed rather than merely bounded: the
    # convergence test must measure the forces the optimizer is allowed to relieve,
    # not the ones the constraint is holding.
    free_mask = np.repeat(~frozen, 3)

    def objective(flat: np.ndarray) -> tuple[float, np.ndarray]:
        energy, gradient, _ = evaluate_point(calc, flat.reshape(-1, 3))
        return energy, np.where(free_mask, gradient.ravel(), 0.0)

    initial_energy, _, _ = evaluate_point(calc, positions)
    # Trust region, enforced with bounds. L-BFGS-B's first trial step is scaled by
    # 1/|gradient|, which on a strained starting geometry is wildly too large: measured
    # on a water with a 1.6 Angstrom O-H, its opening move collapses the bond to 0.20
    # Angstrom, and a step like that puts the SCF somewhere it does not converge at all.
    # Capping each coordinate's motion per leg and restarting from where it lands is the
    # standard remedy, and costs nothing on a geometry that was already close.
    trust = settings.xtb_opt_trust_radius
    current = positions.ravel()
    steps = 0
    max_gradient = float("inf")
    while steps < spec.max_steps:
        outcome = minimize(
            objective,
            current,
            jac=True,
            method="L-BFGS-B",
            bounds=[
                (value - trust, value + trust) if free else (value, value)
                for value, free in zip(current, free_mask, strict=True)
            ],
            options={
                "maxiter": spec.max_steps - steps,
                "gtol": spec.gradient_tolerance,
                # L-BFGS-B also stops when the *energy* stops changing, which for a
                # tight gradient target fires first and reports success at a geometry
                # that is not converged by the criterion we promised. Disable it and
                # let the gradient decide.
                "ftol": 0.0,
            },
        )
        current = np.asarray(outcome.x, dtype=float)
        steps += max(int(outcome.nit), 1)
        _, gradient, _ = evaluate_point(calc, current.reshape(-1, 3))
        max_gradient = float(np.max(np.abs(np.where(free_mask, gradient.ravel(), 0.0))))
        if max_gradient <= spec.gradient_tolerance:
            break

    final = current.reshape(-1, 3)
    energy, _, _ = evaluate_point(calc, final)
    if max_gradient > spec.gradient_tolerance:
        raise ValueError(
            f"geometry optimization did not converge in {steps} steps: "
            f"max |gradient| {max_gradient:.2e} > {spec.gradient_tolerance:.2e} "
            "Hartree/Angstrom"
        )

    key = spec.cache_key(structure)
    optimized = Structure(
        elements=structure.elements,
        positions=[[float(value) for value in row] for row in final],
        charge=structure.charge,
        multiplicity=structure.multiplicity,
        smiles=structure.smiles,
        origin=key.as_str(),
    )
    return OptimizationResult(
        smiles=structure.smiles,
        input_structure_id=structure.structure_id,
        structure=optimized,
        method=spec.method,
        solvent=spec.solvent,
        initial_energy_hartree=initial_energy,
        energy_hartree=energy,
        relaxation_kcal=(initial_energy - energy) * _HARTREE_TO_KCAL,
        steps=steps,
        max_gradient=max_gradient,
        displacement_rms_angstrom=float(np.sqrt(np.mean((final - positions) ** 2))),
        frozen_atoms=list(spec.frozen_atoms),
    )


async def run_cached_optimization(
    store: ResultStore, structure: Structure, spec: OptSpec | None = None
) -> tuple[OptimizationResult, bool]:
    """Return the relaxed form of `structure`, reusing the store on a repeat.

    Takes a `Structure` rather than a SMILES because the callers that matter compose:
    `calc.reaction` optimizes each species it was handed, and `calc.xtb_thermo` needs
    the geometry, not the recipe. The agent-facing entry points build the structure.
    """
    spec = spec or OptSpec()
    return await run_cached(
        store,
        spec.cache_key(structure),
        lambda: optimize_structure(spec, structure),
        OptimizationResult,
    )
