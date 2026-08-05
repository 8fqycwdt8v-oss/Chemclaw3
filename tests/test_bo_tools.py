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

import ast
import asyncio
from pathlib import Path

import pytest
import yaml

from chemclaw.connectors.bo.server import tools as bo_tools
from chemclaw.connectors.bo.server.tools import suggest_next_experiment
from chemclaw.connectors.manifest import ConnectorManifest
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
    """The description has to keep pace with the capability, and it has twice failed to.

    Four states so far, and each transition broke this test on purpose — which is the point of
    having it. Originally it asserted "One objective, no constraints … they are unrepresentable",
    because both were, and probe `op-16` was graded `fabricated` for answering that it had optimized
    "both objectives" anyway. W3 shipped multi-objective, so half that sentence became wrong and was
    replaced while the constraint half survived verbatim. W4 shipped constraints, so the other half
    went — and then, within the same wave, the exclusion turned out to be buildable after all, so
    "a forbidden combination of categories cannot be expressed" had to go too, one commit after it
    was written.

    A refusal that outlives its refusal is worse than no refusal: it teaches the model to decline a
    capability that exists. What is still *not* supported is stated in its own right rather than
    inherited from an older sentence — an exclusion needs an all-categorical problem, and a screen
    carries no constraint at all.

    Asserted against the served MCP description rather than the Python docstring, because that is
    what actually travels to the model.
    """
    from chemclaw.connectors.bo.server.tools import server

    tools = {tool.name: (tool.description or "") for tool in asyncio.run(server.list_tools())}
    description = tools["suggest_next_experiment"]
    # Supported, and said so.
    assert "Several objectives are supported" in description
    assert "front" in description
    assert "problem.constraints" in description
    assert "forbidden pairing of options" in description
    # Scoped, and said so in its own words rather than as a blanket refusal.
    assert "all-categorical" in description
    # Every stale refusal is gone.
    assert "One objective, no constraints" not in description
    assert "pick the one they led with" not in description
    assert "Constraints are still unrepresentable" not in description
    assert "cannot be expressed here" not in description


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


def test_two_categories_with_the_same_descriptor_row_are_refused() -> None:
    """The surrogate cannot tell them apart, so it answers one number for both — measured.

    Featurizing replaces a label with a position in descriptor space; BoFire's
    `CategoricalDescriptorInput` gives the model the position and nothing else. Two categories at
    one position are one point to the model. Measured before the guard existed: with A observed at
    10 and B at 90 on an otherwise identical two-descriptor parameter, `predict_at` returned the
    same 70.85 for each — a confident recommendation for a reagent never distinguished from
    another, with no warning anywhere.

    Refused at the tool boundary rather than in the model, because a campaign stored before this
    rule existed must still deserialize (the reason `require_names_do_not_clash` sits there too).
    """
    problem = OptimizationProblem(
        parameters=[
            ContinuousParameter(name="temperature", lower=20.0, upper=120.0),
            CategoricalParameter(
                name="base",
                categories=["A", "B"],
                descriptors={
                    "A": {"homo_ev": -6.1, "lumo_ev": -1.2},
                    "B": {"homo_ev": -6.1, "lumo_ev": -1.2},
                },
            ),
        ],
        objectives=[Objective(name="yield", direction="maximize")],
    )
    runs = [
        Observation(params={"temperature": 40.0, "base": "A"}, value=10.0),
        Observation(params={"temperature": 80.0, "base": "B"}, value=90.0),
        Observation(params={"temperature": 60.0, "base": "A"}, value=30.0),
    ]
    with pytest.raises(ValueError, match="identical descriptors"):
        asyncio.run(suggest_next_experiment(problem, runs))


