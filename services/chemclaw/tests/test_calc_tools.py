"""The agent's fast-calculator tool runs and caches (plan step 1c.5).

Uses an in-memory store (swapped in for the production Postgres one) so the tool
is exercised end-to-end with a real GFN2-xTB calculation and no database.
"""

import asyncio

import pytest

import agents.calc_tools as calc_tools
from calc.store import InMemoryStore
from chemclaw.config import settings


def test_compute_xtb_energy_tool_runs_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool returns a physical energy and the second call is served from the store."""
    store = InMemoryStore()
    monkeypatch.setattr(calc_tools, "default_store", lambda: store)

    async def _run() -> None:
        first = await calc_tools.compute_xtb_energy("O")
        assert first.method == "GFN2-xTB"
        assert -5.2 < first.total_energy_hartree < -4.9

        # Second call hits the store (same value); nothing recomputed.
        second = await calc_tools.compute_xtb_energy("O")
        assert second.total_energy_hartree == first.total_energy_hartree

    asyncio.run(_run())


def test_electronic_properties_tool_runs_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """The properties tool returns a populated result and reuses the store on a repeat."""
    store = InMemoryStore()
    monkeypatch.setattr(calc_tools, "default_store", lambda: store)

    async def _run() -> None:
        result = await calc_tools.compute_electronic_properties("CCO")
        assert result.gap_ev is not None and result.gap_ev > 0
        assert len(result.atom_charges) == 9  # C2H6O with explicit hydrogens
        assert result.bond_orders  # ethanol is bonded
        again = await calc_tools.compute_electronic_properties("CCO")
        assert again.total_energy_hartree == result.total_energy_hartree

    asyncio.run(_run())


def test_site_reactivity_tool_truncates_to_the_configured_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tool returns the configured number of top sites, ranked, of a stated total.

    The truncation lives in the tool rather than the calculator on purpose: the cached
    result holds every atom, so re-ranking it for another mode (or asking for more
    sites) stays free.
    """
    store = InMemoryStore()
    monkeypatch.setattr(calc_tools, "default_store", lambda: store)
    # The same settings object `calc_tools` bound at import, so patching it takes effect.
    monkeypatch.setattr(settings, "xtb_fukui_top_n", 3)

    async def _run() -> None:
        result = await calc_tools.predict_site_reactivity("Oc1ccccc1")
        assert result.mode == "electrophilic"
        assert result.ranked_by == "f_minus"
        assert len(result.sites) == 3
        assert result.total_atoms == 13  # C6H6O with explicit hydrogens
        assert [site.f_minus for site in result.sites] == sorted(
            (site.f_minus for site in result.sites), reverse=True
        )

        # An explicit top_n overrides the default and can widen it back out.
        widened = await calc_tools.predict_site_reactivity("Oc1ccccc1", top_n=13)
        assert len(widened.sites) == 13

    asyncio.run(_run())


def test_predict_solubility_tool_reports_uncertainty(monkeypatch: pytest.MonkeyPatch) -> None:
    """The solubility tool returns a prediction with a non-zero uncertainty."""
    store = InMemoryStore()
    monkeypatch.setattr(calc_tools, "default_store", lambda: store)

    async def _run() -> None:
        result = await calc_tools.predict_solubility("CCO")
        assert result.model == "esol-delaney@2004"
        assert result.uncertainty_log > 0

    asyncio.run(_run())
