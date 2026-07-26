"""The agent's fast-calculator tool runs and caches (plan step 1c.5).

Uses an in-memory store (swapped in for the production Postgres one) so the tool
is exercised end-to-end with a real GFN2-xTB calculation and no database.
"""

import asyncio

import pytest

import agents.calc_tools as calc_tools
from calc.store import InMemoryStore


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


def test_predict_solubility_tool_reports_uncertainty(monkeypatch: pytest.MonkeyPatch) -> None:
    """The solubility tool returns a prediction with a non-zero uncertainty."""
    store = InMemoryStore()
    monkeypatch.setattr(calc_tools, "default_store", lambda: store)

    async def _run() -> None:
        result = await calc_tools.predict_solubility("CCO")
        assert result.model == "esol-delaney@2004"
        assert result.uncertainty_log > 0

    asyncio.run(_run())
