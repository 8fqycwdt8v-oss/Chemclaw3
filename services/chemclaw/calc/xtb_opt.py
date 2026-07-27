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
from scipy.optimize import OptimizeResult, minimize

from calc import anc, xtb_cli
from calc.store import ResultStore, run_cached
from calc.structure import Structure
from calc.xtb_engine import Calculator, evaluate_point, make_calculator
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

    `max_gradient` is `None` for **GFN-FF only**, and that is the one case where the
    guarantee is worded differently rather than weakened: a force field has no tblite
    equivalent, so this module cannot re-evaluate its gradient, and convergence is xtb's
    own ANCopt convergence on the GFN-FF surface — required, not assumed.
    """

    smiles: str | None
    input_structure_id: str
    structure: Structure
    method: str
    # Which backend produced this geometry. Recorded because the two do not agree to the
    # last decimal, so a reader comparing two results needs to know they are comparable.
    engine: str
    solvent: str | None
    initial_energy_hartree: float
    energy_hartree: float
    # How much the relaxation was worth, in the unit a chemist reads. A large value on
    # a supposedly relaxed input means the starting geometry was misleading.
    relaxation_kcal: float
    steps: int
    # Largest absolute gradient component (Hartree/Angstrom) at the final geometry,
    # over the free atoms — the quantity `OptSpec.gradient_tolerance` bounds. `None` only
    # for GFN-FF, whose surface this module cannot evaluate (see the class docstring).
    max_gradient: float | None
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
    engine: str
    solvent: str | None
    energy_hartree: float
    relaxation_kcal: float
    steps: int
    max_gradient: float | None
    displacement_rms_angstrom: float

    @classmethod
    def of(cls, result: OptimizationResult) -> "OptimizationSummary":
        """Drop the geometry from a full result, keeping its address."""
        return cls(
            smiles=result.smiles,
            structure_id=result.structure.structure_id,
            method=result.method,
            engine=result.engine,
            solvent=result.solvent,
            energy_hartree=result.energy_hartree,
            relaxation_kcal=result.relaxation_kcal,
            steps=result.steps,
            max_gradient=result.max_gradient,
            displacement_rms_angstrom=result.displacement_rms_angstrom,
        )


def optimize_structure(spec: OptSpec, structure: Structure) -> OptimizationResult:
    """Relax `structure` to a minimum, or raise if it does not converge.

    Dispatches on the spec's engine, after `for_structure` has had its say — an
    open-shell species goes to the in-process backend whatever was configured, because
    the binary cannot apply the spin-polarization term its energy needs.

    The `xtb` binary optimizes in approximate normal
    coordinates (ANCopt) and is 9-11x faster on drug-sized molecules than the Cartesian
    L-BFGS below it — measured, see `calc.xtb_cli`. The in-process path remains the
    fallback for a deployment without the binary, and the two are separately cached
    because they do not produce identical geometries.

    Raises `ValueError` if the gradient is still above `spec.gradient_tolerance` after
    `spec.max_steps` — with the numbers, so the caller can tell "nearly there" from
    "this geometry is falling apart".
    """
    resolved = spec.for_structure(structure)
    if resolved.engine == "xtb":
        return _optimize_with_binary(resolved, structure)
    if resolved.method == "GFN-FF":
        # Named here rather than surfacing tblite's own "Method 'GFN-FF' is not available
        # for this calculator", which is true but says nothing about what to do. Reachable
        # two ways: a deployment without the binary, and a *radical*, which `for_structure`
        # sends in-process whatever was configured.
        raise ValueError(
            "GFN-FF is a force field and exists only in the xtb binary, which is "
            f"{'not installed' if not xtb_cli.is_available() else 'unavailable for this input'}"
            "; use a GFN method or install xtb"
        )
    return _optimize_with_library(resolved, structure)


def _preconditioned_leg(
    calc: Calculator,
    spec: OptSpec,
    origin: np.ndarray,
    free_mask: np.ndarray,
    vectors: np.ndarray,
    scale: np.ndarray,
    max_iterations: int,
) -> tuple[np.ndarray, int]:
    """Run L-BFGS-B once in the preconditioned basis; return the geometry and its cost.

    A leg rather than the whole optimization, because the model Hessian depends on the
    interatomic distances and a leg can move them enough that the basis is worth
    rebuilding. Its own function rather than a closure inside the loop so the basis it
    uses is an argument — the version that captured the loop variables was correct only
    by accident of evaluation order.
    """

    def to_cartesian(step: np.ndarray) -> np.ndarray:
        full = origin.copy()
        full[free_mask] += vectors @ (step * scale)
        return full

    # The convergence promise is about the *Cartesian* gradient, and the optimizer sees a
    # preconditioned one — so rather than converting a threshold between the two (the
    # first attempt converted it the wrong way and every leg stopped almost immediately),
    # the objective records what the promise is actually about and a callback stops the
    # leg the moment it is met.
    reached = {"max_gradient": float("inf")}

    def objective(step: np.ndarray) -> tuple[float, np.ndarray]:
        energy, gradient, _ = evaluate_point(calc, to_cartesian(step).reshape(-1, 3))
        free_gradient = gradient.ravel()[free_mask]
        reached["max_gradient"] = float(np.max(np.abs(free_gradient)))
        # Chain rule through the linear transform: dE/ds = scale * (V^T dE/dx).
        return energy, scale * (vectors.T @ free_gradient)

    def stop_when_converged(intermediate: OptimizeResult) -> None:
        """Halt the leg the moment the Cartesian promise is met.

        `StopIteration` is scipy's documented way for a callback to end a minimization;
        it returns the best point found rather than treating it as a failure.
        """
        if reached["max_gradient"] <= spec.gradient_tolerance:
            raise StopIteration

    # The trust region is a Cartesian distance, so it becomes a per-coordinate bound in
    # the preconditioned basis by dividing by that coordinate's own scale — a soft
    # direction is allowed a large `s` because a large `s` moves the atoms little.
    limit = settings.xtb_opt_trust_radius / np.maximum(scale, 1e-12)
    # `type: ignore` scoped to one call: scipy-stubs' `minimize` overload for
    # `jac=True` *with* a callback requires the objective to accept `*args, **kwargs`,
    # which this one has no use for. The call is correct; the stub is narrow.
    outcome = minimize(  # type: ignore[call-overload]
        objective,
        np.zeros(int(free_mask.sum())),
        jac=True,
        method="L-BFGS-B",
        bounds=list(zip(-limit, limit, strict=True)),
        callback=stop_when_converged,
        options={
            "maxiter": max_iterations,
            # Both of L-BFGS-B's own stopping tests are disabled: `ftol` fires long
            # before a tight gradient target is met, and `gtol` is in the preconditioned
            # units the callback exists to avoid reasoning about.
            "gtol": 0.0,
            "ftol": 0.0,
        },
    )
    return to_cartesian(np.asarray(outcome.x, dtype=float)), max(int(outcome.nit), 1)


def _free_max_gradient(gradient: np.ndarray, free_mask: np.ndarray) -> float:
    """The largest gradient component the optimizer is allowed to relieve.

    A frozen coordinate's component is zeroed rather than merely ignored: the convergence test must
    measure the forces the optimizer *can* remove, not the ones a constraint is holding — otherwise
    a constrained scan point could never converge.
    """
    return float(np.max(np.abs(np.where(free_mask, gradient.ravel(), 0.0))))


def _optimize_with_library(spec: OptSpec, structure: Structure) -> OptimizationResult:
    """Relax with tblite's analytic gradient, L-BFGS-B, and an ANC preconditioner.

    The fallback backend, and the only one that can hold atoms fixed or describe an open
    shell — which makes it the optimizer for relaxed scans and radicals no matter what
    else is installed, and is why it is worth preconditioning rather than leaving slow
    (`calc.anc`, plan X9).

    The optimization runs in the eigenbasis of an approximate Hessian, scaled by the
    square root of its curvature, so the surface L-BFGS sees is nearly isotropic. The
    transform is linear, so a step in it is an exact Cartesian displacement and there is
    nothing to back-transform. The trust region and the convergence test both stay in
    Cartesian space, where they mean something physical.
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

    initial_energy, initial_gradient, _ = evaluate_point(calc, positions)
    # Trust region, enforced with bounds. L-BFGS-B's first trial step is scaled by
    # 1/|gradient|, which on a strained starting geometry is wildly too large: measured
    # on a water with a 1.6 Angstrom O-H, its opening move collapses the bond to 0.20
    # Angstrom, and a step like that puts the SCF somewhere it does not converge at all.
    # Capping each coordinate's motion per leg and restarting from where it lands is the
    # standard remedy, and costs nothing on a geometry that was already close.
    current = positions.ravel()
    steps = 0
    # Test convergence on the *input* geometry before displacing it. Without this the loop always
    # runs at least one leg, so re-optimizing an already-converged structure moved it a little and
    # gave it a different `structure_id` — the hash is over rounded coordinates — which forks the
    # calculation cache for everything downstream of that geometry and quietly breaks the
    # compute-once guarantee (D-011). The gradient is free here: `evaluate_point` above already
    # computed it for the initial energy, and it was being discarded.
    max_gradient = _free_max_gradient(initial_gradient, free_mask)
    while max_gradient > spec.gradient_tolerance and steps < spec.max_steps:
        # The basis is rebuilt each leg, from the geometry the last one reached: the
        # model Hessian depends on the distances, and a leg can move them enough to
        # matter. It costs one O(N^2) assembly and one eigendecomposition — negligible
        # against the SCFs the leg is about to run.
        origin = current.copy()
        vectors, scale = anc.basis(numbers, origin.reshape(-1, 3), free_mask)
        current, iterations = _preconditioned_leg(
            calc, spec, origin, free_mask, vectors, scale, spec.max_steps - steps
        )
        steps += iterations
        _, gradient, _ = evaluate_point(calc, current.reshape(-1, 3))
        max_gradient = _free_max_gradient(gradient, free_mask)

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
        engine=spec.engine,
        solvent=spec.solvent,
        initial_energy_hartree=initial_energy,
        energy_hartree=energy,
        relaxation_kcal=(initial_energy - energy) * _HARTREE_TO_KCAL,
        steps=steps,
        max_gradient=max_gradient,
        displacement_rms_angstrom=float(np.sqrt(np.mean((final - positions) ** 2))),
        frozen_atoms=list(spec.frozen_atoms),
    )


