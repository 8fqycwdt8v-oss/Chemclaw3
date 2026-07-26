"""Behavioral tests for the pH-dependent logD calculator (D-092).

Runs real GFN2-xTB (via the reused pKa predictor); asserts the Henderson-Hasselbalch direction
(more acid protonated at low pH → higher logD) and that it inherits `calc.pka`'s domain limits.
"""

import asyncio

import pytest

from calc.logd import LogdInput, predict_logd
from calc.store import InMemoryStore

_BENZOIC_ACID = "OC(=O)c1ccccc1"


def test_logd_defaults_to_configured_ph() -> None:
    """Omitting `ph` uses `settings.logd_default_ph` (7.4), not an arbitrary constant."""
    from chemclaw.config import settings

    async def _run() -> None:
        store = InMemoryStore()
        result = await predict_logd(store, LogdInput(smiles=_BENZOIC_ACID))
        assert result.ph == settings.logd_default_ph

    asyncio.run(_run())


def test_logd_increases_as_ph_drops_below_pka() -> None:
    """Below the pKa the acid is mostly neutral: logD rises toward logP as pH falls."""

    async def _run() -> None:
        store = InMemoryStore()
        physiological = await predict_logd(store, LogdInput(smiles=_BENZOIC_ACID, ph=7.4))
        acidic = await predict_logd(store, LogdInput(smiles=_BENZOIC_ACID, ph=1.0))
        assert acidic.log_d > physiological.log_d
        # Far below the pKa, logD approaches logP (the fully neutral limit).
        assert acidic.log_d == pytest.approx(acidic.clogp, abs=0.05)

    asyncio.run(_run())


def test_logd_reports_pka_uncertainty() -> None:
    """The pKa model's uncertainty is surfaced, not silently dropped."""

    async def _run() -> None:
        store = InMemoryStore()
        result = await predict_logd(store, LogdInput(smiles=_BENZOIC_ACID))
        assert result.uncertainty > 0

    asyncio.run(_run())


def test_logd_rejects_a_molecule_with_no_acidic_site() -> None:
    """No O-H/S-H site → the underlying pKa error propagates unchanged (gate G4)."""

    async def _run() -> None:
        store = InMemoryStore()
        with pytest.raises(ValueError, match="no acidic"):
            await predict_logd(store, LogdInput(smiles="CCCCCC"))

    asyncio.run(_run())


def test_logd_reuses_the_cached_pka() -> None:
    """A second logD call at a different pH does not recompute the xTB pKa."""

    async def _run() -> None:
        from calc.pka import PkaInput, run_cached_pka

        store = InMemoryStore()
        await predict_logd(store, LogdInput(smiles=_BENZOIC_ACID, ph=7.0))
        pka_before, cached_before = await run_cached_pka(store, PkaInput(smiles=_BENZOIC_ACID))
        assert cached_before is True  # already computed by the logD call above

        await predict_logd(store, LogdInput(smiles=_BENZOIC_ACID, ph=3.0))
        pka_after, cached_after = await run_cached_pka(store, PkaInput(smiles=_BENZOIC_ACID))
        assert cached_after is True
        assert pka_after.pka == pka_before.pka

    asyncio.run(_run())
