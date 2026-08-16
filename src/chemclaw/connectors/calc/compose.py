"""Composition over remote primitives: what a composite calculation is, now that it is not shipped.

`D-2026-08-16-the-physics-leaves-the-cache-stays` split `calc` by **composability** rather than by
speed. A *primitive* — one calculation whose identity is derivable from its inputs — moved to
`Chemclaw3-mcp` and is cached here under the key the server derives. A *composite* — anything whose
key would have to name an output — was **not shipped at all**, because a single remote call would
swallow the nested entries that are its entire economy: `compute_thermochemistry`'s key would name
the geometry its refinement loop *settles on*, and moving it whole would have turned every repeat
into a full recompute (measured: `CCO` 0.816 s cold, 0.007 s warm; `CC(=O)OCC` 3.273 s against
0.012 s).

So this module is where the composites live. Every function here is the same shape — ask for the
parts, each separately cached, and do the bookkeeping in between — and the bookkeeping is the part
that was always ours: balance checking, symmetry-number discipline, relative energies, populations,
warnings a chemist acts on.

**Why the tool path and the durable path share it.** `compute_thermochemistry` (an MCP tool) and the
reaction job (a Temporal activity) both need relax-then-Hessian-then-RRHO with the same saddle-point
escape. Writing it twice is how the two drift, and one of them is the one nobody re-reads. What
differs between them is not the chemistry but the *waiting*: an activity must heartbeat through a
minutes-long call or Temporal declares it dead, and `activity.heartbeat` raises outside an activity
context, so a tool cannot use it. That difference is one parameter, `run`, and it defaults to a
plain await.

**Nothing here derives a `calc_version` or a cache key.** Every key comes from the server, through
`cached_remote`. The rule and the reason are in `connectors/calc/remote.py`; the short form is that
a locally-derived version would be *well-formed*, match zero calibration rows, and make
`calculator_trust` report a confident `UNCALIBRATED` with nothing looking broken.
"""

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol, TypeVar

from rdkit import Chem

from chemclaw.connectors.calc.remote import cached_remote, remote_call
from chemclaw.core.chem import require_canonical_smiles
from chemclaw.core.config import settings
from chemclaw.science.calc.artifacts import (
    HESSIAN_ARRAYS,
    ArrayOffloadingStore,
    ArtifactStore,
)
from chemclaw.science.calc.models import (
    ConformerEnsemble,
    CrestEffort,
    EnsemblePayload,
    EnsembleSearch,
    HessianPayload,
    InteractionResult,
    OptimizationResult,
    ReactionEnergyResult,
    ReactionLevel,
    ScanPoint,
    ScanResult,
    SolventComparisonResult,
    SolventEffect,
    SpeciesEnergy,
    Structure,
    ThermochemistryResult,
)
from chemclaw.science.calc.postgres_artifacts import default_artifact_store
from chemclaw.science.calc.store import ResultStore
from chemclaw.science.calc.thermo import (
    HARTREE_TO_KCAL,
    ThermoSettings,
    displaced_along,
    ensemble_from_members,
    thermochemistry_from_hessian,
)

_Result = TypeVar("_Result")

# How many atoms define each internal coordinate, and the unit its value is in.
_COORDINATES: dict[int, tuple[str, str]] = {
    2: ("bond", "angstrom"),
    3: ("angle", "degree"),
    4: ("dihedral", "degree"),
}

# Called with a human-readable line as each unit of work completes. Minute-scale runs on drug-sized
# molecules are the normal case, so a caller that needs liveness (a durable activity's heartbeat)
# passes one.
Progress = Callable[[str], None]


def no_progress(_message: str) -> None:
    """Default progress sink: a composite called from a tool has nobody to report to."""


class RemoteRunner(Protocol):
    """How one remote call is awaited — the single difference between a tool and an activity.

    A Temporal activity must beat its heartbeat while a minutes-long call is in flight or the worker
    is declared dead and the whole job retried from zero; an MCP tool has no activity context and
    `activity.heartbeat` raises outside one. Passing the *waiting strategy* rather than a flag keeps
    the chemistry identical between the two and puts the one real difference in one argument.
    """

    async def __call__(self, awaitable: Awaitable[_Result], what: str) -> _Result:
        """Await `awaitable`, doing whatever this caller must do while it runs."""
        ...


