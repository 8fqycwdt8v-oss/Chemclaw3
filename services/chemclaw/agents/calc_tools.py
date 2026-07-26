"""Agent tools for the calculations that submit durable jobs (plan 1c.5, xTB X8).

What is left here after the calculators moved to `mcp_servers/calc` (X8): the four tools that
route to Temporal above a cost threshold. They stay in-process for one reason, and it is not
chemistry — submitting a durable job needs `require_actor()` and `get_current_session_id()`,
the turn's authenticated user and the conversation to notify. Both are **ambient**, and the
F4-T3 rule is that they are never model-supplied; an MCP server has neither and could only
receive them as arguments, which would make identity a model-authored value.

So these are the tools that *decide and delegate* rather than compute: they price the request
(`calc.xtb_cost`), run it inline when it is cheap, and hand back a job id when it is not. The
computation itself is the same `calc/` code the MCP server hosts.
"""

import numpy as np

from agents.tool_registry import tool
from agents.xtb_job_tools import DeferredJob, defer_to_job
from calc.conformers import ConformerEnsemble, ConformerSpec, run_cached_ensemble
from calc.crest_cli import CrestEffort, CrestSearch
from calc.postgres_store import default_store
from calc.reaction import (
    ReactionEnergyResult,
    ReactionLevel,
    SolventComparisonResult,
    compare_solvent_effects,
)
from calc.reaction import compute_reaction_energy as _compute_reaction_energy
from calc.structure import structure_from_smiles
from calc.xtb_cost import (
    ensemble_seconds,
    exceeds_inline_budget,
    reaction_seconds,
    scan_seconds,
)
from calc.xtb_scan import ScanResult, ScanSpec, run_cached_scan
from chemclaw.config import settings
from workflows.models import EnsembleJobSpec, ReactionJobSpec, ScanJobSpec, SolventScreenJobSpec


@tool
async def scan_coordinate(
    smiles: str,
    atoms: list[int],
    start: float,
    stop: float,
    points: int = 13,
    solvent: str | None = None,
) -> ScanResult | DeferredJob:
    """Map the energy along one bond, angle or torsion while everything else relaxes.

    Answers the shape questions a single optimization cannot: how high is the barrier
    to rotating a bond (an atropisomer that interconverts freely is not a separate
    stereoisomer; one that does not, is), which torsion angles a molecule actually
    adopts, and how the energy rises as a ring closes or a bond stretches.

    Give two atom indices for a bond length (Angstrom), three for an angle or four for
    a torsion (degrees). They must be bonded in sequence. Indices match the heavy atoms
    of the canonical SMILES, with hydrogens following them — check them with
    `compute_electronic_properties` if you are unsure which atom is which.

    The highest point of the profile is an estimate of a rotational barrier, not an
    optimized transition state; for a bond being broken, treat it as a sketch only.

    Args:
        smiles: The molecule as a SMILES string.
        atoms: Two, three or four atom indices defining the coordinate.
        start: First value of the coordinate (Angstrom or degrees).
        stop: Last value of the coordinate.
        points: How many evenly spaced values to compute, `start` to `stop` inclusive.
        solvent: Optional implicit solvent name; omit for gas phase.

    Returns:
        The relaxed energy profile in kcal/mol relative to its own lowest point, the
        coordinate value at that minimum, and the highest point of the profile.
    """
    if points < 2 or points > settings.xtb_scan_max_points:
        raise ValueError(f"points must be between 2 and {settings.xtb_scan_max_points}")
    values = [float(value) for value in np.linspace(start, stop, points)]
    predicted = scan_seconds(smiles, points)
    if exceeds_inline_budget(predicted):
        return await defer_to_job(
            ScanJobSpec(smiles=smiles, atoms=atoms, values=values, solvent=solvent), predicted
        )
    structure = structure_from_smiles(smiles, multiplicity=None, optimize=True)
    spec = ScanSpec(solvent=solvent, atoms=tuple(atoms), values=tuple(values))
    result, _ = await run_cached_scan(default_store(), structure, spec)
    return result


@tool
async def compute_reaction_energy(
    reactants: list[str],
    products: list[str],
    solvent: str | None = None,
    temperature_k: float = 0.0,
    level: ReactionLevel = "standard",
) -> ReactionEnergyResult | DeferredJob:
    """Compute the energy, enthalpy and free energy of a balanced reaction (GFN2-xTB).

    The composite that answers "does this go?". Every species is optimized the same
    way, in the same solvent, and — at `standard` level — given its own frequency
    calculation, so the comparison is internally consistent. List each species once per
    stoichiometric equivalent (two waters is `["O", "O"]`).

    The equation must balance in atoms and charge; an unbalanced one is rejected rather
    than returning a difference that includes the missing atoms. Radicals written with
    explicit radical electrons (`[CH3]`, `[OH]`) are handled, so homolysis and bond
    dissociation energies work.

    A negative ΔG means products are favoured *at equilibrium*. It says nothing about
    rate: there are no transition states here, so a strongly downhill reaction may
    still not happen at room temperature. Quote the reported uncertainty — a
    semiempirical reaction free energy is for comparing related reactions, not for a
    number in a report.

    Args:
        reactants: SMILES of each reactant, repeated per equivalent.
        products: SMILES of each product, repeated per equivalent.
        solvent: Optional implicit solvent name; omit for gas phase.
        temperature_k: Temperature for the thermal corrections; 0 uses 298.15 K.
        level: "standard" gives ΔE, ΔH and ΔG; "quick" optimizes only and gives ΔE.

    Returns:
        The deltas in kcal/mol, the per-species breakdown, how many species were served
        from cache, the method uncertainty, and any warnings about the calculation.
    """
    predicted = reaction_seconds(
        reactants + products, hessian=level != "quick", ensemble=level == "thorough"
    )
    if exceeds_inline_budget(predicted):
        return await defer_to_job(
            ReactionJobSpec(
                reactants=reactants,
                products=products,
                solvent=solvent,
                temperature_k=temperature_k or None,
                level=level,
            ),
            predicted,
        )
    return await _compute_reaction_energy(
        default_store(), reactants, products, solvent, temperature_k or None, level
    )


