"""Tests for the next-experiment agent tool (plan Phase 1d, agent surface).

Proves the agent can turn a decision space + historic runs into a concrete suggestion without
a durable workflow: a fresh problem yields seed points, a problem with observations yields
model-guided candidates inside the space, and a batch returns the asked-for count. BoFire runs
in-process (no Temporal), the same as the campaign tests.

Also pins the tool's own input contract: an observation whose parameters do not match the declared
decision space is refused here, naming the observation and the parameter, rather than reaching
BoFire and coming back as an internal `KeyError` the connector must sanitize into an unrepairable
"an internal error occurred".
"""

import asyncio

import pytest

from chemclaw.connectors.bo.server import tools as bo_tools
from chemclaw.connectors.bo.server.tools import suggest_next_experiment
from chemclaw.science.bo.problem import (
    CategoricalParameter,
    ContinuousParameter,
    Objective,
    Observation,
    OptimizationProblem,
)
from chemclaw.science.calc.store import InMemoryStore


def _problem() -> OptimizationProblem:
    """Maximize yield over a temperature range and a choice of two solvents."""
    return OptimizationProblem(
        parameters=[
            ContinuousParameter(name="temperature", lower=20.0, upper=120.0),
            CategoricalParameter(name="solvent", categories=["THF", "toluene"]),
        ],
        objectives=[Objective(name="yield", direction="maximize")],
    )


def test_seeds_when_no_observations() -> None:
    """With no runs yet, the tool returns space-filling seed points inside the space."""
    problem = _problem()
    suggestion = asyncio.run(suggest_next_experiment(problem, None, count=3))
    candidates = suggestion.candidates
    assert len(candidates) == 3
    for candidate in candidates:
        temperature = candidate.params["temperature"]
        assert isinstance(temperature, float) and 20.0 <= temperature <= 120.0
        assert candidate.params["solvent"] in {"THF", "toluene"}


def test_proposes_from_observations() -> None:
    """Given past runs, the tool proposes a model-guided next point in the space."""
    problem = _problem()
    observations = [
        Observation(params={"temperature": 40.0, "solvent": "THF"}, value=55.0),
        Observation(params={"temperature": 80.0, "solvent": "THF"}, value=78.0),
        Observation(params={"temperature": 100.0, "solvent": "toluene"}, value=64.0),
    ]
    candidates = asyncio.run(suggest_next_experiment(problem, observations)).candidates
    assert len(candidates) == 1
    temperature = candidates[0].params["temperature"]
    assert isinstance(temperature, float) and 20.0 <= temperature <= 120.0
    assert candidates[0].params["solvent"] in {"THF", "toluene"}


def test_accepts_plain_dicts_as_maf_actually_delivers_them() -> None:
    """Regression: MAF calls this tool with plain dicts, not `OptimizationProblem`/`Observation`.

    The agent-framework function-tool boundary validates a call's arguments against the JSON
    schema derived from this signature, then invokes the function with that payload
    `model_dump()`-ed back to plain dicts/lists (the tool-call wire format has no model concept)
    — never with reconstructed instances. Every direct/test caller above passes real model
    instances and would not have caught a regression here; this reproduces the actual shape a
    live turn delivers.
    """
    problem = {
        "parameters": [
            {"kind": "continuous", "name": "temperature", "lower": 20.0, "upper": 120.0},
            {"kind": "categorical", "name": "solvent", "categories": ["THF", "toluene"]},
        ],
        "objective": {"name": "yield", "direction": "maximize"},
    }
    observations = [
        {"params": {"temperature": 40.0, "solvent": "THF"}, "value": 55.0},
        {"params": {"temperature": 80.0, "solvent": "THF"}, "value": 78.0},
    ]
    candidates = asyncio.run(suggest_next_experiment(problem, observations)).candidates
    assert len(candidates) == 1
    temperature = candidates[0].params["temperature"]
    assert isinstance(temperature, float) and 20.0 <= temperature <= 120.0