async def plain(awaitable: Awaitable[_Result], what: str) -> _Result:
    """Await the call and nothing else — the tool path's runner.

    `what` is accepted and unused: it is the description a heartbeating runner reports, and the
    parameter name is part of the `RemoteRunner` protocol rather than decoration.
    """
    del what
    return await awaitable


# --- primitives -----------------------------------------------------------------------------


def radical_multiplicity(smiles: str) -> int:
    """The spin multiplicity a SMILES' explicit radical electrons imply.

    A SMILES *can* state its open shell: `[CH3]` carries one radical electron, `[O][O]` two. Where
    it does, the ground-state multiplicity follows (2S+1 with every radical electron unpaired), and
    there is nothing to guess — which is what makes a homolysis energy computable from two SMILES
    rather than from a hand-declared spin state. Silent on the cases a SMILES genuinely does not
    encode: a closed-shell formula whose ground state is a triplet still needs its multiplicity
    stated explicitly.

    Derived here and passed to `embed_structure` explicitly, because the server reads
    `multiplicity=None` as *closed-shell singlet* rather than as *derive from the radicals* —
    measured: `[CH3]` with
    `multiplicity=None` is refused as "9 electrons at charge 0 cannot be a closed-shell singlet".
    That is a defensible default for a server whose callers state what they mean, and it puts the
    derivation on this side. It belongs here anyway: it is a property of the molecular graph, needs
    no engine, and every reaction species goes through it so a homolysis stays computable.
    """
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(f"invalid SMILES: {smiles!r}")
    return 1 + sum(int(atom.GetNumRadicalElectrons()) for atom in mol.GetAtoms())


async def embed(smiles: str, run: RemoteRunner = plain) -> Structure:
    """The force-field-cleaned starting geometry for one molecule, from the server.

    Remote rather than local even though RDKit is installed here, and the reason is the cache: a
    geometry embedded by a different RDKit build is a different `structure_id`, so every relaxation
    and Hessian keyed on it downstream would miss. Embedding on the same side that keys the results
    keeps the two in agreement by construction instead of by a version comparison nobody runs.

    Takes a `run` like every other remote call here, and defaulting it to `plain` is what hid the
    gap: an activity that forgot to pass one still worked, so all four call sites did. A remote
    call is bounded by `calc_server_timeout_seconds` (900 s) and an activity by
    `xtb_job_heartbeat_timeout_seconds` (600 s), so an embed slow enough to sit in that window
    trips the activity's heartbeat timeout — Temporal retries the job while the original call is
    still running, which is the exact failure `beating` was extracted to end.
    """
    payload = await run(
        remote_call(
            "embed_structure",
            {
                "smiles": smiles,
                "multiplicity": radical_multiplicity(smiles),
                "relax_with_force_field": True,
            },
        ),
        f"starting geometry for {smiles}",
    )
    return Structure.model_validate(payload)


async def relax(
    store: ResultStore,
    structure: Structure,
    solvent: str | None,
    *,
    run: RemoteRunner = plain,
) -> tuple[OptimizationResult, bool]:
    """Relax one geometry to the nearest minimum, cached under the server's key."""
    payload, cached = await run(
        cached_remote(
            store,
            "relax_structure",
            {"structure": structure.model_dump(mode="json"), "solvent": solvent},
        ),
        f"optimising {structure.smiles or structure.structure_id}",
    )
    return OptimizationResult.model_validate(payload), cached


async def hessian(
    store: ResultStore,
    structure: Structure,
    solvent: str | None,
    *,
    artifacts: ArtifactStore | None = None,
    run: RemoteRunner = plain,
) -> tuple[HessianPayload, bool]:
    """Take the second derivatives at one geometry, cached under the server's key.

    Keyed on what can move the matrix and nothing else — geometry, method, solvent — so a second
    thermochemistry question about the same minimum at another temperature is a hit here and a
    millisecond of `science/calc/thermo.py` arithmetic after it.

    **The one calculation whose result does not fit in its row.** A Hessian is megabytes where every
    other payload is numbers — 33 atoms is 99x99 doubles, 120 atoms about 1.4 MB — and
    `durable/retention.py` refuses to prune `calculation_results` at all, because D-011 says a
    persisted result is never recomputed. Storing the matrix inline therefore builds a table that
    grows without bound and has no reclaim path by design, which is exactly what D-124 built the
    content-addressed artifact store to avoid. So the store this hands to `cached_remote` is
    wrapped: the packed arrays go to the artifact store and the row keeps their content hashes.
    Nothing else about the call changes, which is the point of expressing the policy as a
    `ResultStore` rather than as a second caching path.
    """
    blobs = artifacts if artifacts is not None else default_artifact_store()
    payload, cached = await run(
        cached_remote(
            ArrayOffloadingStore(store, blobs, HESSIAN_ARRAYS),
            "compute_hessian",
            {"structure": structure.model_dump(mode="json"), "solvent": solvent},
        ),
        f"second derivatives of {structure.smiles or structure.structure_id}",
    )
    return HessianPayload.model_validate(payload), cached


