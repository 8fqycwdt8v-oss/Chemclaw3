"""Agent tools for the calculations that submit durable jobs (plan 1c.5, xTB X8).

What is left here after the calculators moved to `mcp_servers/calc` (X8): the five tools that
route to Temporal above a cost threshold, plus the prediction ledger (`report_measurement`,
`calculator_trust`), which records and scores what the calculators claimed rather than computing
anything itself.

The job-routing five stay in-process for one reason, and it is not chemistry — submitting a
durable job needs `require_actor()` and `get_current_session_id()`, the turn's authenticated user
and the conversation to notify. Both are **ambient**, and the F4-T3 rule is that they are never
model-supplied; an MCP server has neither and could only receive them as arguments, which would
make identity a model-authored value.

So these are the tools that *decide and delegate* rather than compute: they price the request
(`calc.xtb_cost`), run it inline when it is cheap, and hand back a job id when it is not. The
computation itself is the same `calc/` code the MCP server hosts.

**`_log_prediction` went with the calculators.** It hooks `predict_pka` and `predict_solubility`,
and those are `mcp-calc` tools since X8 — so the hook lives at *that* tool layer
(`mcp_servers/calc/server.py`), which is still the boundary where a prediction becomes advice.
It needs no ambient identity: the ledger is keyed on the canonical SMILES, not on who asked.
"""

import numpy as np

from agents.tool_registry import tool
from agents.xtb_job_tools import DeferredJob, defer_to_job
from calc.calibration import Calibration, calibration_for, record_observation
from calc.complexes import ComplexSpec, InteractionResult, run_cached_interaction
from calc.conformers import ConformerEnsemble, ConformerSpec, run_cached_ensemble
from calc.crest_cli import CrestEffort, EnsembleSearch
from calc.descriptors import DescriptorInput, DescriptorProfile, run_cached_descriptor_profile
from calc.logd import LogdInput, LogdResult
from calc.logd import predict_logd as _predict_logd
from calc.postgres_store import default_store
from calc.reaction import ReactionEnergyResult as ThermodynamicReactionResult
from calc.reaction import (
    ReactionLevel,
    SolventComparisonResult,
    compare_solvent_effects,
)
from calc.reaction import compute_reaction_energy as _compute_reaction_energy

# Two reaction-energy models now coexist, and they answer different questions rather than
# duplicating one. `calc.reaction_energy` (D-092) is a cached *single-point* exotherm screen
# — no geometry optimization, stoichiometric coefficients, a hazard flag. `calc.reaction`
# (D-098) optimizes every species and adds Hessians, so it reports ΔH/ΔG and refuses an
# unbalanced equation. Both export `ReactionEnergyResult`; the thermodynamic one is aliased
# here so the two names cannot be confused at a call site, which a bare re-export invites.
from calc.reaction_energy import ReactionEnergyInput, ReactionEnergyResult, ReactionSpecies
from calc.reaction_energy import estimate_reaction_energy as _estimate_reaction_energy
from calc.structure import structure_from_smiles
from calc.xtb_cost import (
    ensemble_seconds,
    exceeds_inline_budget,
    reaction_seconds,
    scan_seconds,
)
from calc.xtb_scan import ScanResult, ScanSpec, run_cached_scan
from chemclaw.chem import canonical_smiles
from chemclaw.config import settings
from chemclaw.ids import stable_hash
from workflows.models import (
    ComplexJobSpec,
    EnsembleJobSpec,
    ReactionJobSpec,
    ScanJobSpec,
    SolventScreenJobSpec,
)


@tool
async def report_measurement(property_name: str, smiles: str, measured_value: float) -> str:
    """Record a *measured* property value, so predictions can be scored against reality.

    Call this when a chemist reports an experimental measurement for a property the system also
    predicts (`solubility` as log S, or `pka`). It closes the prediction loop: `calculator_trust`
    then reports how far that calculator has actually been off, instead of the agent having to
    reason about trust from prose.

    Args:
        property_name: Which predicted property was measured — "solubility" or "pka".
        smiles: The molecule measured, as SMILES.
        measured_value: The experimental value, in the property's own unit (log S, or pKa).

    Returns:
        Whether the measurement matched an existing prediction. "No prediction on file" is a normal
        answer — say so rather than implying the measurement was scored.
    """
    canonical = canonical_smiles(smiles)
    matched = await record_observation(
        property_name, stable_hash(canonical), measured_value, source="chemist-reported"
    )
    if matched:
        return f"Recorded; it reconciled {matched} prediction(s) for {canonical}."
    return (
        f"Recorded for {canonical}, but nothing had predicted {property_name} for it yet, "
        "so no prediction was scored."
    )


