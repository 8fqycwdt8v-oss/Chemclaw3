"""Trust is a distribution, not a number: the residual listing and its property table (D-169).

`calculator_trust` reduced the whole ledger to six aggregates, and did it through
`if property_name == "solubility"` followed by two ternaries — so *every* other name was answered
with pKa's version and pKa's unit. An unknown property got a confident report about the wrong
calculator, and nothing in the response said so.

The listing is the other half. A model that is 0.3 log units off overall may be fine on neutrals
and two units low on every acid; the two populations average into one reassuring number, and no
aggregate can pull them apart.
"""

import asyncio
import threading

import pytest

from chemclaw.connectors.calc.server import tools
from chemclaw.core.chem import InvalidSmilesError, substructure_pattern
from chemclaw.core.config import settings
from chemclaw.science.calc import xtb_cli
from chemclaw.science.calc.calibration import Residual

# Two acids that the predictor badly under-called, and two neutrals it got nearly right. The
# aggregate over all four looks mediocre; the acids alone look disqualifying.
_LEDGER = [
    Residual(subject="CC(=O)O", predicted=-2.0, observed=0.1, error=-2.1, uncertainty=0.5),
    Residual(subject="OC(=O)c1ccccc1", predicted=-4.5, observed=-2.7, error=-1.8, uncertainty=0.5),
    Residual(subject="CCO", predicted=0.9, observed=1.0, error=-0.1, uncertainty=0.5),
    Residual(subject="CCOCC", predicted=-0.4, observed=-0.2, error=-0.2, uncertainty=0.5),
]


@pytest.fixture(autouse=True)
def _ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve the fixed ledger above instead of the database, for every test here.

    `calibration_enabled` is switched on with it, because the two go together in production: the
    real `reconciled_for` returns `[]` without touching the database when the ledger is off, so a
    populated ledger under a disabled flag is a state that cannot occur. Leaving the flag at its
    default (False) would make every report here read "CALIBRATION NOT RECORDED" beside four
    residuals.
    """

    async def _reconciled(calc_type: str, calc_version: str) -> list[Residual]:
        return list(_LEDGER)

    monkeypatch.setattr(settings, "calibration_enabled", True)
    monkeypatch.setattr(tools, "reconciled_for", _reconciled)


def test_an_uncalibrated_property_is_refused_and_the_message_names_the_real_ones() -> None:
    """The bug this replaces: anything that was not "solubility" was reported as pKa.

    Silently, in pKa's unit, from pKa's current version — a wrong answer indistinguishable from a
    right one, about the reliability of a calculator the chemist is deciding whether to trust.
    """

    async def _run() -> None:
        with pytest.raises(ValueError, match="not a calibrated property"):
            await tools.calculator_trust("logd")
        with pytest.raises(ValueError, match="solubility"):  # it names what does exist
            await tools.calculator_outliers("logd")

    asyncio.run(_run())


def test_a_disabled_ledger_does_not_render_as_a_well_behaved_calculator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default deployment's answer, and it was an empty list with nothing to read it by.

    `calibration_enabled` defaults to **False**, so `reconciled_for` returns `[]` without raising
    and this tool answered `[]` — which its own docstring tells the model means "few
    measurements". Its sibling `calculator_trust` was given a verdict for exactly this collapse in
    the same commit; the listing was left a bare list, so disabled ≡ nothing measured ≡ nothing
    missed, all one payload.
    """
    monkeypatch.setattr(settings, "calibration_enabled", False)

    async def _empty(calc_type: str, calc_version: str) -> list[Residual]:
        return []  # what the real read does when the ledger is off

    monkeypatch.setattr(tools, "reconciled_for", _empty)

    async def _run() -> None:
        report = await tools.calculator_outliers("pka")
        assert report.residuals == [] and report.enabled is False
        assert "CALIBRATION NOT RECORDED" in report.model_dump()["verdict"]

        # An *enabled* but empty ledger is a different state, and says so.
        monkeypatch.setattr(settings, "calibration_enabled", True)
        empty = await tools.calculator_outliers("pka")
        assert empty.enabled is True and "UNCALIBRATED" in empty.verdict

    asyncio.run(_run())


def test_each_calibrated_property_reports_in_its_own_unit() -> None:
    """The unit came from the same conditional, so it was wrong for the same inputs."""

    async def _run() -> None:
        assert (await tools.calculator_outliers("solubility")).residuals[0].unit == "log S"
        assert (await tools.calculator_outliers("pka")).residuals[0].unit == "pKa"

    asyncio.run(_run())


def test_the_worst_miss_comes_first_and_keeps_its_sign() -> None:
    """Ranked by magnitude, reported signed: "consistently low" is correctable, scattered is not."""

    async def _run() -> None:
        found = (await tools.calculator_outliers("solubility")).residuals
        assert [r.smiles for r in found][:2] == ["CC(=O)O", "OC(=O)c1ccccc1"]
        assert found[0].error == pytest.approx(-2.1)
        assert found[0].predicted == pytest.approx(-2.0)
        assert found[0].observed == pytest.approx(0.1)

    asyncio.run(_run())