def _optimize_with_binary(spec: OptSpec, structure: Structure) -> OptimizationResult:
    """Relax with `xtb --opt` (ANCopt), then verify convergence on our own criterion.

    The convergence check is deliberately *ours*, re-evaluated on the returned geometry
    rather than trusted from xtb's exit status: the contract of this module is that an
    `OptimizationResult` satisfies `spec.gradient_tolerance`, and a backend that
    converged to its own looser threshold must not quietly weaken that. It costs one
    gradient evaluation.

    Frozen atoms fall back to the Cartesian path — pinning coordinates is expressible as
    optimizer bounds but not as an xtb flag without writing a control file, which is
    exactly the input surface `calc.xtb_cli` refuses to have.

    **GFN-FF is verified on its own surface**, because there is no other honest option:
    tblite has no force field, so re-evaluating the geometry in-process would test a
    GFN-FF minimum against a *GFN2* gradient — a different potential energy surface, on
    which a converged force-field geometry is simply not a stationary point. Measured, an
    octane relaxed by GFN-FF carries a GFN2 max-gradient of 1.3e-2 against this module's
    5e-4 target, so every GFN-FF optimization raised "did not converge". Its convergence
    is now xtb's own ANC convergence, required rather than assumed, and its energy is the
    binary's own — a GFN2 number labelled GFN-FF was the other half of the same bug.
    """
    if spec.frozen_atoms:
        return _optimize_with_library(spec, structure)
    outcome = xtb_cli.run(
        structure,
        task="opt",
        method=spec.method,
        solvent=spec.solvent,
        max_cycles=spec.max_steps,
    )
    if outcome.structure is None:
        raise ValueError("xtb --opt produced no optimized geometry")
    key = spec.cache_key(structure)
    optimized = outcome.structure.model_copy(update={"origin": key.as_str()})
    if spec.method == "GFN-FF":
        return _force_field_result(spec, structure, optimized, outcome)
    initial, _, _ = _energy_and_gradient(spec, structure, structure)
    energy, gradient, _ = _energy_and_gradient(spec, structure, optimized)
    max_gradient = float(np.max(np.abs(gradient)))
    if max_gradient > spec.gradient_tolerance:
        raise ValueError(
            f"geometry optimization did not converge in {outcome.cycles} ANC cycles: "
            f"max |gradient| {max_gradient:.2e} > {spec.gradient_tolerance:.2e} "
            "Hartree/Angstrom"
        )
    _, positions = structure.arrays()
    final = np.array(optimized.positions)
    return OptimizationResult(
        smiles=structure.smiles,
        input_structure_id=structure.structure_id,
        structure=optimized,
        method=spec.method,
        engine=spec.engine,
        solvent=spec.solvent,
        initial_energy_hartree=initial,
        energy_hartree=energy,
        relaxation_kcal=(initial - energy) * _HARTREE_TO_KCAL,
        steps=outcome.cycles or 0,
        max_gradient=max_gradient,
        displacement_rms_angstrom=float(np.sqrt(np.mean((final - positions) ** 2))),
        frozen_atoms=[],
    )


