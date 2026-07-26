"""Agent tools for the fast calculators (plan step 1c.5).

Exposes cached calculators to the MAF agent as callable tools. Unlike the QM/HPC
path, fast calculators run **inline** (sub-second) — no durable workflow is
needed; the calculation store (Phase 1b) already makes a repeat call free and
idempotent. `default_store` names the production backend and is the seam tests
swap for an in-memory store.
"""

import numpy as np

from agents.tool_registry import tool
from agents.xtb_job_tools import DeferredJob, defer_to_job
from calc.conformers import ConformerEnsemble, ConformerSpec, run_cached_ensemble
from calc.crest_cli import CrestEffort, CrestSearch
from calc.pka import PkaInput, PkaResult, run_cached_pka
from calc.postgres_store import default_store
from calc.reaction import (
    ReactionEnergyResult,
    ReactionLevel,
    SolventComparisonResult,
    compare_solvent_effects,
)
from calc.reaction import compute_reaction_energy as _compute_reaction_energy
from calc.solubility import SolubilityInput, SolubilityResult, run_cached_solubility
from calc.structure import structure_from_smiles
from calc.xtb import XtbInput, XtbResult, run_cached_xtb
from calc.xtb_cost import (
    ensemble_seconds,
    exceeds_inline_budget,
    reaction_seconds,
    scan_seconds,
)
from calc.xtb_opt import OptimizationSummary, OptSpec, run_cached_optimization
from calc.xtb_props import (
    ElectronicProperties,
    FukuiMode,
    SiteReactivityResult,
    run_cached_fukui,
    run_cached_properties,
)
from calc.xtb_scan import ScanResult, ScanSpec, run_cached_scan
from calc.xtb_thermo import ThermochemistryResult, ThermoSpec, relax_to_minimum
from chemclaw.config import settings
from workflows.models import EnsembleJobSpec, ReactionJobSpec, ScanJobSpec, SolventScreenJobSpec


@tool
async def compute_xtb_energy(smiles: str, charge: int = 0) -> XtbResult:
    """Compute the GFN2-xTB total energy of a molecule (fast, semiempirical).

    Runs a quick semiempirical single point (no HPC). Results are cached, so
    repeating the same molecule and charge is free and returns instantly.

    Args:
        smiles: The molecule as a SMILES string.
        charge: Net molecular charge (0 = neutral).

    Returns:
        The method, charge, and total energy in Hartree.
    """
    result, _ = await run_cached_xtb(default_store(), XtbInput(smiles=smiles, charge=charge))
    return result


@tool
async def compute_electronic_properties(
    smiles: str, solvent: str | None = None
) -> ElectronicProperties:
    """Compute frontier orbitals, dipole, partial charges and bond orders (GFN2-xTB).

    One fast semiempirical calculation gives the HOMO and LUMO energies and their gap
    (eV), the dipole moment (Debye), Mulliken partial charges per atom, and Wiberg
    bond orders per bonded pair. Use it to compare the electronic character of related
    molecules — a smaller gap means a more easily excited/reactive π system, a larger
    dipole a more polar molecule, and the partial charges show where the electron
    density sits. These are semiempirical values on a force-field geometry: compare
    them across similar structures rather than quoting one as an absolute measurement.
    Cached, so repeats are free.

    Args:
        smiles: The molecule as a SMILES string.
        solvent: Optional implicit solvent name (e.g. "water", "toluene") for an ALPB
            solvated calculation; omit for gas phase.

    Returns:
        The total energy, HOMO/LUMO/gap in eV, dipole in Debye, per-atom charges and
        the bond orders. Atom indices match the heavy atoms of the canonical SMILES,
        with hydrogens following them.
    """
    result, _ = await run_cached_properties(default_store(), smiles, solvent)
    return result


@tool
async def predict_site_reactivity(
    smiles: str, mode: FukuiMode = "electrophilic", top_n: int = 0
) -> SiteReactivityResult:
    """Rank the atoms of a molecule by how susceptible they are to attack (GFN2-xTB).

    Answers regioselectivity questions — which position of a ring is substituted,
    which site is oxidized, where a nucleophile adds — using condensed Fukui indices
    from three fast semiempirical calculations. Choose `mode` by what attacks the
    molecule: "electrophilic" for attack by an electrophile (e.g. aromatic
    nitration/halogenation), "nucleophilic" for attack by a nucleophile (e.g. addition
    to a carbonyl), "radical" for radical chemistry.

    Read the ranking as a hypothesis, not a prediction of yield: it ranks sites
    *within* this molecule only (never between molecules), it describes electronic
    susceptibility alone — sterics, the specific reagent and the solvent are not in
    the model — and a heteroatom often tops the list because of its lone pair, so for
    a ring-substitution question compare the ring carbons with each other. Cached, and
    asking a second mode for the same molecule is free.

    Args:
        smiles: The molecule as a SMILES string. Must be closed-shell (no radicals).
        mode: Which attack to rank for.
        top_n: How many atoms to return, most susceptible first. 0 uses the configured
            default; pass a larger number to see the whole molecule.

    Returns:
        The ranked sites with all three Fukui indices per atom, and the total number
        of atoms the ranking was drawn from. Atom indices match the heavy atoms of the
        canonical SMILES, with hydrogens following them.
    """
    result, _ = await run_cached_fukui(default_store(), smiles, mode)
    limit = top_n if top_n > 0 else settings.xtb_fukui_top_n
    return result.model_copy(update={"sites": result.sites[:limit]})


