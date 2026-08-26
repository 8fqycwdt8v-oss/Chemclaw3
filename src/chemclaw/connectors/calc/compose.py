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
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Literal, Protocol, TypeVar

from rdkit import Chem

from chemclaw.connectors.calc.remote import cached_remote, remote_call
from chemclaw.core.chem import require_canonical_smiles
from chemclaw.core.config import settings
from chemclaw.core.config.calculators import PkaCalibration
from chemclaw.science.calc.artifacts import (
    HESSIAN_ARRAYS,
    ArrayOffloadingStore,
    ArtifactStore,
)
from chemclaw.science.calc.budget import estimate_units, require_within_budget
from chemclaw.science.calc.geometry import check_server_address, structures_in
from chemclaw.science.calc.models import (
    BondDissociationSurvey,
    Conformer,
    ConformerEnsemble,
    CrestEffort,
    DissociatedBond,
    ElectronicProperties,
    EnsemblePayload,
    EnsembleProperty,
    EnsembleSearch,
    HessianPayload,
    InteractionResult,
    MicrostatePka,
    OptimizationResult,
    RankedSpecies,
    ReactionEnergyResult,
    ReactionLevel,
    RefinedConformer,
    RefinedEnsemble,
    ScanPoint,
    ScanResult,
    SiteReactivityResult,
    SolventComparisonResult,
    SolventEffect,
    SpeciesDistribution,
    SpeciesEnergy,
    Structure,
    ThermochemistryResult,
    WeightedAtom,
    WeightedValue,
)
from chemclaw.science.calc.postgres_artifacts import default_artifact_store
from chemclaw.science.calc.postgres_structures import default_structure_store
from chemclaw.science.calc.store import ResultStore
from chemclaw.science.calc.structures import StructureStore
from chemclaw.science.calc.thermo import (
    HARTREE_TO_KCAL,
    ThermoSettings,
    boltzmann_populations,
    displaced_along,
    ensemble_entropy,
    ensemble_from_members,
    free_energy_populations,
    macrostate_free_energy_kcal,
    rt_kcal,
    thermochemistry_from_hessian,
    weighted_average,
)
from chemclaw.science.calc.uncertainty import CalculationDomainError

# What `ensemble_property` can average, and the field each name reads off its result model. A
# closed set rather than a free-form attribute name: a caller naming a field that does not exist
# would get an AttributeError from inside a fan-out that had already paid for its conformer search.
EnsembleProperties = Literal["dipole_debye", "homo_ev", "lumo_ev", "gap_ev", "charges", "fukui"]

# Which species-set question a distribution answers. The arithmetic is identical across them; the
# label is what stops a reader having to infer the question from the SMILES.
SpeciesKind = Literal["tautomers", "microstates", "stereoisomers", "custom"]

# Below this share of the E-weighted population, a refined ensemble says so rather than presenting
# a truncation as the whole. 0.9 is CENSO's own convention for how much of an ensemble a refinement
# step carries forward, so a chemist reading the warning recognises the threshold.
_REFINED_COVERAGE_WARNING = 0.9

# Which Fukui index a per-atom average reports. The three are one calculation and `ranked_for`
# re-sorts locally, so an ensemble average carries the radical index — the mean of the other two,
# and the only one that is not a claim about which attack was meant.
_DEFAULT_FUKUI_MODE = "radical"
_FUKUI_FIELD = {"electrophilic": "f_minus", "nucleophilic": "f_plus", "radical": "f_zero"}

_Result = TypeVar("_Result")
# The shape a server answer arrives in, before it is validated into a model. Its own variable
# rather than `_Result` so `kept`'s signature says "the same value comes back".
_Payload = TypeVar("_Payload")

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