def test_accepts_observations_json_encoded_as_a_string() -> None:
    """Regression: on a large call, the model sometimes emits `observations` JSON-encoded.

    A single string instead of a real array — a live e2e finding on a 6-parameter problem. MAF's
    schema validation rejected the whole call before this function ever ran, with no detail
    reaching the model to self-correct from ("Error: Argument parsing failed.", no exception
    text). Accepting the string here and decoding it makes the tool robust to that formatting
    slip instead of relying on the model to notice and retry blind.
    """
    problem = _problem()
    observations_json = (
        '[{"params": {"temperature": 40.0, "solvent": "THF"}, "value": 55.0}, '
        '{"params": {"temperature": 80.0, "solvent": "THF"}, "value": 78.0}]'
    )
    candidates = asyncio.run(
        suggest_next_experiment(problem, observations_json, count=1)
    ).candidates
    assert len(candidates) == 1
    temperature = candidates[0].params["temperature"]
    assert isinstance(temperature, float) and 20.0 <= temperature <= 120.0


def _three_factor_problem() -> OptimizationProblem:
    """The live-run shape: a base choice beside the temperature and solvent factors."""
    return OptimizationProblem(
        parameters=[
            ContinuousParameter(name="temperature", lower=20.0, upper=120.0),
            CategoricalParameter(name="solvent", categories=["THF", "toluene"]),
            CategoricalParameter(name="base", categories=["NEt3", "pyridine"]),
        ],
        objectives=[Objective(name="yield", direction="maximize")],
    )


def test_an_observation_missing_a_declared_parameter_names_it_and_its_index() -> None:
    """A declared parameter absent from an observation is refused before BoFire sees anything.

    This is the class of fault behind the live `KeyError: 'base'` from inside BoFire's
    `_optimize_acqf_discrete`, which `connectors/server.py` correctly refuses to forward verbatim
    and so delivered to the model as "an internal error occurred". BoFire does already raise a
    well-worded `ValueError` of its own on this direction (measured), so what is pinned here is
    the *index*: with six observations in the call, "invalid values for `base`" does not say which
    one to repair, and this message does.
    """
    observations = [
        Observation(params={"temperature": 40.0, "solvent": "THF", "base": "NEt3"}, value=55.0),
        Observation(params={"temperature": 80.0, "solvent": "THF"}, value=78.0),
    ]
    with pytest.raises(ValueError, match=r"observations\[1\].*'base'"):
        asyncio.run(suggest_next_experiment(_three_factor_problem(), observations))


def test_an_observation_naming_an_undeclared_parameter_names_it() -> None:
    """The mirror fault, and the one that was not failing loudly at all.

    Measured against this BoFire version before the check existed: an extra key is *silently
    ignored*, the ask succeeds, and candidates come back from a decision space that quietly
    dropped a condition the chemist reported. So this direction is not about error wording — it
    turns a confidently wrong answer into a question the caller can fix.
    """
    observations = [
        Observation(
            params={"temperature": 40.0, "solvent": "THF", "base": "NEt3", "ligand": "PPh3"},
            value=55.0,
        ),
        Observation(params={"temperature": 80.0, "solvent": "THF", "base": "pyridine"}, value=78.0),
    ]
    with pytest.raises(ValueError, match=r"observations\[0\].*'ligand'.*does not declare"):
        asyncio.run(suggest_next_experiment(_three_factor_problem(), observations))


def test_matching_observations_are_unaffected() -> None:
    """Regression guard: the check passes a well-formed three-factor call straight through."""
    observations = [
        Observation(params={"temperature": 40.0, "solvent": "THF", "base": "NEt3"}, value=55.0),
        Observation(params={"temperature": 80.0, "solvent": "THF", "base": "pyridine"}, value=78.0),
        Observation(
            params={"temperature": 100.0, "solvent": "toluene", "base": "NEt3"}, value=64.0
        ),
    ]
    candidates = asyncio.run(
        suggest_next_experiment(_three_factor_problem(), observations)
    ).candidates
    assert len(candidates) == 1
    assert candidates[0].params["base"] in {"NEt3", "pyridine"}


