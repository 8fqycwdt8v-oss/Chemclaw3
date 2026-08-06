"""The agent's fast-calculator tool runs and caches (plan step 1c.5).

Uses an in-memory store (swapped in for the production Postgres one) so the tool
is exercised end-to-end with a real GFN2-xTB calculation and no database.

These tools live in the `calc` connector bundle rather than in the agent: they compute and need
no turn-ambient identity, so they are hosted out of process. The tests are unchanged apart from
where they import from — which is the point of that move being a deployment decision rather than a
behavioural one.
"""

import asyncio

import pytest

import chemclaw.connectors.calc.server.tools as calc_tools
from chemclaw.core.config import settings
from chemclaw.science.calc.store import InMemoryStore


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
    monkeypatch.setattr(calc_tools, "default_store", lambda: store)

    async def _run() -> None:
        result = await calc_tools.predict_developability_profile("CC(=O)Oc1ccccc1C(=O)O")
        assert result.lipinski_violations == 0
        assert result.veber_pass is True

    asyncio.run(_run())


def test_predict_logd_tool_defaults_ph_and_reuses_pka(monkeypatch: pytest.MonkeyPatch) -> None:
    """The logD tool defaults pH and reports the pKa uncertainty it was derived from."""
    store = InMemoryStore()
    monkeypatch.setattr(calc_tools, "default_store", lambda: store)

    async def _run() -> None:
        from chemclaw.core.config import settings

        result = await calc_tools.predict_logd("OC(=O)c1ccccc1")
        assert result.ph == settings.logd_default_ph
        assert result.uncertainty > 0

    asyncio.run(_run())


def test_report_measurement_never_claims_a_store_that_did_not_happen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the ledger disabled — the **default** — the tool must not answer "Recorded".

    `calibration_enabled` is False out of the box, and `record_observation` returned `0` for both
    "disabled, stored nothing" and "stored it, nothing had predicted it". The tool read that single
    zero as the second and told the chemist "the measurement is kept and the next prediction of it
    will be scored against this value" — in every unconfigured deployment, on every call, while no
    table was touched at all.

    Nothing exercised this message, which is why it survived. Set `calibration_enabled` True and
    the assertion below still holds for the right reason: the store then really happens.
    """
    monkeypatch.setattr(settings, "calibration_enabled", False)
    answer = asyncio.run(calc_tools.report_measurement("pka", "CCO", 15.9))
    assert "NOT recorded" in answer
    assert "not stored" in answer
    # The exact phrase the old branch used, which a reader acts on.
    assert "the measurement is kept" not in answer


def test_report_measurement_surfaces_a_failed_write_instead_of_swallowing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database failure must reach the caller, not be logged and reported as success.

    `record_prediction` swallows its errors and is right to: a prediction row is advice *about*
    work that already happened, so losing it must not cost the calculation. `record_observation`
    had inherited the same `except Exception` and it is wrong there — the measurement is the
    entire deliverable of the call, so swallowing turns the tool's only job into a false success
    (D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed).
    """
    monkeypatch.setattr(settings, "calibration_enabled", True)

    def _explode(*_args: object, **_kwargs: object) -> object:
        raise ConnectionError("database is down")

    monkeypatch.setattr("chemclaw.science.calc.calibration.db.connection", _explode)
    with pytest.raises(ConnectionError):
        asyncio.run(calc_tools.report_measurement("pka", "CCO", 15.9))


def test_a_disabled_ledger_is_none_and_a_stored_unpredicted_value_is_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The contract the caller depends on: `None` is "not stored", `0` is "stored, none matched".

    Pinned separately from the tool because it is the distinction the tool's honesty rests on —
    collapsing them back to a single `0` is exactly the regression this file exists to catch.
    """
    from chemclaw.science.calc import calibration

    monkeypatch.setattr(settings, "calibration_enabled", False)
    assert asyncio.run(calibration.record_observation("pka", "h", 1.0, source="bench")) is None
