"""F8-T1: a prediction says how sure it is, where that came from, and whether to trust it at all.

The row: `implementation-tickets.md` specified a `science/calc/uncertainty.py` module with
`value + uncertainty + in_domain + method` and conformal prediction where feasible; none of it
existed, so `predict_solubility` on a molecule ESOL cannot describe returned a confident number
with a training-set RMSE attached and nothing machine-readable said otherwise.

The tests below hold three claims that are easy to state and easy to get subtly wrong:

- **"unknown" is not "fine".** `in_domain=None` must never read as trustworthy.
- **The conformal interval is a claim about future predictions**, which is what the `(n + 1)` in
  the rank is for. Drop it and the interval is reliably too narrow — a coverage guarantee turned
  into an in-sample summary, which is exactly the miscalibration `calibration.py` warns about.
- **The domain check refuses rather than widens.** An out-of-domain prediction does not get a bigger
  error bar; it gets a flag, because extrapolating a linear fit leaves the distribution its
  residuals were drawn from.
"""

import pytest
from rdkit import Chem

from chemclaw.science.calc.solubility import SolubilityInput, predict_solubility
from chemclaw.science.calc.uncertainty import Estimate, conformal_uncertainty, structural_domain


def test_an_unknown_domain_is_not_a_trustworthy_one() -> None:
    """`None` means nobody checked, and nobody-checked must not read as fine.

    The whole reason `in_domain` is three-valued: a calculator with no declared domain has not
    cleared its prediction, and a consumer that treats a missing answer as an affirmative one is
    the bug the third value exists to expose.
    """
    unknown = Estimate(value=1.0, unit="log10(mol/L)", uncertainty=0.75, method="reported")
    assert unknown.in_domain is None
    assert unknown.trustworthy is False

    cleared = unknown.model_copy(update={"in_domain": True})
    assert cleared.trustworthy is True


def test_a_salt_is_out_of_domain_for_a_single_molecule_model() -> None:
    """Not "less accurate" — undefined. The equation has no term for the counter-ion."""
    in_domain, reasons = structural_domain(Chem.MolFromSmiles("CCN.Cl"))
    assert in_domain is False
    assert any("multi-component" in reason for reason in reasons)


def test_a_charged_species_is_out_of_domain() -> None:
    """Crippen contributions are parameterised for neutral species.

    An ionised form's solubility differs from its neutral form's by orders of magnitude, which is
    far outside anything a 0.75-log RMSE describes — so reporting that RMSE beside it is the
    specific misleading answer this check exists to stop.
    """
    in_domain, reasons = structural_domain(Chem.MolFromSmiles("CC(=O)[O-]"))
    assert in_domain is False
    assert any("formal charge" in reason for reason in reasons)


def test_an_organometallic_is_out_of_domain() -> None:
    """RDKit returns a logP for a ferrocene-like structure rather than refusing.

    That is the dangerous shape: a number, with no signal that the descriptor sum skipped the part
    of the molecule that matters.
    """
    in_domain, reasons = structural_domain(Chem.MolFromSmiles("[Fe]"))
    assert in_domain is False
    assert any("non-organic" in reason for reason in reasons)


def test_an_ordinary_neutral_organic_is_in_domain() -> None:
    """The check must clear the common case, or it is a flag nobody reads."""
    in_domain, reasons = structural_domain(Chem.MolFromSmiles("CCO"))
    assert in_domain is True
    assert reasons == ()


def test_solubility_reports_the_uniform_shape_and_flags_what_it_cannot_describe() -> None:
    """The row's own example, end to end: a confident number is now labelled.

    Both predictions still return a value — refusing outright would break the calculator's contract
    and hide the number a chemist may still want — but only one of them says it can be relied on.
    """
    ethanol = predict_solubility(SolubilityInput(smiles="CCO"))
    assert ethanol.estimate is not None
    assert ethanol.estimate.value == ethanol.log_s_mol_per_l
    assert ethanol.estimate.unit == "log10(mol/L)"
    assert ethanol.estimate.method == "reported"
    assert ethanol.estimate.trustworthy is True

    salt = predict_solubility(SolubilityInput(smiles="CCN.Cl"))
    assert salt.estimate is not None
    assert salt.estimate.trustworthy is False
    assert salt.estimate.domain_reasons, "an out-of-domain prediction gave no reason"
    # The uncertainty is unchanged, deliberately. Widening it would imply the error is merely larger
    # and still drawn from the same distribution, which is the claim being refused.
    assert salt.estimate.uncertainty == ethanol.estimate.uncertainty


def test_the_conformal_interval_covers_the_next_prediction_not_the_recorded_ones() -> None:
    """The `(n + 1)` in the rank is the finite-sample guarantee, and it is easy to drop.

    With 19 residuals and 90% coverage the honest rank is `ceil(20 × 0.9) = 18`, not
    `ceil(19 × 0.9) = 18`… which happens to agree, so the case that separates them is chosen
    deliberately: 9 residuals at 90% needs rank `ceil(10 × 0.9) = 9` — the largest of the nine —
    where the in-sample version would take the 9th of 9 by a different route and a 95% request
    would need rank 10 and have to refuse.
    """
    residuals = [0.1 * i for i in range(1, 10)]  # 0.1 … 0.9, n = 9
    assert conformal_uncertainty(residuals, coverage=0.9, minimum_samples=1) == pytest.approx(0.9)
    # 95% of ten future predictions cannot be bounded by nine observations:
    # ceil(10 × 0.95) = 10 > 9. Returning 0.9 anyway would state a guarantee the data lacks.
    assert conformal_uncertainty(residuals, coverage=0.95, minimum_samples=1) is None


def test_float_drift_does_not_push_the_rank_one_place_too_high() -> None:
    """`75 * 0.68` is `51.00000000000001`, so an unrounded ceiling asks for rank 52, not 51.

    The interval is then one residual wider than the guarantee requires — an overstated uncertainty,
    which is the quieter of the two ways to be wrong and the one nobody investigates. 0.68 is not a
    contrived coverage: it is one sigma, and it is the value that makes the drift visible where 0.9
    and 0.95 happen to land on exact binary fractions. (That "happen to" is the point — the first
    version of this test asserted drift at 0.95, where there is none, and passed with the rounding
    removed.)
    """
    residuals = [float(i) for i in range(1, 75)]  # n = 74; ceil(75 × 0.68) = 51 → the 51st, = 51.0
    assert conformal_uncertainty(residuals, coverage=0.68, minimum_samples=1) == pytest.approx(51.0)


def test_too_few_observations_report_nothing_rather_than_a_badly_estimated_interval() -> None:
    """Valid and badly estimated are different failures, and only the second is silent.

    Nine residuals give an arithmetically valid 90% interval whose value is the largest of nine
    numbers — one unusual compound sets it. The floor is where the caller falls back to the model's
    reported constant and says so through `method`.
    """
    residuals = [0.1 * i for i in range(1, 10)]
    assert conformal_uncertainty(residuals, coverage=0.9, minimum_samples=20) is None


def test_the_sign_of_a_residual_never_matters() -> None:
    """An interval is two-sided; residuals that happen to skew negative do not make it narrower."""
    positive = [float(i) for i in range(1, 21)]
    mixed = [float(i) if i % 2 else -float(i) for i in range(1, 21)]
    assert conformal_uncertainty(
        positive, coverage=0.9, minimum_samples=1
    ) == conformal_uncertainty(mixed, coverage=0.9, minimum_samples=1)
