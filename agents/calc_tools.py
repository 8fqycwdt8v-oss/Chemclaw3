"""Agent tools for the fast calculators (plan step 1c.5).

Exposes cached calculators to the MAF agent as callable tools. Unlike the QM/HPC
path, fast calculators run **inline** (sub-second) — no durable workflow is
needed; the calculation store (Phase 1b) already makes a repeat call free and
idempotent. `default_store` names the production backend and is the seam tests
swap for an in-memory store.
"""

from calc.postgres_store import PostgresStore
from calc.solubility import SolubilityInput, SolubilityResult, run_cached_solubility
from calc.store import ResultStore
from calc.xtb import XtbInput, XtbResult, run_cached_xtb


def default_store() -> ResultStore:
    """Return the production result store (Postgres). Overridden in tests."""
    return PostgresStore()


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
