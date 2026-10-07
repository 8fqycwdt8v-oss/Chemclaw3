"""The activity behind the `calc` connector's durable jobs.

One activity: each task is a single call into `connectors/calc/compose.py`, whose expensive parts
(optimizations, Hessians, CREST searches) are individually content-addressed in the calculation
store, so a retry walks straight through work already done.

Runs are minute-scale and spent inside remote calls to `Chemclaw3-mcp`'s `servers/calc`, so every
remote call is wrapped in `durable/heartbeat.py::beating`; without a heartbeat Temporal would
declare the activity dead and retry it from zero. That wrapper guarantees no exit leaves the
wrapped work running. Composites also report progress between units (species, solvent, scan point)
through the `progress` callback: progress says how far, the heartbeat says alive.

Runs on the bundle's own worker (`chemclaw.connectors.calc.worker`); `chemclaw.durable.registry`
serves core's single queue.
"""

from collections.abc import Awaitable, Iterator, Sequence
from contextlib import contextmanager
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
    MicrostatePkaJobSpec,
    ReactionJobSpec,
    RefinedEnsembleJobSpec,
    RotationJobSpec,
    ScanJobSpec,
    SolventScreenJobSpec,
    SpeciesRankingJobSpec,
    SpeciesSolventScreenJobSpec,
    XtbJobSpec,
)
from chemclaw.connectors.queues import bundle_queue
from chemclaw.core.chem import require_canonical_smiles
from chemclaw.core.config import settings
from chemclaw.core.identity_context import (
    reset_current_correlation_id,
    reset_current_identity,
    set_current_correlation_id,
    set_current_identity,
)
from chemclaw.durable.heartbeat import beating
from chemclaw.durable.registry import durable_activity
from chemclaw.science.calc.models import (
    FailedBond,
    FailedMedium,
    RotationProfile,
    Structure,
    Torsion,
)
from chemclaw.science.calc.postgres_store import default_store
from chemclaw.science.calc.postgres_structures import default_structure_store
from chemclaw.science.calc.structures import require_structure

_Result = TypeVar("_Result")


async def _beating(awaitable: Awaitable[_Result], what: str) -> _Result:
    """Await one remote calculation while beating this activity's heartbeat.

    The `RemoteRunner` composites get on the durable path. The beat interval derives from the
    activity's configured `heartbeat_timeout`, so the two cannot drift apart.
    """
    return await beating(awaitable, what, settings.xtb_job_heartbeat_timeout_seconds)


