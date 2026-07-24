"""Tests for the durable BO campaign (plan step 1d.4).

The registry and activities are exercised directly (fast, no server). The full
durable workflow runs on Temporal's time-skipping server in CI and skips in the
offline sandbox — proving a real reaction campaign runs end-to-end and resumably.
"""

import asyncio
import warnings
from collections.abc import Callable, Sequence
from typing import Any

import pytest
from temporalio.client import Client
from temporalio.worker import Worker

from bo.benchmarks.reizman_suzuki import build_problem, load_dataset
from bo.campaign import optimize
from bo.objectives import (
    MOLECULE_KEY,
    get_objective,
    molecule_library_problem,
    solubility_objective,
)
from bo.problem import (
    CampaignSpec,
    CategoricalParameter,
    ContinuousParameter,
    Objective,
    Observation,
    OptimizationProblem,
    Parameter,
    ParamValue,
    best_of,
    discrete_candidate_count,
    distinct_candidate_count,
    require_rounds_within_ceiling,
)
from calc.solubility import SolubilityInput, predict_solubility
from calc.store import InMemoryStore
from chemclaw.chem import InvalidSmilesError
from chemclaw.config import settings
from tests.temporal_env import pydantic_client, start_env_or_skip
from workflows.bo_activities import evaluate_candidates, propose_initial, propose_next
from workflows.bo_campaign import BoCampaignWorkflow

warnings.filterwarnings("ignore")

_BO_ACTIVITIES: Sequence[Callable[..., Any]] = [propose_initial, propose_next, evaluate_candidates]


def test_get_objective_unknown_raises() -> None:
    """An unknown objective name is a clear error listing the known ones (G4)."""
    with pytest.raises(ValueError, match="unknown objective"):
        get_objective("does-not-exist")


@pytest.mark.parametrize("n_initial", [0, 1])
def test_campaign_spec_rejects_insufficient_seed(n_initial: int) -> None:
    """n_initial below the surrogate floor (2) fails at spec time, not at round 1.

    BoFire's SOBO strategy needs at least two experiments to fit; a spec with
    fewer would burn its seed evaluations and then crash non-retryably.
    """
    problem = build_problem(load_dataset())
    with pytest.raises(ValueError, match="greater than or equal to 2"):
        CampaignSpec(problem=problem, objective_name="reizman_suzuki", n_initial=n_initial)


def test_campaign_spec_carries_per_campaign_seed() -> None:
    """The spec is the per-campaign seed seam; unset means the config default."""
    problem = build_problem(load_dataset())
    spec = CampaignSpec(problem=problem, objective_name="reizman_suzuki")
    assert spec.seed is None  # engine resolves None to settings.bo_seed
    replicate = spec.model_copy(update={"seed": 7})
    assert replicate.seed == 7


def test_best_of_honors_direction() -> None:
    """best_of picks max for maximize and min for minimize."""
    params: list[Parameter] = [ContinuousParameter(name="x", lower=0.0, upper=1.0)]
    observations = [
        Observation(params={"x": 0.0}, value=1.0),
        Observation(params={"x": 1.0}, value=5.0),
    ]
    maximize = OptimizationProblem(
        parameters=params, objective=Objective(name="y", direction="maximize")
    )
    minimize = OptimizationProblem(
        parameters=params, objective=Objective(name="y", direction="minimize")
    )
    assert best_of(maximize, observations).value == 5.0
    assert best_of(minimize, observations).value == 1.0


def test_activities_seed_and_evaluate() -> None:
    """The seed and evaluate activities produce candidates and scored observations."""

    async def _run() -> None:
        problem = build_problem(load_dataset())
        seed = await propose_initial(problem, 3)
        assert len(seed) == 3
        observations = await evaluate_candidates("reizman_suzuki", seed)
        assert len(observations) == 3
        assert all(o.value >= 0 for o in observations)  # yields are non-negative

    asyncio.run(_run())


def test_solubility_objective_scores_via_calculator() -> None:
    """The calculator-backed objective (1d.3) scores a molecule via the cached calculator."""

    async def _run() -> None:
        store = InMemoryStore()
        objective = solubility_objective(store)

        ethanol = await objective({MOLECULE_KEY: "CCO"})
        hexadecane = await objective({MOLECULE_KEY: "CCCCCCCCCCCCCCCC"})

        # The objective returns exactly the calculator's predicted log S...
        assert ethanol == predict_solubility(SolubilityInput(smiles="CCO")).log_s_mol_per_l
        assert ethanol > hexadecane  # ethanol far more soluble than the alkane
        # ...and a repeat is served from the store (same value, no recompute error).
        assert await objective({MOLECULE_KEY: "CCO"}) == ethanol

    asyncio.run(_run())


def test_get_objective_resolves_calculator_objective() -> None:
    """The calculator-backed objective is registered and resolvable by name."""
    assert callable(get_objective("solubility_max"))


