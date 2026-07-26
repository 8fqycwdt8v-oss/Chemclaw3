"""Behavioral tests for the xTB-based pKa predictor (plan step 1c.4).

Runs real GFN2-xTB solvated calculations. Asserts the chemistry ordering (a
carboxylic acid is more acidic than a phenol) and the store integration, rather
than exact values (a semiempirical estimate with ~1.6 pKa uncertainty).
"""

import asyncio
from importlib.metadata import version

import pytest

from calc.pka import PkaInput, _calc_version, predict_pka, run_cached_pka
from calc.store import InMemoryStore


def test_calc_version_embeds_engine_build() -> None:
    """The pKa cache key carries the tblite and RDKit builds (D-011).

    An engine or geometry-stack upgrade recomputes rather than serving a stale pKa.
    """
    assert version("tblite") in _calc_version()
    assert version("rdkit") in _calc_version()


def test_charged_input_raises() -> None:
    """A net-charged acid is rejected: the v1 calibration covers neutral acids only (G4).

    Protonated nicotinic acid (net +1, true pKa ~2) would otherwise run the acid
    at charge 0 and the conjugate base at -1 — both wrong electron counts — and
    return a silently inverted pKa.
    """
    with pytest.raises(ValueError, match="neutral"):
        predict_pka(PkaInput(smiles="OC(=O)c1cccc[nH+]1"))


def test_pka_is_independent_of_smiles_spelling() -> None:
    """Equivalent spellings predict the same pKa (D-011 determinism).

    The cache key canonicalizes, so the computation must run on the canonical
    form too — before the fix, `CCS` vs `SCC` differed by ~2e-3 pKa units.
    Fresh stores force both spellings to actually compute. tblite's SCF carries
    ~1e-12 run-to-run numerical noise, so assert agreement far below chemical
    significance rather than bitwise equality.
    """

    async def _run() -> None:
        first, _ = await run_cached_pka(InMemoryStore(), PkaInput(smiles="CCS"))
        second, _ = await run_cached_pka(InMemoryStore(), PkaInput(smiles="SCC"))
        assert first.pka == pytest.approx(second.pka, abs=1e-8)
        assert first.smiles == second.smiles  # both report the canonical form

    asyncio.run(_run())


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