async def _subject(structure_id: str | None, smiles: str) -> Structure | None:
    """Resolve a geometry handle, checking it is a geometry *of the molecule that was named*.

    An unresolvable handle is reported by `require_structure` (never replaced by a fresh embedding,
    which would silently compute a different conformer). A handle whose stored SMILES canonically
    disagrees with `smiles` is refused, since atom indices, reaction balance and the filed note all
    assume `smiles`. A stored geometry with no SMILES is accepted: the check is on disagreement,
    never
    absence.
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


@contextmanager
def _acting_for(actor: str, correlation_id: str) -> Iterator[None]:
    """Stamp the run's requester and correlation id ambient for the duration of the calculation.

    A worker has no request context, so both arrive as activity arguments (off the run's memo,
    outside
    `spec` so identity cannot change the cache key) and are bound here for
    `connectors.identity.turn_identity_hook`, which `connectors/calc/remote.py::calc_session` hands
    to `core.mcp_session.open_session`. The calc server's logs then name the person a durable run is
    for. Off the durable path both are empty and nothing is stamped. `durable/interceptor.py` reads
    the same two arguments; this bracket covers direct calls with no interceptor.
    """
    if not actor and not correlation_id:
        yield
        return
    identity_token = set_current_identity(actor, frozenset()) if actor else None
    correlation_token = set_current_correlation_id(correlation_id) if correlation_id else None
    try:
        yield
    finally:
        if correlation_token is not None:
            reset_current_correlation_id(correlation_token)
        if identity_token is not None:
            reset_current_identity(identity_token)


@durable_activity(bundle_queue("calc"))
@activity.defn
async def run_xtb_calculation(
    spec: XtbJobSpec, actor: str = "", correlation_id: str = ""
) -> XtbJobResult:
    """Run one durable xTB task and return its typed result.

    Dispatches on the spec's `kind`. `summary` is written here, where the numbers are, as the one
    line
    completion push-backs and job listings share. `calc_refs` is collected around the whole dispatch
    (the collector de-duplicates). `actor` and `correlation_id` come from the run's memo and are
    stamped ambient for every remote call; both default to empty for direct callers.
    """
    with _acting_for(actor, correlation_id), collecting() as calc_refs:
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
        finding = (
            f"only {best} could be computed, so nothing was compared"
            if compose.lost_the_comparison(comparison.failed, len(comparison.effects))
            else (
                f"most favourable of {len(comparison.effects)}: {best} "
                f"(spread {comparison.spread_kcal:.1f} kcal/mol)"
            )
        )
        return XtbJobResult(
            kind=spec.kind,
            summary=finding + _not_computed(comparison.failed, "media"),
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
    if isinstance(spec, RotationJobSpec):
        rotation = await compose.rotation_profile(
            store,
            spec.smiles,
            Torsion(
                torsion_id=spec.torsion.torsion_id,
                atoms=spec.torsion.atoms,
                bond=spec.torsion.bond,
                label=spec.torsion.label,
                symmetry_order=spec.torsion.symmetry_order,
                period_degrees=spec.torsion.period_degrees,
            ),
            subject=await _subject(spec.structure_id, spec.smiles),
            solvent=spec.solvent,
            temperature_k=spec.temperature_k,
            step_degrees=spec.step_degrees,
            level=spec.level,
            progress=activity.heartbeat,
            run=_beating,
        )
        return XtbJobResult(
            kind=spec.kind,
            # The barrier and the lifetime it implies; the band is in the summary because a single
            # half-life
            # otherwise reads like a measurement.
            summary=_rotation_summary(rotation),
            rotation=rotation,
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
    if isinstance(spec, MicrostatePkaJobSpec):
        pka = await compose.microstate_pka(
            store,
            spec.smiles,
            branch=spec.branch,
            solvent=spec.solvent,
            temperature_k=spec.temperature_k,
            effort=spec.effort,
            progress=activity.heartbeat,
            run=_beating,
        )
        equilibrium = "pKa" if pka.branch == "acid" else "pKaH"
        return XtbJobResult(
            kind=spec.kind,
            summary=(
                f"{pka.smiles}: {equilibrium} {pka.pka:.1f} ± {pka.uncertainty:.1f} "
                f"at {pka.site_smiles or 'an unperceived site'}, over {pka.microstates_found} "
                f"microstates"
            ),
            pka=pka,
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
            # Named from the result: the pair is canonically ordered (`compose.py::_ordered`), so
            # the summary
            # describes the calculation that actually ran.
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
            # The coverage ("G-weighted over 5 of 47") is in the summary, the line readers actually
            # see.
            summary=(
                f"{spec.smiles}: {refined.refined_count} of {refined.total_found} conformers "
                f"refined ({refined.refined_population_covered:.0%} of the population), "
                # "lowest", not "dominant": with degeneracy weighting the lowest free energy need
                # not be the most
                # populated member.
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
        # The summary says whether the major form is the same everywhere — "shifts" versus
        # "reorders" —
        # since that changes what every downstream number is about.
        verdict = (
            "the dominant form changes with the medium"
            if screen.dominance_changes
            else f"{screen.distributions[0].dominant.label or 'the same form'} dominates in all"
        )
        only = screen.distributions[0]
        finding = (
            f"{spec.ranking} of {len(spec.species)}: only "
            f"{only.solvent or 'the gas phase'} could be ranked, so nothing was compared"
            if compose.lost_the_comparison(screen.failed, len(screen.distributions))
            else (
                f"{spec.ranking} of {len(spec.species)} across {len(screen.distributions)} "
                f"media: {verdict}, largest swing {screen.largest_swing_kcal:.1f} kcal/mol"
            )
        )
        return XtbJobResult(
            kind=spec.kind,
            summary=finding + _not_computed(screen.failed, "media"),
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
                f"{spec.smiles}: weakest of {len(survey.bonds)} bonds is {weakest.bond} at "
                f"{weakest.dissociation_energy_kcal:.0f} ± {survey.uncertainty_kcal:.0f} kcal/mol"
                + _not_computed(survey.failed, "bonds")
            ),
            bonds=survey,
        )
    raise ValueError(f"unsupported xTB job kind: {spec!r}")


def _not_computed(failed: Sequence[FailedMedium | FailedBond], what: str) -> str:
    """The clause a screen's summary carries when some of its items could not be computed.

    A screen that lost items says so in its summary. A stop by the server's clock is named apart,
    because its remedy (smaller calculation or larger budget) differs from a refused input's.
    """
    if not failed:
        return ""
    stopped = sum(1 for item in failed if item.cause == "time_budget")
    clocked = f", {stopped} stopped by the time budget" if stopped else ""
    return f"; {len(failed)} of the {what} could not be computed{clocked} (see failed)"


def _rotation_summary(rotation: RotationProfile) -> str:
    """One readable line: which bond, how many rotamers, how high the pass and how long it holds."""
    if not rotation.barriers:
        return (
            f"{rotation.smiles}: {rotation.label} has {len(rotation.rotamers)} rotamer(s) and no "
            "resolved barrier between them"
        )
    highest = max(rotation.barriers, key=lambda barrier: barrier.forward_kcal)
    lifetime = highest.interconversion
    span = (
        ""
        if lifetime is None
        else (
            f", t1/2 {_readable(lifetime.half_life_seconds)} "
            f"({_readable(lifetime.half_life_seconds_fastest)} to "
            f"{_readable(lifetime.half_life_seconds_slowest)} at "
            f"±{lifetime.uncertainty_kcal:.0f} kcal/mol)"
        )
    )
    return (
        f"{rotation.smiles}: {rotation.label}, {len(rotation.rotamers)} rotamers, highest barrier "
        f"{highest.forward_kcal:.1f} kcal/mol ({highest.basis}){span}"
    )


def _readable(seconds: float) -> str:
    """A duration in the unit a chemist would say it in — seconds to years, one significant step.

    Half-lives from barriers span many orders of magnitude; "12 hours" is actionable, `4.36e+04 s`
    is not.
    """
    for limit, divisor, unit in (
        (90.0, 1.0, "s"),
        (5400.0, 60.0, "min"),
        (172800.0, 3600.0, "h"),
        (63072000.0, 86400.0, "days"),
    ):
        if seconds < limit:
            return f"{seconds / divisor:.3g} {unit}"
    return f"{seconds / 31557600.0:.3g} years"
