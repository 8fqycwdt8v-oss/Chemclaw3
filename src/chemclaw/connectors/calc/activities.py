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
from chemclaw.science.calc.postgres_store import default_store

_Result = TypeVar("_Result")


async def _beating(awaitable: Awaitable[_Result], what: str) -> _Result:
    """Await one remote calculation while beating this activity's heartbeat.

    The `RemoteRunner` a composite is handed on the durable path, and the only difference between
    running one here and running one from an MCP tool. The beat interval is derived from the
    configured `heartbeat_timeout` the workflow sets on this activity, so a deployment that shortens
    one shortens the other and the two cannot drift apart.
    """
    return await beating(awaitable, what, settings.xtb_job_heartbeat_timeout_seconds)


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
        interaction = await compose.interaction(
            store,
            spec.smiles_a,
            spec.smiles_b,
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
    raise ValueError(f"unsupported xTB job kind: {spec!r}")
