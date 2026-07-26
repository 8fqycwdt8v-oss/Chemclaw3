"""The activity behind the durable xTB job (xTB plan X3/X4).

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
"""

from temporalio import activity

from calc.postgres_store import default_store
from calc.reaction import compare_solvent_effects, compute_reaction_energy
from calc.structure import structure_from_smiles
from calc.xtb_scan import ScanSpec, run_cached_scan
from workflows.models import (
    ReactionJobSpec,
    ScanJobSpec,
    SolventScreenJobSpec,
    XtbJobInput,
    XtbJobResult,
)
from workflows.registry import durable_activity


@durable_activity("hpc")
@activity.defn
async def run_xtb_calculation(job: XtbJobInput) -> XtbJobResult:
    """Run one durable xTB task and return its typed result.

    Dispatches on the spec's `kind`. The `summary` field is written here rather than by
    the caller because this is where the numbers are: a completion push-back and a job
    listing both want one readable line, and deriving it twice from the same result is
    how the two drift apart.
    """
    store = default_store()
    spec = job.spec
    activity.heartbeat(f"starting {spec.kind}")
    if isinstance(spec, ReactionJobSpec):
        reaction = await compute_reaction_energy(
            store,
            spec.reactants,
            spec.products,
            spec.solvent,
            spec.temperature_k,
            spec.level,
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
        structure = structure_from_smiles(spec.smiles, multiplicity=None, optimize=True)
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
    raise ValueError(f"unsupported xTB job kind: {spec!r}")