# --- thermochemistry ------------------------------------------------------------------------


async def relax_to_minimum(
    store: ResultStore,
    structure: Structure,
    solvent: str | None,
    thermo: ThermoSettings | None = None,
    *,
    run: RemoteRunner = plain,
) -> tuple[OptimizationResult, ThermochemistryResult, bool]:
    """Optimize until the geometry is a genuine minimum, then return it with its thermochemistry.

    A plain gradient optimization converges to the nearest *stationary* point, which is not always a
    minimum. The common case is mundane and universal: a force field hands over a molecule with an
    eclipsed methyl, and a Cartesian optimizer preserves that symmetry all the way down onto the
    rotational saddle. Measured on ethyl acetate — an ordinary ester, not a contrived example — the
    optimizer settles at a -42 cm^-1 mode, and the free energy computed there is not a free energy.

    The escape is standard practice and cheap: displace along the imaginary mode and re-optimize.
    Ethyl acetate needs one such step and lands 0.016 kcal/mol lower, confirming what it was — a
    shallow rotor saddle rather than a different structure.

    **This loop is why thermochemistry was not shipped.** Its key would have to name the geometry
    the loop settles on, which is an output; the parts it walks through are each keyed on their own
    input, so a repeat pays two round trips and no SCF.

    Bounded by `settings.xtb_minimum_refinement_attempts`, after which the result is returned as it
    stands with `is_minimum=False` intact. A structure that will not settle is reporting something
    real about itself, and looping on it is not the fix.

    The third element of the return is whether *every* underlying calculation was a cache hit —
    what a caller reports as "this cost nothing". The RRHO arithmetic is always recomputed and is
    deliberately not counted: it is milliseconds, and it is what depends on the temperature.
    """
    thermo = thermo or ThermoSettings()
    current = structure
    cached = True
    for _ in range(settings.xtb_minimum_refinement_attempts + 1):
        optimization, opt_cached = await relax(store, current, solvent, run=run)
        matrix, hess_cached = await hessian(store, optimization.structure, solvent, run=run)
        # Off the event loop: a 3N x 3N eigendecomposition on a drug-sized molecule is real work,
        # and this coroutine shares its loop with every other in-flight request.
        result = await asyncio.to_thread(
            thermochemistry_from_hessian, thermo, optimization.structure, matrix
        )
        cached = cached and opt_cached and hess_cached
        if result.is_minimum or result.imaginary_displacement is None:
            return optimization, result, cached
        current = displaced_along(optimization.structure, result.imaginary_displacement)
    return optimization, result, cached


# --- relaxed scan ---------------------------------------------------------------------------


