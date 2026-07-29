"""Behavioral tests for the pH-dependent logD calculator (D-092).

Runs real GFN2-xTB (via the reused pKa predictor); asserts the Henderson-Hasselbalch direction
(more acid protonated at low pH → higher logD) and that it inherits `chemclaw.science.calc.pka`'s
domain limits.
"""

import asyncio

import pytest

from chemclaw.science.calc.logd import LogdInput, predict_logd
from chemclaw.science.calc.store import InMemoryStore

_BENZOIC_ACID = "OC(=O)c1ccccc1"


def test_logd_defaults_to_configured_ph() -> None:
    """Omitting `ph` uses `settings.logd_default_ph` (7.4), not an arbitrary constant."""
    from chemclaw.core.config import settings

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
        from chemclaw.science.calc.pka import PkaInput, run_cached_pka

        store = InMemoryStore()
        await predict_logd(store, LogdInput(smiles=_BENZOIC_ACID, ph=7.0))
        pka_before, cached_before = await run_cached_pka(store, PkaInput(smiles=_BENZOIC_ACID))
        assert cached_before is True  # already computed by the logD call above

        await predict_logd(store, LogdInput(smiles=_BENZOIC_ACID, ph=3.0))
        pka_after, cached_after = await run_cached_pka(store, PkaInput(smiles=_BENZOIC_ACID))
        assert cached_after is True
        assert pka_after.pka == pka_before.pka

    asyncio.run(_run())


def test_a_base_is_corrected_in_the_other_direction() -> None:
    """Henderson-Hasselbalch runs the opposite way for a base, and the sign is everything.

    A cross-branch regression, invisible to either side alone. `chemclaw.science.calc.logd` was
    written when
    `chemclaw.science.calc.pka` covered acids only, so it hard-coded the acid form
    `logD = clogP - log10(1 + 10**(pH - pKa))`. X11 widened the predictor to aromatic and
    aryl nitrogen, and pyridine — which previously *raised* — began flowing into that
    formula as though it were an acid.

    Measured: pyridine (pKaH 5.4) at pH 7.4 came out at -0.92 against a clogP of 1.08, two
    full log units too lipophobic, and nothing raised. A base two units *below* the working
    pH is essentially all neutral, so its logD must be its clogP — which is the assertion.
    """

    async def _run() -> None:
        from rdkit import Chem
        from rdkit.Chem import Crippen

        result = await predict_logd(InMemoryStore(), LogdInput(smiles="c1ccncc1", ph=7.4))
        mol = Chem.MolFromSmiles(result.smiles)
        assert mol is not None
        assert result.pka < 6.5  # a weak base, well below the working pH
        assert result.log_d == pytest.approx(Crippen.MolLogP(mol), abs=0.05)

    asyncio.run(_run())


def test_an_aliphatic_amine_is_refused_rather_than_given_a_logd() -> None:
    """The refusal propagates: no pKa means no pH correction, so no logD (gate G4).

    `chemclaw.science.calc.pka` declines aliphatic amines because it cannot rank them at all, and a
    logD
    built on a number that does not exist would be a plausible-looking product of two
    guesses rather than one.
    """

    async def _run() -> None:
        with pytest.raises(ValueError, match="aliphatic nitrogen"):
            await predict_logd(InMemoryStore(), LogdInput(smiles="C1CCNCC1", ph=7.4))

    asyncio.run(_run())