def test_observations_json_encoded_as_a_non_array_are_refused_with_a_sentence() -> None:
    """`json.loads` decodes any JSON, so the tolerance needed a floor under it.

    The string tolerance exists for a real failure — the model sometimes sends the observations
    array JSON-encoded as one string. But three call sites decoded it and iterated the result
    unchecked, so `"42"` became an int nothing can iterate (a `TypeError` the connector reports as
    "an internal error occurred") and `"{}"` iterated its *keys*, failing with a validation error
    about strings that were never observations. Both now say what is wrong.
    """
    problem = _problem()
    with pytest.raises(ValueError, match="must be an array of objects"):
        asyncio.run(suggest_next_experiment(problem, "42"))
    with pytest.raises(ValueError, match="must be an array of objects"):
        asyncio.run(suggest_next_experiment(problem, "{}"))
    with pytest.raises(ValueError, match="not valid JSON"):
        asyncio.run(suggest_next_experiment(problem, "[{"))
    # And the tolerance itself still works: a real array, JSON-encoded, is accepted.
    encoded = (
        '[{"params": {"temperature": 40.0, "solvent": "THF"}, "value": 55.0},'
        ' {"params": {"temperature": 90.0, "solvent": "toluene"}, "value": 71.0}]'
    )
    assert asyncio.run(suggest_next_experiment(problem, encoded)).candidates


def test_every_tool_that_spends_is_declared_state_changing() -> None:
    """The manifest's partition is derived from the code, not maintained beside it.

    The `state_changing`/`read_only` split drives `agent/authz.py` and the plan gate, and it **fails
    open**: a tool wrongly listed read-only ships an ungated spend that looks exactly like a gated
    one. Both BO tools that featurize were listed read-only until a review traced the call chain —
    `featurize_problem` runs xTB per option and upserts into `calculation_results`, and
    `record_suggestion` writes two tables.

    So this reads the tool bodies rather than restating the answer: any `@server.tool()` that calls
    one of those two must appear under `state_changing`. Restating the list would pin today's names
    and stay green the day someone adds featurization to `campaign_progress`, which is the only
    change worth catching.
    """
    source = ast.parse(Path(bo_tools.__file__).read_text())
    spending = {"featurize_problem", "record_suggestion"}
    should_be_gated = {
        node.name
        for node in ast.walk(source)
        if isinstance(node, ast.AsyncFunctionDef)
        and any(isinstance(d, ast.Call) for d in node.decorator_list)
        and {
            call.func.id
            for call in ast.walk(node)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        }
        & spending
    }
    manifest = ConnectorManifest.model_validate(
        yaml.safe_load((Path(bo_tools.__file__).parents[1] / "connector.yaml").read_text())
    )
    assert manifest.endpoint is not None
    declared = set(manifest.endpoint.state_changing)
    assert should_be_gated, "the scan found no spending tool at all — it has stopped measuring"
    assert should_be_gated <= declared, (
        f"{sorted(should_be_gated - declared)} spend or write but are not declared state_changing"
    )


def test_a_cold_multi_objective_start_does_not_announce_an_empty_front() -> None:
    """With nothing measured, "front holds the 0 runs that nothing else beats" is a contradiction.

    The trade-off sentence told the model to quote a front, and named its length as zero, about a
    campaign that supplied no runs at all — leaving the model to reconcile "quote the trade-off"
    with "there is no trade-off". A model asked to resolve a contradiction resolves it by inventing.
    Now the cold case says what an empty front means: nothing measured, not nothing survived.
    """
    problem = OptimizationProblem(
        parameters=[
            ContinuousParameter(name="temperature", lower=20.0, upper=120.0),
            CategoricalParameter(name="solvent", categories=["THF", "toluene"]),
        ],
        objectives=[
            Objective(name="yield", direction="maximize"),
            Objective(name="impurity", direction="minimize"),
        ],
    )
    summary = asyncio.run(suggest_next_experiment(problem, None, count=2)).summary
    assert "no runs were supplied" in summary
    assert "nothing has been measured, not because nothing survived" in summary
    assert "quote those as the trade-off" not in summary
