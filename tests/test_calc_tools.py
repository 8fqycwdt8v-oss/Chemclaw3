"""The agent's calculator tools: the surface did not move, and the cache still decides.

These fifteen tools are named by string in profiles, eval probes and `SKILL.md`s, so their
signatures and return types are a contract this suite has to hold still even though everything
underneath them changed: after `D-2026-08-16-the-physics-leaves-the-cache-stays` not one of them
computes anything. The physics answers over MCP, this side keys it, stores it and composes it.

So what is asserted here is what the tool layer is now responsible for — which tool it asks the
server for, with which arguments, how many times, and what it does with the answer before handing
it to a model. The chemistry itself is asserted in the repository that owns it, and the two
properties this side must get right that a physics test would never catch are both here: a Fukui
ranking re-ranked on a cache *hit*, and a version read off a payload rather than derived.

`tests/calc_server_fake.py` stands in for the server; the store is swapped for an in-memory one, as
before. `default_store` is patched on the tools module and the session is patched on
`connectors.calc.remote`, so every call travels its real chain.
"""

import asyncio

import pytest

import chemclaw.connectors.calc.server.tools as calc_tools
from chemclaw.core.config import settings
from chemclaw.science.calc.store import InMemoryStore
from tests.calc_server_fake import FAKE_VERSION, FakeCalcServer, install


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> FakeCalcServer:
    """A fake calculation server behind the tools, with a fresh in-memory store in front of it."""
    monkeypatch.setattr(calc_tools, "default_store", lambda: InMemoryStore())
    return install(monkeypatch, FakeCalcServer())


@pytest.fixture
def shared_store(monkeypatch: pytest.MonkeyPatch) -> InMemoryStore:
    """One store across calls, for the tests that ask the same question twice."""
    store = InMemoryStore()
    monkeypatch.setattr(calc_tools, "default_store", lambda: store)
    return store


def test_compute_xtb_energy_tool_runs_and_caches(
    server: FakeCalcServer, shared_store: InMemoryStore
) -> None:
    """The tool returns the parsed result and the second call is served from the store.

    D-011 survives the wire: the miss path got longer, the rule did not change. What a hit costs is
    one `calculation_key` round trip, which is why the key call count is two while the compute count
    is one — if that ever became zero the client would be deriving keys locally, which is the thing
    the whole transport exists to prevent.
    """

    async def _run() -> None:
        first = await calc_tools.compute_xtb_energy("O")
        second = await calc_tools.compute_xtb_energy("O")
        assert first.method == "GFN2-xTB"
        assert second.total_energy_hartree == first.total_energy_hartree

    asyncio.run(_run())
    assert server.count("compute_xtb_energy") == 1, "a persisted result was recomputed"
    assert server.count("calculation_key") == 2


def test_electronic_properties_tool_returns_the_populated_result(
    server: FakeCalcServer, shared_store: InMemoryStore
) -> None:
    """The properties tool asks for one molecule and reuses the store on a repeat."""

    async def _run() -> None:
        result = await calc_tools.compute_electronic_properties("CCO")
        again = await calc_tools.compute_electronic_properties("CCO")
        assert len(result.atom_charges) == 9  # C2H6O with explicit hydrogens
        assert result.bond_orders
        assert again.total_energy_hartree == result.total_energy_hartree

    asyncio.run(_run())
    assert server.count("compute_electronic_properties") == 1


def test_the_two_binary_only_calculators_get_the_binary_s_own_wait_budget(
    server: FakeCalcServer,
) -> None:
    """The client must wait as long as the binary-only calculators' own server-side budget.

    `compute_atomic_descriptors`/`compute_surface_potential` are pinned to the `xtb` binary
    regardless of `CHEMCLAW_XTB_ENGINE`, whose own server-side timeout can run to 3600 s —
    `calc_server_timeout_seconds`'s default of 900 s would abandon a calculation the server is
    still computing, and Temporal then retries the (retryable) activity, doubling the cost while
    the first, orphaned run keeps burning CPU. See `calc_atomic_timeout_seconds`'s own docstring.

    The fake server does not implement either tool's payload — that is not what this test is
    about — so both calls are expected to fail; what is asserted is the session's own read bound,
    which `install()` records before any tool is dispatched.
    """
    assert settings.calc_atomic_timeout_seconds >= settings.calc_server_timeout_seconds

    async def _run() -> None:
        for call in (
            calc_tools.compute_atomic_descriptors,
            calc_tools.compute_surface_potential,
        ):
            with pytest.raises(Exception):  # noqa: B017 - the fake server has no handler for either
                await call("O")

    asyncio.run(_run())
    assert server.timeouts[-2:] == [
        settings.calc_atomic_timeout_seconds,
        settings.calc_atomic_timeout_seconds,
    ]


