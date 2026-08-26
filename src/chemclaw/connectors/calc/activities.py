"""The activity behind the `calc` connector's durable jobs (xTB plan X3/X4).

One activity, deliberately. Each task routed here is a single call into
`connectors/calc/compose.py`, whose expensive parts — every optimization, every Hessian, every
CREST search — are individually content-addressed in the calculation store. So a retry after a
worker restart re-enters the same function and walks straight through the work it already did,
which is the resumability a fan-out of per-species activities would buy at the cost of a
decomposition the workflow would have to own.

**These are minute-scale, not second-scale.** On drug-sized molecules a multi-species reaction or a
solvent screen runs for minutes, and after
`D-2026-08-16-the-physics-leaves-the-cache-stays` every one of those minutes is spent inside a
*remote* call: the physics is in `Chemclaw3-mcp`'s `servers/calc` and this side composes the parts
and caches them.

**Which is why every remote call here is wrapped in `durable/heartbeat.py::beating`.** A blocking
call with no heartbeat is an activity Temporal declares dead: against
`xtb_job_heartbeat_timeout_seconds` a longer run is retried from zero, up to `activity_max_attempts`
times, each restarting from whatever the cache already holds — and before the split that cost
roughly fifty minutes of saturated CPU to fail a CREST search that would have succeeded. That
wrapper was extracted for exactly this shape ("one opaque call with nothing finer to report than
*still running*") from the CREST subprocess, the HPC poll and the BoFire fit; a remote computation
is the fourth instance. Its guarantee — **no exit from the wrapper leaves the wrapped work
running** — is what makes a dropped connection safe rather than a detached write.

The composites still report progress *between* units of work (one species, one solvent, one scan
point) through the `progress` callback, because that is a real boundary and "still running" is the
weaker signal where a better one exists. The two are complementary: `progress` says how far, the
heartbeat timer says alive.

Non-determinism (the store, the wire, wall-clock cost) lives here and not in the workflow, which is
the standard Temporal split the QM job already follows.

It runs on the bundle's own worker (`chemclaw.connectors.calc.worker`), not core's, and is
registered there explicitly rather than through `chemclaw.durable.registry` — that registry serves
core's two queues, and a connector's queue is the connector's own business.
"""

from collections.abc import Awaitable
from typing import TypeVar

from temporalio import activity

from chemclaw.connectors.calc import compose
from chemclaw.connectors.calc.remote import collecting
from chemclaw.connectors.calc.results import XtbJobResult
from chemclaw.connectors.calc.specs import (
    BondSurveyJobSpec,
    ComplexJobSpec,
    EnsembleJobSpec,
    EnsemblePropertyJobSpec,
    ReactionJobSpec,
    RefinedEnsembleJobSpec,
    ScanJobSpec,
    SolventScreenJobSpec,
    SpeciesRankingJobSpec,
    SpeciesSolventScreenJobSpec,
    XtbJobSpec,
)
from chemclaw.connectors.queues import bundle_queue
from chemclaw.core.chem import require_canonical_smiles
from chemclaw.core.config import settings
from chemclaw.durable.heartbeat import beating
from chemclaw.durable.registry import durable_activity
from chemclaw.science.calc.models import Structure
from chemclaw.science.calc.postgres_store import default_store
from chemclaw.science.calc.postgres_structures import default_structure_store
from chemclaw.science.calc.structures import require_structure

_Result = TypeVar("_Result")


async def _beating(awaitable: Awaitable[_Result], what: str) -> _Result:
    """Await one remote calculation while beating this activity's heartbeat.

    The `RemoteRunner` a composite is handed on the durable path, and the only difference between
    running one here and running one from an MCP tool. The beat interval is derived from the
    configured `heartbeat_timeout` the workflow sets on this activity, so a deployment that shortens
    one shortens the other and the two cannot drift apart.
    """
    return await beating(awaitable, what, settings.xtb_job_heartbeat_timeout_seconds)


async def _subject(structure_id: str | None, smiles: str) -> Structure | None:
    """Resolve a geometry handle, checking it is a geometry *of the molecule that was named*.

    Two failures, both silent without this, both reported as a value the caller can act on
    (D-2026-08-21-a-geometry-is-an-address-not-a-payload):

    - **An unresolvable handle.** `require_structure` says so and names what to re-run. Answering
      by falling back to a fresh embedding would be the worst outcome available — the chemist chose
      a conformer, and the calculation would silently be about a different one.
    - **A handle for the wrong molecule.** A `structure_id` addresses a geometry, not a compound,
      so nothing about the id itself says which molecule it is of; a scan's atom indices, a
      reaction's balance and the note the result is filed under all assume `smiles`. The two are
      compared canonically, so `CCO` and `OCC` agree and a genuinely different molecule does not.

    A stored geometry with **no** SMILES is accepted rather than refused: `Structure.smiles` is
    optional by construction and a structure that came from a route which did not record one is
    still the geometry the caller asked for. That is a stated trade, not an oversight — the check
    is on a disagreement, never on an absence.
    """
    if structure_id is None:
        return None
    structure = await require_structure(default_structure_store(), structure_id)
    named = require_canonical_smiles(smiles)
    if structure.smiles is not None and require_canonical_smiles(structure.smiles) != named:
        raise ValueError(
            f"{structure_id!r} is a geometry of {structure.smiles!r}, not of {smiles!r}. "
            "A structure id addresses one 3D geometry; use one reported by a calculation on the "
            "molecule you are asking about."
        )
    return structure