@tool
async def optimize_geometry(smiles: str, solvent: str | None = None) -> OptimizationSummary:
    """Relax a molecule to its nearest stable 3D shape with GFN2-xTB.

    Every other fast calculation here describes whichever conformer was embedded from
    the SMILES and cleaned up with a force field. This one finds an actual minimum of
    the quantum-mechanical surface, which is what the energy and the frequencies are
    computed on. Use it before comparing energies that need to be trustworthy, and to
    see how far a starting guess was from a real structure — a large `relaxation_kcal`
    on a molecule means the unrelaxed numbers for it were describing a strained shape.

    It finds the *nearest* minimum, not the best one: a flexible molecule has many
    conformers and this relaxes into whichever basin it started in. Cached, so repeats
    are free, and the thermochemistry and reaction tools reuse the same result.

    Args:
        smiles: The molecule as a SMILES string.
        solvent: Optional implicit solvent name (e.g. "water", "thf"); omit for gas phase.

    Returns:
        The converged energy, how much the relaxation lowered it, how far the atoms
        moved, and the id of the resulting geometry.
    """
    structure = structure_from_smiles(smiles, multiplicity=None, optimize=True)
    result, _ = await run_cached_optimization(default_store(), structure, OptSpec(solvent=solvent))
    return OptimizationSummary.of(result)


@tool
async def compute_thermochemistry(
    smiles: str,
    solvent: str | None = None,
    symmetry_number: int = 1,
    temperature_k: float = 0.0,
    top_bands: int = 0,
) -> ThermochemistryResult:
    """Compute vibrational frequencies, an IR spectrum, and free energy (GFN2-xTB).

    Optimizes the molecule, then takes its second derivatives. That gives three things:
    whether the structure is a genuine minimum (`is_minimum`, with any imaginary
    frequencies listed), a predicted IR spectrum with band positions and intensities,
    and ideal-gas thermochemistry — zero-point energy, enthalpy, entropy and Gibbs free
    energy. Use the spectrum to test a proposed structure against a measured one, and
    the free energy for equilibrium questions that an electronic energy cannot answer.

    Read it with three limits in mind. Frequencies are semiempirical and systematically
    a few percent off, so compare *patterns and orderings* with a measured spectrum
    rather than expecting positions to match. Everything describes one conformer, not
    the molecule's real population. And the entropy depends on the rotational symmetry
    number, which defaults to 1 — pass the true value (2 for water, 3 for ammonia, 6
    for ethane, 12 for benzene) when the molecule is symmetric, or the entropy comes
    out too high by R·ln(symmetry number).

    Args:
        smiles: The molecule as a SMILES string.
        solvent: Optional implicit solvent name; omit for gas phase.
        symmetry_number: Rotational symmetry number; 1 if the molecule has no symmetry.
        temperature_k: Temperature for the thermal corrections; 0 uses 298.15 K.
        top_bands: How many IR bands to report, strongest first. 0 uses the configured
            default; imaginary modes are always reported in full.

    Returns:
        Frequencies with IR intensities, whether the geometry is a minimum, and the
        thermochemistry with the uncertainty to quote alongside it.
    """
    structure = structure_from_smiles(smiles, multiplicity=None, optimize=True)
    spec = ThermoSpec(
        solvent=solvent,
        symmetry_number=symmetry_number,
        temperature_k=temperature_k or settings.xtb_thermo_temperature_k,
    )
    _, result, _ = await relax_to_minimum(
        default_store(), structure, OptSpec(solvent=solvent), spec
    )
    limit = top_bands if top_bands > 0 else settings.xtb_ir_bands_top_n
    # The imaginary mode's 3N-vector is refinement machinery, not something a model can
    # read; the frequency itself is already in `imaginary_frequencies_cm`.
    return result.model_copy(
        update={"modes": result.strongest_bands(limit), "imaginary_displacement": None}
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


@tool
async def predict_solubility(smiles: str) -> SolubilityResult:
    """Predict aqueous solubility (log S, mol/L) of a molecule, with uncertainty.

    Uses a fast property model; the result reports an uncertainty that you should
    pass on to the user rather than treating the value as exact. Cached, so repeats
    are free.

    Args:
        smiles: The molecule as a SMILES string.

    Returns:
        The predicted log solubility, its uncertainty, and the model used.
    """
    result, _ = await run_cached_solubility(default_store(), SolubilityInput(smiles=smiles))
    return result


@tool
async def predict_pka(smiles: str) -> PkaResult:
    """Predict the pKa of a molecule's most acidic O-H/S-H site via GFN2-xTB.

    Uses a semiempirical solvated deprotonation-energy method with a linear
    calibration; the result reports an uncertainty (~1.6 pKa units) that you
    should pass on. Only O-H/S-H acids (carboxylic acids, phenols, alcohols,
    thiols) are supported; an error is returned if there is no such site. Cached.

    Args:
        smiles: The molecule as a SMILES string.

    Returns:
        The predicted pKa, the deprotonation energy, and the uncertainty.
    """
    result, _ = await run_cached_pka(default_store(), PkaInput(smiles=smiles))
    return result