def test_the_descriptor_bearing_path_is_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression guard on the `structures` path, which the boundary check now runs ahead of.

    Featurization rewrites the *parameters* (it fills `descriptors`) but never their names, so
    the check has to sit before it and still agree with what BoFire is finally handed. Real xTB
    through an in-memory store, so this exercises the whole path rather than a stubbed one.
    """
    monkeypatch.setattr(bo_tools, "default_store", InMemoryStore)
    problem = OptimizationProblem(
        parameters=[
            ContinuousParameter(name="temperature", lower=20.0, upper=120.0),
            CategoricalParameter(
                name="base",
                categories=["NEt3", "pyridine"],
                structures={"NEt3": "CCN(CC)CC", "pyridine": "c1ccncc1"},
            ),
        ],
        objectives=[Objective(name="yield", direction="maximize")],
    )
    observations = [
        Observation(params={"temperature": 40.0, "base": "NEt3"}, value=55.0),
        Observation(params={"temperature": 80.0, "base": "pyridine"}, value=78.0),
    ]
    suggestion = asyncio.run(suggest_next_experiment(problem, observations))
    assert len(suggestion.candidates) == 1
    assert suggestion.candidates[0].params["base"] in {"NEt3", "pyridine"}
    assert suggestion.calc_refs  # the descriptors really were computed, not skipped


def test_the_seeding_path_with_no_observations_is_unaffected() -> None:
    """Nothing to check when there are no observations — seeding must not be made harder."""
    suggestion = asyncio.run(suggest_next_experiment(_three_factor_problem(), [], count=2))
    assert len(suggestion.candidates) == 2


def test_the_tool_the_model_sees_states_what_is_and_is_not_supported() -> None:
    """The description must be true about *both* halves of story 3.3, and they now differ.

    This test used to assert the opposite — that the description said "One objective, no
    constraints … they are unrepresentable" — because both were, and a live probe (`op-16`) was
    graded `fabricated` for answering that it had optimized "both objectives" anyway.

    Multi-objective shipped in W3, so that sentence became **wrong**, and a refusal instruction that
    outlives its refusal is worse than none: it teaches the model to decline a capability that
    exists. Constraints did not ship, so that half of the refusal has to survive verbatim. Inverting
    the whole assertion would have been as wrong as leaving it.

    Asserted against the served MCP description rather than the Python docstring, because that is
    what actually travels to the model.
    """
    from chemclaw.connectors.bo.server.tools import server

    tools = {tool.name: (tool.description or "") for tool in asyncio.run(server.list_tools())}
    description = tools["suggest_next_experiment"]
    # Supported, and said so.
    assert "Several objectives are supported" in description
    assert "front" in description
    # Still not supported, and still said so.
    assert "Constraints are still unrepresentable" in description
    # The stale refusal is gone.
    assert "One objective, no constraints" not in description
    assert "pick the one they led with" not in description


# --- multi-objective (W3) ---------------------------------------------------------------------


def _trade_off_problem() -> OptimizationProblem:
    """Maximize yield while minimizing an impurity — the shape every ELN run already records."""
    return OptimizationProblem(
        parameters=[
            ContinuousParameter(name="temperature", lower=20.0, upper=120.0),
            CategoricalParameter(name="solvent", categories=["THF", "toluene"]),
        ],
        objectives=[
            Objective(name="yield", direction="maximize"),
            Objective(name="impurity", direction="minimize"),
        ],
    )


def _trade_off_runs() -> list[Observation]:
    """Four runs where no single one wins on both axes, plus one that loses on both."""
    return [
        Observation(
            params={"temperature": 40.0, "solvent": "THF"},
            value=55.0,
            values={"yield": 55.0, "impurity": 1.0},
        ),
        Observation(
            params={"temperature": 80.0, "solvent": "THF"},
            value=78.0,
            values={"yield": 78.0, "impurity": 4.0},
        ),
        Observation(
            params={"temperature": 100.0, "solvent": "toluene"},
            value=64.0,
            values={"yield": 64.0, "impurity": 2.0},
        ),
        # Dominated: worse yield and worse impurity than the 40C/THF run.
        Observation(
            params={"temperature": 30.0, "solvent": "toluene"},
            value=50.0,
            values={"yield": 50.0, "impurity": 3.0},
        ),
    ]


def test_a_two_objective_ask_returns_a_front_of_the_runs_supplied() -> None:
    """The front is what turns "here is the trade-off" from a sentence into a computation."""
    suggestion = asyncio.run(suggest_next_experiment(_trade_off_problem(), _trade_off_runs()))
    on_front = {(o.values["yield"], o.values["impurity"]) for o in suggestion.front}
    assert on_front == {(55.0, 1.0), (78.0, 4.0), (64.0, 2.0)}
    assert (50.0, 3.0) not in on_front, "a run beaten on both axes is not on the front"


def test_a_single_objective_ask_draws_no_front() -> None:
    """One objective has one best point, and a "front" there would invite reading a trade-off."""
    suggestion = asyncio.run(
        suggest_next_experiment(
            _problem(),
            [
                Observation(params={"temperature": 40.0, "solvent": "THF"}, value=55.0),
                Observation(params={"temperature": 80.0, "solvent": "THF"}, value=78.0),
            ],
        )
    )
    assert suggestion.front == []
    assert len(suggestion.scales) == 1


def test_the_summary_says_there_is_no_single_best_point() -> None:
    """The caveat has to reach the model composing the answer, not just this file."""
    suggestion = asyncio.run(suggest_next_experiment(_trade_off_problem(), _trade_off_runs()))
    assert "trade-off over 2 objectives" in suggestion.summary
    assert "no single best point" in suggestion.summary
    assert "summary" in suggestion.model_dump(mode="json")


def test_each_objective_gets_its_own_scale() -> None:
    """An sd is read against its own objective's spread; yield's spread says nothing about ppm."""
    suggestion = asyncio.run(suggest_next_experiment(_trade_off_problem(), _trade_off_runs()))
    by_name = {scale.name: scale for scale in suggestion.scales}
    assert by_name["yield"].spread == pytest.approx(28.0)
    assert by_name["impurity"].spread == pytest.approx(3.0)
    assert by_name["impurity"].direction == "minimize"


def test_candidates_carry_a_prediction_per_objective() -> None:
    """M-1 measured `<objective>_pred`/`_sd` per objective; this is that reaching the caller."""
    suggestion = asyncio.run(suggest_next_experiment(_trade_off_problem(), _trade_off_runs()))
    candidate = suggestion.candidates[0]
    assert set(candidate.predicted_values) == {"yield", "impurity"}
    assert set(candidate.predicted_sds) == {"yield", "impurity"}
    # The scalars keep the lead objective, which is what every persisted row already holds.
    assert candidate.predicted_value == pytest.approx(candidate.predicted_values["yield"])


def test_an_observation_missing_an_objective_is_refused_by_index() -> None:
    """A run that reports yield but not the impurity cannot seed a trade-off, and says so."""
    runs = _trade_off_runs()
    runs[1] = Observation(params=runs[1].params, value=78.0, values={"yield": 78.0})
    with pytest.raises(ValueError, match=r"observations\[1\]"):
        asyncio.run(suggest_next_experiment(_trade_off_problem(), runs))


def test_an_observation_that_disagrees_with_itself_is_refused() -> None:
    """`value` is the lead objective's number and both are persisted, so they cannot differ."""
    runs = _trade_off_runs()
    runs[0] = Observation(
        params=runs[0].params, value=55.0, values={"yield": 61.0, "impurity": 1.0}
    )
    with pytest.raises(ValueError, match="disagrees with itself"):
        asyncio.run(suggest_next_experiment(_trade_off_problem(), runs))
