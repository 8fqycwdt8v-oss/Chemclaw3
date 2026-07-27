"""mcp-calc: the fast calculators as an MCP capability server (xTB plan X8).

**Why these tools live in a separate process.** The calculators carry the heavy half of this
system's dependency closure — RDKit, tblite, scipy, and the `xtb`/`crest` binaries — and the
CPU load to match. Hosting them behind MCP lets that scale on its own pod, independently of
the agent, which is the whole point: an optimization that saturates a core should not be
competing with a conversation for one.

**What is deliberately *not* here, and the rule it comes from.** The tools that submit durable
jobs — `compute_reaction_energy`, `compare_solvents`, `scan_coordinate`, `sample_conformers` —
stay in-process, and `run_xtb_task` with them. Submitting a job needs `require_actor()` and
`get_current_session_id()`: the authenticated user and the conversation to notify, both
**ambient to the turn** and, by the F4-T3 reject-if-absent rule, never model-supplied. An MCP
server is a separate process with no conversation and no authenticated user, so the only way to
give it those would be as tool *arguments* — which would make identity a model-authored value,
precisely what that rule exists to prevent.

So the line is: **MCP carries capability, the agent keeps identity.** It also predicts what can
ever move here — anything that computes, nothing that authorizes.

Thin, like `molfp` and `rxnfp`: every body below already lives in `calc/`, and this file is
transport. Judgment stays out (G6) — `calculation-selection` decides *which* calculator answers
a question, `reactivity-descriptors` how to read a ranking. Run as
`python -m mcp_servers.calc.server` (stdio), or as its own pod via `CHEMCLAW_COMPONENT=mcp-calc`.
"""

from mcp.server.fastmcp import FastMCP

from calc.calibration import PredictionRecord, record_prediction
from calc.pka import PkaInput, PkaResult, run_cached_pka
from calc.postgres_store import default_store
from calc.solubility import SolubilityInput, SolubilityResult, run_cached_solubility
from calc.structure import structure_from_smiles
from calc.xtb import XtbInput, XtbResult, run_cached_xtb
from calc.xtb_opt import OptimizationSummary, OptSpec, run_cached_optimization
from calc.xtb_props import (
    ElectronicProperties,
    FukuiMode,
    SiteReactivityResult,
    run_cached_fukui,
    run_cached_properties,
)
from calc.xtb_thermo import ThermochemistryResult, ThermoSpec, relax_to_minimum
from chemclaw.chem import canonical_smiles
from chemclaw.config import settings
from chemclaw.ids import stable_hash

server = FastMCP("mcp-calc")


@server.tool()
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


@server.tool()
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


@server.tool()
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


@server.tool()
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


@server.tool()
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


async def _log_prediction(
    calc_type: str, smiles: str, value: float, uncertainty: float | None, unit: str
) -> None:
    """Record a prediction for later reconciliation against a measurement (gap IDEA-2).

    Hooked at the *tool* layer rather than inside the calculators, because this is the boundary
    where a prediction becomes advice a chemist acts on — a cache hit deep in a workflow does not
    need re-logging, and the ledger is keyed on the input, not on how often it was read.

    The subject key is the canonical SMILES, the same identity the calculation cache uses, so a
    measurement of the same molecule meets its prediction without a second naming scheme.
    """
    canonical = canonical_smiles(smiles)
    await record_prediction(
        PredictionRecord(
            calc_type=calc_type,
            input_hash=stable_hash(canonical),
            subject=canonical,
            predicted_value=value,
            predicted_uncertainty=uncertainty,
            unit=unit,
        )
    )


@server.tool()
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
    await _log_prediction(
        "solubility", smiles, result.log_s_mol_per_l, result.uncertainty_log, "log S"
    )
    return result


@server.tool()
async def predict_pka(smiles: str) -> PkaResult:
    """Predict a molecule's pKa via GFN2-xTB — an acid site, or a base's conjugate acid.

    Two domains with different accuracy, and `site` on the result says which one ran.
    **Acids** (`site="acid"`): the most acidic O-H/S-H proton — carboxylic acids, phenols,
    alcohols, thiols — reported with ~1.6 units of uncertainty. **Bases** (`site="base"`),
    when there is no acidic proton: the pKa of the *conjugate acid* (pKaH), the number
    tabulated for amines, reported with +/-1.0. An acid site wins when a molecule has both.

    Base coverage is **aromatic and aryl nitrogen only** — pyridines, imidazoles, azoles,
    anilines. Aliphatic amines raise instead of returning a value, and that refusal is
    load-bearing rather than cautious: over 13 reference amines the method ranks them at
    Spearman -0.17, because a continuum solvent cannot represent the ammonium ion's hydrogen
    bonding to water. Report that the value is not predictable rather than substituting
    another tool's output. Cached.

    Args:
        smiles: The molecule as a SMILES string.

    Returns:
        The predicted pKa, which site it describes, the protonation/deprotonation energy,
        and the uncertainty.
    """
    result, _ = await run_cached_pka(default_store(), PkaInput(smiles=smiles))
    await _log_prediction("pka", smiles, result.pka, result.uncertainty, "pKa")
    return result


def main() -> None:
    """Run the server over stdio (the default MCP transport)."""
    server.run()


if __name__ == "__main__":
    main()