async def scan_profile(
    store: ResultStore,
    smiles: str,
    atoms: tuple[int, ...],
    values: tuple[float, ...],
    solvent: str | None,
    *,
    progress: Progress = no_progress,
    run: RemoteRunner = plain,
) -> ScanResult:
    """Relax the molecule at every value of one internal coordinate and assemble the profile.

    Each point is a `scan_point` call: the server drives the coordinate with RDKit's
    `rdMolTransforms` — which moves the whole attached fragment, so the point starts from a
    chemically sensible geometry — freezes the defining atoms and relaxes everything else. Each is
    separately keyed, so a scan re-run with two extra values pays for two points.

    Every point is driven from the *input* geometry rather than from the previous one. That costs a
    little convergence speed and buys determinism: a sequential scan's result depends on the
    direction it was walked, which is exactly the kind of hidden input a content-addressed cache
    must not have (D-011).

    `maximum_relative_kcal` is the highest point of the *profile*, not an optimized transition
    state. For a torsion it is a sound barrier estimate; for a bond being broken it is an
    upper-bound sketch — there is no saddle-point search here.
    """
    limit = settings.xtb_scan_max_points
    if len(values) > limit:
        raise ValueError(
            f"a relaxed scan is capped at {limit} points "
            f"(xtb_scan_max_points); {len(values)} were requested"
        )
    if len(atoms) not in _COORDINATES:
        raise ValueError(f"a scan coordinate is 2, 3 or 4 atoms; {len(atoms)} were given")
    coordinate, unit = _COORDINATES[len(atoms)]
    structure = await embed(smiles, run=run)
    if max(atoms) >= len(structure.elements) or min(atoms) < 0:
        raise ValueError(f"scan atom index out of range for {len(structure.elements)} atoms")

    relaxed: list[OptimizationResult] = []
    for index, value in enumerate(values, start=1):
        progress(f"point {index}/{len(values)}: {coordinate} = {value:g} {unit}")
        payload, _ = await run(
            cached_remote(
                store,
                "scan_point",
                {
                    "structure": structure.model_dump(mode="json"),
                    "atoms": list(atoms),
                    "value": value,
                    "solvent": solvent,
                },
            ),
            f"{coordinate} at {value:g} {unit}",
        )
        relaxed.append(OptimizationResult.model_validate(payload))

    energies = [point.energy_hartree for point in relaxed]
    lowest = min(range(len(energies)), key=lambda index: energies[index])
    relative = [(energy - energies[lowest]) * HARTREE_TO_KCAL for energy in energies]
    return ScanResult(
        smiles=structure.smiles,
        input_structure_id=structure.structure_id,
        method=relaxed[0].method,
        solvent=solvent,
        coordinate=coordinate,
        atoms=list(atoms),
        unit=unit,
        points=[
            ScanPoint(value=value, energy_hartree=energy, relative_kcal=round(shift, 3))
            for value, energy, shift in zip(values, energies, relative, strict=True)
        ],
        minimum_value=values[lowest],
        maximum_relative_kcal=round(max(relative), 3),
        minimum_structure=relaxed[lowest].structure,
    )


# --- conformer ensembles --------------------------------------------------------------------


async def conformer_ensemble(
    store: ResultStore,
    smiles: str,
    *,
    search: EnsembleSearch = "conformers",
    effort: CrestEffort | None = None,
    solvent: str | None = None,
    temperature_k: float | None = None,
    run: RemoteRunner = plain,
) -> tuple[ConformerEnsemble, bool]:
    """Search conformational space and weight what was found at `temperature_k`.

    One remote call, because a CREST search cannot be decomposed: it is a single metadynamics run
    with no unit boundary inside it. It is also the most expensive single calculation in the system,
    which is why the *weighting* stayed here — populations and the conformational entropy depend on
    a temperature the search never saw, so asking the same molecule at 310 K after 298 K is a cache
    hit plus arithmetic rather than a second search.

    The search is stochastic, so this is a sample of conformational space rather than an enumeration
    of it. The cache is what makes it stable: the first run's members are what every later question
    about that molecule is weighted from.
    """
    payload, cached = await run(
        cached_remote(
            store,
            "search_conformer_ensemble",
            {
                "structure": (await embed(smiles, run=run)).model_dump(mode="json"),
                "search": search,
                "effort": effort or settings.crest_effort,
                "solvent": solvent,
            },
        ),
        f"{search} of {smiles}",
    )
    return (
        ensemble_from_members(
            EnsemblePayload.model_validate(payload),
            smiles=require_canonical_smiles(smiles),
            search=search,
            temperature_k=temperature_k or settings.xtb_thermo_temperature_k,
            max_members=settings.crest_max_members,
        ),
        cached,
    )


# --- non-covalent complexes -----------------------------------------------------------------


def _ordered(smiles_a: str, smiles_b: str) -> tuple[str, str]:
    """The pair in a canonical order, so A-with-B and B-with-A are one calculation.

    The interaction of two molecules is one physical quantity, but the starting arrangement is not
    symmetric in its arguments: `combine_structures` holds the first monomer at the origin and
    offsets the second along +x, so swapping them negates the intermolecular vector while leaving
    each monomer's own orientation alone. That is a *different* starting geometry, and it would key
    to a different cache entry — paying twice, at minutes per search, for the same answer.
    """
    first, second = require_canonical_smiles(smiles_a), require_canonical_smiles(smiles_b)
    return (first, second) if first <= second else (second, first)