@tool
async def calculator_trust(property_name: str) -> Calibration:
    """Report how far a calculator's predictions have actually been off, measured not asserted.

    Use this before leaning on a predicted value in an answer, and quote it: "the solubility model
    has run about 0.4 log units low over 18 measurements" is a far more useful caveat than a generic
    "predictions are uncertain".

    Read `n` first. Below the configured minimum the figures are not yet meaningful — say the
    calculator has not been calibrated rather than quoting a bias from three points.
    `uncertainty_coverage` is the subtle one: a low value means the stated error bars are too
    narrow, so the *uncertainty* is misleading even when the values look close.

    Args:
        property_name: "solubility" or "pka".

    Returns:
        Bias, mean absolute error, RMSE, and uncertainty coverage, with the observation count.
    """
    return await calibration_for(
        property_name, unit="log S" if property_name == "solubility" else "pKa"
    )


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
) -> ThermodynamicReactionResult | DeferredJob:
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
async def compute_interaction_energy(
    smiles_a: str,
    smiles_b: str,
    solvent: str | None = None,
    effort: CrestEffort = "quick",
) -> InteractionResult | DeferredJob:
    """Find how two molecules bind to each other, and how strongly (GFN2-xTB + CREST).

    The only tool here that answers a question about **two molecules together**. Use it
    for an API with an excipient, a substrate with a catalyst or additive, a solute with
    a solvent molecule, a host with a guest — anything where the question is association
    rather than reaction. It searches binding modes rather than assuming one, so the
    answer describes how the pair actually arranges itself.

    Read the number with three limits. It is an **energy, not a free energy**: two
    molecules becoming one costs entropy, and that term is not included — a favourable
    interaction energy does not by itself mean the complex exists at room temperature,
    and for weak pairs the missing term is comparable to the whole interaction. The
    search is **stochastic**, so a mode that was not sampled is not reported. And it is
    one isolated pair in a continuum: no bulk, no competing solvent, no stoichiometry
    beyond two.

    Validated against high-level reference values: the water dimer comes out at −4.97
    against a reference −5.0 kcal/mol, ammonia dimer −2.9 against −3.1, methane dimer
    −0.4 against −0.5. Treat magnitudes of a few kcal/mol as meaningful and differences
    below ~0.5 as noise.

    Args:
        smiles_a: The first molecule as a SMILES string.
        smiles_b: The second molecule as a SMILES string.
        solvent: Optional implicit solvent name; omit for gas phase.
        effort: "quick" for screening; raise it when a missed binding mode matters.

    Returns:
        The interaction energy in kcal/mol (negative = bound), how many binding modes
        were found, and the geometry of the best one.
    """
    # Priced on the *pair*, not on the two monomers summed. The search runs over the
    # combined system, and the cost model's exponent is ~3 (D-100) — so two 30-atom
    # partners cost 60^3, roughly four times the 2 x 30^3 that summing them predicts.
    # Under-pricing here would run a minutes-long search inline instead of deferring it.
    predicted = ensemble_seconds(f"{smiles_a}.{smiles_b}")
    if exceeds_inline_budget(predicted):
        return await defer_to_job(
            ComplexJobSpec(smiles_a=smiles_a, smiles_b=smiles_b, solvent=solvent, effort=effort),
            predicted,
        )
    result, _ = await run_cached_interaction(
        default_store(), smiles_a, smiles_b, ComplexSpec(solvent=solvent, effort=effort)
    )
    return result


@tool
async def sample_conformers(
    smiles: str,
    search: EnsembleSearch = "conformers",
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


@tool
async def predict_developability_profile(smiles: str) -> DescriptorProfile:
    """Compute a developability descriptor panel: MW, LogP, TPSA, H-bond counts, Ro5/Veber flags.

    Use this to triage a candidate before committing bench time — Lipinski's Rule-of-Five
    (`lipinski_violations`) and Veber's rule (`veber_pass`) are widely used oral-bioavailability
    heuristics, not developability verdicts. Report them as flags to weigh alongside everything
    else known about the molecule, never as a pass/fail gate on their own. Cached, so repeats
    are free.

    Args:
        smiles: The molecule as a SMILES string.

    Returns:
        The descriptor panel plus the two rule-of-thumb flags.
    """
    result, _ = await run_cached_descriptor_profile(default_store(), DescriptorInput(smiles=smiles))
    return result


@tool
async def predict_logd(smiles: str, ph: float | None = None) -> LogdResult:
    """Predict the pH-dependent distribution coefficient (logD) of a neutral O-H/S-H acid.

    Answers "how lipophilic is this at the pH I actually work at?" — useful for HPLC
    mobile-phase pH selection, extraction, and formulation, where the pH-independent LogP alone
    is not the number that matters. Built from the same acidic-site model as `predict_pka`, so it
    shares its domain limits: only O-H/S-H acids (carboxylic acids, phenols, alcohols, thiols);
    it raises an error for a base or a molecule with no such site rather than guessing.

    Args:
        smiles: The molecule as a SMILES string.
        ph: The pH to evaluate at. Defaults to 7.4 (physiological pH) if omitted.

    Returns:
        logD at the given pH, plus the LogP and pKa it was derived from and the pKa model's
        uncertainty (state it — this is not an exact value).
    """
    return await _predict_logd(default_store(), LogdInput(smiles=smiles, ph=ph))


@tool
async def estimate_reaction_energy(
    reactants: list[ReactionSpecies], products: list[ReactionSpecies]
) -> ReactionEnergyResult:
    """Estimate a reaction's GFN2-xTB electronic energy and flag if it is strongly exothermic.

    A process-safety screening signal, not a validated heat of reaction: it omits entropy,
    solvation beyond xTB's implicit model, and phase changes. Use it the way the structural
    hazard screen is used — to flag attention, never to certify a reaction is safe to scale.
    Each species is a cached xTB single point, so reactions sharing species with an
    earlier one are mostly free to re-score.

    Args:
        reactants: The reactant side, each species with its SMILES, net charge, and
            stoichiometric coefficient (must be a balanced equation — this does not check
            atom/mass balance for you).
        products: The product side, same shape as `reactants`.

    Returns:
        The reaction electronic energy in kcal/mol, whether it crosses the configured
        exotherm threshold, and the threshold itself so the flag can be checked.
    """
    return await _estimate_reaction_energy(
        default_store(), ReactionEnergyInput(reactants=reactants, products=products)
    )
