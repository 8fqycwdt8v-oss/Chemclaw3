"""Agent tools for the fast calculators (plan step 1c.5).

Exposes cached calculators to the MAF agent as callable tools. Unlike the QM/HPC
path, fast calculators run **inline** (sub-second) — no durable workflow is
needed; the calculation store (Phase 1b) already makes a repeat call free and
idempotent. `default_store` names the production backend and is the seam tests
swap for an in-memory store.
"""

from calc.calibration import (
    Calibration,
    PredictionRecord,
    calibration_for,
    record_observation,
    record_prediction,
)
from calc.pka import PkaInput, PkaResult, run_cached_pka
from calc.postgres_store import PostgresStore
from calc.solubility import SolubilityInput, SolubilityResult, run_cached_solubility
from calc.store import ResultStore
from calc.xtb import XtbInput, XtbResult, run_cached_xtb
from chemclaw.chem import canonical_smiles
from chemclaw.ids import stable_hash


def default_store() -> ResultStore:
    """Return the production result store (Postgres). Overridden in tests."""
    return PostgresStore()


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
    await _log_prediction(
        "solubility", smiles, result.log_s_mol_per_l, result.uncertainty_log, "log S"
    )
    return result


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
    await _log_prediction("pka", smiles, result.pka, result.uncertainty, "pKa")
    return result