async def interaction(
    store: ResultStore,
    smiles_a: str,
    smiles_b: str,
    *,
    effort: CrestEffort | None = None,
    solvent: str | None = None,
    run: RemoteRunner = plain,
) -> InteractionResult:
    """Search the binding modes of two molecules and difference the relaxed species.

    The interaction energy is computed the only way that means anything: the complex at its
    optimized binding mode, minus each monomer optimized on its own. That deliberately includes the
    deformation cost of binding, which is part of the interaction and is what a "rigid monomer"
    definition leaves out.

    Five cached calls and no composite key: two monomer relaxations (shared with every other
    question about those molecules), the binding-mode search, and one relaxation of the mode it
    picked. The pair is canonicalized first, so either argument order reaches the same entries.

    Three limits belong with every number this produces. **It is an energy, not a free energy** —
    association costs entropy and that term is absent, so a complex with a favourable interaction
    energy can be entirely unbound at room temperature. **The search is stochastic**, so a binding
    mode that was not sampled cannot be reported. **It is one pair, in a continuum**: no bulk, no
    competing solvent molecules, no stoichiometry beyond two.
    """
    smiles_a, smiles_b = _ordered(smiles_a, smiles_b)
    monomers = []
    for smiles in (smiles_a, smiles_b):
        relaxed, _ = await relax(store, await embed(smiles, run=run), solvent, run=run)
        monomers.append(relaxed)
    # The separation between the two monomers' bounding spheres is the server's own default: it is
    # only a starting point — the wall potential and the search decide where they end up — and it
    # belongs to the geometry builder rather than to this orchestration.
    combined = Structure.model_validate(
        await run(
            remote_call(
                "combine_structures",
                {
                    "first": monomers[0].structure.model_dump(mode="json"),
                    "second": monomers[1].structure.model_dump(mode="json"),
                },
            ),
            f"starting complex geometry for {smiles_a} and {smiles_b}",
        )
    )
    payload, _ = await run(
        cached_remote(
            store,
            "search_binding_modes",
            {
                "structure": combined.model_dump(mode="json"),
                "effort": effort or settings.crest_effort,
                "solvent": solvent,
            },
        ),
        f"binding modes of {smiles_a} and {smiles_b}",
    )
    modes = EnsemblePayload.model_validate(payload)
    if not modes.members:
        raise ValueError("the complex search returned no binding modes")
    best = min(modes.members, key=lambda member: member.energy_hartree)
    bound, _ = await relax(store, best.structure, solvent, run=run)
    separated = sum(monomer.energy_hartree for monomer in monomers)
    return InteractionResult(
        smiles_a=smiles_a,
        smiles_b=smiles_b,
        method=modes.method,
        solvent=solvent,
        interaction_energy_kcal=round((bound.energy_hartree - separated) * HARTREE_TO_KCAL, 2),
        complex_energy_hartree=bound.energy_hartree,
        monomer_energies_hartree=[monomer.energy_hartree for monomer in monomers],
        binding_modes=modes.total_found,
        structure=bound.structure,
    )


# --- reaction energetics --------------------------------------------------------------------


def _composition(smiles: str) -> tuple[Counter[str], int]:
    """Element counts (hydrogens explicit) and formal charge of one species."""
    parsed = Chem.MolFromSmiles(smiles)
    if parsed is None:
        raise ValueError(f"invalid SMILES: {smiles!r}")
    mol = Chem.AddHs(parsed)
    counts: Counter[str] = Counter(atom.GetSymbol() for atom in mol.GetAtoms())
    return counts, Chem.GetFormalCharge(mol)


def check_balance(reactants: list[str], products: list[str]) -> None:
    """Raise unless the equation conserves atoms and charge (gate G4).

    An unbalanced equation produces a difference that includes whatever atoms the two sides do not
    share — a number that is meaningless rather than merely imprecise, and one that looks entirely
    ordinary. Named rather than just detected: the message says which element is short and by how
    much, because the usual cause is a forgotten water or proton and that is immediately fixable
    once stated.

    Local, and it should be: it is a graph property with no engine behind it, and it is what stops
    a request before any remote call is made.
    """
    if not reactants or not products:
        raise ValueError("a reaction needs at least one reactant and one product")
    left: Counter[str] = Counter()
    right: Counter[str] = Counter()
    left_charge = right_charge = 0
    for smiles in reactants:
        counts, charge = _composition(smiles)
        left += counts
        left_charge += charge
    for smiles in products:
        counts, charge = _composition(smiles)
        right += counts
        right_charge += charge
    if left != right:
        difference = {
            element: left[element] - right[element]
            for element in sorted(set(left) | set(right))
            if left[element] != right[element]
        }
        raise ValueError(
            "reaction is not atom-balanced (reactants minus products): "
            + ", ".join(f"{element} {count:+d}" for element, count in difference.items())
        )
    if left_charge != right_charge:
        raise ValueError(
            f"reaction is not charge-balanced: reactants {left_charge:+d}, "
            f"products {right_charge:+d}"
        )


