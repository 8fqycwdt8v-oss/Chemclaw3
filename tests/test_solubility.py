"""Behavioral tests for the solubility predictor (plan step 1c.3).

Uses the ESOL baseline (deterministic, no data download). Proves ordering matches
chemistry, invalid input fails fast, and the store integration computes once.
"""

import asyncio

import pytest

from calc.solubility import (
    SolubilityInput,
    predict_solubility,
    run_cached_solubility,
)
from calc.store import InMemoryStore


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
