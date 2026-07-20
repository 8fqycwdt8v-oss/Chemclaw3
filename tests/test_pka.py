"""Behavioral tests for the xTB-based pKa predictor (plan step 1c.4).

Runs real GFN2-xTB solvated calculations. Asserts the chemistry ordering (a
carboxylic acid is more acidic than a phenol) and the store integration, rather
than exact values (a semiempirical estimate with ~1.6 pKa uncertainty).
"""

import asyncio

import pytest

from calc.pka import PkaInput, predict_pka, run_cached_pka
from calc.store import InMemoryStore


def test_acid_ordering_is_physical() -> None:
    """Acetic acid is predicted more acidic (lower pKa) than phenol."""
    acetic = predict_pka(PkaInput(smiles="CC(=O)O"))
    phenol = predict_pka(PkaInput(smiles="Oc1ccccc1"))
    assert acetic.pka < phenol.pka
    # Both land in a sane window for a semiempirical estimate.
    assert 1.0 < acetic.pka < 9.0
    assert 6.0 < phenol.pka < 14.0
    assert acetic.uncertainty > 0


def test_no_acidic_site_raises() -> None:
    """A molecule with no O-H/S-H proton has nothing to deprotonate (gate G4)."""
    with pytest.raises(ValueError, match="no acidic"):
        predict_pka(PkaInput(smiles="c1ccccc1"))


def test_invalid_smiles_raises() -> None:
    """An unparseable SMILES fails fast."""
    with pytest.raises(ValueError, match="invalid SMILES"):
        predict_pka(PkaInput(smiles="?!not-a-mol"))


def test_cached_pka_computes_once() -> None:
    """A repeat request is served from the store, not recomputed."""

    async def _run() -> None:
        store = InMemoryStore()
        job = PkaInput(smiles="CC(=O)O")
        first, cached1 = await run_cached_pka(store, job)
        second, cached2 = await run_cached_pka(store, job)
        assert cached1 is False
        assert cached2 is True
        assert first.pka == second.pka

    asyncio.run(_run())
