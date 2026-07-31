"""Behavioral tests for xTB descriptor featurization of categorical BO choices (U1).

Real GFN2-xTB calculations. The point of featurization is that the surrogate stops seeing
an opaque label and starts seeing a position in chemical space, so the tests check both
halves: that the descriptors are chemically meaningful, and that BoFire actually *uses*
them rather than falling back to an ordinal index.
"""

import asyncio

import pandas as pd
import pytest
from bofire.data_models.enum import CategoricalEncodingEnum
from bofire.data_models.features.api import CategoricalDescriptorInput, CategoricalInput
from bofire.data_models.strategies.api import SoboStrategy
from bofire.strategies import api as strategies

from chemclaw.science.bo.engine import _to_domain, propose_candidates
from chemclaw.science.bo.featurize import DESCRIPTOR_NAMES, featurize_problem
from chemclaw.science.bo.problem import (
    CategoricalParameter,
    ContinuousParameter,
    Objective,
    Observation,
    OptimizationProblem,
)
from chemclaw.science.calc.store import InMemoryStore

# Phosphine ligands whose structures are unambiguous, spanning the donor range a
# cross-coupling ligand screen actually covers: two trialkyl (strong donors, one bulky)
# and two triaryl (weaker donors, low-lying pi* orbitals).
_LIGANDS = {
    "PMe3": "CP(C)C",
    "PtBu3": "CC(C)(C)P(C(C)(C)C)C(C)(C)C",
    "PCy3": "C1CCCCC1P(C1CCCCC1)C1CCCCC1",
    "PPh3": "c1ccc(cc1)P(c1ccccc1)c1ccccc1",
}


def _ligand_problem(structures: dict[str, str] | None = _LIGANDS) -> OptimizationProblem:
    """A ligand-choice-plus-temperature problem, featurized or not."""
    return OptimizationProblem(
        parameters=[
            CategoricalParameter(name="ligand", categories=sorted(_LIGANDS), structures=structures),
            ContinuousParameter(name="temperature", lower=20.0, upper=100.0),
        ],
        objective=Objective(name="yield", direction="maximize"),
    )


def _featurized(problem: OptimizationProblem) -> OptimizationProblem:
    return asyncio.run(featurize_problem(InMemoryStore(), problem)).problem


def test_featurization_describes_every_category() -> None:
    """Each category gains the full descriptor vector, with finite values."""
    parameter = _featurized(_ligand_problem()).parameters[0]
    assert isinstance(parameter, CategoricalParameter)
    assert parameter.descriptors is not None
    assert set(parameter.descriptors) == set(_LIGANDS)
    for row in parameter.descriptors.values():
        assert set(row) == set(DESCRIPTOR_NAMES)
        assert all(value == value for value in row.values())  # no NaN


def test_descriptors_capture_real_donor_chemistry() -> None:
    """The descriptor space separates the ligands the way a chemist would.

    Trialkylphosphines are stronger sigma-donors than triarylphosphines, which shows up as
    a higher HOMO; the aryl ligand's low-lying pi* shows up as a much lower LUMO. If the
    featurization did not carry real chemistry this ordering would not survive.
    """
    parameter = _featurized(_ligand_problem()).parameters[0]
    assert isinstance(parameter, CategoricalParameter)
    assert parameter.descriptors is not None
    descriptors = parameter.descriptors
    assert descriptors["PtBu3"]["homo_ev"] > descriptors["PMe3"]["homo_ev"]
    assert descriptors["PPh3"]["lumo_ev"] < descriptors["PCy3"]["lumo_ev"]


def test_gap_is_excluded_as_a_collinear_descriptor() -> None:
    """HOMO-LUMO gap is exactly lumo - homo, so shipping it too would be a redundant column."""
    assert "gap_ev" not in DESCRIPTOR_NAMES


def test_featurized_parameter_maps_to_a_descriptor_input() -> None:
    """A featurized categorical becomes a descriptor input; a bare one stays categorical."""
    featurized = _to_domain(_featurized(_ligand_problem())).inputs.features[0]
    bare = _to_domain(_ligand_problem(structures=None)).inputs.features[0]
    assert isinstance(featurized, CategoricalDescriptorInput)
    assert isinstance(bare, CategoricalInput) and not isinstance(bare, CategoricalDescriptorInput)


def test_descriptor_matrix_rows_and_columns_follow_the_declared_order() -> None:
    """BoFire matches the values matrix by position, so a transposed row would mislabel chemistry.

    This is the one mapping bug that would produce a working campaign built on the wrong
    molecules, so it is asserted directly rather than trusted.
    """
    # One featurization, used for both sides: two independent runs would differ in tblite's
    # ~1e-12 SCF noise, which would make this an exact-float test of the wrong thing.
    problem = _featurized(_ligand_problem())
    parameter = problem.parameters[0]
    assert isinstance(parameter, CategoricalParameter)
    assert parameter.descriptors is not None
    feature = _to_domain(problem).inputs.features[0]
    assert isinstance(feature, CategoricalDescriptorInput)
    for row_index, category in enumerate(feature.categories):
        for column_index, name in enumerate(feature.descriptors):
            assert feature.values[row_index][column_index] == parameter.descriptors[category][name]