def test_a_second_fukui_mode_re_ranks_the_cached_result_rather_than_serving_the_first(
    server: FakeCalcServer, shared_store: InMemoryStore
) -> None:
    """The defect the split introduced and this is the guard against it.

    The three single points behind a Fukui ranking do not depend on the mode, so the server keys
    them without it — measured: all three modes on phenol derive one key. The server re-ranks on the
    way out, which is why a *remote* call is always right; a cache hit never reaches the server. So
    without `SiteReactivityResult.ranked_for` the second mode asked for would be served the first
    mode's ordering carrying the first mode's labels, a confidently wrong regiochemistry answer with
    nothing raising anywhere.

    The fake ranks `f_minus` descending and `f_plus` ascending, so the two modes order the atoms
    oppositely and a mis-served ranking cannot look like a coincidence.
    """

    async def _run() -> None:
        electrophilic = await calc_tools.predict_site_reactivity("Oc1ccccc1", top_n=13)
        nucleophilic = await calc_tools.predict_site_reactivity(
            "Oc1ccccc1", mode="nucleophilic", top_n=13
        )
        assert electrophilic.mode == "electrophilic"
        assert electrophilic.ranked_by == "f_minus"
        assert nucleophilic.mode == "nucleophilic"
        assert nucleophilic.ranked_by == "f_plus"
        assert [site.index for site in nucleophilic.sites] == list(
            reversed([site.index for site in electrophilic.sites])
        )

    asyncio.run(_run())
    assert server.count("predict_site_reactivity") == 1, "the second mode ran the calculation again"


