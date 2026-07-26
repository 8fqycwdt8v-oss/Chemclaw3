"""Tests for the next-experiment agent tool (plan Phase 1d, agent surface).

Proves the agent can turn a decision space + historic runs into a concrete suggestion without
a durable workflow: a fresh problem yields seed points, a problem with observations yields
model-guided candidates inside the space, and a batch returns the asked-for count. BoFire runs
in-process (no Temporal), the same as the campaign tests.
"""

import asyncio

from agents.bo_tools import suggest_next_experiment
from bo.problem import (
    CategoricalParameter,
    ContinuousParameter,
    Objective,
    Observation,
    OptimizationProblem,
)


def _problem() -> OptimizationProblem:
    """Maximize yield over a temperature range and a choice of two solvents."""
    return OptimizationProblem(
        parameters=[
            ContinuousParameter(name="temperature", lower=20.0, upper=120.0),
            CategoricalParameter(name="solvent", categories=["THF", "toluene"]),
        ],
        objective=Objective(name="yield", direction="maximize"),
    )


def test_seeds_when_no_observations() -> None:
    """With no runs yet, the tool returns space-filling seed points inside the space."""
    problem = _problem()
    candidates = asyncio.run(suggest_next_experiment(problem, None, count=3))
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
    candidates = asyncio.run(suggest_next_experiment(problem, observations))
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
    candidates = asyncio.run(suggest_next_experiment(problem, observations))  # type: ignore[arg-type]
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
    candidates = asyncio.run(suggest_next_experiment(problem, observations_json, count=1))
    assert len(candidates) == 1
    temperature = candidates[0].params["temperature"]
    assert isinstance(temperature, float) and 20.0 <= temperature <= 120.0
