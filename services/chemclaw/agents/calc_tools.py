"""Agent tools for the fast calculators (plan step 1c.5).

Exposes cached calculators to the MAF agent as callable tools. Unlike the QM/HPC
path, fast calculators run **inline** (sub-second) — no durable workflow is
needed; the calculation store (Phase 1b) already makes a repeat call free and
idempotent. `default_store` names the production backend and is the seam tests
swap for an in-memory store.
"""

from agents.tool_registry import tool
from calc.pka import PkaInput, PkaResult, run_cached_pka
from calc.postgres_store import PostgresStore
from calc.solubility import SolubilityInput, SolubilityResult, run_cached_solubility
from calc.store import ResultStore
from calc.xtb import XtbInput, XtbResult, run_cached_xtb
from calc.xtb_props import (
    ElectronicProperties,
    FukuiMode,
    SiteReactivityResult,
    run_cached_fukui,
    run_cached_properties,
)
from chemclaw.config import settings


def default_store() -> ResultStore:
    """Return the production result store (Postgres). Overridden in tests."""
    return PostgresStore()


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