async def kept(payload: _Payload, *, structures: StructureStore | None = None) -> _Payload:
    """Persist every geometry in a server payload, then hand the payload back unchanged.

    **The write half of the handle** (D-2026-08-21-a-geometry-is-an-address-not-a-payload). Every
    geometry this repository ever shows a chemist comes back through one of the calls below, and
    every one of them is reported by its `structure_id` rather than by its coordinates. That address
    has to resolve, and this is where it comes to.

    Applied to the *returned* payload rather than on the miss path, deliberately: a cache **hit**
    never reaches the server, so persisting only on a miss would leave every handle from a
    previously-computed calculation unresolvable — including, on the first deployment that has this
    store, every geometry already on disk.

    **A failed write raises**, deliberately, where most bookkeeping in this tree is swallowed. A
    geometry store that is not writing is a deployment whose next `structure_id` argument will be
    refused as unresolvable, and a calculation that fails loudly now is the better half of that
    than a handle that fails mysteriously later. It costs little: the calculation's own result is
    already in the cache by the time this runs, so the retry Temporal issues for a database fault
    pays no SCF.

    Args:
        payload: What the server (or the cache) answered with.
        structures: Where geometries go; the configured store by default.

    Returns:
        `payload`, unchanged — so a call site reads `Model.model_validate(await kept(payload))`.
    """
    check_server_address(payload)
    found = list(structures_in(payload))
    if found:
        store = structures if structures is not None else default_structure_store()
        await store.put(found)
    return payload


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
    return Structure.model_validate(await kept(payload))


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
    return OptimizationResult.model_validate(await kept(payload)), cached


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
    subject: Structure | None = None,
    progress: Progress = no_progress,
    run: RemoteRunner = plain,
) -> ScanResult:
    """Relax the molecule at every value of one internal coordinate and assemble the profile.

    `subject` is the geometry to scan *from*, when the caller named one
    (D-2026-08-21-a-geometry-is-an-address-not-a-payload). Without it the profile is driven from a
    fresh force-field embedding, which is the right default and the wrong answer after a conformer
    search: a rotational barrier depends on which conformer it is measured in, and re-embedding
    throws away the choice the search was run to make. `smiles` is then only the label the result
    is reported under.

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
    structure = subject if subject is not None else await embed(smiles, run=run)
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
        relaxed.append(OptimizationResult.model_validate(await kept(payload)))

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
    subject: Structure | None = None,
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
    payload, cached = await searched_members(
        store, smiles, subject=subject, search=search, effort=effort, solvent=solvent, run=run
    )
    return (
        ensemble_from_members(
            payload,
            smiles=require_canonical_smiles(smiles),
            search=search,
            temperature_k=temperature_k or settings.xtb_thermo_temperature_k,
            max_members=settings.crest_max_members,
        ),
        cached,
    )


async def searched_members(
    store: ResultStore,
    smiles: str,
    *,
    subject: Structure | None = None,
    search: EnsembleSearch = "conformers",
    effort: CrestEffort | None = None,
    solvent: str | None = None,
    run: RemoteRunner = plain,
) -> tuple[EnsemblePayload, bool]:
    """One CREST search, cached, with its members' **absolute** energies still on them.

    Split out of `conformer_ensemble` once a second caller needed what that function drops.
    `ConformerEnsemble` reports energies *relative to its own lowest member* and truncates to
    `crest_max_members`, which is right for reading an ensemble and useless for comparing two of
    them: an acid and its conjugate base have different lowest members, so a difference of
    relative energies is a difference of nothing. `microstate_pka` needs the absolute Hartrees and
    the whole member list, and taking them by repeating the remote call would have been a second
    place for the arguments — and therefore the cache key — to be written.
    """
    starting = subject if subject is not None else await embed(smiles, run=run)
    payload, cached = await run(
        cached_remote(
            store,
            "search_conformer_ensemble",
            {
                "structure": starting.model_dump(mode="json"),
                "search": search,
                "effort": effort or settings.crest_effort,
                "solvent": solvent,
            },
        ),
        f"{search} of {smiles}",
    )
    return EnsemblePayload.model_validate(await kept(payload)), cached


# --- acid/base equilibria -----------------------------------------------------------------------

# Heteroatoms whose bound protons mean "the pKa" is the acid one. It is a *domain* guard rather than
# a site enumeration, and that distinction is the whole point of doing this with CREST: which proton
# actually comes off is decided by energy over every site the sampler finds, including the C-H ones
# no rule here would have offered. What this decides is only which of the two questions to ask.
#
# **Nitrogen is deliberately not in this tuple even though N-H protons exist.** An amine has N-H and
# nobody means its N-H acidity (pKa ~36) by "the pKa of ethylamine" — they mean the conjugate acid,
# 10.7. Amides and anilines are the same: their N-H is too weak an acid in water to be the number
# anyone quotes. So an N-H molecule with no O-H or S-H takes the base branch, and a molecule that is
# genuinely both — an aminophenol — is the case the `branch` argument exists for.
_ACIDIC_HETEROATOMS = (8, 16)  # O, S


def _acid_or_base(smiles: str) -> Literal["acid", "base"]:
    """Which equilibrium to compute when the caller did not say.

    Acid whenever a proton sits on O or S — the pKa a chemist means by "the pKa" of a carboxylic
    acid, a phenol or a thiol. Base for anything else carrying nitrogen, which covers both pyridine
    (no N-H at all) and ethylamine (N-H, but 10.7 is its *conjugate acid's* number, not its N-H
    acidity at ~36).

    **Oxygen and sulfur are deliberately not a base branch.** CREST will happily protonate an ether
    or a ketone and rank the protomers, and the result would be a confident pKaH for a species that
    is not protonated at any pH a chemist works at. A caller who genuinely wants that number asks
    for it explicitly.
    """
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(f"invalid SMILES: {smiles!r}")
    if any(
        atom.GetAtomicNum() in _ACIDIC_HETEROATOMS and atom.GetTotalNumHs() > 0
        for atom in mol.GetAtoms()
    ):
        return "acid"
    if any(atom.GetAtomicNum() == 7 for atom in mol.GetAtoms()):
        return "base"
    raise CalculationDomainError(
        f"{smiles!r} has no proton on O or S and no nitrogen to protonate, so it has no "
        "acid/base equilibrium in water. Name the branch explicitly if you meant the C-H acidity "
        "or the protonation of an ether or carbonyl — both are outside this calibration"
    )


def _macrostate_hartree(payload: EnsemblePayload, temperature_k: float) -> float:
    """One ensemble's free energy in Hartree: its lowest member plus the sum over the rest.

    The whole member list, not the truncated one a `ConformerEnsemble` reports — a macrostate is the
    sum over everything that carries population, and dropping the tail biases the side with more
    accessible states, which is systematically the anion.
    """
    lowest = min(member.energy_hartree for member in payload.members)
    relative = [(member.energy_hartree - lowest) * HARTREE_TO_KCAL for member in payload.members]
    degeneracies = [member.degeneracy for member in payload.members]
    correction = macrostate_free_energy_kcal(relative, degeneracies, temperature_k)
    return lowest + correction / HARTREE_TO_KCAL


def _aryl_protonation(site_smiles: str | None) -> bool | None:
    """Whether a protonated nitrogen is aromatic or aryl-attached; None where it cannot be read.

    The one class boundary this composite still has to police, and it is physics rather than
    enumeration — which is why CREST does not remove it. Over 13 reference aliphatic amines the
    computed basicity correlates with the measured pKa at Spearman **-0.17**: no ranking ability at
    all. The cause is the continuum solvent, since aqueous aliphatic amine basicity is set by how
    many hydrogen bonds the ammonium ion donates to water, and that is not a thing ALPB can see.
    Aromatic and aryl nitrogen is dominated instead by delocalisation into the ring, which GFN2 does
    capture.
    """
    if site_smiles is None:
        return None
    mol = Chem.MolFromSmiles(site_smiles)
    if mol is None:
        return None
    protonated = [
        atom for atom in mol.GetAtoms() if atom.GetAtomicNum() == 7 and atom.GetFormalCharge() == 1
    ]
    if not protonated:
        return None
    return any(
        atom.GetIsAromatic() or any(neighbour.GetIsAromatic() for neighbour in atom.GetNeighbors())
        for atom in protonated
    )


def _off_domain_anion(site_smiles: str) -> str | None:
    """The element a deprotonation landed on when it is one the calibration was not fitted on.

    The acid reference set is **O-H and S-H only**, so a winning deprotomer at carbon or nitrogen is
    an extrapolation and says so. Both are real answers to "which proton is most acidic" and wrong
    answers to "what is its pKa in water" if quoted unqualified: CREST ranks every site including
    C-H, and an N-H acid (an imide, a sulfonamide) is a class the linear map has never seen.

    Read off the atom carrying the charge rather than matched against the string, because a
    substring test cannot tell `[CH2-]` from `[Cl-]` or from a bracketed carbon that is not the
    anion. Returns `None` for the fitted case and for a site that cannot be read.
    """
    mol = Chem.MolFromSmiles(site_smiles)
    if mol is None:
        return None
    charged = [atom for atom in mol.GetAtoms() if atom.GetFormalCharge() < 0]
    off = [atom for atom in charged if atom.GetAtomicNum() in (6, 7)]
    if not off or any(atom.GetAtomicNum() in (8, 16) for atom in charged):
        return None
    return "carbon" if off[0].GetAtomicNum() == 6 else "nitrogen"


async def microstate_pka(
    store: ResultStore,
    smiles: str,
    *,
    subject: Structure | None = None,
    branch: Literal["auto", "acid", "base"] = "auto",
    solvent: str | None = None,
    temperature_k: float | None = None,
    effort: CrestEffort | None = None,
    progress: Progress = no_progress,
    run: RemoteRunner = plain,
) -> MicrostatePka:
    """Predict a pKa from two sampled macrostates: the neutral's conformers and its microstates.

    Two CREST searches and one subtraction. The neutral molecule's conformer ensemble gives the
    protonated macrostate; `--deprotonate` (or `--protonate`) enumerates every site, optimises each
    product and ranks them, giving the other. Each side is reduced to `-RT ln sum g exp(-E/RT)` over
    everything it found, and the difference is mapped to a pKa by a calibration fitted through this
    same pipeline.

    **Why this is a composite here rather than a tool on the server.** Its key would have to name
    the microstate the deprotonation search settles on, which is an *output* — the structural
    giveaway that a calculation is a loop with state and belongs to whoever orchestrates it. Both
    searches underneath are keyed primitives, so the expensive halves are cached separately: asking
    about the same molecule at another temperature, or asking for its conformer ensemble on its own,
    is arithmetic over rows that already exist.

    **What it is not.** A macroscopic aqueous pKa of one ionisable centre, at the semiempirical
    level, in a continuum solvent. Not a microscopic pKa per site (the ranking of sites is reported,
    the individual constants are not), not a polyprotic titration curve, and not a number to put in
    a specification — quote the uncertainty, which is the fit's own standard error.
    """
    canonical = require_canonical_smiles(smiles)
    chosen: Literal["acid", "base"] = _acid_or_base(canonical) if branch == "auto" else branch
    medium = solvent if solvent is not None else settings.pka_ensemble_solvent
    temperature = temperature_k or settings.xtb_thermo_temperature_k
    calibration = settings.pka_ensemble_acid if chosen == "acid" else settings.pka_ensemble_base
    # Two CREST searches, counted before either starts: the second is not conditional on the first,
    # and a ceiling reached after the expensive half has been paid is not a ceiling.
    require_within_budget(
        estimate_units(2, level="thorough"), f"the pKa of {canonical} from two CREST searches"
    )

    starting = subject if subject is not None else await embed(canonical, run=run)
    progress(f"conformer search of {canonical}")
    neutral_payload, _ = await searched_members(
        store,
        canonical,
        subject=starting,
        search="conformers",
        effort=effort,
        solvent=medium,
        run=run,
    )
    search: EnsembleSearch = "deprotomers" if chosen == "acid" else "protomers"
    progress(f"{search} search of {canonical}")
    ionised_payload, _ = await searched_members(
        store, canonical, subject=starting, search=search, effort=effort, solvent=medium, run=run
    )

    # Deprotonated minus protonated, always — so one calibration sign convention covers a base's
    # conjugate acid (B + H+ <- BH+) and an acid's own dissociation without a second formula.
    neutral_g = _macrostate_hartree(neutral_payload, temperature)
    ionised_g = _macrostate_hartree(ionised_payload, temperature)
    delta_g = (
        (ionised_g - neutral_g) if chosen == "acid" else (neutral_g - ionised_g)
    ) * HARTREE_TO_KCAL
    pka = calibration.slope * delta_g + calibration.intercept

    ordered = sorted(ionised_payload.members, key=lambda member: member.energy_hartree)
    site = ordered[0].structure.smiles
    within_rt = sum(
        1
        for member in ordered
        if (member.energy_hartree - ordered[0].energy_hartree) * HARTREE_TO_KCAL
        <= rt_kcal(temperature)
    )
    warnings = _pka_warnings(
        chosen, site, pka, within_rt, medium, neutral_payload.effort, calibration
    )
    return MicrostatePka(
        smiles=canonical,
        branch=chosen,
        pka=round(pka, 2),
        uncertainty=calibration.uncertainty,
        delta_g_kcal=round(delta_g, 3),
        site_smiles=site,
        method=neutral_payload.method,
        solvent=medium,
        temperature_k=temperature,
        neutral=ensemble_from_members(
            neutral_payload,
            smiles=canonical,
            search="conformers",
            temperature_k=temperature,
            max_members=settings.crest_max_members,
        ),
        ionised=ensemble_from_members(
            ionised_payload,
            smiles=canonical,
            search=search,
            temperature_k=temperature,
            max_members=settings.crest_max_members,
        ),
        microstates_found=ionised_payload.total_found,
        microstates_within_rt=within_rt,
        warnings=warnings,
    )


def _pka_warnings(
    branch: Literal["acid", "base"],
    site: str | None,
    pka: float,
    within_rt: int,
    solvent: str | None,
    effort: str,
    calibration: PkaCalibration,
) -> list[str]:
    """Everything a reader has to know before using the number, gathered in one place.

    Each of these is a case where the arithmetic succeeds and the answer means less than it looks
    like it does — which is exactly the class that has to be *carried on the result* rather than
    left in a docstring nobody reads at the point of use.
    """
    warnings: list[str] = []
    if branch == "base":
        aryl = _aryl_protonation(site)
        if aryl is False:
            warnings.append(
                "the most stable protomer is an aliphatic nitrogen, which this calibration does "
                "not cover: over 13 reference amines the computed basicity correlates with the "
                "measured pKa at Spearman -0.17, so this number carries no ranking information. "
                "The cause is the implicit solvent — aqueous aliphatic amine basicity is set by "
                "the ammonium ion's hydrogen bonding to water, which a continuum cannot represent"
            )
        elif aryl is None:
            warnings.append(
                "the protonation site could not be read from the winning geometry, so whether it "
                "falls in this calibration's aromatic/aryl-nitrogen domain is unknown"
            )
    if site is None:
        warnings.append(
            "the ionised microstate's constitution could not be perceived from its geometry, so "
            "which proton this pKa is about is not reported"
        )
    elif branch == "acid" and (element := _off_domain_anion(site)) is not None:
        warnings.append(
            f"the proton came off {element} ({site}), and this calibration was fitted on O-H and "
            "S-H acids only. The ranking of sites stands — it is what the search measured — but "
            "the mapping of this free energy to a pKa is an extrapolation to a class the fit has "
            "never seen"
        )
    if not calibration.fitted_from < pka < calibration.fitted_to:
        warnings.append(
            f"pKa {pka:.1f} is outside the range this calibration was fitted over "
            f"({calibration.fitted_from:g} to {calibration.fitted_to:g}), so the residual off the "
            "end of the reference set is unknown rather than merely larger"
        )
    if within_rt > 1:
        warnings.append(
            f"{within_rt} ionised microstates lie within RT of the best, so this molecule has no "
            "single conjugate base — the number is the macrostate's, and a site-resolved "
            "(microscopic) pKa would be a different question"
        )
    if effort != calibration.fitted_effort:
        warnings.append(
            f"this ran at effort={effort!r} and the calibration was fitted at "
            f"{calibration.fitted_effort!r}: a deeper search finds lower members on both sides, so "
            "the ensembles are the better ones and the mapping to a pKa is still the quick one's"
        )
    if solvent != settings.pka_ensemble_solvent:
        warnings.append(
            f"both calibrations were fitted in {settings.pka_ensemble_solvent}; this ran in "
            f"{solvent or 'gas phase'}, so the free energy is for that medium and the mapping to a "
            "pKa is not"
        )
    return warnings


# --- non-covalent complexes -----------------------------------------------------------------


def _ordered(
    first: tuple[str, Structure], second: tuple[str, Structure]
) -> tuple[tuple[str, Structure], tuple[str, Structure]]:
    """The pair in a canonical order, so A-with-B and B-with-A are one calculation.

    The interaction of two molecules is one physical quantity, but the starting arrangement is not
    symmetric in its arguments: `combine_structures` holds the first monomer at the origin and
    offsets the second along +x, so swapping them negates the intermolecular vector while leaving
    each monomer's own orientation alone. That is a *different* starting geometry, and it would key
    to a different cache entry — paying twice, at minutes per search, for the same answer.

    **Each molecule travels with its geometry**, which is why this takes pairs rather than two
    SMILES. Once a caller may name the starting geometries
    (D-2026-08-21-a-geometry-is-an-address-not-a-payload), sorting the names while leaving the
    structures in argument order would pair each monomer with the *other* one's conformer — a
    calculation about neither molecule, reported as being about both.
    """
    return (first, second) if first[0] <= second[0] else (second, first)


async def interaction(
    store: ResultStore,
    smiles_a: str,
    smiles_b: str,
    *,
    subjects: tuple[Structure, Structure] | None = None,
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
    given = (
        (await embed(smiles_a, run=run), await embed(smiles_b, run=run))
        if subjects is None
        else subjects
    )
    (smiles_a, structure_a), (smiles_b, structure_b) = _ordered(
        (require_canonical_smiles(smiles_a), given[0]),
        (require_canonical_smiles(smiles_b), given[1]),
    )
    monomers = []
    for structure in (structure_a, structure_b):
        relaxed, _ = await relax(store, structure, solvent, run=run)
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
    # Deliberately not `kept`: this is the *starting* arrangement the search is about to discard,
    # and no result reports its address. The geometry a caller can name is the relaxed binding mode
    # below, which `relax` keeps.
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
    modes = EnsemblePayload.model_validate(await kept(payload))
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
    found = 0
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
        found = ensemble.total_found
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
            structure_id=optimization.structure.structure_id,
            conformers_found=found,
            was_cached=cached,
            method=optimization.method,
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
        structure_id=minimum.structure.structure_id,
        conformers_found=found,
        is_minimum=result.is_minimum,
        was_cached=cached,
        method=minimum.method,
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
        # **The server's method, not this deployment's configured name.** `settings.xtb_method`
        # describes a calculation this process no longer runs: the physics is `Chemclaw3-mcp`'s
        # since `D-2026-08-16-the-physics-leaves-the-cache-stays`, and a deployment whose env says
        # `GFN2-xTB` while the server runs GFN1 published a `ReactionEnergyResult` — a Temporal wire
        # type, PR-gated into the knowledge graph — asserting the wrong level of theory. The
        # neighbouring composites (`scan_profile`, `solvent_comparison`) already read it off the
        # result; this one did not. The fallback is for histories written before `SpeciesEnergy`
        # carried the field, never for a live run.
        method=species[0].method or settings.xtb_method,
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


# --- ensembles refined, averaged and ranked ---------------------------------------------------
#
# **The fan-out lives here and not in a template, and that placement is the whole design.** A
# template is an ordered step list with deliberately no loops, no conditionals and no expressions,
# and the agent's own loop is capped at `harness_max_loop_iterations`. A tautomer ratio over eight
# species, or a bond survey over twenty bonds, fits neither — so the loop is a composite and the
# sequence around it is a template. Everything below is one of those loops, and every one of them
# counts its cost before it starts (`science/calc/budget.py`).


async def refined_ensemble(
    store: ResultStore,
    smiles: str,
    *,
    subject: Structure | None = None,
    solvent: str | None = None,
    temperature_k: float | None = None,
    top_n: int | None = None,
    progress: Progress = no_progress,
    run: RemoteRunner = plain,
) -> RefinedEnsemble:
    """Re-weight a conformer ensemble by free energy instead of by electronic energy.

    D-101 recorded that this system does not do this, and gave the reason: "one Hessian per member,
    half an hour each at 76 atoms". That reason has not changed, so this does not replace
    `conformer_ensemble` — it is the shape for when a caller decides to pay, bounded to the top
    `ensemble_refine_top_n` members by electronic energy.

    **What the refinement buys.** Weighting by E assumes every conformer has the same zero-point,
    thermal and entropic contribution, which is exactly wrong for the case a conformer search is run
    on: a compact hydrogen-bonded fold has a low electronic energy *and* a stiff, ordered set of low
    modes, so E-weighting over-populates it. G-weighting is the distribution the populations are
    supposed to be.

    **What it costs in honesty.** Refining five of forty-seven and calling the result "the ensemble"
    is the error `ensemble_from_members` already refuses for `max_members`, and it is worse here
    because a free energy looks more careful than an electronic one. So the result carries
    `refined_population_covered` — the E-weighted population fraction the refined members account
    for — and warns below a threshold rather than leaving a reader to notice.
    """
    # **Counted before the search, not after it.** This used to await `conformer_ensemble` first
    # and then check the budget, which is the one thing `budget.py` exists to prevent: a CREST
    # search is minutes to hours (measured, 1142 s at 33 atoms), so the fence was reached with the
    # single most expensive call in the bundle already paid. The ceiling has to be read against the
    # work the caller *asked for*, which is knowable here — the search plus a relax and a Hessian
    # per conformer kept — rather than against the count the search happens to return.
    keep = top_n or settings.ensemble_refine_top_n
    require_within_budget(
        estimate_units(1, level="thorough") + estimate_units(keep, level="standard"),
        f"refining the top {keep} conformers of {smiles}",
    )

    ensemble, _ = await conformer_ensemble(
        store, smiles, subject=subject, solvent=solvent, temperature_k=temperature_k, run=run
    )
    chosen = ensemble.conformers[:keep]
    temperature = temperature_k or settings.xtb_thermo_temperature_k

    settled: list[tuple[Conformer, OptimizationResult, ThermochemistryResult]] = []
    for index, conformer in enumerate(chosen, start=1):
        progress(f"refining conformer {index}/{len(chosen)} of {smiles}")
        minimum, result, _ = await relax_to_minimum(
            store,
            conformer.structure,
            solvent,
            ThermoSettings(temperature_k=temperature),
            run=run,
        )
        settled.append((conformer, minimum, result))

    degeneracies = [conformer.degeneracy for conformer, _, _ in settled]
    populations = free_energy_populations(
        [result.gibbs_free_energy_hartree for _, _, result in settled], degeneracies, temperature
    )
    lowest_gibbs = min(result.gibbs_free_energy_hartree for _, _, result in settled)
    members = sorted(
        (
            RefinedConformer(
                structure=minimum.structure,
                relative_kcal=round(
                    (result.gibbs_free_energy_hartree - lowest_gibbs) * HARTREE_TO_KCAL, 3
                ),
                population=round(population, 4),
                degeneracy=conformer.degeneracy,
                gibbs_free_energy_hartree=result.gibbs_free_energy_hartree,
                electronic_energy_hartree=minimum.energy_hartree,
                is_minimum=result.is_minimum,
            )
            for (conformer, minimum, result), population in zip(settled, populations, strict=True)
        ),
        key=lambda member: member.relative_kcal,
    )
    entropy = ensemble_entropy(populations, degeneracies)
    covered = sum(conformer.population for conformer in chosen)
    warnings: list[str] = []
    if covered < _REFINED_COVERAGE_WARNING:
        warnings.append(
            f"the {len(chosen)} refined conformers carry {covered:.0%} of the ensemble population; "
            "these free energies describe that fraction rather than the whole ensemble"
        )
    if any(not member.is_minimum for member in members):
        warnings.append(
            "at least one refined conformer did not settle on a genuine minimum, so its free "
            "energy is computed at a saddle point and its population is not meaningful"
        )
    return RefinedEnsemble(
        smiles=require_canonical_smiles(smiles),
        method=ensemble.method,
        solvent=solvent,
        temperature_k=temperature,
        conformers=members,
        total_found=ensemble.total_found,
        refined_count=len(members),
        refined_population_covered=round(covered, 4),
        refined_conformational_entropy_cal_per_mol_k=round(entropy, 3),
        refined_ensemble_correction_kcal=round(-temperature * entropy / 1000.0, 3),
        warnings=warnings,
    )


async def ensemble_property(
    store: ResultStore,
    smiles: str,
    *,
    prop: EnsembleProperties = "dipole_debye",
    solvent: str | None = None,
    temperature_k: float | None = None,
    max_members: int | None = None,
    progress: Progress = no_progress,
    run: RemoteRunner = plain,
) -> EnsembleProperty:
    """Compute one property at every populated conformer and weight it by their populations.

    The caveat under every other number in this system is that it describes **one** conformer —
    whichever geometry was embedded. This is the composite that lifts it, and the lift is not
    cosmetic: a dipole, a HOMO-LUMO gap and a Fukui ranking can all move by more between two
    populated conformers of one molecule than between two different molecules.

    Which is why the answer carries a *spread* and not only a mean. When the values scatter more
    than the difference the number is being used to argue, the honest report is that the molecule
    does not have one value of this property at this temperature.

    Per-atom properties (`fukui`, `charges`) are averaged atom by atom over the same populations,
    which is the same arithmetic — `weighted_average` is called once per atom rather than once.
    **Atoms are paired by index, never by position in the returned list**: a Fukui result is ranked
    by susceptibility and truncated, so its order is a property of the conformer rather than of the
    molecule. `_per_atom` carries the argument; this sentence used to claim the opposite.
    """
    # `crest_max_members`, not `ensemble_refine_top_n`. That setting is documented as how many
    # members get their own optimization *and Hessian*; a property average costs one single point
    # per member and no Hessian, so borrowing it silently covered five of up to twenty members for
    # the cheap composite. And counted before the search, for the reason `refined_ensemble` gives.
    keep = max_members or settings.crest_max_members
    require_within_budget(
        estimate_units(1, level="thorough") + keep,
        f"a {prop} average over {keep} conformers of {smiles}",
    )

    ensemble, _ = await conformer_ensemble(
        store, smiles, solvent=solvent, temperature_k=temperature_k, run=run
    )
    chosen = ensemble.conformers[:keep]

    tool = "compute_fukui_at" if prop == "fukui" else "compute_properties_at"
    payloads: list[Any] = []
    for index, conformer in enumerate(chosen, start=1):
        progress(f"{prop} at conformer {index}/{len(chosen)} of {smiles}")
        payload, _ = await run(
            cached_remote(
                store,
                tool,
                {"structure": conformer.structure.model_dump(mode="json"), "solvent": solvent},
            ),
            f"{prop} of {smiles} conformer {index}",
        )
        payloads.append(await kept(payload))

    populations = [conformer.population for conformer in chosen]
    total = sum(populations) or 1.0
    populations = [population / total for population in populations]
    scalar, per_atom, dropped = _averaged(prop, payloads, populations)
    covered = sum(conformer.population for conformer in chosen)
    property_warnings: list[str] = []
    if covered < _REFINED_COVERAGE_WARNING:
        property_warnings.append(
            f"the {len(chosen)} conformers averaged carry {covered:.0%} of the ensemble "
            f"population of {ensemble.total_found} found; this average describes that fraction "
            "rather than the whole ensemble"
        )
    if dropped:
        property_warnings.append(
            f"{dropped} atom(s) were not present in every conformer's result and were left out of "
            "the per-atom average; a Fukui result is truncated to the most susceptible sites, so a "
            "marginal atom can fall inside one conformer's list and outside another's"
        )
    return EnsembleProperty(
        smiles=require_canonical_smiles(smiles),
        property_name=prop,
        method=ensemble.method,
        solvent=solvent,
        temperature_k=ensemble.temperature_k,
        members_averaged=len(chosen),
        total_found=ensemble.total_found,
        value=scalar,
        per_atom=per_atom,
        population_covered=round(covered, 4),
        warnings=property_warnings,
    )


def _per_atom(
    members: list[dict[int, tuple[str, float]]], populations: list[float]
) -> tuple[list[WeightedAtom], int]:
    """Average a per-atom property across conformers, pairing atoms by **index**.

    **Not by list position, and the difference is a wrong answer rather than a rough one.** The
    first version of this did `member[position]` over `enumerate(members[0])`, which is only correct
    if every conformer returns its atoms in one order. `SiteReactivityResult` documents the
    opposite in its own docstring: `sites` is *"ordered most-susceptible first by the index named in
    `ranked_by`, and truncated to the most susceptible `len(sites)` of `total_atoms`"*. Ranked, and
    cut. Two conformers of a floppy molecule rank their atoms differently — that is the entire
    reason to average over an ensemble at all — so position *k* was a different atom in each, and
    the mean was labelled with the first conformer's index.

    The bug therefore fired hardest in exactly the case the composite exists for: had the ranking
    not moved with geometry, `compute_fukui_at` and the `DEFERRED.md` row it closed would have had
    no purpose. Truncation made it worse than a mispairing — conformers can carry different atom
    *sets*, and a short list raised `IndexError`.

    Atoms missing from any member are dropped rather than averaged over a subset, because a mean
    over three of five conformers is not a population-weighted average and nothing downstream could
    tell. The caller reports the count so a truncated ranking is visible instead of implied.
    """
    if not members:
        return [], 0
    seen = [set(member) for member in members]
    common = set.intersection(*seen)
    averaged = [
        WeightedAtom(
            index=index,
            element=members[0][index][0],
            value=weighted_average([member[index][1] for member in members], populations),
        )
        for index in sorted(common)
    ]
    return averaged, len(set.union(*seen) - common)


def _averaged(
    prop: EnsembleProperties, payloads: list[Any], populations: list[float]
) -> tuple[WeightedValue | None, list[WeightedAtom], int]:
    """Split one property out of each payload and weight it — scalar or per atom.

    A `match` over the property name rather than a registry, because there are four of them and a
    registry with four entries is indirection that hides what is being read off which model.
    """
    if prop == "fukui":
        sites = [SiteReactivityResult.model_validate(payload).sites for payload in payloads]
        field = _FUKUI_FIELD[_DEFAULT_FUKUI_MODE]
        per_atom, dropped = _per_atom(
            [
                {site.index: (site.element, getattr(site, field)) for site in member}
                for member in sites
            ],
            populations,
        )
        return None, per_atom, dropped
    properties = [ElectronicProperties.model_validate(payload) for payload in payloads]
    if prop == "charges":
        per_atom, dropped = _per_atom(
            [
                {charge.index: (charge.element, charge.charge) for charge in member.atom_charges}
                for member in properties
            ],
            populations,
        )
        return None, per_atom, dropped
    values = [getattr(member, prop) for member in properties]
    if any(value is None for value in values):
        raise ValueError(
            f"{prop} is not defined for every conformer of this molecule "
            "(a species with no unoccupied orbital has no LUMO and no gap)"
        )
    return weighted_average(values, populations), [], 0


async def species_ranking(
    store: ResultStore,
    species: Sequence[tuple[str, str]],
    *,
    kind: SpeciesKind = "custom",
    solvent: str | None = None,
    temperature_k: float | None = None,
    level: ReactionLevel = "standard",
    symmetry_numbers: Mapping[str, int] | None = None,
    progress: Progress = no_progress,
    run: RemoteRunner = plain,
) -> SpeciesDistribution:
    """Rank a set of *distinct species* by free energy and report their equilibrium populations.

    One composite for three questions, because they are the same arithmetic over different species
    sets: which tautomer dominates, which protonation microstate is present, which stereoisomer is
    favoured. `kind` records which was asked — the number means something different in each, and a
    reader must not have to infer it from the SMILES.

    Each species goes through `_species_energy`, which is the engine every reaction energy already
    uses: embed, optionally search its conformers, relax, and at `standard` or above take its
    Hessian for a free energy. So a ranking and a reaction energy cannot disagree about what one
    species' free energy is, and a species computed for one is a cache hit for the other.

    **The set is the answer's universe, and it is not checked here.** A species that was not
    enumerated was not ranked, so a distribution over an incomplete set is confident about the wrong
    universe. `enumerated` carries the count the caller started from for exactly that reason.
    """
    if not species:
        raise ValueError("a distribution needs at least one species")
    ceiling = settings.species_ranking_max
    considered = list(species[:ceiling])
    temperature = temperature_k or settings.xtb_thermo_temperature_k
    require_within_budget(
        estimate_units(len(considered), level=level),
        f"ranking {len(considered)} species",
    )

    thermo = (
        None if level == "quick" else ThermoSettings(temperature_k=temperature, symmetry_number=1)
    )
    stated = dict(symmetry_numbers or {})
    energies: list[SpeciesEnergy] = []
    for index, (smiles, _) in enumerate(considered, start=1):
        progress(f"species {index}/{len(considered)}: {smiles}")
        energies.append(
            # **`stated.get(smiles)`, not a literal 1.** Passing 1 marked the number *stated*, so
            # the machinery `reaction_energy` uses to withhold or flag an assumed sigma never ran
            # here — and this composite ranks by the free energy that sigma shifts. `None` computes
            # at sigma=1 exactly as before but records that nobody said so, which is what makes the
            # warning below possible.
            await _species_energy(
                store, smiles, "reactant", solvent, thermo, stated.get(smiles), level, run
            )
        )

    gibbs = [energy.gibbs_free_energy_hartree for energy in energies]
    use_gibbs = all(value is not None for value in gibbs)
    scale = (
        [value for value in gibbs if value is not None]
        if use_gibbs
        else [energy.electronic_energy_hartree for energy in energies]
    )
    lowest = min(scale)
    relative = [(value - lowest) * HARTREE_TO_KCAL for value in scale]
    populations = boltzmann_populations(relative, [1] * len(scale), temperature)

    warnings: list[str] = []
    if not use_gibbs:
        warnings.append(
            "ranked by electronic energy: at level='quick' no species has a free energy, so the "
            "populations ignore the zero-point and entropy differences between these forms"
        )
    unstated = sorted({energy.smiles for energy in energies if energy.symmetry_number is None})
    if unstated and use_gibbs:
        # Warned rather than withheld, and the difference from `reaction_energy` is deliberate.
        # There, an unstated sigma means no ΔG is reported at all, because ΔE and ΔH still answer
        # the question. Here the free energy *is* the question — an E-only ranking is what `quick`
        # already is — so downgrading silently would be its own wrong answer. sigma shifts G by
        # exactly R·T·ln(sigma), ~0.41 kcal/mol per factor of two at 298 K, which is comparable to
        # the tautomer gaps this job exists to resolve, so it is worth a sentence every time.
        warnings.append(
            "no rotational symmetry number was given for "
            + ", ".join(unstated)
            + ": their rotational entropy was computed at sigma=1. Any species with a rotational "
            "axis is over-weighted here by R ln(sigma) — 0.41 kcal/mol per factor of two at "
            "298 K — so a ranking whose forms differ in symmetry can be wrong by more than its "
            "gap. Pass symmetry_numbers (1 = none, 2 = a C2 axis, 3 = ammonia, 6 = ethane, "
            "12 = benzene) to correct it"
        )
    if len(species) > ceiling:
        # `len(species) - ceiling`, not the reverse: this branch only runs when the set is *over*
        # the ceiling, so the old expression was always negative and told a chemist that "-3
        # species were not computed". And the cut is `species[:ceiling]` — first N in the order the
        # caller passed them — so "lowest-priority" described a prioritisation that never happened.
        warnings.append(
            f"{len(species)} species were enumerated and only the first {len(considered)} were "
            f"computed, in the order they were given; the populations describe those and not the "
            f"{len(species) - ceiling} that were dropped"
        )
    ranked = sorted(
        (
            RankedSpecies(
                smiles=energy.smiles,
                label=label,
                relative_kcal=round(value, 3),
                population=round(population, 4),
                gibbs_free_energy_hartree=energy.gibbs_free_energy_hartree,
                electronic_energy_hartree=energy.electronic_energy_hartree,
                # Off the energy rather than hardcoded: this was a literal 0 beside
                # `sampled=True`, which reads as "a search ran and found nothing", and the geometry
                # of the dominant form was unreachable downstream — the one thing every other
                # ensemble model in this bundle hoists an id for.
                structure_id=energy.structure_id,
                conformers_found=energy.conformers_found,
            )
            for (_, label), energy, value, population in zip(
                considered, energies, relative, populations, strict=True
            )
        ),
        key=lambda candidate: candidate.relative_kcal,
    )
    return SpeciesDistribution(
        kind=kind,
        method=energies[0].method,
        solvent=solvent,
        temperature_k=temperature,
        level=level,
        species=ranked,
        enumerated=len(species),
        uncertainty_kcal=settings.xtb_reaction_uncertainty_kcal,
        sampled=level == "thorough",
        warnings=warnings,
    )


async def bond_dissociation_survey(
    store: ResultStore,
    smiles: str,
    cleavages: Sequence[tuple[tuple[int, int], str, list[str]]],
    *,
    solvent: str | None = None,
    temperature_k: float | None = None,
    level: ReactionLevel = "quick",
    progress: Progress = no_progress,
    run: RemoteRunner = plain,
) -> BondDissociationSurvey:
    """Compute the dissociation energy of every enumerated bond and rank them.

    Each bond is one `reaction_energy` — parent on the left, its two fragments on the right — so
    the arithmetic, the balance check and the open-shell handling are the ones already in use rather
    than a second implementation. `radical_multiplicity` reads the explicit radical electrons the
    enumeration wrote (`[CH3]`, `[H]`), which is what makes a homolysis computable from two SMILES
    with no declared spin state.

    Defaults to `level="quick"`: a survey is a *ranking*, the ordering is what it supports, and a
    Hessian per fragment per bond multiplies a twenty-bond survey by three for a magnitude
    semiempirical theory does not deliver anyway. Ask for `standard` when the question is one bond.
    """
    if not cleavages:
        raise ValueError(f"no breakable bond was enumerated for {smiles}")
    require_within_budget(
        estimate_units(len(cleavages) * 3, level=level),
        f"a {len(cleavages)}-bond dissociation survey of {smiles}",
    )

    results: list[DissociatedBond] = []
    methods: list[str] = []
    for index, (atoms, bond, fragments) in enumerate(cleavages, start=1):
        progress(f"bond {index}/{len(cleavages)} ({bond}) of {smiles}")
        # Keyword arguments deliberately: `BondCleavageSpec`'s own docstring argues that a
        # positional payload is one field-order change away from computing a different bond than
        # the caller named, and seven positionals here — with the symmetry map in slot seven — is
        # the same hazard one call up.
        reaction = await reaction_energy(
            store,
            [smiles],
            list(fragments),
            solvent=solvent,
            temperature_k=temperature_k,
            level=level,
            symmetry_numbers=dict.fromkeys([smiles, *fragments], 1),
            progress=no_progress,
            run=run,
        )
        methods.append(reaction.method)
        energy = (
            reaction.delta_h_kcal if reaction.delta_h_kcal is not None else reaction.delta_e_kcal
        )
        results.append(
            DissociatedBond(
                atoms=list(atoms),
                bond=bond,
                fragments=list(fragments),
                dissociation_energy_kcal=round(energy, 1),
            )
        )

    results.sort(key=lambda entry: entry.dissociation_energy_kcal)
    if results:
        results[0] = results[0].model_copy(update={"is_weakest": True})
    return BondDissociationSurvey(
        smiles=require_canonical_smiles(smiles),
        # **The server's method, not this deployment's configured name** — the argument
        # `reaction_energy` already carries, and this composite is on the same publication path.
        # `settings.xtb_method` describes a calculation this process no longer runs, so a
        # deployment whose env says `GFN2-xTB` while the server runs GFN1 published a
        # `BondDissociationSurvey` — a Temporal wire type, PR-gated into the knowledge graph —
        # asserting the wrong level of theory. The two sibling composites added alongside this one
        # both read it off the result; this one alone regressed a fix already argued for.
        method=methods[0] or settings.xtb_method,
        solvent=solvent,
        temperature_k=temperature_k or settings.xtb_thermo_temperature_k,
        mode="homolytic",
        bonds=results,
        considered=len(cleavages),
        uncertainty_kcal=settings.xtb_reaction_uncertainty_kcal,
        warnings=[
            "semiempirical bond dissociation energies carry several kcal/mol of error, so the "
            "ordering is the answer and the magnitudes are not"
        ],
    )