@durable_activity(bundle_queue("calc"))
@activity.defn
async def run_xtb_calculation(spec: XtbJobSpec) -> XtbJobResult:
    """Run one durable xTB task and return its typed result.

    Dispatches on the spec's `kind`. The `summary` field is written here rather than by
    the caller because this is where the numbers are: a completion push-back and a job
    listing both want one readable line, and deriving it twice from the same result is
    how the two drift apart.

    `calc_refs` is collected around the whole dispatch rather than per branch, because every branch
    wants it and the collector already de-duplicates: a solvent screen reaching the same relaxation
    in five media cites it once.
    """
    with collecting() as calc_refs:
        result = await _dispatch(spec)
    return result.model_copy(update={"calc_refs": calc_refs})


async def _dispatch(spec: XtbJobSpec) -> XtbJobResult:
    """Run the calculation the spec asks for. The activity's body, without its bookkeeping."""
    store = default_store()
    activity.heartbeat(f"starting {spec.kind}")
    if isinstance(spec, ReactionJobSpec):
        reaction = await compose.reaction_energy(
            store,
            spec.reactants,
            spec.products,
            spec.solvent,
            spec.temperature_k,
            spec.level,
            spec.symmetry_numbers,
            progress=activity.heartbeat,
            run=_beating,
        )
        delta = (
            reaction.delta_g_kcal if reaction.delta_g_kcal is not None else reaction.delta_e_kcal
        )
        label = "dG" if reaction.delta_g_kcal is not None else "dE"
        return XtbJobResult(
            kind=spec.kind,
            summary=(
                f"{' + '.join(spec.reactants)} -> {' + '.join(spec.products)}: "
                f"{label} {delta:+.1f} ± {reaction.uncertainty_kcal:.1f} kcal/mol"
            ),
            reaction=reaction,
        )
    if isinstance(spec, SolventScreenJobSpec):
        comparison = await compose.solvent_comparison(
            store,
            spec.reactants,
            spec.products,
            spec.solvents,
            spec.temperature_k,
            spec.level,
            spec.symmetry_numbers,
            progress=activity.heartbeat,
            run=_beating,
        )
        best = comparison.best_solvent or "gas phase"
        return XtbJobResult(
            kind=spec.kind,
            summary=(
                f"most favourable of {len(comparison.effects)}: {best} "
                f"(spread {comparison.spread_kcal:.1f} kcal/mol)"
            ),
            solvents=comparison,
        )
    if isinstance(spec, ScanJobSpec):
        scan = await compose.scan_profile(
            store,
            spec.smiles,
            tuple(spec.atoms),
            tuple(spec.values),
            spec.solvent,
            subject=await _subject(spec.structure_id, spec.smiles),
            progress=activity.heartbeat,
            run=_beating,
        )
        return XtbJobResult(
            kind=spec.kind,
            summary=(
                f"{scan.coordinate} scan of {spec.smiles}: minimum at "
                f"{scan.minimum_value:g} {scan.unit}, highest point "
                f"{scan.maximum_relative_kcal:.1f} kcal/mol above it"
            ),
            scan=scan,
        )
    if isinstance(spec, EnsembleJobSpec):
        ensemble, _ = await compose.conformer_ensemble(
            store,
            spec.smiles,
            subject=await _subject(spec.structure_id, spec.smiles),
            search=spec.search,
            effort=spec.effort,
            solvent=spec.solvent,
            run=_beating,
        )
        return XtbJobResult(
            kind=spec.kind,
            summary=(
                f"{spec.search} of {spec.smiles}: {ensemble.total_found} found, lowest at "
                f"{ensemble.conformers[0].population:.0%} population"
            ),
            ensemble=ensemble,
        )
    if isinstance(spec, ComplexJobSpec):
        pair = (
            await _subject(spec.structure_id_a, spec.smiles_a),
            await _subject(spec.structure_id_b, spec.smiles_b),
        )
        interaction = await compose.interaction(
            store,
            spec.smiles_a,
            spec.smiles_b,
            # The spec refuses a half-specified pair, so either both are resolved or both are None.
            subjects=None if pair[0] is None or pair[1] is None else (pair[0], pair[1]),
            effort=spec.effort,
            solvent=spec.solvent,
            run=_beating,
        )
        return XtbJobResult(
            kind=spec.kind,
            # Named from the result, not the request: the pair is canonically ordered
            # (`connectors/calc/compose.py::_ordered`) so that either direction is one cache entry,
            # and the summary should describe the calculation that actually ran.
            summary=(
                f"{interaction.smiles_a} + {interaction.smiles_b}: interaction "
                f"{interaction.interaction_energy_kcal:+.1f} kcal/mol over "
                f"{interaction.binding_modes} binding modes"
            ),
            interaction=interaction,
        )
    if isinstance(spec, RefinedEnsembleJobSpec):
        refined = await compose.refined_ensemble(
            store,
            spec.smiles,
            subject=await _subject(spec.structure_id, spec.smiles),
            solvent=spec.solvent,
            temperature_k=spec.temperature_k,
            top_n=spec.top_n,
            progress=activity.heartbeat,
            run=_beating,
        )
        lowest = refined.conformers[0]
        return XtbJobResult(
            kind=spec.kind,
            # The coverage is in the one-line summary rather than only in the payload, because that
            # line is what a completion push-back and a job listing show — and "G-weighted over 5 of
            # 47" is exactly the qualifier a reader would otherwise never see.
            summary=(
                f"{spec.smiles}: {refined.refined_count} of {refined.total_found} conformers "
                f"refined ({refined.refined_population_covered:.0%} of the population), "
                # "lowest", not "dominant": `conformers[0]` is the lowest *free energy*, and with
                # degeneracy weighting that need not be the most populated member — a two-rotamer
                # conformer 0.3 kcal/mol up outranks it.
                f"lowest free energy at {lowest.population:.0%}"
            ),
            refined=refined,
        )
    if isinstance(spec, EnsemblePropertyJobSpec):
        averaged = await compose.ensemble_property(
            store,
            spec.smiles,
            prop=spec.prop,
            solvent=spec.solvent,
            temperature_k=spec.temperature_k,
            max_members=spec.max_members,
            progress=activity.heartbeat,
            run=_beating,
        )
        detail = (
            f"{averaged.value.mean:.3g} (spread {averaged.value.spread:.3g})"
            if averaged.value is not None
            else f"{len(averaged.per_atom)} atoms"
        )
        return XtbJobResult(
            kind=spec.kind,
            summary=(
                f"{spec.smiles}: {spec.prop} over {averaged.members_averaged} conformers = {detail}"
            ),
            averaged=averaged,
        )
    if isinstance(spec, SpeciesRankingJobSpec):
        labels = spec.labels or [""] * len(spec.species)
        distribution = await compose.species_ranking(
            store,
            list(zip(spec.species, labels, strict=True)),
            kind=spec.ranking,
            solvent=spec.solvent,
            temperature_k=spec.temperature_k,
            level=spec.level,
            symmetry_numbers=spec.symmetry_numbers,
            progress=activity.heartbeat,
            run=_beating,
        )
        dominant = distribution.dominant
        return XtbJobResult(
            kind=spec.kind,
            summary=(
                f"{spec.ranking} of {len(distribution.species)}: "
                f"{dominant.label or dominant.smiles} dominates at {dominant.population:.0%}"
            ),
            distribution=distribution,
        )
    if isinstance(spec, SpeciesSolventScreenJobSpec):
        labels = spec.labels or [""] * len(spec.species)
        screen = await compose.species_solvent_comparison(
            store,
            list(zip(spec.species, labels, strict=True)),
            spec.solvents,
            kind=spec.ranking,
            temperature_k=spec.temperature_k,
            level=spec.level,
            symmetry_numbers=spec.symmetry_numbers,
            progress=activity.heartbeat,
            run=_beating,
        )
        # The summary is what a completion push-back and a job listing show, so it carries the one
        # finding that changes what every downstream number is about: whether the major form is the
        # same everywhere. "shifts" and "reorders" are different answers and a reader must not have
        # to open the payload to tell which happened.
        verdict = (
            "the dominant form changes with the medium"
            if screen.dominance_changes
            else f"{screen.distributions[0].dominant.label or 'the same form'} dominates in all"
        )
        return XtbJobResult(
            kind=spec.kind,
            summary=(
                f"{spec.ranking} of {len(spec.species)} across {len(screen.distributions)} "
                f"media: {verdict}, largest swing {screen.largest_swing_kcal:.1f} kcal/mol"
            ),
            species_solvents=screen,
        )
    if isinstance(spec, BondSurveyJobSpec):
        survey = await compose.bond_dissociation_survey(
            store,
            spec.smiles,
            [
                ((cleavage.atoms[0], cleavage.atoms[1]), cleavage.bond, cleavage.fragments)
                for cleavage in spec.cleavages
            ],
            solvent=spec.solvent,
            temperature_k=spec.temperature_k,
            level=spec.level,
            progress=activity.heartbeat,
            run=_beating,
        )
        weakest = survey.bonds[0]
        return XtbJobResult(
            kind=spec.kind,
            summary=(
                f"{spec.smiles}: weakest of {survey.considered} bonds is {weakest.bond} at "
                f"{weakest.dissociation_energy_kcal:.0f} ± {survey.uncertainty_kcal:.0f} kcal/mol"
            ),
            bonds=survey,
        )
    raise ValueError(f"unsupported xTB job kind: {spec!r}")
