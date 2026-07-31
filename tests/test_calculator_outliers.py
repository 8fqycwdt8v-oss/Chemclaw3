"""Trust is a distribution, not a number: the residual listing and its property table (D-167).

`calculator_trust` reduced the whole ledger to six aggregates, and did it through
`if property_name == "solubility"` followed by two ternaries — so *every* other name was answered
with pKa's version and pKa's unit. An unknown property got a confident report about the wrong
calculator, and nothing in the response said so.

The listing is the other half. A model that is 0.3 log units off overall may be fine on neutrals
and two units low on every acid; the two populations average into one reassuring number, and no
aggregate can pull them apart.
"""

import asyncio

import pytest

from chemclaw.connectors.calc.server import tools
from chemclaw.core.chem import InvalidSmilesError, substructure_pattern
from chemclaw.core.config import settings
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
    """Serve the fixed ledger above instead of the database, for every test here."""

    async def _reconciled(calc_type: str, calc_version: str) -> list[Residual]:
        return list(_LEDGER)

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


def test_each_calibrated_property_reports_in_its_own_unit() -> None:
    """The unit came from the same conditional, so it was wrong for the same inputs."""

    async def _run() -> None:
        assert (await tools.calculator_outliers("solubility"))[0].unit == "log S"
        assert (await tools.calculator_outliers("pka"))[0].unit == "pKa"

    asyncio.run(_run())


def test_the_worst_miss_comes_first_and_keeps_its_sign() -> None:
    """Ranked by magnitude, reported signed: "consistently low" is correctable, scattered is not."""

    async def _run() -> None:
        found = await tools.calculator_outliers("solubility")
        assert [r.smiles for r in found][:2] == ["CC(=O)O", "OC(=O)c1ccccc1"]
        assert found[0].error == pytest.approx(-2.1)
        assert found[0].predicted == pytest.approx(-2.0)
        assert found[0].observed == pytest.approx(0.1)

    asyncio.run(_run())


def test_a_substructure_filter_isolates_the_class_the_aggregate_hides() -> None:
    """The whole point: the acids are twice as bad as the calculator's overall record."""

    async def _run() -> None:
        acids = await tools.calculator_outliers("solubility", matching="C(=O)O")
        assert [r.smiles for r in acids] == ["CC(=O)O", "OC(=O)c1ccccc1"]
        everything = await tools.calculator_outliers("solubility")
        assert len(everything) == 4

    asyncio.run(_run())


def test_a_filter_matching_nothing_returns_nothing_rather_than_everything() -> None:
    """An empty list is the honest answer; falling back to the unfiltered set would be a lie."""

    async def _run() -> None:
        assert await tools.calculator_outliers("solubility", matching="[Pt]") == []

    asyncio.run(_run())


def test_uncertainty_coverage_is_reported_per_molecule() -> None:
    """Missed by 2 log units *and* outside its own error bar is the actionable statement."""

    async def _run() -> None:
        found = await tools.calculator_outliers("solubility")
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
        assert len(await tools.calculator_outliers("solubility", limit=1000)) == 2
        assert len(await tools.calculator_outliers("solubility", limit=0)) == 1

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
