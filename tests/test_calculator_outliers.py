"""Trust is a distribution, not a number: the residual listing and its property table (D-169).

Each property name resolves through a table, so an unknown property is refused rather than
answered with pKa's version and unit. The listing exists because aggregates hide sub-populations
(e.g. fine on neutrals, two units low on acids).
"""

import pytest

from chemclaw.connectors.calc.server import tools
from chemclaw.core.chem import InvalidSmilesError, substructure_pattern
from chemclaw.core.config import settings
from chemclaw.science.calc.calibration import Calibration, Residual
from tests.calc_server_fake import FAKE_VERSION, FakeCalcServer, install

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

    `calibration_enabled` is switched on with it, because a populated ledger under a disabled flag
    cannot occur in production.
    """

    async def _reconciled(calc_type: str, calc_version: str) -> list[Residual]:
        return list(_LEDGER)

    monkeypatch.setattr(settings, "calibration_enabled", True)
    monkeypatch.setattr(tools, "reconciled_for", _reconciled)
    # The version these tools report against is a round trip to the calculation server now, so the
    # fake has to be behind them even where the ledger itself is stubbed.
    install(monkeypatch, FakeCalcServer())


async def test_an_uncalibrated_property_is_refused_and_the_message_names_the_real_ones() -> None:
    """The bug this replaces: anything that was not "solubility" was reported as pKa.

    Silently, in pKa's unit, from pKa's current version — a wrong answer indistinguishable from a
    right one, about the reliability of a calculator the chemist is deciding whether to trust.
    """
    with pytest.raises(ValueError, match="not a calibrated property"):
        await tools.calculator_trust("logd")
    with pytest.raises(ValueError, match="solubility"):  # it names what does exist
        await tools.calculator_outliers("logd")


async def test_a_disabled_ledger_does_not_render_as_a_well_behaved_calculator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disabled ledger does not render as a well-behaved calculator.

    `calibration_enabled` defaults to False and `reconciled_for` then returns `[]`; the tool must
    say the ledger is off rather than return a bare empty list meaning "nothing missed".
    """
    monkeypatch.setattr(settings, "calibration_enabled", False)

    async def _empty(calc_type: str, calc_version: str) -> list[Residual]:
        return []  # what the real read does when the ledger is off

    monkeypatch.setattr(tools, "reconciled_for", _empty)

    report = await tools.calculator_outliers("pka")
    assert report.residuals == [] and report.enabled is False
    assert "CALIBRATION NOT RECORDED" in report.model_dump()["verdict"]

    # An *enabled* but empty ledger is a different state, and says so.
    monkeypatch.setattr(settings, "calibration_enabled", True)
    empty = await tools.calculator_outliers("pka")
    assert empty.enabled is True and "UNCALIBRATED" in empty.verdict


async def test_each_calibrated_property_reports_in_its_own_unit() -> None:
    """The unit came from the same conditional, so it was wrong for the same inputs."""
    assert (await tools.calculator_outliers("solubility")).residuals[0].unit == "log S"
    assert (await tools.calculator_outliers("pka")).residuals[0].unit == "pKa"


async def test_the_worst_miss_comes_first_and_keeps_its_sign() -> None:
    """Ranked by magnitude, reported signed: "consistently low" is correctable, scattered is not."""
    found = (await tools.calculator_outliers("solubility")).residuals
    assert [r.smiles for r in found][:2] == ["CC(=O)O", "OC(=O)c1ccccc1"]
    assert found[0].error == pytest.approx(-2.1)
    assert found[0].predicted == pytest.approx(-2.0)
    assert found[0].observed == pytest.approx(0.1)


async def test_a_substructure_filter_isolates_the_class_the_aggregate_hides() -> None:
    """The whole point: the acids are twice as bad as the calculator's overall record."""
    acids = await tools.calculator_outliers("solubility", matching="C(=O)O")
    assert [r.smiles for r in acids.residuals] == ["CC(=O)O", "OC(=O)c1ccccc1"]
    everything = await tools.calculator_outliers("solubility")
    assert len(everything.residuals) == 4


async def test_a_filter_matching_nothing_returns_nothing_rather_than_everything() -> None:
    """An empty list is the honest answer; falling back to the unfiltered set would be a lie.

    And it says *which* emptiness it is: the ledger holds four measurements, none of them of a
    platinum compound, so the class is untested rather than well handled.
    """
    report = await tools.calculator_outliers("solubility", matching="[Pt]")
    assert report.residuals == [] and report.measured == 4
    assert "untested" in report.verdict


async def test_uncertainty_coverage_is_reported_per_molecule() -> None:
    """Missed by 2 log units *and* outside its own error bar is the actionable statement."""
    found = (await tools.calculator_outliers("solubility")).residuals
    assert found[0].within_uncertainty is False  # |−2.1| > 0.5
    assert found[-1].within_uncertainty is True  # |−0.1| < 0.5


def test_a_prediction_that_claimed_no_uncertainty_is_not_reported_as_a_miss() -> None:
    """`None`, not `False`: it made no claim, so there is nothing to have failed."""
    unclaimed = Residual(subject="CCO", predicted=1.0, observed=2.0, error=-1.0)
    assert unclaimed.within_uncertainty is None


async def test_limit_is_clamped_to_the_configured_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """The listing exists to be read; the cap is the deployment's, not the model's."""
    monkeypatch.setattr(settings, "calc_outliers_max_results", 2)
    assert len((await tools.calculator_outliers("solubility", limit=1000)).residuals) == 2
    assert len((await tools.calculator_outliers("solubility", limit=0)).residuals) == 1


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


async def test_the_calibrated_version_comes_from_the_server_and_never_from_here() -> None:
    """The calibrated version comes from the server, never from here.

    A locally derived version would be well-formed, match no ledger row and report `UNCALIBRATED`
    silently. Both tools take the `calculation_key` route; asserted is that the string reaching the
    ledger query is the server's and that neither tool has its own derivation.
    `tests/test_calc_remote.py` holds the static half.
    """
    asked: list[tuple[str, str]] = []

    async def _reconciled(calc_type: str, calc_version: str) -> list[Residual]:
        asked.append((calc_type, calc_version))
        return list(_LEDGER)

    async def _calibration(calc_type: str, calc_version: str, unit: str) -> Calibration:
        asked.append((calc_type, calc_version))
        return Calibration(calc_type=calc_type, unit=unit, n=0)

    with pytest.MonkeyPatch.context() as patch:
        server = install(patch, FakeCalcServer())
        patch.setattr(tools, "reconciled_for", _reconciled)
        patch.setattr(tools, "calibration_for", _calibration)
        await tools.calculator_outliers("pka")
        await tools.calculator_trust("solubility")
        # The probe molecule is configuration, and it is the only argument the derivation
        # needs — a version is a property of the programs behind a calculator, not of a
        # molecule.
        assert [args["tool"] for args in server.arguments("calculation_key")] == [
            "predict_pka",
            "predict_solubility",
        ]
        assert all(
            args["arguments"] == {"smiles": settings.calc_version_probe_smiles}
            for args in server.arguments("calculation_key")
        )

    assert asked == [("pka", FAKE_VERSION), ("solubility", FAKE_VERSION)]
