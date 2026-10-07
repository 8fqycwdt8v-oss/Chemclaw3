"""Composition over remote primitives: the composite calculations, built here from cached parts.

`D-2026-08-16-the-physics-leaves-the-cache-stays` split `calc` by composability: a primitive (one
calculation whose identity derives from its inputs) runs in `Chemclaw3-mcp` and is cached here
under the server's key; a composite (whose key would name an output, e.g. the geometry a
refinement loop settles on) is decomposed here so every part is separately cached and a repeat
costs round trips, not SCFs. This module holds the composites and their bookkeeping: balance
checking, symmetry numbers, relative energies, populations and warnings.

The MCP tool path and the durable activity path share it; they differ only in how a remote call
is awaited (an activity must heartbeat), which is the `run` parameter, defaulting to a plain
await.

Nothing here derives a `calc_version` or cache key; every key comes from the server via
`cached_remote` (see `connectors/calc/remote.py`).
"""

import asyncio
import logging
import math
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Literal, NamedTuple, Protocol, TypeVar

import numpy as np
from pydantic import ValidationError
from rdkit import Chem

from chemclaw.connectors.calc.remote import CalcTimeBudgetError, cached_remote, remote_call
from chemclaw.core.chem import require_canonical_smiles, require_molecule, torsion_handle
from chemclaw.core.config import settings
from chemclaw.core.config.calculators import PkaCalibration
from chemclaw.core.errors import ChemclawError
from chemclaw.science.calc.artifacts import (
    HESSIAN_ARRAYS,
    ArrayOffloadingStore,
    ArtifactStore,
)
from chemclaw.science.calc.budget import (
    estimate_units,
    require_hessian_affordable,
    require_within_budget,
    rotation_units,
)
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
    FailedBond,
    FailedMedium,
    FailureCause,
    HessianPayload,
    InteractionResult,
    MicrostatePka,
    OptimizationResult,
    RankedSpecies,
    ReactionEnergyResult,
    ReactionLevel,
    RefinedConformer,
    RefinedEnsemble,
    Rotamer,
    RotationBarrier,
    RotationProfile,
    ScanPoint,
    ScanResult,
    SiteReactivityResult,
    SolventComparisonResult,
    SolventEffect,
    SpeciesDistribution,
    SpeciesEnergy,
    SpeciesSolventComparison,
    SpeciesSolventResponse,
    SpeciesStanding,
    Structure,
    ThermochemistryResult,
    Torsion,
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
    half_life_from_barrier,
    macrostate_free_energy_kcal,
    rt_kcal,
    standard_state_for,
    thermochemistry_from_hessian,
    weighted_average,
)
from chemclaw.science.calc.uncertainty import CalculationDomainError

# What `ensemble_property` can average, and the result field each name reads. A closed set, so a
# bad name fails before the fan-out pays for a conformer search.
EnsembleProperties = Literal["dipole_debye", "homo_ev", "lumo_ev", "gap_ev", "charges", "fukui"]

# Which species-set question a distribution answers. The arithmetic is identical across them; the
# label is what stops a reader having to infer the question from the SMILES.
SpeciesKind = Literal["tautomers", "microstates", "stereoisomers", "custom"]

# Below this share of the E-weighted population a refined ensemble warns rather than presenting a
# truncation as the whole; 0.9 is CENSO's convention.
_REFINED_COVERAGE_WARNING = 0.9

# Which Fukui index a per-atom average reports: the radical index, the mean of the other two and
# the only one that assumes no particular attack.
_DEFAULT_FUKUI_MODE = "radical"
_FUKUI_FIELD = {"electrophilic": "f_minus", "nucleophilic": "f_plus", "radical": "f_zero"}

logger = logging.getLogger(__name__)

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

# Called with a human-readable line as each unit of work completes; a durable activity uses it for
# liveness.
Progress = Callable[[str], None]


def no_progress(_message: str) -> None:
    """Default progress sink: a composite called from a tool has nobody to report to."""


class RemoteRunner(Protocol):
    """How one remote call is awaited — the single difference between a tool and an activity.

    An activity must heartbeat during a long call or Temporal retries it from zero, while
    `activity.heartbeat` raises outside an activity. Passing the waiting strategy keeps the
    chemistry
    identical on both paths.
    """

    async def __call__(self, awaitable: Awaitable[_Result], what: str) -> _Result:
        """Await `awaitable`, doing whatever this caller must do while it runs."""
        ...


async def plain(awaitable: Awaitable[_Result], what: str) -> _Result:
    """Await the call and nothing else — the tool path's runner.

    `what` is unused here; it is part of the `RemoteRunner` protocol.
    """
    del what
    return await awaitable


async def kept(payload: _Payload, *, structures: StructureStore | None = None) -> _Payload:
    """Persist every geometry in a server payload, then hand the payload back unchanged.

    Every geometry is reported by `structure_id`, so the address must resolve; this is where it is
    written. Applied to the returned payload, not only on a miss, so geometries from cache hits are
    persisted too. A failed write raises: a store that is not writing would make the next
    `structure_id` unresolvable, and the result is already cached, so a retry pays no SCF.

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

    `[CH3]` carries one radical electron, `[O][O]` two; the multiplicity is 2S+1 with all of them
    unpaired, which makes a homolysis computable from SMILES alone. A closed-shell formula whose
    ground state is a triplet still needs its multiplicity stated.

    Passed to `embed_structure` explicitly because the server reads `multiplicity=None` as a
    closed-shell singlet.
    """
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(f"invalid SMILES: {smiles!r}")
    return 1 + sum(int(atom.GetNumRadicalElectrons()) for atom in mol.GetAtoms())


async def embed(smiles: str, run: RemoteRunner = plain) -> Structure:
    """The force-field-cleaned starting geometry for one molecule, from the server.

    Remote rather than local: a geometry embedded by a different RDKit build would be a different
    `structure_id`, and every downstream key would miss. Pass `run` from an activity — a slow embed
    would otherwise trip the heartbeat timeout while the call is still running.
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

    Keyed on geometry, method and solvent only, so another temperature is a cache hit plus
    `science/calc/thermo.py` arithmetic.

    A Hessian is megabytes, and `calculation_results` is never pruned (D-011), so the store handed
    to
    `cached_remote` is wrapped: packed arrays go to the content-addressed artifact store and the row
    keeps their hashes. Every Hessian passes through here, so the atom fence
    (`require_hessian_affordable`) lives here too, with a refusal that names this system's
    alternatives.
    """
    require_hessian_affordable(
        len(structure.elements), f"a Hessian of {structure.smiles or structure.structure_id}"
    )
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

    A gradient optimization converges to the nearest stationary point, often a rotational saddle (an
    eclipsed methyl preserved by symmetry), where a free energy is meaningless. The standard escape
    is
    applied: displace along the imaginary mode and re-optimize. Each part is keyed on its own input,
    so
    a repeat costs round trips and no SCF.

    Bounded by `settings.xtb_minimum_refinement_attempts`, after which the result is returned with
    `is_minimum=False`. The third element of the return is whether every underlying calculation was
    a
    cache hit; the RRHO arithmetic (temperature-dependent, milliseconds) is not counted.
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

    `subject` is the geometry to scan from; a barrier depends on the conformer, so after a conformer
    search the caller passes its choice. Without it a fresh embedding is used and `smiles` is only
    the
    label.

    Each point is a separately keyed `scan_point` call (the server moves the attached fragment,
    freezes the defining atoms and relaxes the rest), always from the input geometry so the result
    does
    not depend on walk direction (D-011). `maximum_relative_kcal` is the profile's highest point,
    not
    an optimized transition state: sound for a torsion, an upper-bound sketch for a bond being
    broken.
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

    One remote call: a CREST search has no internal unit boundary. The weighting stays here because
    populations and conformational entropy depend on temperature, so another temperature is a cache
    hit plus arithmetic. The search is stochastic; the cache makes later questions consistent with
    the
    first run.
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

    `ConformerEnsemble` reports energies relative to its own lowest member and truncates to
    `crest_max_members`; comparing two ensembles (as `microstate_pka` does) needs absolute Hartrees
    over every member. Shared so the arguments, and hence the cache key, are written once.
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
            # Conformer searches on drug-sized molecules exceed the default read bound, so they get
            # their own.
            timeout_seconds=settings.calc_sampling_timeout_seconds,
        ),
        f"{search} of {smiles}",
    )
    return EnsemblePayload.model_validate(await kept(payload)), cached


# --- acid/base equilibria -----------------------------------------------------------------------

# Heteroatoms whose bound protons mean "the pKa" is the acid one. A domain guard, not a site
# enumeration: CREST decides which proton comes off over every site it finds.
#
# Nitrogen is excluded: "the pKa of ethylamine" means its conjugate acid (10.7), not its N-H acidity
# (~36), and the same holds for amides and anilines. An aminophenol is what the `branch` argument is
# for.
_ACIDIC_HETEROATOMS = (8, 16)  # O, S


