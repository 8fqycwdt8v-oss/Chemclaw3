"""Behavioral tests for the GFN2-xTB calculator (plan step 1c.2).

Runs real semiempirical calculations (tblite is a pip dependency, no HPC), and
proves the store integration computes once and reuses thereafter.
"""

import asyncio

import pytest

from calc.store import InMemoryStore
from calc.xtb import XtbInput, run_cached_xtb, run_xtb


def test_water_energy_is_physical() -> None:
    """A GFN2-xTB single point on water gives its known ballpark energy."""
    result = run_xtb(XtbInput(smiles="O"))
    assert result.method == "GFN2-xTB"
    # GFN2-xTB water total energy is ~ -5.07 Hartree; assert a tight-ish window.
    assert -5.2 < result.total_energy_hartree < -4.9


def test_invalid_smiles_raises() -> None:
    """An unparseable SMILES fails fast, not with a bogus energy (gate G4)."""
    with pytest.raises(ValueError, match="invalid SMILES"):
        run_xtb(XtbInput(smiles="not_a_molecule)("))


def test_cached_xtb_computes_once() -> None:
    """The second identical request is served from the store, not recomputed."""

    async def _run() -> None:
        store = InMemoryStore()
        job = XtbInput(smiles="CCO")

        first, cached1 = await run_cached_xtb(store, job)
        second, cached2 = await run_cached_xtb(store, job)

        assert cached1 is False
        assert cached2 is True
        assert first.total_energy_hartree == second.total_energy_hartree

    asyncio.run(_run())


def test_charge_changes_the_key_and_result() -> None:
    """Different charge is a distinct calculation (not a false cache hit)."""

    async def _run() -> None:
        store = InMemoryStore()
        neutral, _ = await run_cached_xtb(store, XtbInput(smiles="CCO", charge=0))
        cation, cached = await run_cached_xtb(store, XtbInput(smiles="CCO", charge=1))
        assert cached is False  # charge is in the key → miss, real recompute
        assert neutral.total_energy_hartree != cation.total_energy_hartree

    asyncio.run(_run())
