"""The agent's calculator tools: the surface did not move, and the cache still decides.

The tools are named by string in profiles, eval probes and `SKILL.md`s, so their signatures are a
contract. The physics answers over MCP; what is asserted here is the tool layer's part — which
server tool it asks for, with which arguments, how many times, and what it does with the answer
(e.g. re-ranking a Fukui result on a cache hit, reading the version off the payload).
`tests/calc_server_fake.py` stands in for the server and the store is in-memory, so every call
travels its real chain.
"""

import asyncio

import pytest

import chemclaw.connectors.calc.server.tools as calc_tools
from chemclaw.core.config import settings
from chemclaw.science.calc.store import InMemoryStore
from tests.calc_server_fake import FAKE_VERSION, FakeCalcServer, install
from tests.pg import migrated_db_or_skip


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


async def test_compute_xtb_energy_tool_runs_and_caches(
    server: FakeCalcServer, shared_store: InMemoryStore
) -> None:
    """The tool returns the parsed result and the second call is served from the store.

    A hit costs one `calculation_key` round trip, so the key count is two while the compute count is
    one; zero key calls would mean the client derives keys locally.
    """
    first = await calc_tools.compute_xtb_energy("O")
    second = await calc_tools.compute_xtb_energy("O")
    assert first.method == "GFN2-xTB"
    assert second.total_energy_hartree == first.total_energy_hartree

    assert server.count("compute_xtb_energy") == 1, "a persisted result was recomputed"
    assert server.count("calculation_key") == 2


async def test_electronic_properties_tool_returns_the_populated_result(
    server: FakeCalcServer, shared_store: InMemoryStore
) -> None:
    """The properties tool asks for one molecule and reuses the store on a repeat."""
    result = await calc_tools.compute_electronic_properties("CCO")
    again = await calc_tools.compute_electronic_properties("CCO")
    assert len(result.atom_charges) == 9  # C2H6O with explicit hydrogens
    assert result.bond_orders
    assert again.total_energy_hartree == result.total_energy_hartree

    assert server.count("compute_electronic_properties") == 1


async def test_the_two_binary_only_calculators_get_the_binary_s_own_wait_budget(
    server: FakeCalcServer,
) -> None:
    """The binary-only calculators get the `xtb` binary's own, longer wait budget.

    Their server-side timeout can exceed `calc_server_timeout_seconds`; waiting less abandons a
    running calculation and the retried activity doubles the cost. The fake does not implement these
    tools, so the calls fail; what is asserted is the session's read bound, recorded before
    dispatch.
    """
    assert settings.calc_atomic_timeout_seconds >= settings.calc_server_timeout_seconds

    for call in (
        calc_tools.compute_atomic_descriptors,
        calc_tools.compute_surface_potential,
    ):
        with pytest.raises(Exception):  # noqa: B017 - the fake server has no handler for either
            await call("O")

    assert server.timeouts[-2:] == [
        settings.calc_atomic_timeout_seconds,
        settings.calc_atomic_timeout_seconds,
    ]


async def test_a_second_fukui_mode_re_ranks_the_cached_result_rather_than_serving_the_first(
    server: FakeCalcServer, shared_store: InMemoryStore
) -> None:
    """A second Fukui mode re-ranks the cached result rather than serving the first mode's ranking.

    The server keys the three single points without the mode and re-ranks on the way out, but a
    cache hit never reaches the server, so `SiteReactivityResult.ranked_for` is what prevents a
    wrong regiochemistry answer. The fake ranks the two modes oppositely, so a mis-served ranking
    cannot pass by coincidence.
    """
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

    assert server.count("predict_site_reactivity") == 1, "the second mode ran the calculation again"