def _checked_symmetry_numbers(
    symmetry_numbers: dict[str, int] | None, species: set[str]
) -> dict[str, int]:
    """Validate a caller's sigma map against the equation it claims to describe.

    A key that names no species in the equation is a typo — most often a differently written SMILES
    for a species that *is* there. Left unchecked it would look exactly like an omission, and the
    caller would be told their symmetry number is missing while staring at the line where they
    passed it.
    """
    if not symmetry_numbers:
        return {}
    if foreign := sorted(set(symmetry_numbers) - species):
        raise ValueError(
            "symmetry_numbers names species the equation does not contain (SMILES must "
            f"match the reactant/product strings exactly): {', '.join(foreign)}"
        )
    if invalid := sorted(name for name, sigma in symmetry_numbers.items() if sigma < 1):
        raise ValueError(f"a rotational symmetry number is at least 1: {', '.join(invalid)}")
    return dict(symmetry_numbers)


async def _species_energy(
    store: ResultStore,
    smiles: str,
    role: Literal["reactant", "product"],
    solvent: str | None,
    thermo: ThermoSettings | None,
    symmetry_number: int | None,
    level: ReactionLevel,
    run: RemoteRunner,
) -> SpeciesEnergy:
    """Optimize one species and, above `quick`, run its Hessian.

    Multiplicity comes from the SMILES' own radical electrons (`radical_multiplicity`), so a
    homolysis — the reaction whose whole point is that one side is open-shell — needs no extra
    argument to be computable.

    `symmetry_number` is this species' sigma, or None when the caller did not state one. The
    thermochemistry settings are specialized here rather than handed in ready-made so that the
    stated value and the value actually used cannot disagree.
    """
    structure = await embed(smiles, run=run)
    ensemble_correction = 0.0
    if level == "thorough":
        ensemble, _ = await conformer_ensemble(
            store,
            smiles,
            solvent=solvent,
            temperature_k=thermo.temperature_k if thermo else None,
            run=run,
        )
        structure = ensemble.lowest
        ensemble_correction = ensemble.ensemble_correction_kcal
    if thermo is None:
        optimization, cached = await relax(store, structure, solvent, run=run)
        return SpeciesEnergy(
            smiles=smiles,
            role=role,
            multiplicity=structure.multiplicity,
            symmetry_number=None,
            electronic_energy_hartree=optimization.energy_hartree,
            enthalpy_hartree=None,
            gibbs_free_energy_hartree=None,
            is_minimum=None,
            was_cached=cached,
        )
    at_sigma = thermo.model_copy(
        update={"symmetry_number": 1 if symmetry_number is None else symmetry_number}
    )
    minimum, result, cached = await relax_to_minimum(store, structure, solvent, at_sigma, run=run)
    # The conformational entropy is a free-energy term only: it changes G, never H.
    gibbs = result.gibbs_free_energy_hartree + ensemble_correction / HARTREE_TO_KCAL
    return SpeciesEnergy(
        smiles=smiles,
        role=role,
        multiplicity=structure.multiplicity,
        symmetry_number=symmetry_number,
        electronic_energy_hartree=minimum.energy_hartree,
        enthalpy_hartree=result.enthalpy_hartree,
        gibbs_free_energy_hartree=gibbs,
        # `is not None`, not truthiness: a rigid species has a genuine 0.000 correction, and
        # `0.0 or None` reported that as "not computed at this level".
        conformational_entropy_kcal=(
            round(ensemble_correction, 3) if level == "thorough" else None
        ),
        is_minimum=result.is_minimum,
        was_cached=cached,
    )


def _difference(species: list[SpeciesEnergy], attribute: str) -> float | None:
    """Products minus reactants of one energy attribute, in kcal/mol."""
    total = 0.0
    for entry in species:
        value: Any = getattr(entry, attribute)
        if value is None:
            return None
        total += value if entry.role == "product" else -value
    return total * HARTREE_TO_KCAL


def _round(value: float | None) -> float | None:
    """Round a kcal/mol delta, passing None through."""
    return None if value is None else round(value, 2)