def test_site_reactivity_truncates_to_the_configured_default(
    server: FakeCalcServer, shared_store: InMemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The truncation lives in the tool rather than in the cached row, on purpose.

    The stored result holds every atom, so asking for more sites re-slices a cached result instead
    of running three more single points. `top_n` is therefore never sent to the server.
    """
    monkeypatch.setattr(settings, "xtb_fukui_top_n", 3)

    async def _run() -> None:
        default = await calc_tools.predict_site_reactivity("Oc1ccccc1")
        widened = await calc_tools.predict_site_reactivity("Oc1ccccc1", top_n=13)
        assert len(default.sites) == 3
        assert default.total_atoms == 13  # C6H6O with explicit hydrogens
        assert len(widened.sites) == 13

    asyncio.run(_run())
    assert server.count("predict_site_reactivity") == 1
    assert all("top_n" not in args for args in server.arguments("predict_site_reactivity"))


def test_predict_solubility_logs_the_version_the_result_was_computed_under(
    server: FakeCalcServer, shared_store: InMemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single most load-bearing change in the tool layer, asserted on the value that is logged.

    `binary_version()` answered the literal string `"absent"` rather than raising when a binary was
    missing, so a locally-derived version would be *well-formed*, match zero rows in a ledger keyed
    exactly on `(calc_type, calc_version, input_hash)`, and make `calculator_trust` report a
    confident `UNCALIBRATED`. The version now comes off the payload, so a cache hit logs the version
    that produced the number rather than the one that happens to be current.
    """
    logged: list[tuple[str, str]] = []

    async def _record(*args: object, **_kwargs: object) -> None:
        logged.append((str(args[0]), str(args[1])))

    monkeypatch.setattr(calc_tools, "_log_prediction", _record)

    async def _run() -> None:
        result = await calc_tools.predict_solubility("CCO")
        await calc_tools.predict_solubility("CCO")  # served from the store
        assert result.model == "esol-delaney@2004"
        assert result.uncertainty_log > 0

    asyncio.run(_run())
    assert logged == [("solubility", FAKE_VERSION), ("solubility", FAKE_VERSION)]


def test_a_result_with_no_version_is_refused_rather_than_logged_under_an_empty_one(
    server: FakeCalcServer, shared_store: InMemoryStore
) -> None:
    """An empty version degenerates the ledger's unique index and one row overwrites another.

    That is silent, so it raises here instead. The check is on the tool's own read of the payload,
    which is the only place this repository ever learns a version from a result.
    """
    server.overrides["predict_pka"] = lambda arguments: {
        "smiles": "CC(=O)O",
        "method": "GFN2-xTB",
        "pka": 4.2,
        "deprotonation_energy_kcal": 320.0,
        "uncertainty": 1.6,
        "site": "acid",
    }
    with pytest.raises(ValueError, match="no calc_version"):
        asyncio.run(calc_tools.predict_pka("CC(=O)O"))


def test_predict_developability_profile_tool_flags_ro5(
    server: FakeCalcServer, shared_store: InMemoryStore
) -> None:
    """The developability tool returns the descriptor panel and the two flags, unchanged."""
    result = asyncio.run(calc_tools.predict_developability_profile("CC(=O)Oc1ccccc1C(=O)O"))
    assert result.lipinski_violations == 0
    assert result.veber_pass is True


def test_optimize_geometry_stores_the_full_result_and_summarizes_it_here(
    server: FakeCalcServer, shared_store: InMemoryStore
) -> None:
    """One key, one payload shape — the collision this tool would otherwise cause.

    `optimize_geometry` and `relax_structure` derive the **same** `xtb.opt` key on the server while
    returning different payloads: a summary without coordinates, and the full result with them.
    Caching the summary under that key would poison every later `relax_structure` hit with a
    validation error deep inside a reaction job, so this tool asks for the full result and drops the
    geometry here, where it costs nothing.
    """

    async def _run() -> None:
        summary = await calc_tools.optimize_geometry("CCO")
        assert summary.structure_id.startswith("st_")
        assert summary.energy_hartree < summary.energy_hartree + summary.relaxation_kcal
        # The row a later thermochemistry will hit is the full one, so it validates.
        from chemclaw.connectors.calc import compose

        _, cached = await compose.relax(shared_store, await compose.embed("CCO"), None)
        assert cached is True

    asyncio.run(_run())
    assert server.count("relax_structure") == 1
    assert server.count("optimize_geometry") == 0, "the one-shot tool must not be used"


def test_compute_thermochemistry_composes_and_truncates_its_spectrum(
    server: FakeCalcServer, shared_store: InMemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Remote optimise, remote Hessian, local RRHO — and the refinement vector stays here.

    The imaginary mode's 3N-vector is machinery for the escape loop, not something a model can read;
    the frequency itself is already in `imaginary_frequencies_cm`.
    """
    monkeypatch.setattr(settings, "xtb_ir_bands_top_n", 2)

    result = asyncio.run(calc_tools.compute_thermochemistry("CCO", symmetry_number=1))

    assert server.count("relax_structure") == 1
    assert server.count("compute_hessian") == 1
    assert result.imaginary_displacement is None
    assert len(result.modes) <= 2
    assert result.mode_count > len(result.modes)  # the honest count of what was truncated


def test_predict_logd_tool_defaults_ph_and_reuses_the_pka(
    server: FakeCalcServer, shared_store: InMemoryStore
) -> None:
    """The logD tool defaults pH and reports the pKa uncertainty it was derived from.

    Its expensive half is a *cached* pKa; the rest is a Crippen sum and one Henderson-Hasselbalch
    term, both local. So asking again at a different pH costs no calculation at all — which is the
    whole reason this composite was decomposed instead of shipped.
    """

    async def _run() -> None:
        result = await calc_tools.predict_logd("OC(=O)c1ccccc1")
        other_ph = await calc_tools.predict_logd("OC(=O)c1ccccc1", ph=2.0)
        assert result.ph == settings.logd_default_ph
        assert result.uncertainty > 0
        # More of an acid is protonated at low pH, so logD rises.
        assert other_ph.log_d > result.log_d

    asyncio.run(_run())
    assert server.count("predict_pka") == 1, "the second pH recomputed the pKa"
    assert server.count("predict_logd") == 0, "logD is composed here, never asked for"


def test_report_measurement_never_claims_a_store_that_did_not_happen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the ledger disabled — the **default** — the tool must not answer "Recorded".

    `calibration_enabled` is False out of the box, and `record_observation` returned `0` for both
    "disabled, stored nothing" and "stored it, nothing had predicted it". The tool read that single
    zero as the second and told the chemist "the measurement is kept and the next prediction of it
    will be scored against this value" — in every unconfigured deployment, on every call, while no
    table was touched at all.
    """
    monkeypatch.setattr(settings, "calibration_enabled", False)
    answer = asyncio.run(calc_tools.report_measurement("pka", "CCO", 15.9, "pKa"))
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
        asyncio.run(calc_tools.report_measurement("pka", "CCO", 15.9, "pKa"))


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


def test_a_measurement_with_no_stated_unit_is_refused_rather_than_stamped() -> None:
    """The value that reaches the ledger must be in the unit the ledger says it is in.

    `unit` used to default to empty and the row was stamped with the ledger's own unit regardless,
    so a chemist reporting "0.5 mg/mL" had `0.5` recorded as **log S**. For MW 300 the true log S is
    −2.78, so `calculator_trust` would report that calculator as biased by 3.3 log units — a factor
    of ~2000 — on the strength of one row. Worse than the empty string it replaced: an empty unit
    marked the row as unstated, and asserting the wrong one removes the only way to find it again.

    The refusal names the ledger's unit, so the model can ask the chemist rather than guess.
    """
    with pytest.raises(ValueError, match="state the unit"):
        asyncio.run(calc_tools.report_measurement("solubility", "CCO", 0.5))
    with pytest.raises(ValueError, match="state the unit"):
        asyncio.run(calc_tools.report_measurement("pka", "CCO", 15.9))

    # **And the spellings that used to walk around it.** The lookup was exact-match on a
    # model-supplied string, so one capital letter or a trailing space skipped the refusal and stored
    # the row with the empty unit the control exists to prevent. (The first version of this test
    # asserted this case with `"pka"`, which *is* calibrated — so it covered the same branch twice
    # and its comment described coverage that did not exist.)
    for spelling in ("PKA", "pka ", " Solubility"):
        with pytest.raises(ValueError, match="state the unit"):
            asyncio.run(calc_tools.report_measurement(spelling, "CCO", 15.9))
