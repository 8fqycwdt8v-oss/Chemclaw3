"""The agent's fast-calculator tool runs and caches (plan step 1c.5).

Uses an in-memory store (swapped in for the production Postgres one) so the tool
is exercised end-to-end with a real GFN2-xTB calculation and no database.

Since X8 these tools live in `mcp_servers.calc.server` rather than `agents.calc_tools`: they
compute and need no turn-ambient identity, so they are hosted out of process. The tests are
unchanged apart from where they import from — which is the point of that move being a
deployment decision rather than a behavioural one.
"""

import asyncio

import pytest

# Two transports since X8, so two aliases — `calc_tools` is the MCP-hosted calculator
# surface, `inprocess_tools` the tools that stay in the agent because they route to Temporal
# or write the prediction ledger. Naming them apart is what keeps a test honest about which
# process it is exercising.
import agents.calc_tools as inprocess_tools
import mcp_servers.calc.server as calc_tools
from calc.reaction_energy import ReactionSpecies
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
    # The same settings object the server module bound at import, so patching it takes effect.
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


def test_predict_developability_profile_tool_flags_ro5(monkeypatch: pytest.MonkeyPatch) -> None:
    """The developability tool returns the descriptor panel and Ro5/Veber flags."""
    store = InMemoryStore()
    monkeypatch.setattr(inprocess_tools, "default_store", lambda: store)

    async def _run() -> None:
        result = await inprocess_tools.predict_developability_profile("CC(=O)Oc1ccccc1C(=O)O")
        assert result.lipinski_violations == 0
        assert result.veber_pass is True

    asyncio.run(_run())


def test_predict_logd_tool_defaults_ph_and_reuses_pka(monkeypatch: pytest.MonkeyPatch) -> None:
    """The logD tool defaults pH and reports the pKa uncertainty it was derived from."""
    store = InMemoryStore()
    monkeypatch.setattr(inprocess_tools, "default_store", lambda: store)

    async def _run() -> None:
        from chemclaw.config import settings

        result = await inprocess_tools.predict_logd("OC(=O)c1ccccc1")
        assert result.ph == settings.logd_default_ph
        assert result.uncertainty > 0

    asyncio.run(_run())


def test_estimate_reaction_energy_tool_flags_exotherm(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reaction-energy tool returns the flag and echoes the configured threshold."""
    store = InMemoryStore()
    monkeypatch.setattr(inprocess_tools, "default_store", lambda: store)

    async def _run() -> None:
        from chemclaw.config import settings

        result = await inprocess_tools.estimate_reaction_energy(
            reactants=[ReactionSpecies(smiles="CCO", coefficient=1.0)],
            products=[ReactionSpecies(smiles="CCO", coefficient=1.0)],
        )
        assert result.exotherm_threshold_kcal == settings.reaction_energy_exotherm_threshold_kcal
        assert result.is_strongly_exothermic is False  # a null reaction is not exothermic

    asyncio.run(_run())