@tool
async def compare_solvents(
    reactants: list[str],
    products: list[str],
    solvents: list[str],
    temperature_k: float = 0.0,
    level: ReactionLevel = "standard",
) -> SolventComparisonResult | DeferredJob:
    """Rank solvents by how far each pushes the same reaction toward its products.

    Runs the reaction in each solvent plus the gas phase and orders them by free
    energy. Useful for the thermodynamic half of a solvent choice — which medium
    stabilizes the products relative to the starting materials.

    It is an implicit continuum model: it sees the solvent's polarity and nothing else.
    Specific hydrogen bonding, coordination, ion pairing, phase behaviour and
    solubility are invisible, and those often decide a real solvent choice. Check
    `spread_kcal` against the uncertainty before believing an ordering — when the
    solvents span less than the method's error, the calculation has not distinguished
    them and saying so is the correct answer.

    Args:
        reactants: SMILES of each reactant, repeated per equivalent.
        products: SMILES of each product, repeated per equivalent.
        solvents: Implicit solvent names to compare (e.g. ["water", "thf", "toluene"]).
        temperature_k: Temperature for the thermal corrections; 0 uses 298.15 K.
        level: "standard" gives ΔG; "quick" optimizes only and ranks on ΔE.

    Returns:
        One entry per solvent plus the gas phase, most favourable first, with the
        spread across them and a warning when that spread is inside the uncertainty.
    """
    predicted = reaction_seconds(
        reactants + products,
        hessian=level != "quick",
        repeats=len(solvents) + 1,
        ensemble=level == "thorough",
    )
    if exceeds_inline_budget(predicted):
        return await defer_to_job(
            SolventScreenJobSpec(
                reactants=reactants,
                products=products,
                solvents=solvents,
                temperature_k=temperature_k or None,
                level=level,
            ),
            predicted,
        )
    return await compare_solvent_effects(
        default_store(), reactants, products, solvents, temperature_k or None, level
    )


@tool
async def sample_conformers(
    smiles: str,
    search: CrestSearch = "conformers",
    solvent: str | None = None,
    effort: CrestEffort = "quick",
) -> ConformerEnsemble | DeferredJob:
    """Search a molecule's conformers, tautomers or protonation sites (CREST).

    Every other calculation here describes **one** shape of the molecule. This searches
    the space properly by metadynamics and returns what is actually populated, with
    Boltzmann populations at room temperature.

    Choose `search` by the question:
    - "conformers": which 3D shapes the molecule adopts, and in what proportion. Also
      gives the conformational entropy that every single-conformer free energy is missing.
    - "tautomers": which tautomer dominates. Worth asking *first* about any molecule with
      an amide, an enol, or a heterocyclic N-H, because every other number — a pKa, a
      reactivity ranking, a reaction energy — describes whichever tautomer was drawn.
    - "protomers" / "deprotomers": where the molecule protonates or deprotonates, ranked.

    Two things to read carefully. The search is **stochastic**: it samples rather than
    enumerates, so populations are approximate and two runs differ slightly (results are
    cached, so a given molecule stays consistent once computed). And it is by far the
    most expensive calculation available here — minutes for a small molecule, longer for
    a real substrate — so it will usually return a job id rather than a result.

    Args:
        smiles: The molecule as a SMILES string.
        search: Which space to sample.
        solvent: Optional implicit solvent name; omit for gas phase.
        effort: "quick" for screening, "normal" or "extensive" when a missed conformer
            would change the answer.

    Returns:
        The populated members with their relative energies and populations, the
        conformational entropy, and how many were found in total.
    """
    predicted = ensemble_seconds(smiles)
    if exceeds_inline_budget(predicted):
        return await defer_to_job(
            EnsembleJobSpec(smiles=smiles, search=search, solvent=solvent, effort=effort),
            predicted,
        )
    structure = structure_from_smiles(smiles, multiplicity=None, optimize=True)
    spec = ConformerSpec(search=search, solvent=solvent, effort=effort)
    result, _ = await run_cached_ensemble(default_store(), structure, spec)
    return result