def _force_field_result(
    spec: OptSpec, structure: Structure, optimized: Structure, outcome: xtb_cli.CliResult
) -> OptimizationResult:
    """Package a GFN-FF relaxation, whose only convergence evidence is xtb's own.

    `outcome.cycles` is parsed from xtb's "CONVERGED AFTER" line, so requiring it is
    requiring the binary to say it converged — not inferring it from an exit code, which
    `calc.xtb_cli` documents as unreliable. Without it there is no evidence at all, and
    the contract is that an `OptimizationResult` is a converged one.

    `initial_energy_hartree` equals the final energy because a force-field single point at
    the input geometry would be a second subprocess for a number nothing reads; the
    relaxation is reported as 0.0 rather than invented.
    """
    if outcome.cycles is None:
        raise ValueError(
            "xtb --opt with GFN-FF did not report convergence, and a force-field geometry "
            "cannot be verified in-process (tblite has no GFN-FF): refusing to return it"
        )
    _, positions = structure.arrays()
    final = np.array(optimized.positions)
    return OptimizationResult(
        smiles=structure.smiles,
        input_structure_id=structure.structure_id,
        structure=optimized,
        method=spec.method,
        engine=spec.engine,
        solvent=spec.solvent,
        initial_energy_hartree=outcome.energy_hartree,
        energy_hartree=outcome.energy_hartree,
        relaxation_kcal=0.0,
        steps=outcome.cycles,
        max_gradient=None,
        displacement_rms_angstrom=float(np.sqrt(np.mean((final - positions) ** 2))),
        frozen_atoms=[],
    )


def _energy_and_gradient(
    spec: OptSpec, template: Structure, at: Structure
) -> tuple[float, np.ndarray, np.ndarray]:
    """Evaluate energy and gradient at `at`, using the in-process engine.

    Used to verify a binary-produced geometry against our own convergence criterion.
    Never reached for GFN-FF — that path returns before this, because substituting GFN2
    here is what made a force-field optimization fail against the wrong surface.
    """
    numbers, _ = template.arrays()
    calc = make_calculator(
        spec.method,
        numbers,
        np.array(at.positions),
        charge=at.charge,
        uhf=at.uhf,
        solvent=spec.solvent,
    )
    return evaluate_point(calc, np.array(at.positions))


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
