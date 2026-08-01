"""Behavioral tests for the solubility predictor (plan step 1c.3).

Uses the ESOL baseline (deterministic, no data download). Proves ordering matches
chemistry, invalid input fails fast, and the store integration computes once.
"""

import asyncio

import pytest

from chemclaw.core.chem import require_canonical_smiles
from chemclaw.science.calc import store as store_module
from chemclaw.science.calc.solubility import (
    CALC_TYPE,
    SolubilityInput,
    calc_version,
    predict_solubility,
    run_cached_solubility,
)
from chemclaw.science.calc.store import CalculationKey, InMemoryStore, StoredResult


def test_ordering_matches_chemistry() -> None:
    """A lipophilic alkane is predicted far less soluble than ethanol."""
    ethanol = predict_solubility(SolubilityInput(smiles="CCO"))
    hexadecane = predict_solubility(SolubilityInput(smiles="CCCCCCCCCCCCCCCC"))
    assert ethanol.log_s_mol_per_l > hexadecane.log_s_mol_per_l
    assert hexadecane.log_s_mol_per_l < -3  # very insoluble
    assert ethanol.uncertainty_log > 0  # uncertainty is always reported


def test_model_label_is_recorded() -> None:
    """The result names the model+version behind the prediction."""
    result = predict_solubility(SolubilityInput(smiles="c1ccccc1"))
    assert result.model == "esol-delaney@2004"


def test_invalid_smiles_raises() -> None:
    """An unparseable SMILES fails fast (gate G4)."""
    with pytest.raises(ValueError, match="invalid SMILES"):
        predict_solubility(SolubilityInput(smiles="%%%not-a-mol%%%"))


def test_the_domain_check_survives_a_cache_hit() -> None:
    """A cached salt still says OUT OF DOMAIN — the flag survives the round trip through a row."""

    async def _run() -> None:
        store = InMemoryStore()
        job = SolubilityInput(smiles="[Na+].[Cl-]")
        fresh, _ = await run_cached_solubility(store, job)
        served, cached = await run_cached_solubility(store, job)
        assert cached is True
        assert fresh.estimate is not None
        assert served.estimate is not None
        assert served.estimate.in_domain is False
        assert served.estimate.domain_reasons == fresh.estimate.domain_reasons

    asyncio.run(_run())


def test_a_row_written_before_the_domain_flag_existed_is_not_served() -> None:
    """The defect: `SolubilityResult` gained `estimate` and no version moved.

    `estimate` is optional, so a payload written before it existed validates back cleanly with
    `estimate=None` — and `Estimate.render` spells `None` as "applicability not assessed". A salt
    the current code refuses to speak about therefore came back looking merely unchecked, forever:
    `durable/retention.py` never prunes `calculation_results`.

    A `calc_version` bump was the wrong lever (that string is also the REV-12 calibration ledger's
    key, and the ESOL calibration was still valid), so the guard is `CALCULATION_EPOCH`, folded
    into every key by `CalculationKey.build`. Here the old row is written under the previous epoch;
    it must be invisible to the current one.
    """

    async def _run() -> None:
        store = InMemoryStore()
        job = SolubilityInput(smiles="[Na+].[Cl-]")
        # The payload as it was persisted before `estimate` existed — same calculator version,
        # because the ESOL arithmetic never changed.
        legacy = {
            "smiles": job.smiles,
            "model": "esol-delaney@2004",
            "log_s_mol_per_l": 0.0,
            "uncertainty_log": 0.75,
        }
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(store_module, "CALCULATION_EPOCH", "before")
            old_key = CalculationKey.build(
                calc_type=CALC_TYPE,
                calc_version=calc_version(),
                inputs={"smiles": require_canonical_smiles(job.smiles)},
            )
        await store.put(StoredResult(key=old_key, result=legacy))

        result, cached = await run_cached_solubility(store, job)

        assert cached is False  # the pre-epoch row is not addressable from here
        assert result.log_s_mol_per_l != 0.0  # ...so this is a fresh prediction, not that row
        assert result.estimate is not None
        assert result.estimate.in_domain is False
        assert "multi-component" in result.estimate.domain_reasons[0]

    asyncio.run(_run())


def test_cached_solubility_computes_once() -> None:
    """A repeat request is served from the store."""

    async def _run() -> None:
        store = InMemoryStore()
        job = SolubilityInput(smiles="CCO")
        first, cached1 = await run_cached_solubility(store, job)
        second, cached2 = await run_cached_solubility(store, job)
        assert cached1 is False
        assert cached2 is True
        assert first.log_s_mol_per_l == second.log_s_mol_per_l

    asyncio.run(_run())