def test_a_substructure_filter_isolates_the_class_the_aggregate_hides() -> None:
    """The whole point: the acids are twice as bad as the calculator's overall record."""

    async def _run() -> None:
        acids = await tools.calculator_outliers("solubility", matching="C(=O)O")
        assert [r.smiles for r in acids.residuals] == ["CC(=O)O", "OC(=O)c1ccccc1"]
        everything = await tools.calculator_outliers("solubility")
        assert len(everything.residuals) == 4

    asyncio.run(_run())


def test_a_filter_matching_nothing_returns_nothing_rather_than_everything() -> None:
    """An empty list is the honest answer; falling back to the unfiltered set would be a lie.

    And it says *which* emptiness it is: the ledger holds four measurements, none of them of a
    platinum compound, so the class is untested rather than well handled.
    """

    async def _run() -> None:
        report = await tools.calculator_outliers("solubility", matching="[Pt]")
        assert report.residuals == [] and report.measured == 4
        assert "untested" in report.verdict

    asyncio.run(_run())


def test_uncertainty_coverage_is_reported_per_molecule() -> None:
    """Missed by 2 log units *and* outside its own error bar is the actionable statement."""

    async def _run() -> None:
        found = (await tools.calculator_outliers("solubility")).residuals
        assert found[0].within_uncertainty is False  # |−2.1| > 0.5
        assert found[-1].within_uncertainty is True  # |−0.1| < 0.5

    asyncio.run(_run())


def test_a_prediction_that_claimed_no_uncertainty_is_not_reported_as_a_miss() -> None:
    """`None`, not `False`: it made no claim, so there is nothing to have failed."""
    unclaimed = Residual(subject="CCO", predicted=1.0, observed=2.0, error=-1.0)
    assert unclaimed.within_uncertainty is None


def test_limit_is_clamped_to_the_configured_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """The listing exists to be read; the cap is the deployment's, not the model's."""

    async def _run() -> None:
        monkeypatch.setattr(settings, "calc_outliers_max_results", 2)
        assert len((await tools.calculator_outliers("solubility", limit=1000)).residuals) == 2
        assert len((await tools.calculator_outliers("solubility", limit=0)).residuals) == 1

    asyncio.run(_run())


def test_a_substructure_query_is_smarts_first_then_smiles() -> None:
    """Every SMILES parses as SMARTS but not conversely, and only SMARTS can say "any aromatic"."""
    assert substructure_pattern("[#6]!@[OX2H]").GetNumAtoms() == 2
    assert substructure_pattern("c1ccccc1").GetNumAtoms() == 6


def test_an_empty_substructure_query_is_rejected_rather_than_matching_everything() -> None:
    """RDKit matches a zero-atom pattern against every molecule, so "nothing" would answer "all"."""
    with pytest.raises(InvalidSmilesError, match="no atoms"):
        substructure_pattern("")
    with pytest.raises(InvalidSmilesError, match="unparseable"):
        substructure_pattern("not a molecule at all )(")


def test_the_calc_connector_resolves_its_backend_before_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The blocking call is hoisted to startup, so no request path can be the first to pay it.

    Resolving `pka_calc_version()` shells out to `xtb --version` once per process whenever the
    resolved backend is the binary, `lru_cache`d thereafter. `run_cached_pka` threads its own
    call; `_log_prediction` and `_calibrated` (behind `calculator_trust` and `calculator_outliers`)
    do not, so an ordinary first `calculator_trust("pka")` could hold the connector's single event
    loop for the subprocess timeout.

    What is asserted is the property every one of those call sites now depends on: the version
    resolution happens at startup **and off the event loop**, proved by recording the thread the
    version call runs on. `binary_version` is stubbed rather than executed — there is no `xtb`
    binary in this environment, and with `xtb_engine` at its `auto` default the resolution never
    reaches it, so the engine is pinned to make the blocking branch the one under test.
    """
    monkeypatch.setattr(settings, "xtb_engine", "xtb")
    ran_on: list[str] = []

    def _version() -> str:
        ran_on.append(threading.current_thread().name)
        return "6.6.1"

    monkeypatch.setattr(xtb_cli, "binary_version", _version)
    asyncio.run(tools.resolve_calculator_versions())
    assert ran_on, "the backend version was never resolved at startup"
    assert threading.main_thread().name not in ran_on  # i.e. never on the loop's thread

    # And it cannot take the connector down: the `on_start` contract is diagnostics-only.
    def _explode() -> str:
        raise OSError("no such binary")

    monkeypatch.setattr(tools, "pka_calc_version", _explode)
    asyncio.run(tools.resolve_calculator_versions())