def _acid_or_base(smiles: str) -> Literal["acid", "base"]:
    """Which equilibrium to compute when the caller did not say.

    Acid whenever a proton sits on O or S (carboxylic acids, phenols, thiols); base for anything
    else
    carrying nitrogen (pyridine, ethylamine). Oxygen and sulfur never take the base branch: a pKaH
    for
    a protonated ether or ketone is irrelevant at any working pH, so a caller must ask for it
    explicitly.
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

    Over every member, not the truncated list: dropping the tail biases the side with more
    accessible
    states, systematically the anion.
    """
    lowest = min(member.energy_hartree for member in payload.members)
    relative = [(member.energy_hartree - lowest) * HARTREE_TO_KCAL for member in payload.members]
    degeneracies = [member.degeneracy for member in payload.members]
    correction = macrostate_free_energy_kcal(relative, degeneracies, temperature_k)
    return lowest + correction / HARTREE_TO_KCAL


def _aryl_protonation(site_smiles: str | None) -> bool | None:
    """Whether a protonated nitrogen is aromatic or aryl-attached; None where it cannot be read.

    Computed basicity of aliphatic amines does not rank with measured pKa, because aqueous ammonium
    basicity is set by hydrogen bonding to water, which ALPB cannot see. Aromatic and aryl nitrogen
    is
    dominated by delocalisation, which GFN2 captures.
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

    The acid reference set is O-H and S-H only, so a winning deprotomer at C or N is an
    extrapolation
    and is labelled as one. Read off the atom carrying the charge, since a substring test cannot
    tell
    `[CH2-]` from `[Cl-]`. Returns `None` for the fitted case and for an unreadable site.
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

    Two CREST searches and one subtraction: the neutral's conformer ensemble gives one macrostate,
    `--deprotonate` (or `--protonate`) the other. Each is reduced to `-RT ln sum g exp(-E/RT)` and
    the
    difference mapped to a pKa by a calibration fitted through this same pipeline. A composite
    because
    its key would name the microstate the search settles on; both searches are cached primitives.

    A macroscopic aqueous pKa of one ionisable centre, semiempirical, in a continuum solvent — not
    per-site microscopic pKas, not a titration curve, not a specification number. Quote the fit's
    standard error.
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

    Each is a case where the arithmetic succeeds but the answer means less than it looks, so it is
    carried on the result.
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

    `combine_structures` places the first monomer at the origin and offsets the second along +x, so
    order changes the starting geometry and the cache key. Each molecule travels with its geometry,
    so
    sorting cannot pair a monomer with the other one's conformer.
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

    The complex at its optimized binding mode minus each monomer optimized alone, so the deformation
    cost of binding is included. Five cached calls and no composite key: two monomer relaxations,
    the
    binding-mode search, and one relaxation of the chosen mode; the pair is canonicalized first.

    Limits: it is an energy, not a free energy (association entropy is absent); the search is
    stochastic; it is one pair in a continuum.
    """
    # An ion pair in vacuum is the pc-03 defect as a binding energy: two bare opposite charges
    # attract by hundreds of kcal/mol there and by a few in solution.
    require_solvent_for_ions([smiles_a, smiles_b], solvent)
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
    # The monomer separation is the server's default: only a starting point that the wall potential
    # and
    # search move.
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
    # Not `kept`: this starting arrangement is discarded by the search; the relaxed binding mode
    # below is
    # the geometry callers can name.
    payload, _ = await run(
        cached_remote(
            store,
            "search_binding_modes",
            {
                "structure": combined.model_dump(mode="json"),
                "effort": effort or settings.crest_effort,
                "solvent": solvent,
            },
            # A wall-potential search over a *pair* is the other CREST call, and it is the more
            # expensive of the two — same budget, same reason.
            timeout_seconds=settings.calc_sampling_timeout_seconds,
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

    An unbalanced difference is meaningless yet looks ordinary. The message names which element is
    short and by how much (usually a forgotten water or proton). Local, so it stops a request before
    any remote call.
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


def ionic_species(species: Sequence[str]) -> list[str]:
    """The species in `species` that are, or contain, a free ion — in first-seen order.

    A species is ionic when any dot-separated fragment carries a net formal charge (`O=C([O-])[O-]`,
    `[Na+].[Cl-]`). A fragment whose charges cancel is not: nitro groups, N-oxides, azides and diazo
    compounds are neutral molecules a thermal-hazard screen needs. A zwitterion is therefore not
    caught
    either.
    """
    found: list[str] = []
    for smiles in dict.fromkeys(species):
        parsed = Chem.MolFromSmiles(smiles)
        if parsed is None:
            continue
        if any(Chem.GetFormalCharge(part) for part in Chem.GetMolFrags(parsed, asMols=True)):
            found.append(smiles)
    return found


def require_solvent_for_ions(species: Sequence[str], solvent: str | None) -> None:
    """Refuse a gas-phase energy difference over free ions, before anything is computed.

    In the gas phase GFN2-xTB treats each ion as isolated in vacuum, so differences are dominated by
    unscreened charge localisation (hundreds of kcal/mol that are a few in water) — a different
    physical situation, not an imprecise one. Charge balance (`check_balance`) does not catch this.

    Raises:
        ValueError: `solvent` is None and some species is ionic (`ionic_species`). The message names
            the species and the two ways forward.
    """
    if solvent is not None:
        return
    ions = ionic_species(species)
    if ions:
        raise ValueError(
            f"a gas-phase energy over charged species is not physically meaningful: "
            f"{', '.join(ions)} carry a net charge, and with no solvent each is treated as a bare "
            "ion in vacuum, which puts hundreds of kcal/mol of unscreened charge into the "
            "difference. Pass an implicit solvent (e.g. solvent='water'), or write the reaction "
            "over neutral species"
        )


def solvated_ion_caveat(species: Sequence[str]) -> str | None:
    """The warning every energy difference over ions carries once a solvent has let it run.

    The other half of `require_solvent_for_ions`: what the continuum still leaves uncertain. Shared
    by
    every composite that differences species.
    """
    ions = ionic_species(species)
    if not ions:
        return None
    return (
        f"charged species present ({', '.join(ions)}): an ion's solvation comes entirely from "
        "the implicit solvent model, the least reliable part of this method, so read this as "
        "an ordering between related cases, not as a heat or an absolute energy"
    )


def media_with_gas_reference(
    species: Sequence[str], solvents: Sequence[str]
) -> tuple[list[str | None], str | None]:
    """The media an across-solvents screen runs in: the gas phase first, unless an ion forbids it.

    Over ions the gas reference is what `require_solvent_for_ions` refuses, so it is left out and
    the
    second value is the warning saying so. One function so both screens agree.
    """
    if ionic_species(species):
        return list(solvents), (
            "no gas-phase reference: this set has charged species, and a gas-phase energy over "
            "free ions is not physically meaningful"
        )
    return [None, *solvents], None


def _checked_symmetry_numbers(
    symmetry_numbers: dict[str, int] | None, species: set[str]
) -> dict[str, int]:
    """Validate a caller's sigma map against the equation it claims to describe.

    A key naming no species in the equation is a typo (often a differently written SMILES) and is
    refused rather than treated as an omission.
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

    Multiplicity comes from the SMILES' radical electrons (`radical_multiplicity`).
    `symmetry_number`
    is this species' sigma or None when unstated; the thermochemistry settings are specialized here
    so
    the stated and used values cannot disagree.
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

    Pure composition over per-species optimizations and Hessians, each cached; there is no
    reaction-level cache entry. Enforced: balance; identical treatment of every species (settings,
    solvent, level); and a stated rotational symmetry number per species, since sigma shifts entropy
    by
    R ln(sigma) and does not cancel — with any sigma unstated, ΔE and ΔH are reported and ΔG is
    withheld with a warning.

    ΔG is quoted at the medium's standard state (`standard_state`): 1 atm in the gas phase, 1 mol/L
    in
    solution. This matters only for Δn ≠ 0: 1.894·Δn kcal/mol at 298.15 K.

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
    require_solvent_for_ions([*reactants, *products], solvent)
    sigmas = _checked_symmetry_numbers(symmetry_numbers, set(reactants) | set(products))
    temperature = temperature_k or settings.xtb_thermo_temperature_k
    thermo = ThermoSettings(temperature_k=temperature) if level != "quick" else None

    roles: tuple[tuple[Literal["reactant", "product"], list[str]], ...] = (
        ("reactant", reactants),
        ("product", products),
    )
    queue = [(role, smiles) for role, group in roles for smiles in group]
    require_within_budget(
        estimate_units(len(queue), level=level),
        f"a reaction energy over {len(queue)} species",
    )
    species = []
    for index, (role, smiles) in enumerate(queue, start=1):
        progress(f"species {index}/{len(queue)}: {smiles}")
        species.append(
            await _species_energy(
                store, smiles, role, solvent, thermo, sigmas.get(smiles), level, run
            )
        )
    # "not a minimum" covers both a saddle (imaginary mode) and a non-stationary geometry (often no
    # imaginary mode, but a zero-point energy that is too low).
    warnings = [
        f"{entry.smiles} is not a minimum (an imaginary mode, or a geometry that is not a "
        "stationary point): its free energy is not a free energy"
        for entry in species
        if entry.is_minimum is False
    ]
    # Every level: the open-shell caveat is about the energies, which every level differences.
    if any(entry.multiplicity > 1 for entry in species):
        warnings.append(
            "open-shell species present: unrestricted GFN2 energies are less reliable "
            "than closed-shell ones, so treat a homolysis energy as an ordering"
        )
    # Reached only with a solvent — `require_solvent_for_ions` refused the gas phase above.
    caveat = solvated_ion_caveat([*reactants, *products])
    if caveat:
        warnings.append(caveat)
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
        # The server's method, not `settings.xtb_method`: the calculation runs in `Chemclaw3-mcp`,
        # and this
        # wire type is recorded into the knowledge graph. The fallback is for old histories only.
        method=species[0].method or settings.xtb_method,
        solvent=solvent,
        temperature_k=temperature,
        # From `science/calc/thermo.py`'s one rule, so the label matches the state the partition
        # functions
        # were evaluated at. ΔE and ΔH do not depend on it.
        standard_state=standard_state_for(solvent),
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


async def _attempt(awaitable: Awaitable[_Result]) -> _Result | ValueError:
    """Await one item of a screen, handing back its refusal instead of raising it.

    The boundary is `ValueError`, this repository's "bad input" contract (`ChemclawError` and
    `CalcToolError` included), which no retry can fix. Outages (`CalcServerError`, `CalcBusyError`)
    and cancellation propagate so the activity is retried. `CalcTimeBudgetError` is a refusal that
    `_cause` records as a `time_budget` stop.

    pydantic's `ValidationError` propagates: a server payload the client's model rejects is a
    contract
    skew that must fail the job, not one item. A plain `ValueError` is still one item's answer but
    is
    logged with its traceback, since a data-dependent bug looks the same.
    """
    try:
        return await awaitable
    except ValidationError:
        raise
    except ValueError as refusal:
        if not isinstance(refusal, ChemclawError):
            logger.warning("calc.item_refused_by_plain_value_error", exc_info=refusal)
        return refusal


async def _every_medium(branches: Sequence[Awaitable[_Result]]) -> list[_Result]:
    """Run a screen's media together, and stop the rest the moment one of them fails the screen.

    What escapes a branch is an outage or contract fault, which fails the activity. Unlike a bare
    `asyncio.gather`, siblings are cancelled and awaited before the first error is re-raised
    unchanged
    (not an `ExceptionGroup`, which `durable/publish.py`'s retry classification would not
    recognise).
    """
    tasks = [asyncio.ensure_future(branch) for branch in branches]
    try:
        return list(await asyncio.gather(*tasks))
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _cause(refusal: ValueError) -> FailureCause:
    """Whether one item's refusal was about the item, or the server's clock stopping it."""
    return "time_budget" if isinstance(refusal, CalcTimeBudgetError) else "refused"


def _refusal(causes: Sequence[FailureCause]) -> type[ValueError]:
    """The class a screen refuses with when nothing it was asked for could be answered.

    `CalcTimeBudgetError` when every failure was a stop by the server's clock; a plain `ValueError`
    as
    soon as one input was refused. Both are non-retryable.
    """
    if causes and all(cause == "time_budget" for cause in causes):
        return CalcTimeBudgetError
    return ValueError


def _named(failures: Sequence[tuple[str, str]]) -> str:
    """`label (reason); label (reason)`: every failed item of a screen, in the order asked."""
    return "; ".join(f"{label} ({reason})" for label, reason in failures)


def _medium(solvent: str | None) -> str:
    """How a medium is named to a chemist: its solvent, or the gas phase."""
    return solvent or "gas phase"


def lost_the_comparison(failed: Sequence[FailedMedium], computed: int) -> bool:
    """Whether failures left a screen with fewer than two media, so nothing was compared.

    Only a failure can do that; a screen asked for one medium never had a comparison. Public because
    the job summary asks the same question.
    """
    return bool(failed) and computed < 2


def _media_warnings(failed: Sequence[FailedMedium], computed: int) -> list[str]:
    """What a solvent screen must say about the media it lost, and about a comparison they cost.

    A spread over one medium is zero by construction, so the caller withholds the verdict
    (`lost_the_comparison`) and this says there is nothing to compare.
    """
    warnings: list[str] = []
    if failed:
        # Names only; reasons are under `failed`, since joining them makes an unbounded warning and
        # every
        # warning becomes a published flag row.
        warnings.append(
            f"{len(failed)} of {computed + len(failed)} media could not be computed and are not in "
            "this comparison: "
            + ", ".join(_medium(entry.solvent) for entry in failed)
            + " (each reason is under `failed`)"
        )
    if lost_the_comparison(failed, computed):
        warnings.append(
            "only one medium could be computed, so there is nothing to compare it against"
        )
    return warnings


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

    Includes the gas phase as a reference, so "the solvent barely matters" is visible — except over
    ions, where it is left out with a warning. Fan-out is bounded by `calc_screen_max_parallel`,
    default 1: each branch is a remote SCF expecting a whole pod's cores.
    """
    if not solvents:
        raise ValueError("give at least one solvent to compare")
    # The equation's own checks run once before the fan-out, so a bad equation is refused once
    # rather
    # than once per medium; `reaction_energy` still runs them itself.
    check_balance(reactants, products)
    _checked_symmetry_numbers(symmetry_numbers, set(reactants) | set(products))
    media, no_reference = media_with_gas_reference([*reactants, *products], solvents)
    species_count = len(reactants) + len(products)
    require_within_budget(
        estimate_units(species_count, level=level) * len(media),
        f"comparing a {species_count}-species reaction across {len(solvents)} solvents"
        + ("" if no_reference else " plus the gas-phase reference"),
    )
    limit = asyncio.Semaphore(settings.calc_screen_max_parallel)

    async def one(solvent: str | None) -> ReactionEnergyResult | ValueError:
        """One medium, under the fan-out bound, reporting progress prefixed with its own name.

        Returns the medium's refusal (`_attempt`), so it is reported beside the others.
        """
        label = _medium(solvent)

        def relay(line: str) -> None:
            """Prefix the inner reaction's progress with which medium it is running in.

            Keeps interleaved parallel lines attributable.
            """
            progress(f"{label}: {line}")

        async with limit:
            return await _attempt(
                reaction_energy(
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
            )

    # Results come back in argument order, so the gas-phase reference stays first and the ranking
    # below sorts from a list whose order does not depend on which branch finished first.
    outcomes = await _every_medium([one(solvent) for solvent in media])
    failed = [
        FailedMedium(solvent=solvent, reason=str(outcome), cause=_cause(outcome))
        for solvent, outcome in zip(media, outcomes, strict=True)
        if isinstance(outcome, ValueError)
    ]
    results = [outcome for outcome in outcomes if not isinstance(outcome, ValueError)]
    if not results:
        raise _refusal([entry.cause for entry in failed])(
            "no medium of this solvent screen could be computed: "
            + _named([(_medium(entry.solvent), entry.reason) for entry in failed])
        )
    effects = [
        SolventEffect(
            solvent=result.solvent,
            standard_state=result.standard_state,
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
    if no_reference:
        warnings.append(no_reference)
    warnings.extend(_media_warnings(failed, len(results)))
    # The gas reference (1 atm) and solution rows (1 mol/L) are in different standard states.
    # Solvent
    # against solvent needs no caveat, but for Δn ≠ 0 the gas-to-solution gap includes 1.894·Δn
    # kcal/mol of reference state, which a reader would misread as a solvent effect.
    delta_n = len(products) - len(reactants)
    # Both phases present, not just the gas row: a screen whose every solvent failed has no
    # solution row for this sentence to be about.
    both_phases = {effect.solvent is None for effect in effects} == {True, False}
    if delta_n and both_phases and any(effect.delta_g_kcal is not None for effect in effects):
        warnings.append(
            f"this equation changes the molecule count by {delta_n:+d}, and the gas-phase row is "
            "quoted at the 1 atm standard state while every solvent row is quoted at 1 mol/L (the "
            "convention each phase uses). Solvent-against-solvent comparisons are unaffected; the "
            f"gas-to-solution difference additionally carries {abs(delta_n) * 1.894:.2f} kcal/mol "
            "of standard state and is not a solvation energy"
        )
    if not lost_the_comparison(failed, len(effects)) and spread <= uncertainty:
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
        failed=failed,
    )


# --- ensembles refined, averaged and ranked ---------------------------------------------------
#
# Fan-out loops live here rather than in templates: a template has no loops and the agent's loop is
# iteration-capped. Every loop below counts its cost before it starts (`science/calc/budget.py`).


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

    Costs one Hessian per member, so it is opt-in and bounded to the top `ensemble_refine_top_n`
    members by electronic energy. E-weighting assumes equal zero-point, thermal and entropic terms,
    which over-populates compact hydrogen-bonded folds; G-weighting gives the intended distribution.
    The result carries `refined_population_covered` (the E-weighted share the refined members
    account
    for) and warns below `_REFINED_COVERAGE_WARNING`.
    """
    # Counted before the search: the budget is read against the work requested (the search plus a
    # relax
    # and Hessian per kept conformer), not after the most expensive call has been paid.
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
            "energy is computed at a geometry that is not one and its population is not meaningful"
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

    Lifts the one-conformer caveat: a dipole, gap or Fukui ranking can differ more between
    conformers
    of one molecule than between molecules. The answer carries a spread as well as a mean; when the
    values scatter widely, the molecule has no single value at this temperature.

    Per-atom properties (`fukui`, `charges`) are averaged atom by atom, paired by atom index
    (`_per_atom`).
    """
    # `crest_max_members`, not `ensemble_refine_top_n` (which sizes Hessian work); a property
    # average
    # costs one single point per member. Counted before the search, as in `refined_ensemble`.
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

    Never by list position: `SiteReactivityResult.sites` is ranked by susceptibility and truncated,
    so
    position *k* is a different atom in each conformer, and conformers may carry different atom
    sets.
    Atoms missing from any member are dropped rather than averaged over a subset; the caller reports
    the count.
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

    A `match` rather than a registry: four entries do not need indirection.
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

    One composite for tautomers, protonation microstates and stereoisomers; `kind` records which was
    asked. Each species goes through `_species_energy`, as reaction energies do, so the two agree
    and
    share cache entries. The set is the answer's universe and is not checked here; `enumerated`
    carries the count the caller started from.
    """
    if not species:
        raise ValueError("a distribution needs at least one species")
    ceiling = settings.species_ranking_max
    considered = list(species[:ceiling])
    # A protonation-microstate set is the worst case of the pc-03 defect: its members differ in net
    # charge by construction, and in vacuum that difference is unscreened charge, not a ranking.
    require_solvent_for_ions([smiles for smiles, _ in considered], solvent)
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
    refused: list[tuple[str, str]] = []
    causes: list[FailureCause] = []
    for index, (smiles, _) in enumerate(considered, start=1):
        progress(f"species {index}/{len(considered)}: {smiles}")
        # `stated.get(smiles)`, not a literal 1: `None` computes at sigma=1 but records it as
        # unstated, so
        # the warning below fires.
        outcome = await _attempt(
            _species_energy(
                store, smiles, "reactant", solvent, thermo, stated.get(smiles), level, run
            )
        )
        if isinstance(outcome, ValueError):
            # By SMILES rather than label: it is the string the caller removes and passes back.
            refused.append((smiles, str(outcome)))
            causes.append(_cause(outcome))
        else:
            energies.append(outcome)
    if refused:
        # Refused rather than ranked over the rest, after every species was tried: dropping a form
        # would
        # redistribute its population. Trying all first names every failing form and caches the
        # rest. Per
        # `_refusal`, a set stopped entirely by the clock is named as a stop.
        raise _refusal(causes)(
            f"{len(refused)} of {len(considered)} species could not be computed, and a "
            "distribution over the rest would re-share their population among the forms that "
            "were: " + _named(refused) + ". Each reason says what that form needs — a form "
            "the server cannot handle is removed or corrected, one stopped by a time budget "
            "needs a smaller calculation or a larger budget. The "
            f"{len(energies)} that were computed are cached and will not be recomputed"
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
    caveat = solvated_ion_caveat([smiles for smiles, _ in considered])
    if caveat:
        warnings.append(caveat)
    if not use_gibbs:
        warnings.append(
            "ranked by electronic energy: at level='quick' no species has a free energy, so the "
            "populations ignore the zero-point and entropy differences between these forms"
        )
    unstated = sorted({energy.smiles for energy in energies if energy.symmetry_number is None})
    if unstated and use_gibbs:
        # Warned rather than withheld (unlike `reaction_energy`): here the free energy is the
        # question.
        # sigma shifts G by R·T·ln(sigma), ~0.41 kcal/mol per factor of two at 298 K — comparable to
        # tautomer
        # gaps.
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
        # This branch runs only when the set exceeds the ceiling; the cut is the first N in caller
        # order.
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
                # The dominant form's geometry id, so it is reachable downstream like every other
                # ensemble's.
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


async def species_solvent_comparison(
    store: ResultStore,
    species: Sequence[tuple[str, str]],
    solvents: list[str],
    *,
    kind: SpeciesKind = "custom",
    temperature_k: float | None = None,
    level: ReactionLevel = "standard",
    symmetry_numbers: Mapping[str, int] | None = None,
    progress: Progress = no_progress,
    run: RemoteRunner = plain,
) -> SpeciesSolventComparison:
    """Rank one species set in each of several media, and report how the ranking moves.

    `solvent_comparison`'s shape over a distribution: the gas phase prepended (left out over ions,
    `media_with_gas_reference`), media under the same bound, and the spread checked against the
    method's uncertainty. Implicit solvation does best here: the same species in every medium, so
    the
    continuum's systematic error largely cancels. Report the ordering and swing, not one medium's
    number alone. The budget counts the whole `species x media` fan-out.
    """
    if not solvents:
        raise ValueError("give at least one solvent to compare")
    considered = list(species)
    if not considered:
        # Here rather than left to `species_ranking`: every medium would refuse it identically,
        # and one sentence is the answer rather than the same one per medium.
        raise ValueError("a distribution needs at least one species")
    media, no_reference = media_with_gas_reference(
        [smiles for smiles, _ in considered[: settings.species_ranking_max]], solvents
    )
    require_within_budget(
        estimate_units(len(considered[: settings.species_ranking_max]), level=level) * len(media),
        f"ranking {len(considered)} species in {len(media)} media",
    )
    limit = asyncio.Semaphore(settings.calc_screen_max_parallel)

    async def one(solvent: str | None) -> SpeciesDistribution | ValueError:
        """One medium, under the fan-out bound, with its progress attributed to its own branch.

        Returns the medium's refusal (`_attempt`); `species_ranking` refuses a partial set, so a
        medium is
        whole or in `failed`.
        """
        label = _medium(solvent)

        def relay(line: str) -> None:
            progress(f"{label}: {line}")

        async with limit:
            return await _attempt(
                species_ranking(
                    store,
                    considered,
                    kind=kind,
                    solvent=solvent,
                    temperature_k=temperature_k,
                    level=level,
                    symmetry_numbers=symmetry_numbers,
                    progress=relay,
                    run=run,
                )
            )

    # Results come back in argument order, so the gas-phase reference (when present) stays first
    # and every `standings` list is in the order the caller can read against `media`.
    outcomes = await _every_medium([one(solvent) for solvent in media])
    failed = [
        FailedMedium(solvent=solvent, reason=str(outcome), cause=_cause(outcome))
        for solvent, outcome in zip(media, outcomes, strict=True)
        if isinstance(outcome, ValueError)
    ]
    distributions = [outcome for outcome in outcomes if not isinstance(outcome, ValueError)]
    if not distributions:
        raise _refusal([entry.cause for entry in failed])(
            "no medium of this species screen could be ranked: "
            + _named([(_medium(entry.solvent), entry.reason) for entry in failed])
        )

    # Keyed by SMILES, not position: `species_ranking` sorts by energy, so indices differ between
    # media
    # exactly when the ranking reorders.
    responses: list[SpeciesSolventResponse] = []
    for smiles, label in considered[: len(distributions[0].species)]:
        standings = [
            SpeciesStanding(
                solvent=distribution.solvent,
                relative_kcal=ranked.relative_kcal,
                population=ranked.population,
            )
            for distribution in distributions
            for ranked in distribution.species
            if ranked.smiles == smiles
        ]
        if not standings:
            continue
        relatives = [standing.relative_kcal for standing in standings]
        populations = [standing.population for standing in standings]
        responses.append(
            SpeciesSolventResponse(
                smiles=smiles,
                label=label,
                standings=standings,
                population_swing=round(max(populations) - min(populations), 4),
                relative_swing_kcal=round(max(relatives) - min(relatives), 3),
            )
        )

    dominant = [distribution.dominant.smiles for distribution in distributions]
    largest = max((response.relative_swing_kcal for response in responses), default=0.0)
    uncertainty = settings.xtb_reaction_uncertainty_kcal
    warnings = list(
        dict.fromkeys(
            warning for distribution in distributions for warning in distribution.warnings
        )
    )
    if no_reference:
        warnings.append(no_reference)
    warnings.extend(_media_warnings(failed, len(distributions)))
    if not lost_the_comparison(failed, len(distributions)) and largest <= uncertainty:
        warnings.append(
            f"no species moves by more than {largest:.1f} kcal/mol across these media, within the "
            f"method's ±{uncertainty:.1f}: this calculation does not distinguish them"
        )
    if len(set(dominant)) > 1:
        warnings.append(
            "the dominant form is not the same in every medium, so any property computed for "
            "'the compound' in one of them is a property of a different species in another"
        )
    return SpeciesSolventComparison(
        kind=kind,
        method=distributions[0].method,
        temperature_k=distributions[0].temperature_k,
        level=level,
        distributions=distributions,
        responses=responses,
        dominance_changes=len(set(dominant)) > 1,
        largest_swing_kcal=largest,
        uncertainty_kcal=uncertainty,
        warnings=warnings,
        failed=failed,
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

    Each bond is one `reaction_energy` (parent → two fragments), reusing its arithmetic, balance
    check
    and open-shell handling; `radical_multiplicity` reads the fragments' explicit radicals.

    Defaults to `level="quick"`: a survey supports a ranking, and a Hessian per fragment per bond
    triples the cost for a magnitude semiempirical theory does not deliver. Energies are ΔH (ΔE at
    `quick`); no symmetry number is asserted, so `reaction_energy` withholds ΔG and its warning is
    surfaced. No bond dissociation free energy is reported.
    """
    if not cleavages:
        raise ValueError(f"no breakable bond was enumerated for {smiles}")
    require_within_budget(
        estimate_units(len(cleavages) * 3, level=level),
        f"a {len(cleavages)}-bond dissociation survey of {smiles}",
    )

    # The parent is computed once here, with exactly the settings `reaction_energy` derives, so each
    # bond reads it as a cache hit and a refused parent fails the survey once rather than once per
    # bond.
    require_solvent_for_ions([smiles], solvent)
    temperature = temperature_k or settings.xtb_thermo_temperature_k
    progress(f"parent {smiles}")
    await _species_energy(
        store,
        smiles,
        "reactant",
        solvent,
        ThermoSettings(temperature_k=temperature) if level != "quick" else None,
        None,
        level,
        run,
    )

    results: list[DissociatedBond] = []
    failed: list[FailedBond] = []
    methods: list[str] = []
    # Every composed reaction's caveats, deduplicated, so unstated-sigma and open-shell warnings
    # surface.
    caveats: list[str] = []
    for index, (atoms, bond, fragments) in enumerate(cleavages, start=1):
        progress(f"bond {index}/{len(cleavages)} ({bond}) of {smiles}")
        # Keyword arguments, so a field-order change cannot compute a different bond than named.
        outcome = await _attempt(
            reaction_energy(
                store,
                [smiles],
                list(fragments),
                solvent=solvent,
                temperature_k=temperature_k,
                level=level,
                # No symmetry map: sigma=1 is wrong for most homolysis products, and `None` records
                # it as unstated
                # so `reaction_energy`'s withhold-and-warn applies.
                symmetry_numbers=None,
                progress=no_progress,
                run=run,
            )
        )
        if isinstance(outcome, ValueError):
            # One bond the calculation refuses is that bond's answer, not the survey's: the bonds
            # are independent reactions, and the rest still rank each other.
            progress(f"bond {index}/{len(cleavages)} ({bond}) could not be computed: {outcome}")
            failed.append(
                FailedBond(
                    atoms=list(atoms),
                    bond=bond,
                    fragments=list(fragments),
                    reason=str(outcome),
                    cause=_cause(outcome),
                )
            )
            continue
        reaction = outcome
        methods.append(reaction.method)
        caveats.extend(reaction.warnings)
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

    # A bond is named with its atoms: "C-H" alone is ambiguous in any molecule with two of them.
    names = [f"{entry.bond} {entry.atoms}" for entry in failed]
    if not results:
        raise _refusal([entry.cause for entry in failed])(
            f"no bond of {smiles} could be computed: "
            + _named([(name, entry.reason) for name, entry in zip(names, failed, strict=True)])
        )
    results.sort(key=lambda entry: entry.dissociation_energy_kcal)
    results[0] = results[0].model_copy(update={"is_weakest": True})
    lost = (
        [
            f"{len(failed)} of {len(cleavages)} bonds could not be computed, so the weakest bond "
            "flagged here is the weakest of the rest and any of these may be weaker: "
            + ", ".join(names)
            + " (each reason is under `failed`)"
        ]
        if failed
        else []
    )
    return BondDissociationSurvey(
        smiles=require_canonical_smiles(smiles),
        # The server's method, not `settings.xtb_method`, since this survey is published.
        method=methods[0] or settings.xtb_method,
        solvent=solvent,
        temperature_k=temperature_k or settings.xtb_thermo_temperature_k,
        mode="homolytic",
        bonds=results,
        considered=len(cleavages),
        uncertainty_kcal=settings.xtb_reaction_uncertainty_kcal,
        warnings=[
            "semiempirical bond dissociation energies carry several kcal/mol of error, so the "
            "ordering is the answer and the magnitudes are not",
            *lost,
            *dict.fromkeys(caveats),
        ],
        failed=failed,
    )


# --- rotational profile ---------------------------------------------------------------------


async def rotation_profile(
    store: ResultStore,
    smiles: str,
    torsion: Torsion,
    *,
    subject: Structure | None = None,
    solvent: str | None = None,
    temperature_k: float | None = None,
    step_degrees: float | None = None,
    level: ReactionLevel = "quick",
    progress: Progress = no_progress,
    run: RemoteRunner = plain,
) -> RotationProfile:
    """Drive one torsion through a full period and report its rotamers and their barriers.

    A composite (its key would name the wells it settles on), so every part is separately keyed and
    a
    finer re-run pays only for new points. Four stages:

    1. **The coarse profile**, over `period_degrees` (a symmetric rotor repeats).
    2. **Refine each pass**: a coarse grid steps over maxima, so each is rescanned finely between
       its
       neighbours.
    3. **Release each well**: a scan point is constrained, so each well is optimized freely into a
       rotamer with a `structure_id`; wells relaxing into one basin are merged.
    4. **Rank and time them**: populations at `temperature_k` weighted by `symmetry_order`, then
       Eyring
       half-lives with the band the method's uncertainty implies.

    Above `quick` each rotamer gets a Hessian (ranking by free energy); at `thorough` each pass does
    too, giving ΔG‡ with its one imaginary mode dropped. A pass with more than one imaginary mode is
    reported on `E` and says so.

    Args:
        store: The D-011 result cache every primitive is keyed in.
        smiles: The molecule, and the label the result is reported under.
        torsion: The bond to rotate, as `enumerate_torsions` reported it. Verified against the
            structure rather than trusted — see `_verified_torsion`.
        subject: The conformer to measure in. A barrier depends on which one, so a caller that has
            run a conformer search passes its choice rather than re-embedding.
        solvent: Implicit solvent for every point, or None for gas phase.
        temperature_k: What the populations and half-lives are quoted at.
        step_degrees: The coarse step; the configured default otherwise.
        level: `quick` (electronic), `standard` (free energies at the rotamers), `thorough` (also
            at the passes, giving ΔG‡).
        progress: Called as each unit of work completes, for an activity's heartbeat.
        run: How each remote call is awaited.

    Returns:
        The profile, its rotamers as released minima, and the directional barriers between them.
    """
    step = settings.xtb_rotation_step_degrees if step_degrees is None else step_degrees
    temperature = settings.xtb_thermo_temperature_k if temperature_k is None else temperature_k
    if step >= torsion.period_degrees:
        raise ValueError(
            f"a {step:g} degree step cannot resolve a {torsion.period_degrees:g} degree period: "
            "the profile would be one or two points and every well and barrier in it an artefact. "
            "Lower step_degrees, or check the torsion's symmetry_order."
        )
    structure = subject if subject is not None else await embed(smiles, run=run)
    atoms = _verified_torsion(structure, torsion)

    coarse = [index * step for index in range(max(2, math.ceil(torsion.period_degrees / step)))]
    passes = _pass_count(torsion.period_degrees, step)
    require_within_budget(
        rotation_units(
            len(coarse) + passes * settings.xtb_rotation_refine_points, passes, level=level
        ),
        f"a rotational profile of {smiles} at {step:g} degrees",
    )

    profile, method = await _driven(store, structure, atoms, coarse, solvent, progress, run)
    refined = await _refined_maxima(
        store, structure, atoms, profile, step, torsion.period_degrees, solvent, progress, run
    )
    profile = dict(sorted({**profile, **refined}.items()))

    wells, warnings = await _released_wells(
        store, structure, atoms, profile, torsion, solvent, temperature, level, progress, run
    )
    barriers, unresolved = await _barriers(
        store,
        structure,
        atoms,
        profile,
        wells,
        torsion,
        solvent,
        temperature,
        level,
        progress,
        run,
    )
    warnings.extend(unresolved)
    warnings.extend(_profile_warnings(profile, torsion, step))
    energies = list(profile.values())
    lowest = min(energies)
    return RotationProfile(
        smiles=structure.smiles or smiles,
        input_structure_id=structure.structure_id,
        # The server's method, not `settings.xtb_method`: `publish/project` turns it into a
        # `TheoryLevel`.
        # The config is the fallback only for a payload stating no method.
        method=method or settings.xtb_method,
        solvent=solvent,
        temperature_k=temperature,
        level=level,
        torsion_id=torsion.torsion_id,
        atoms=list(atoms),
        label=torsion.label,
        symmetry_order=torsion.symmetry_order,
        period_degrees=torsion.period_degrees,
        points=[
            ScanPoint(
                value=value,
                energy_hartree=energy,
                relative_kcal=round((energy - lowest) * HARTREE_TO_KCAL, 3),
            )
            for value, energy in profile.items()
        ],
        rotamers=[well.rotamer for well in wells],
        barriers=barriers,
        # `None`, not 0.0, when nothing resolved: `publish/project.py` would publish a zero as a
        # real
        # `rotational_barrier`, indistinguishable from free rotation.
        highest_barrier_kcal=(
            round(max(barrier.forward_kcal for barrier in barriers), 2) if barriers else None
        ),
        uncertainty_kcal=settings.xtb_reaction_uncertainty_kcal,
        warnings=warnings,
    )


def _verified_torsion(structure: Structure, torsion: Torsion) -> tuple[int, int, int, int]:
    """Check the handle names this molecule's bond, and hand back the four atoms.

    Indices carried between turns are one rewritten SMILES away from naming a different, equally
    valid bond, so the handle is recomputed from this molecule and compared; a mismatch is a refusal
    saying what to do. The bond must be acyclic (driving a ring bond is a ring pucker, not a
    rotation).
    A rotor whose rotating end carries only hydrogens has no reported dihedral; `_rotor_dihedral`
    builds one or refuses.

    Raises:
        ValueError: the handle does not name a bond of this molecule, an index is out of range, the
            named atoms are not bonded, the bond is in a ring, or the rotor is a symmetric top.
    """
    if structure.smiles is None:
        raise ValueError(
            "a rotational profile needs a molecule to check the torsion against, and this "
            "geometry carries no SMILES"
        )
    mol = require_molecule(structure.smiles)
    if max(torsion.bond) >= mol.GetNumAtoms():
        raise ValueError(
            f"atom {max(torsion.bond)} is not an atom of {structure.smiles!r}, which has "
            f"{mol.GetNumAtoms()} heavy atoms. Take the torsion from enumerate_torsions on this "
            "molecule rather than working the indices out."
        )
    bond = mol.GetBondBetweenAtoms(*torsion.bond)
    if bond is None:
        raise ValueError(f"atoms {torsion.bond} are not bonded in {structure.smiles!r}")
    if bond.IsInRing():
        raise ValueError(
            f"atoms {torsion.bond} are a ring bond of {structure.smiles!r}. Driving one is a ring "
            "pucker rather than a rotation; use sample_conformers for ring conformations."
        )
    minted = torsion_handle(mol, (torsion.bond[0], torsion.bond[1]))
    if minted != torsion.torsion_id:
        raise ValueError(
            f"{torsion.torsion_id} does not name a bond of {structure.smiles!r} — atoms "
            f"{torsion.bond} there are {minted}. A torsion handle is derived from the molecule, so "
            "one carried from another compound, another way of writing this one, or another RDKit "
            "build will not resolve. Re-run enumerate_torsions on this molecule."
        )
    explicit = _explicit_molecule(structure, mol)
    if not torsion.atoms:
        return _rotor_dihedral(explicit, torsion)
    if len(torsion.atoms) != 4:
        raise ValueError(
            f"{torsion.label!r} carries {len(torsion.atoms)} atoms, not four — a dihedral is four "
            "atoms bonded in sequence. Pass the enumerate_torsions entry through unchanged rather "
            "than assembling one."
        )
    first, begin, end, last = _checked_dihedral(structure, explicit, torsion)
    return first, begin, end, last


def _explicit_molecule(structure: Structure, mol: Chem.Mol) -> Chem.Mol:
    """`mol` with explicit hydrogens, in the atom order `structure` itself is numbered in.

    Every geometry is `AddHs` over the canonical SMILES (heavy atoms in canonical order, hydrogens
    appended by parent), as the calc server's `structure_from_smiles` builds it and `scan_point`
    validates. The element lists are compared to assert that cross-repository contract. Heavy
    indices
    are unchanged; hydrogen indices become addressable, which X-H rotors need.
    """
    explicit = Chem.AddHs(mol)
    elements = [atom.GetAtomicNum() for atom in explicit.GetAtoms()]
    if elements != structure.elements:
        raise ValueError(
            f"{structure.smiles!r} does not describe this geometry: it expands to {len(elements)} "
            f"atoms and the structure carries {len(structure.elements)}. A torsion is driven by "
            "atom index, so the two must be the same molecule in the same order."
        )
    return explicit


def _rotor_dihedral(explicit: Chem.Mol, torsion: Torsion) -> tuple[int, int, int, int]:
    """The dihedral for a rotor `enumerate_torsions` reported without one, or a refusal saying why.

    A symmetric top (three hydrogens on the rotating end, e.g. methyl) is refused: its barrier is
    already in the quasi-RRHO free-rotor treatment. An X-H rotor (one or two hydrogens: O-H, S-H,
    N-H)
    is scanned, since its barrier is not in the low modes. The dihedral is built in the structure's
    explicit-H numbering with deterministic end atoms, so each scan point's cache key is stable.
    """
    begin, end = (explicit.GetAtomWithIdx(index) for index in torsion.bond)
    rotating, anchor = (end, begin) if _heavy_neighbours(begin, end) else (begin, end)
    if _heavy_neighbours(rotating, anchor):
        raise ValueError(
            f"{torsion.label!r} carries no dihedral, but both ends of {torsion.bond} carry a heavy "
            "neighbour, so this bond has one. Re-run enumerate_torsions on this molecule and pass "
            "its entry through unchanged."
        )
    hydrogens = sorted(
        atom.GetIdx() for atom in rotating.GetNeighbors() if atom.GetAtomicNum() == 1
    )
    if len(hydrogens) >= 3:
        raise ValueError(
            f"{torsion.label!r} is a symmetric top (a methyl or tert-butyl rotation): every "
            "orientation is the same structure, so there is no barrier to profile. Its energetic "
            "effect is already in the free-rotor treatment of the low modes."
        )
    # A dihedral needs an off-axis atom at each end; `enumerate_torsions` never reports a bond
    # without
    # them, so this catches a hand-assembled entry with a sentence rather than an `IndexError`.
    anchored = sorted(
        atom.GetIdx() for atom in anchor.GetNeighbors() if atom.GetIdx() != rotating.GetIdx()
    )
    if not hydrogens or not anchored:
        raise ValueError(
            f"{torsion.label!r} has nothing off the axis to measure an angle against: rotating "
            "about it moves no atom. Take the torsion from enumerate_torsions on this molecule."
        )
    # Heavy atoms preferred, then lowest index — a deterministic choice at each end, so two runs of
    # the same question drive the same four atoms and hit the same scan-point cache rows.
    reference = min(
        anchored, key=lambda index: (explicit.GetAtomWithIdx(index).GetAtomicNum() == 1, index)
    )
    return reference, anchor.GetIdx(), rotating.GetIdx(), hydrogens[0]


def _heavy_neighbours(atom: Chem.Atom, other: Chem.Atom) -> bool:
    """Does `atom` carry a heavy neighbour besides `other`? The test for "there is a dihedral"."""
    return any(
        neighbour.GetAtomicNum() > 1 and neighbour.GetIdx() != other.GetIdx()
        for neighbour in atom.GetNeighbors()
    )


def _checked_dihedral(
    structure: Structure, mol: Chem.Mol, torsion: Torsion
) -> tuple[int, int, int, int]:
    """The four atoms that will actually be driven, checked as carefully as the bond was.

    The handle guards `bond`, but `atoms` is what the scan drives: negative indices, repeated atoms
    or
    out-of-range indices would otherwise give a profile of the wrong atoms or a numpy `IndexError`.
    The
    indices must address this molecule, be four distinct atoms, be bonded in sequence, and carry the
    named bond in the middle.
    """
    atoms = list(torsion.atoms)
    # `mol` carries explicit hydrogens, so the bound is the structure's own atom count rather than
    # its heavy-atom count — an X-H rotor's dihedral ends on a hydrogen and must be in range.
    count = mol.GetNumAtoms()
    if [index for index in atoms if not 0 <= index < count]:
        raise ValueError(
            f"the dihedral {atoms} of {torsion.label!r} is not four atoms of "
            f"{structure.smiles!r}, which has {count} with its hydrogens. Take the torsion from "
            "enumerate_torsions rather than working the indices out; a negative index in "
            "particular addresses a real atom and would have driven a different dihedral silently."
        )
    if len(set(atoms)) != 4:
        raise ValueError(
            f"the dihedral {atoms} of {torsion.label!r} repeats an atom, so it does not define an "
            "angle. Take the torsion from enumerate_torsions on this molecule."
        )
    if sorted(atoms[1:3]) != sorted(torsion.bond):
        raise ValueError(
            f"the dihedral {atoms} does not turn about the bond {torsion.bond} it is paired with — "
            "the middle two atoms are the bond being rotated. Pass the enumerate_torsions entry "
            "through unchanged rather than assembling one."
        )
    unbonded = [
        (one, other)
        for one, other in zip(atoms, atoms[1:], strict=False)
        if mol.GetBondBetweenAtoms(one, other) is None
    ]
    if unbonded:
        raise ValueError(
            f"the dihedral {atoms} is not a bonded chain in {structure.smiles!r}: {unbonded} "
            "are not bonded. A dihedral is defined by four atoms bonded in sequence."
        )
    first, begin, end, last = atoms
    return first, begin, end, last


def _pass_count(period_degrees: float, step: float) -> int:
    """How many maxima a period can hold, for the budget count made before anything is computed.

    Bounded by the number of coarse intervals, since the preflight must count before computing.
    """
    return max(1, math.ceil(period_degrees / step) // 2)


async def _driven(
    store: ResultStore,
    structure: Structure,
    atoms: tuple[int, ...],
    values: Sequence[float],
    solvent: str | None,
    progress: Progress,
    run: RemoteRunner,
) -> tuple[dict[float, float], str]:
    """Relax the molecule at each dihedral value and return `{degrees: energy}`, and the method.

    Each point starts from the input geometry, as in `scan_profile`, so results do not depend on
    walk
    direction (D-011). The method is the server's (read off the first point; the whole profile is
    one
    method) because `RotationProfile.method` is a published claim; an empty string means the payload
    did not say.
    """
    energies: dict[float, float] = {}
    method = ""
    for index, value in enumerate(values, start=1):
        progress(f"dihedral {value:g} degrees ({index}/{len(values)})")
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
            f"dihedral at {value:g} degrees",
        )
        point = OptimizationResult.model_validate(await kept(payload))
        energies[value] = point.energy_hartree
        method = method or point.method
    return energies, method


def _wrapped(profile: dict[float, float], period_degrees: float) -> list[tuple[float, float]]:
    """The profile as an ordered ring: the last point's neighbour is the first point again.

    A torsion is periodic, so the interval across the wrap has a real maximum that a linear reading
    would miss.
    """
    ordered = sorted(profile.items())
    return ordered + [(ordered[0][0] + period_degrees, ordered[0][1])]


def _maxima(profile: dict[float, float], period_degrees: float) -> list[float]:
    """The angles of the coarse profile's local maxima, read around the ring."""
    ring = _wrapped(profile, period_degrees)
    return [
        ring[index][0] % period_degrees
        for index in range(len(ring))
        if ring[index][1] > ring[index - 1][1]
        and ring[index][1] >= ring[(index + 1) % len(ring)][1]
    ]


def _minima(profile: dict[float, float], period_degrees: float) -> list[float]:
    """The angles of the coarse profile's local minima — where a well is, before it is released."""
    ring = _wrapped(profile, period_degrees)
    return [
        ring[index][0] % period_degrees
        for index in range(len(ring))
        if ring[index][1] < ring[index - 1][1]
        and ring[index][1] <= ring[(index + 1) % len(ring)][1]
    ]


async def _refined_maxima(
    store: ResultStore,
    structure: Structure,
    atoms: tuple[int, ...],
    profile: dict[float, float],
    step: float,
    period_degrees: float,
    solvent: str | None,
    progress: Progress,
    run: RemoteRunner,
) -> dict[float, float]:
    """Rescan finely around each maximum, because a coarse grid steps over one.

    Adds `xtb_rotation_refine_points` between the coarse neighbours on each side, so the barrier is
    not a lower bound of unknown error.
    """
    extra = settings.xtb_rotation_refine_points
    if not extra:
        return {}
    refined: dict[float, float] = {}
    for peak in _maxima(profile, period_degrees):
        spacing = 2 * step / (extra + 1)
        wanted = [
            round((peak - step + spacing * offset) % period_degrees, 4)
            for offset in range(1, extra + 1)
        ]
        progress(f"resolving the maximum near {peak:g} degrees")
        points, _ = await _driven(
            store,
            structure,
            atoms,
            [value for value in wanted if value not in profile and value not in refined],
            solvent,
            progress,
            run,
        )
        refined.update(points)
    return refined


class _Well(NamedTuple):
    """A released rotamer beside the absolute energies it was derived from.

    Barriers subtract a pass from a well; carrying absolute energies keeps both on one zero, rather
    than mixing constrained scan points with released minima. Internal: absolute Hartrees are not
    for
    `Rotamer`, a published record.
    """

    rotamer: Rotamer
    energy_hartree: float
    gibbs_hartree: float | None


async def _released_wells(
    store: ResultStore,
    structure: Structure,
    atoms: tuple[int, ...],
    profile: dict[float, float],
    torsion: Torsion,
    solvent: str | None,
    temperature: float,
    level: ReactionLevel,
    progress: Progress,
    run: RemoteRunner,
) -> tuple[list[_Well], list[str]]:
    """Turn each well of the profile into a real minimum, then rank and populate them.

    A scan point is the best constrained geometry, not a minimum, and the `structure_id` a chemist
    carries forward must be the minimum. Wells relaxing into one basin are merged and reported.
    Above
    `quick` each survivor gets a Hessian and the ranking is by free energy; the result says which.
    """
    warnings: list[str] = []
    found: list[tuple[float, OptimizationResult, float | None]] = []
    for angle in _minima(profile, torsion.period_degrees):
        progress(f"releasing the well near {angle:g} degrees")
        point, _ = await run(
            cached_remote(
                store,
                "scan_point",
                {
                    "structure": structure.model_dump(mode="json"),
                    "atoms": list(atoms),
                    "value": angle,
                    "solvent": solvent,
                },
            ),
            f"well at {angle:g} degrees",
        )
        constrained = OptimizationResult.model_validate(await kept(point))
        relaxed, _ = await relax(store, constrained.structure, solvent, run=run)
        gibbs = None
        if level != "quick":
            progress(f"free energy of the rotamer near {angle:g} degrees")
            # The refined geometry is the rotamer: `relax_to_minimum` may displace and re-optimize,
            # and the
            # structure, dihedral and energies must all describe the same geometry.
            relaxed, thermo, _ = await relax_to_minimum(
                store,
                relaxed.structure,
                solvent,
                ThermoSettings(temperature_k=temperature),
                run=run,
            )
            gibbs = thermo.gibbs_free_energy_hartree
            if not thermo.is_minimum:
                # The refinement gave up (`xtb_minimum_refinement_attempts`), and a free energy at a
                # saddle is not a
                # free energy. Name the imaginary modes if any, otherwise the gradient: a
                # non-stationary well can
                # report no frequencies at all.
                why = (
                    f"{thermo.imaginary_frequencies_cm} cm^-1"
                    if thermo.imaginary_frequencies_cm
                    else f"max |gradient| {thermo.max_gradient_hartree_per_angstrom} Ha/A"
                )
                warnings.append(
                    f"the rotamer near {angle:g} degrees is still not a minimum after "
                    f"refinement ({why}), so its free energy and any barrier measured from it "
                    "describe a geometry that is not one"
                )
        # Read the angle off the geometry that is actually being kept, and merge on *that* — so a
        # refinement that walked a well into its neighbour is caught rather than recorded twice.
        settled = _dihedral_of(relaxed.structure, atoms) % torsion.period_degrees
        if any(
            _angular_distance(settled, other, torsion.period_degrees)
            < settings.xtb_rotation_merge_degrees
            for other, _, _ in found
        ):
            warnings.append(
                f"the wells near {angle:g} degrees and its neighbour relax into one minimum at "
                f"{settled:.0f} degrees — the coarse profile saw a feature that is not there"
            )
            continue
        found.append((settled, relaxed, gibbs))
    if not found:
        warnings.append(
            "the profile has no interior minimum at this step — it is either flat or too coarsely "
            "sampled to resolve one, and nothing here is a rotamer"
        )
        return [], warnings
    return _ranked(found, torsion, temperature, level), warnings


def _ranked(
    found: list[tuple[float, OptimizationResult, float | None]],
    torsion: Torsion,
    temperature: float,
    level: ReactionLevel,
) -> list[_Well]:
    """Relative energies and populations over the released minima, lowest first.

    Degeneracy is `symmetry_order`: the profile covers one of several identical periods, so each
    well
    stands for that many copies.
    """
    electronic = [relaxed.energy_hartree for _, relaxed, _ in found]
    lowest = min(electronic)
    relative = [(energy - lowest) * HARTREE_TO_KCAL for energy in electronic]
    degeneracies = [torsion.symmetry_order] * len(found)
    gibbs = [value for _, _, value in found]
    by_free_energy = level != "quick" and all(value is not None for value in gibbs)
    relative_g = (
        [(value - min(gibbs)) * HARTREE_TO_KCAL for value in gibbs]  # type: ignore[type-var,operator]
        if by_free_energy
        else [None] * len(found)
    )
    populations = (
        free_energy_populations(gibbs, degeneracies, temperature)  # type: ignore[arg-type]
        if by_free_energy
        else boltzmann_populations(relative, degeneracies, temperature)
    )
    wells = [
        _Well(
            rotamer=Rotamer(
                dihedral_degrees=round(angle, 1),
                structure_id=relaxed.structure.structure_id,
                relative_kcal=round(shift, 2),
                population=round(population, 4),
                degeneracy=torsion.symmetry_order,
                relative_g_kcal=None if shift_g is None else round(shift_g, 2),
            ),
            energy_hartree=relaxed.energy_hartree,
            gibbs_hartree=absolute_g,
        )
        for (angle, relaxed, absolute_g), shift, shift_g, population in zip(
            found, relative, relative_g, populations, strict=True
        )
    ]
    return sorted(wells, key=lambda well: -well.rotamer.population)


def _dihedral_of(structure: Structure, atoms: tuple[int, ...]) -> float:
    """The dihedral these four atoms span in `structure`, in degrees on [0, 360).

    Computed locally; it is four dot products.
    """
    _, positions = structure.arrays()
    first, second, third, fourth = (np.asarray(positions[index]) for index in atoms)
    before, axis, after = second - first, third - second, fourth - third
    normal, other = np.cross(before, axis), np.cross(axis, after)
    angle = math.degrees(
        math.atan2(
            float(np.dot(np.cross(normal, other), axis / np.linalg.norm(axis))),
            float(np.dot(normal, other)),
        )
    )
    return angle % 360.0


def _angular_distance(one: float, other: float, period_degrees: float) -> float:
    """How far apart two angles are on a ring of this period — never more than half of it."""
    gap = abs(one - other) % period_degrees
    return min(gap, period_degrees - gap)


async def _barriers(
    store: ResultStore,
    structure: Structure,
    atoms: tuple[int, ...],
    profile: dict[float, float],
    wells: Sequence[_Well],
    torsion: Torsion,
    solvent: str | None,
    temperature: float,
    level: ReactionLevel,
    progress: Progress,
    run: RemoteRunner,
) -> tuple[list[RotationBarrier], list[str]]:
    """The pass between each adjacent pair of rotamers, in both directions, with its half-life.

    Out of and back into a well differ unless the wells are degenerate; separability depends on the
    barrier out of the populated one. Pass and wells are both absolute Hartree energies, so a
    barrier
    is one subtraction on one zero.

    At `thorough` the pass gets its own Hessian and the barrier becomes a free energy: one imaginary
    mode along the driven coordinate is a first-order saddle, which `_vibrational` skips. With more
    than one, the barrier stays electronic and says so.
    """
    warnings: list[str] = []
    if not wells:
        return [], warnings
    by_angle = sorted(range(len(wells)), key=lambda index: wells[index].rotamer.dihedral_degrees)
    barriers: list[RotationBarrier] = []
    for position, index in enumerate(by_angle):
        following = by_angle[(position + 1) % len(by_angle)]
        peak, top = _highest_between(
            profile,
            wells[index].rotamer.dihedral_degrees,
            wells[following].rotamer.dihedral_degrees,
            torsion.period_degrees,
        )
        if peak is None:
            # Not "no barrier": no *resolved* one. Saying so is the difference between a profile
            # too coarse to see a pass and a bond that turns freely.
            warnings.append(
                f"no scanned point lies between the rotamers at "
                f"{wells[index].rotamer.dihedral_degrees:.0f} and "
                f"{wells[following].rotamer.dihedral_degrees:.0f} degrees, so the barrier between "
                "them is unresolved rather than absent — a smaller step_degrees would settle it"
            )
            continue
        forward, reverse, basis = _pass_energies(wells[index], wells[following], top)
        if level == "thorough":
            progress(f"free energy of the pass at {peak:g} degrees")
            gibbs_forward, gibbs_reverse, gibbs_basis, saddle_modes = await _free_energy_barrier(
                store,
                structure,
                atoms,
                peak,
                solvent,
                temperature,
                wells[index],
                wells[following],
                forward,
                reverse,
                run,
            )
            if gibbs_basis == "E":
                warnings.append(
                    f"the pass at {peak:g} degrees has {saddle_modes} imaginary mode(s) rather "
                    "than the one a first-order saddle has, so its free energy is not a barrier's; "
                    "the electronic barrier is reported instead"
                )
            elif min(gibbs_forward, gibbs_reverse) <= 0.0:
                # Keep the electronic barrier rather than dropping the pass: a saddle's RRHO free
                # energy lacks its
                # imaginary mode's zero-point term, which can invert a small barrier's sign. Report
                # `E` with a
                # warning.
                warnings.append(
                    f"the free-energy barrier at {peak:g} degrees came out non-positive "
                    f"({gibbs_forward:+.2f} forward, {gibbs_reverse:+.2f} reverse kcal/mol), "
                    "which happens when the pass's missing imaginary mode outweighs a small "
                    "electronic barrier; the electronic barrier is reported instead"
                )
            else:
                forward, reverse, basis = gibbs_forward, gibbs_reverse, gibbs_basis
        if min(forward, reverse) <= 0.0:
            # A pass below the well it separates is not a barrier and must not become a rate. It
            # means the
            # profile's maximum and the released minima disagree — usually a well that relaxed out
            # of its basin.
            warnings.append(
                f"the pass at {peak:g} degrees is not above both rotamers it separates "
                f"({forward:+.2f} and {reverse:+.2f} kcal/mol), so no barrier is reported for it: "
                "a released minimum has probably left the basin its scan point was in"
            )
            continue
        barriers.append(
            RotationBarrier(
                from_rotamer=index,
                to_rotamer=following,
                at_degrees=round(peak, 1),
                forward_kcal=round(forward, 2),
                reverse_kcal=round(reverse, 2),
                basis=basis,
                interconversion=half_life_from_barrier(forward, temperature),
            )
        )
    return barriers, warnings


def _pass_energies(
    one: _Well, other: _Well, top_hartree: float
) -> tuple[float, float, Literal["E", "G"]]:
    """The pass height measured from each of the two wells it separates, in kcal/mol.

    From the released rotamer energies, the minimum the molecule is actually in, on one absolute
    zero.
    """
    return (
        (top_hartree - one.energy_hartree) * HARTREE_TO_KCAL,
        (top_hartree - other.energy_hartree) * HARTREE_TO_KCAL,
        "E",
    )


def _highest_between(
    profile: dict[float, float], one: float, other: float, period_degrees: float
) -> tuple[float | None, float]:
    """The highest scanned point strictly between two angles, going the short way round the ring.

    Returns `(angle, its absolute energy in Hartree)`, or `(None, 0.0)` when no point lies between
    them — no resolved pass, not a zero barrier; the caller warns rather than publishing a zero.
    """
    span = [
        (angle, energy)
        for angle, energy in profile.items()
        if _between(angle, one, other, period_degrees)
    ]
    if not span:
        return None, 0.0
    return max(span, key=lambda point: point[1])


def _between(angle: float, one: float, other: float, period_degrees: float) -> bool:
    """Is `angle` on the arc from `one` to `other` that does not pass through the other well?

    A zero-length forward arc means the whole period: two wells at one angle are one well and its
    image a period away, as in a hindered rotor with a single populated form (e.g.
    N,N-dimethylacetamide). Treating that arc as empty would drop the barrier.
    """
    forward = (other - one) % period_degrees or period_degrees
    offset = (angle - one) % period_degrees
    return 0.0 < offset < forward


async def _free_energy_barrier(
    store: ResultStore,
    structure: Structure,
    atoms: tuple[int, ...],
    peak: float,
    solvent: str | None,
    temperature: float,
    one: _Well,
    other: _Well,
    forward: float,
    reverse: float,
    run: RemoteRunner,
) -> tuple[float, float, Literal["E", "G"], int]:
    """The barrier as a free energy — `G(pass) - G(well)` — or the electronic one, saying which.

    Both sides must be free energies; adding only the pass's absolute thermal correction to an
    electronic barrier would be wrong by the molecule's whole thermal term. So this falls back to
    the
    electronic barrier, reported through the returned basis, unless both wells carry their own free
    energy and the pass is a first-order saddle.
    """
    if one.gibbs_hartree is None or other.gibbs_hartree is None:
        return forward, reverse, "E", 0
    point, _ = await run(
        cached_remote(
            store,
            "scan_point",
            {
                "structure": structure.model_dump(mode="json"),
                "atoms": list(atoms),
                "value": peak,
                "solvent": solvent,
            },
        ),
        f"pass geometry at {peak:g} degrees",
    )
    top = OptimizationResult.model_validate(await kept(point))
    matrix, _ = await hessian(store, top.structure, solvent, run=run)
    thermo = await asyncio.to_thread(
        thermochemistry_from_hessian,
        ThermoSettings(temperature_k=temperature),
        top.structure,
        matrix,
    )
    modes = len(thermo.imaginary_frequencies_cm)
    if modes != 1:
        return forward, reverse, "E", modes
    gibbs = thermo.gibbs_free_energy_hartree
    return (
        (gibbs - one.gibbs_hartree) * HARTREE_TO_KCAL,
        (gibbs - other.gibbs_hartree) * HARTREE_TO_KCAL,
        "G",
        modes,
    )


def _profile_warnings(profile: dict[float, float], torsion: Torsion, step: float) -> list[str]:
    """What the profile says about how far to trust itself.

    The three pathologies `skills/conformational-analysis` describes, checked arithmetically over
    the
    points.
    """
    del step
    warnings: list[str] = []
    ring = _wrapped(profile, torsion.period_degrees)
    # Each jump carries the interval it spans, read off the ring: refinement points are spaced
    # `2*step/(refine+1)`, so `angle - step` is usually not in the profile.
    jumps = [
        (
            ring[index - 1][0] % torsion.period_degrees,
            ring[index][0] % torsion.period_degrees,
            (ring[index][1] - ring[index - 1][1]) * HARTREE_TO_KCAL,
        )
        for index in range(1, len(ring))
    ]
    sizes = sorted(abs(jump) for _, _, jump in jumps)
    typical = sizes[len(sizes) // 2]
    lower, upper, biggest = max(jumps, key=lambda jump: abs(jump[2]))
    if (
        typical > 0.0
        and abs(biggest) > settings.xtb_rotation_discontinuity_ratio * typical
        and abs(biggest) > settings.xtb_reaction_uncertainty_kcal
    ):
        warnings.append(
            f"the profile steps {biggest:+.1f} kcal/mol between {lower:g} and {upper:g} degrees, "
            f"against a typical step of {typical:.1f} — a step that far out of line usually means "
            "a point relaxed into a different basin than its neighbours, and it is worth looking "
            "at rather than smoothing over"
        )
    if len(profile) < 4:
        warnings.append(
            f"{len(profile)} points over {torsion.period_degrees:g} degrees is too coarse to "
            "resolve a torsional profile; a smaller step_degrees would say more"
        )
    return warnings