async def reaction_energy(
    store: ResultStore,
    reactants: list[str],
    products: list[str],
    solvent: str | None = None,
    temperature_k: float | None = None,
    level: ReactionLevel = "standard",
    symmetry_numbers: dict[str, int] | None = None,
    *,
    progress: Progress = no_progress,
    run: RemoteRunner = plain,
) -> ReactionEnergyResult:
    """Compute the energetics of a balanced reaction, one entry per stoichiometric equivalent.

    Pure composition — this contributed *no* primitive to the server, because a reaction energy is a
    subtraction over per-species optimizations and Hessians that are already keyed individually. A
    second reaction sharing a species reuses it, and there is deliberately no reaction-level cache
    entry: it could never be hit by anything the per-species entries miss.

    Three disciplines are enforced here rather than trusted. **Balance** — an unbalanced equation is
    rejected. **Same treatment on both sides** — every species is optimized with the same settings,
    in the same solvent, at the same level; mixing an optimized product with an unoptimized reactant
    is the other way a reaction energy silently becomes fiction. **A rotational symmetry number is
    stated or ΔG is not reported** — sigma shifts a species' entropy by exactly R ln(sigma), and
    that does *not* cancel across a balanced equation (any hydrogenation consumes H2; anything
    aromatic consumes or makes benzene), so a reaction with any species' sigma unstated reports ΔE
    and ΔH and withholds ΔG with a warning naming the species.

    Args:
        store: The calculation store; every species is computed once, ever.
        reactants: SMILES of every reactant, repeated per stoichiometric equivalent.
        products: SMILES of every product, repeated per stoichiometric equivalent.
        solvent: ALPB implicit solvent name, or None for gas phase.
        temperature_k: Temperature for the thermal corrections; None takes the config default.
        level: `quick` optimizes and gives ΔE only; `standard` adds ΔH and ΔG; `thorough` searches
            conformational space first and adds the conformational entropy.
        symmetry_numbers: Rotational symmetry number per distinct species SMILES, keyed by the exact
            string given in `reactants`/`products`. Stating 1 explicitly is a real statement and
            does yield a ΔG — "no symmetry" and "not considered" are different claims.
        progress: Called with a line as each species completes.
        run: How each remote call is awaited; a durable activity passes a heartbeating runner.

    Returns:
        ΔE and (above `quick`) ΔH/ΔG in kcal/mol, the per-species breakdown, how many species came
        from the cache, and the method uncertainty to report with them.
    """
    check_balance(reactants, products)
    sigmas = _checked_symmetry_numbers(symmetry_numbers, set(reactants) | set(products))
    temperature = temperature_k or settings.xtb_thermo_temperature_k
    thermo = ThermoSettings(temperature_k=temperature) if level != "quick" else None

    roles: tuple[tuple[Literal["reactant", "product"], list[str]], ...] = (
        ("reactant", reactants),
        ("product", products),
    )
    queue = [(role, smiles) for role, group in roles for smiles in group]
    species = []
    for index, (role, smiles) in enumerate(queue, start=1):
        progress(f"species {index}/{len(queue)}: {smiles}")
        species.append(
            await _species_energy(
                store, smiles, role, solvent, thermo, sigmas.get(smiles), level, run
            )
        )
    warnings = [
        f"{entry.smiles} is not a minimum (imaginary frequency): its free energy is not "
        "a free energy"
        for entry in species
        if entry.is_minimum is False
    ]
    # Every level, not just `standard`: the caveat is about the *energies*, which every level
    # differences, so gating it on one level dropped it from exactly the `thorough` homolysis a user
    # paid the most for.
    if any(entry.multiplicity > 1 for entry in species):
        warnings.append(
            "open-shell species present: unrestricted GFN2 energies are less reliable "
            "than closed-shell ones, so treat a homolysis energy as an ordering"
        )
    # Only above `quick`, where an entropy exists at all.
    unstated = (
        sorted({entry.smiles for entry in species if entry.symmetry_number is None})
        if thermo is not None
        else []
    )
    if unstated:
        warnings.append(
            "no rotational symmetry number was given for "
            + ", ".join(unstated)
            + ": their rotational entropy was computed at sigma=1, which is too high by "
            "R ln(sigma) for any symmetric species, so no ΔG is reported. Pass "
            "symmetry_numbers (1 = no rotational symmetry, 2 = H2/N2/O2/CO2/water, "
            "3 = ammonia, 6 = ethane, 12 = benzene). ΔE and ΔH do not depend on it and "
            "stand as reported"
        )
    # Electronic energies are always present, so this delta is never optional.
    delta_e = HARTREE_TO_KCAL * sum(
        entry.electronic_energy_hartree * (1 if entry.role == "product" else -1)
        for entry in species
    )
    return ReactionEnergyResult(
        reactants=reactants,
        products=products,
        method=settings.xtb_method,
        solvent=solvent,
        temperature_k=temperature,
        level=level,
        delta_e_kcal=round(delta_e, 2),
        delta_h_kcal=_round(_difference(species, "enthalpy_hartree")),
        delta_g_kcal=(
            None if unstated else _round(_difference(species, "gibbs_free_energy_hartree"))
        ),
        species=species,
        cache_hits=sum(entry.was_cached for entry in species),
        uncertainty_kcal=settings.xtb_reaction_uncertainty_kcal,
        is_strongly_exothermic=delta_e <= settings.reaction_energy_exotherm_threshold_kcal,
        exotherm_threshold_kcal=settings.reaction_energy_exotherm_threshold_kcal,
        conformer_treatment=(
            "lowest-plus-conformational-entropy" if level == "thorough" else "single"
        ),
        warnings=warnings,
    )