def test_the_surrogate_actually_uses_the_descriptors() -> None:
    """BoFire descriptor-encodes a featurized categorical and ordinal-encodes a bare one.

    The load-bearing assertion of U1. Descriptor encoding is a BoFire *default* we depend
    on rather than set, and the failure mode if it changed is silent: the campaign would
    still run, still return candidates, and simply stop generalizing across ligands.
    """

    def encoding(problem: OptimizationProblem) -> CategoricalEncodingEnum:
        strategy = SoboStrategy(domain=_to_domain(problem), seed=1)
        surrogate = strategy.surrogate_specs.surrogates[0]
        return surrogate.categorical_encodings["ligand"]

    assert encoding(_featurized(_ligand_problem())) == CategoricalEncodingEnum.DESCRIPTOR
    assert encoding(_ligand_problem(structures=None)) == CategoricalEncodingEnum.ORDINAL


def test_a_featurized_campaign_still_proposes_real_categories() -> None:
    """Descriptors change how the space is modelled, never what a valid candidate is."""
    problem = _featurized(_ligand_problem())
    observations = [
        Observation(params={"ligand": "PPh3", "temperature": 30.0}, value=40.0),
        Observation(params={"ligand": "PtBu3", "temperature": 80.0}, value=75.0),
        Observation(params={"ligand": "PMe3", "temperature": 50.0}, value=55.0),
    ]
    for candidate in propose_candidates(problem, observations, 2):
        assert candidate.params["ligand"] in _LIGANDS
        assert 20.0 <= float(candidate.params["temperature"]) <= 100.0


def test_descriptors_inform_a_ligand_that_was_never_run() -> None:
    """The payoff of U1, measured on the surrogate's own prediction.

    Three ligands are observed and PCy3 is not. Without descriptors the surrogate has
    literally no information about PCy3: its prediction collapses to the mean of the
    observed values, because an ordinal code for an unseen category carries nothing. With
    descriptors, PCy3 sits near the high-performing PtBu3 in electronic space and the
    prediction moves accordingly.

    This is the assertion that would fail if the featurization were wired up but inert —
    the failure mode a candidate-shape test cannot see.
    """
    observations = pd.DataFrame(
        [
            {"ligand": "PMe3", "temperature": 50.0, "yield": 10.0, "valid_yield": 1},
            {"ligand": "PPh3", "temperature": 50.0, "yield": 15.0, "valid_yield": 1},
            {"ligand": "PtBu3", "temperature": 50.0, "yield": 90.0, "valid_yield": 1},
        ]
    )
    uninformed = observations["yield"].mean()

    def predict_unobserved(problem: OptimizationProblem) -> float:
        strategy = strategies.map(SoboStrategy(domain=_to_domain(problem), seed=1))
        strategy.tell(observations)
        prediction = strategy.predict(pd.DataFrame([{"ligand": "PCy3", "temperature": 50.0}]))
        return float(prediction["yield_pred"].iloc[0])

    assert predict_unobserved(_ligand_problem(structures=None)) == pytest.approx(
        uninformed, abs=1.0
    )
    assert abs(predict_unobserved(_featurized(_ligand_problem())) - uninformed) > 2.0


def test_parameters_without_structures_pass_through_untouched() -> None:
    """Featurization is opt-in: a non-molecular categorical is left exactly as it was."""
    problem = _ligand_problem(structures=None)
    assert _featurized(problem) == problem


def test_partial_structures_are_rejected() -> None:
    """A structures map that misses a category is malformed, not partial (G4)."""
    with pytest.raises(ValueError, match="must cover exactly the categories"):
        CategoricalParameter(
            name="ligand", categories=["PMe3", "PPh3"], structures={"PMe3": "CP(C)C"}
        )


def test_ragged_descriptors_are_rejected() -> None:
    """Every category must carry the same descriptor names — BoFire wants a dense matrix."""
    with pytest.raises(ValueError, match="same descriptors"):
        CategoricalParameter(
            name="ligand",
            categories=["a", "b"],
            descriptors={"a": {"homo_ev": -9.0}, "b": {"lumo_ev": -3.0}},
        )


def test_an_unfeaturizable_category_names_itself() -> None:
    """The error says which option failed, because that is the only actionable part."""
    problem = _ligand_problem(structures={**_LIGANDS, "PMe3": "not a molecule"})
    with pytest.raises(ValueError, match="cannot featurize category 'PMe3'"):
        _featurized(problem)


def test_featurization_is_cached() -> None:
    """Re-featurizing the same molecules costs nothing — the second pass is all store hits."""

    async def _run() -> None:
        store = InMemoryStore()
        first = (await featurize_problem(store, _ligand_problem())).problem
        second = (await featurize_problem(store, _ligand_problem())).problem
        assert first == second

    asyncio.run(_run())
