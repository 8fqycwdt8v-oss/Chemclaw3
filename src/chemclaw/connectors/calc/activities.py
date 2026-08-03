"""The activity behind the `calc` connector's durable jobs (xTB plan X3/X4).

One activity, deliberately. Each task routed here is a single call into `calc/` whose
expensive parts — every optimization and every Hessian — are *already* content-addressed
in the calculation store. So a retry after a worker restart re-enters the same function
and walks straight through the work it already did, which is the resumability a fan-out
of per-species activities would buy at the cost of a decomposition the workflow would
have to own.

**These are minute-scale, not second-scale.** On drug-sized molecules (measured:
ibuprofen at 33 atoms takes 19 s to optimize and take a Hessian; cost grows steeply from
there), a multi-species reaction or a solvent screen runs for minutes. Two consequences
are built in here rather than assumed: the activity **heartbeats** between species and
scan points, so a worker that dies is detected in seconds instead of at the hour-long
start-to-close timeout; and each heartbeat carries how far it has got, which is what a
caller watching a long job actually wants to know.

Non-determinism (the store, the SCF, wall-clock cost) lives here and not in the workflow,
which is the standard Temporal split the QM job already follows.

It runs on the bundle's own worker (`chemclaw.connectors.calc.worker`), not core's, and is
registered
there explicitly rather than through `chemclaw.durable.registry` — that registry serves core's two
queues, and a connector's queue is the connector's own business.
"""

import asyncio

from temporalio import activity

from chemclaw.connectors.calc.results import XtbJobResult
from chemclaw.connectors.calc.specs import (
    ComplexJobSpec,
    EnsembleJobSpec,
    ReactionJobSpec,
    ScanJobSpec,
    SolventScreenJobSpec,
    XtbJobSpec,
)
from chemclaw.connectors.queues import bundle_queue
from chemclaw.core.config import settings
from chemclaw.durable.heartbeat import beating
from chemclaw.durable.registry import durable_activity
from chemclaw.science.calc.complexes import ComplexSpec, run_cached_interaction
from chemclaw.science.calc.conformers import ConformerSpec, run_cached_ensemble
from chemclaw.science.calc.postgres_store import default_store
from chemclaw.science.calc.reaction import compare_solvent_effects, compute_reaction_energy
from chemclaw.science.calc.structure import structure_from_smiles
from chemclaw.science.calc.xtb_scan import ScanSpec, run_cached_scan

# The two CREST searches below (`EnsembleJobSpec`, `ComplexJobSpec`) are a single opaque
# subprocess with no unit boundary to report progress at — unlike the other xTB tasks, which
# report *between* units of work (one species, one solvent, one scan point) via `progress=
# activity.heartbeat` directly. `chemclaw.durable.heartbeat.beating` is the shared fix (Conn-F2):
# these are the only two jobs marked `expensive: true`, and their own manifest says a search's
# cost "is not bounded by the input's size" — against `xtb_job_heartbeat_timeout_seconds` (600 s)
# a CREST run over ten minutes used to be declared dead and retried, up to `activity_max_attempts`
# times, each restarting from zero because the store is written only on completion: roughly fifty
# minutes of saturated CPU spent to fail a calculation that would have succeeded.


@durable_activity(bundle_queue("calc"))
@activity.defn
async def run_xtb_calculation(spec: XtbJobSpec) -> XtbJobResult:
    """Run one durable xTB task and return its typed result.

    Dispatches on the spec's `kind`. The `summary` field is written here rather than by
    the caller because this is where the numbers are: a completion push-back and a job
    listing both want one readable line, and deriving it twice from the same result is
    how the two drift apart.
    """
    store = default_store()
    activity.heartbeat(f"starting {spec.kind}")
    if isinstance(spec, ReactionJobSpec):
        reaction = await compute_reaction_energy(
            store,
            spec.reactants,
            spec.products,
            spec.solvent,
            spec.temperature_k,
            spec.level,
            spec.symmetry_numbers,
            progress=activity.heartbeat,
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
        comparison = await compare_solvent_effects(
            store,
            spec.reactants,
            spec.products,
            spec.solvents,
            spec.temperature_k,
            spec.level,
            spec.symmetry_numbers,
            progress=activity.heartbeat,
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
        # Activities here are coroutines on the worker's one event loop (no `activity_executor`),
        # so a synchronous RDKit embed also stalls task polling and heartbeats.
        structure = await asyncio.to_thread(
            structure_from_smiles, spec.smiles, multiplicity=None, optimize=True
        )
        scan_spec = ScanSpec(
            solvent=spec.solvent, atoms=tuple(spec.atoms), values=tuple(spec.values)
        )
        scan, _ = await run_cached_scan(store, structure, scan_spec, progress=activity.heartbeat)
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
        structure = await asyncio.to_thread(
            structure_from_smiles, spec.smiles, multiplicity=None, optimize=True
        )
        ensemble, _ = await beating(
            run_cached_ensemble(
                store,
                structure,
                ConformerSpec(search=spec.search, solvent=spec.solvent, effort=spec.effort),
            ),
            f"{spec.search} of {spec.smiles}",
            settings.xtb_job_heartbeat_timeout_seconds,
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
        interaction, _ = await beating(
            run_cached_interaction(
                store,
                spec.smiles_a,
                spec.smiles_b,
                ComplexSpec(solvent=spec.solvent, effort=spec.effort),
            ),
            f"interaction of {spec.smiles_a} and {spec.smiles_b}",
            settings.xtb_job_heartbeat_timeout_seconds,
        )
        return XtbJobResult(
            kind=spec.kind,
            # Named from the result, not the request: the pair is canonically ordered
            # (`calc.complexes._ordered`) so that either direction is one cache entry, and
            # the summary should describe the calculation that actually ran.
            summary=(
                f"{interaction.smiles_a} + {interaction.smiles_b}: interaction "
                f"{interaction.interaction_energy_kcal:+.1f} kcal/mol over "
                f"{interaction.binding_modes} binding modes"
            ),
            interaction=interaction,
        )
    raise ValueError(f"unsupported xTB job kind: {spec!r}")