async def solvent_comparison(
    store: ResultStore,
    reactants: list[str],
    products: list[str],
    solvents: list[str],
    temperature_k: float | None = None,
    level: ReactionLevel = "standard",
    symmetry_numbers: dict[str, int] | None = None,
    *,
    progress: Progress = no_progress,
    run: RemoteRunner = plain,
) -> SolventComparisonResult:
    """Rank solvents by how far they push the same reaction toward products.

    Includes the gas phase as a reference point, because "the solvent barely matters here" is a real
    and useful answer and it is invisible without one.

    **Bounded fan-out, and the bound defaults to 1** — which is the serial loop it replaces. The
    media are independent (each is its own cache key, so no branch recomputes another's work), but a
    branch here is a remote SCF and the server sizes itself to its own machine: six solvents at once
    is six calculations each expecting every core of one pod. So the fan-out is available and off,
    and `calc_screen_max_parallel` says what raising it is paired with.
    """
    if not solvents:
        raise ValueError("give at least one solvent to compare")
    limit = asyncio.Semaphore(settings.calc_screen_max_parallel)

    async def one(solvent: str | None) -> ReactionEnergyResult:
        """One medium, under the fan-out bound, reporting progress prefixed with its own name."""
        label = solvent or "gas phase"

        def relay(line: str) -> None:
            """Prefix the inner reaction's progress with which medium it is running in.

            What makes a parallel loop readable: interleaved lines stay attributable to the branch
            that wrote them.
            """
            progress(f"{label}: {line}")

        async with limit:
            return await reaction_energy(
                store,
                reactants,
                products,
                solvent,
                temperature_k,
                level,
                symmetry_numbers,
                progress=relay,
                run=run,
            )

    # `gather` preserves argument order, so the gas-phase reference stays first and the ranking
    # below sorts from a list whose order does not depend on which branch finished first.
    results = list(await asyncio.gather(*(one(solvent) for solvent in [None, *solvents])))
    effects = [
        SolventEffect(
            solvent=result.solvent,
            delta_e_kcal=result.delta_e_kcal,
            delta_h_kcal=result.delta_h_kcal,
            delta_g_kcal=result.delta_g_kcal,
        )
        for result in results
    ]

    def ranking(effect: SolventEffect) -> float:
        return effect.delta_g_kcal if effect.delta_g_kcal is not None else effect.delta_e_kcal

    effects.sort(key=ranking)
    spread = ranking(effects[-1]) - ranking(effects[0])
    uncertainty = settings.xtb_reaction_uncertainty_kcal
    warnings = list(dict.fromkeys(warning for result in results for warning in result.warnings))
    if spread <= uncertainty:
        warnings.append(
            f"the solvents span {spread:.1f} kcal/mol, within the method's "
            f"±{uncertainty:.1f}: this calculation does not distinguish them"
        )
    return SolventComparisonResult(
        reactants=reactants,
        products=products,
        method=results[0].method,
        temperature_k=results[0].temperature_k,
        level=level,
        effects=effects,
        best_solvent=effects[0].solvent,
        spread_kcal=round(spread, 2),
        uncertainty_kcal=uncertainty,
        warnings=warnings,
    )