def test_candidate_set_bo_finds_soluble_molecule() -> None:
    """Candidate-set BO over a molecule library finds a top molecule sub-exhaustively."""

    async def _run() -> None:
        store = InMemoryStore()
        # 14 diverse molecules; only a few (glycerol, glycol, water, urea) are very soluble.
        library = [
            "CCCCCCCCCCCCCCCC",
            "c1ccccc1",
            "CCCCCCCC",
            "CCCCCCO",
            "CCO",
            "O",
            "OCC(O)CO",
            "NC(=O)N",
            "CC(=O)O",
            "Oc1ccccc1",
            "CCOCC",
            "ClCCl",
            "CCCCCCCCCCCC",
            "OCCO",
        ]
        problem = molecule_library_problem(library)

        result = await optimize(problem, solubility_objective(store), n_initial=4, n_rounds=5)

        all_values = sorted(
            predict_solubility(SolubilityInput(smiles=s)).log_s_mol_per_l for s in library
        )
        median = all_values[len(all_values) // 2]
        assert len(result.history) < len(library)  # BO did not evaluate the whole library
        assert result.best.value > median  # yet steered to a soluble molecule (top half)

    asyncio.run(_run())


def test_discrete_candidate_count() -> None:
    """Pure-categorical spaces are finite (product of categories); mixed spaces are infinite."""
    assert discrete_candidate_count(molecule_library_problem(["CCO", "O", "c1ccccc1"])) == 3
    assert discrete_candidate_count(build_problem(load_dataset())) is None  # has continuous dims


def test_molecule_library_rejects_bad_smiles_up_front() -> None:
    """One unparseable library entry fails at problem construction, naming the entry.

    Without this, the campaign would fail non-retryably only when BO finally
    proposes the bad molecule, discarding all completed rounds.
    """
    with pytest.raises(InvalidSmilesError, match="C1CC"):
        molecule_library_problem(["CCO", "C1CC", "O"])


def test_molecule_library_collapses_duplicate_spellings() -> None:
    """Two spellings of one molecule become one candidate, not two."""
    problem = molecule_library_problem(["CCO", "OCC", "O"])
    parameter = problem.parameters[0]
    assert isinstance(parameter, CategoricalParameter)
    assert parameter.categories == ["CCO", "O"]


def test_optimize_stops_gracefully_on_exhausted_discrete_space() -> None:
    """A budget exceeding the discrete space stops cleanly instead of crashing in BoFire."""

    async def _run() -> None:
        store = InMemoryStore()
        library = ["CCO", "O", "c1ccccc1", "CCCCCCCCCCCCCCCC"]  # only 4 candidates
        problem = molecule_library_problem(library)

        # Budget 2 + 10 far exceeds the 4-candidate space; must not raise.
        result = await optimize(problem, solubility_objective(store), n_initial=2, n_rounds=10)

        best_possible = max(
            predict_solubility(SolubilityInput(smiles=s)).log_s_mol_per_l for s in library
        )
        assert distinct_candidate_count(result.history) <= len(library)
        assert result.best.value == pytest.approx(best_possible)

    asyncio.run(_run())


def test_durable_campaign_runs_end_to_end() -> None:
    """The workflow runs a small Reizman campaign durably and returns a correct result.

    This test's job is the *durable workflow* — that a real campaign seeds, runs its
    rounds, and returns a complete, correctly-reduced result across the Temporal
    serialization boundary. It deliberately does not assert an absolute yield (e.g.
    "beats the dataset median"): a 6-evaluation campaign can't clear that reliably, and
    the BoTorch acqf optimizer's trajectory differs across BLAS/scipy builds, so such a
    threshold is platform-flaky. Optimization *quality* is covered deterministically by
    `test_bo.py`'s convergence tests and `test_candidate_set_bo_finds_soluble_molecule`.
    """

    async def _run() -> None:
        spec = CampaignSpec(
            problem=build_problem(load_dataset()),
            objective_name="reizman_suzuki",
            n_initial=4,
            n_rounds=2,
        )
        async with await start_env_or_skip() as env:
            client: Client = pydantic_client(env)
            async with Worker(
                client,
                task_queue="test-bo",
                workflows=[BoCampaignWorkflow],
                activities=_BO_ACTIVITIES,
            ):
                result = await client.execute_workflow(
                    BoCampaignWorkflow.run,
                    spec,
                    id="bo-campaign-test",
                    task_queue="test-bo",
                )
        # Every round ran and every point was actually evaluated by the objective.
        assert len(result.history) == 6  # 4 seed + 2 rounds x batch 1
        assert all(o.provenance == "predicted" for o in result.history)
        # The best that survived serialization is the true optimum of the returned
        # history — i.e. the durable reduce is correct, not desynced from the history.
        assert result.best == best_of(spec.problem, result.history)

    asyncio.run(_run())


def test_round_ceiling_is_enforced_at_creation_not_in_the_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`require_rounds_within_ceiling` gates creation; the spec model itself stays config-free.

    The workflow re-sends the full observation history to every propose round, so an unbounded
    round count grows Temporal event history quadratically until the server terminates the
    campaign — hence the config-backed ceiling. But `CampaignSpec` crosses the Temporal
    serialization boundary: a model validator reading live `bo_max_rounds` would make an
    in-flight campaign's own input fail deserialization at replay when the setting is lowered.
    So the ceiling is a creation-time check, and a spec serialized under a higher ceiling must
    still round-trip after the ceiling drops.
    """
    monkeypatch.setattr(settings, "bo_max_rounds", 3)
    with pytest.raises(ValueError, match="bo_max_rounds=3"):
        require_rounds_within_ceiling(4)
    require_rounds_within_ceiling(3)  # at the ceiling is fine — the bound is inclusive

    # Replay survives a lowered ceiling: the in-flight spec's own input still deserializes.
    problem = build_problem(load_dataset())
    spec = CampaignSpec(problem=problem, objective_name="reizman_suzuki", n_rounds=4)
    assert CampaignSpec.model_validate(spec.model_dump()).n_rounds == 4


def test_optimize_rejects_rounds_beyond_the_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """The in-process campaign entry point enforces the config ceiling before spending budget."""
    monkeypatch.setattr(settings, "bo_max_rounds", 3)
    problem = molecule_library_problem(["CCO", "O", "c1ccccc1"])

    async def _never(_params: dict[str, ParamValue]) -> float:
        raise AssertionError("no evaluation may run for a rejected campaign")

    with pytest.raises(ValueError, match="bo_max_rounds=3"):
        asyncio.run(optimize(problem, _never, n_initial=2, n_rounds=4))