async def test_site_reactivity_truncates_to_the_configured_default(
    server: FakeCalcServer, shared_store: InMemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The truncation lives in the tool rather than in the cached row, on purpose.

    The stored result holds every atom, so asking for more sites re-slices a cached result instead
    of running three more single points. `top_n` is therefore never sent to the server.
    """
    monkeypatch.setattr(settings, "xtb_fukui_top_n", 3)

    default = await calc_tools.predict_site_reactivity("Oc1ccccc1")
    widened = await calc_tools.predict_site_reactivity("Oc1ccccc1", top_n=13)
    assert len(default.sites) == 3
    assert default.total_atoms == 13  # C6H6O with explicit hydrogens
    assert len(widened.sites) == 13

    assert server.count("predict_site_reactivity") == 1
    assert all("top_n" not in args for args in server.arguments("predict_site_reactivity"))


async def test_predict_solubility_logs_the_version_the_result_was_computed_under(
    server: FakeCalcServer, shared_store: InMemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`predict_solubility` logs the version the result was computed under, read off the payload.

    A locally derived version would be well-formed, match no ledger row, and make `calculator_trust`
    report `UNCALIBRATED`; and a cache hit must log the version that produced the number.
    """
    logged: list[tuple[str, str]] = []

    async def _record(*args: object, **_kwargs: object) -> None:
        logged.append((str(args[0]), str(args[1])))

    monkeypatch.setattr(calc_tools, "_log_prediction", _record)

    result = await calc_tools.predict_solubility("CCO")
    await calc_tools.predict_solubility("CCO")  # served from the store
    assert result.model == "esol-delaney@2004"
    assert result.uncertainty_log > 0

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


async def test_optimize_geometry_stores_the_full_result_and_summarizes_it_here(
    server: FakeCalcServer, shared_store: InMemoryStore
) -> None:
    """`optimize_geometry` stores the full result and summarizes it here.

    It shares the `xtb.opt` key with `relax_structure`, so caching a coordinate-less summary would
    poison later `relax_structure` hits.
    """
    summary = await calc_tools.optimize_geometry("CCO")
    assert summary.structure_id.startswith("st_")
    assert summary.energy_hartree < summary.energy_hartree + summary.relaxation_kcal
    # The row a later thermochemistry will hit is the full one, so it validates.
    from chemclaw.connectors.calc import compose

    _, cached = await compose.relax(shared_store, await compose.embed("CCO"), None)
    assert cached is True

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


async def test_predict_logd_tool_defaults_ph_and_reuses_the_pka(
    server: FakeCalcServer, shared_store: InMemoryStore
) -> None:
    """The logD tool defaults pH and reports the pKa uncertainty it was derived from.

    The expensive half is a cached pKa; the rest is local, so another pH costs no calculation.
    """
    result = await calc_tools.predict_logd("OC(=O)c1ccccc1")
    other_ph = await calc_tools.predict_logd("OC(=O)c1ccccc1", ph=2.0)
    assert result.ph == settings.logd_default_ph
    assert result.uncertainty > 0
    # More of an acid is protonated at low pH, so logD rises.
    assert other_ph.log_d > result.log_d

    assert server.count("predict_pka") == 1, "the second pH recomputed the pKa"
    assert server.count("predict_logd") == 0, "logD is composed here, never asked for"


def test_report_measurement_never_claims_a_store_that_did_not_happen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the ledger disabled (the default) the tool must not answer "Recorded".

    `record_observation` must distinguish "disabled, stored nothing" from "stored, nothing had
    predicted it".
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
    """A database failure reaches the caller rather than being reported as success.

    Unlike `record_prediction` (advice about finished work, rightly best-effort), the measurement is
    the whole deliverable of this call.
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
    """A measurement with no stated unit is refused rather than stamped with the ledger's unit.

    Otherwise "0.5 mg/mL" is recorded as log S and skews `calculator_trust` by orders of magnitude.
    The refusal names the ledger's unit, so the model can ask the chemist.
    """
    with pytest.raises(ValueError, match="state the unit"):
        asyncio.run(calc_tools.report_measurement("solubility", "CCO", 0.5))
    with pytest.raises(ValueError, match="state the unit"):
        asyncio.run(calc_tools.report_measurement("pka", "CCO", 15.9))

    # The property lookup is normalised, so case and whitespace variants cannot skip the refusal.
    for spelling in ("PKA", "pka ", " Solubility"):
        with pytest.raises(ValueError, match="state the unit"):
            asyncio.run(calc_tools.report_measurement(spelling, "CCO", 15.9))


def test_a_measurement_is_filed_under_the_name_the_ledger_reads_not_the_one_it_was_typed_as(
    server: FakeCalcServer, shared_store: InMemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A measurement is filed under the normalised name the ledger reads.

    Otherwise `"PKA"` passes the unit check but is stored where `calculator_trust("pka")` never sees
    it, and no prediction is reconciled.
    """
    monkeypatch.setattr(settings, "calibration_enabled", True)

    async def _run() -> tuple[str, int]:
        await migrated_db_or_skip()
        await calc_tools.predict_pka("CC(=O)O")  # logged as calc_type "pka"
        said = await calc_tools.report_measurement("PKA", "CC(=O)O", 4.76, "pKa")
        return said, (await calc_tools.calculator_trust("pka")).n

    said, scored = asyncio.run(_run())
    assert scored == 1, "the measurement was stored under a name the ledger cannot read"
    assert "reconciled 1 prediction" in said
