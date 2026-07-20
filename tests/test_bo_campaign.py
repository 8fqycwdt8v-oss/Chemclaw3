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
from bo.objectives import get_objective
from bo.problem import (
    CampaignSpec,
    ContinuousParameter,
    Objective,
    Observation,
    OptimizationProblem,
    Parameter,
    best_of,
)
from tests.temporal_env import pydantic_client, start_env_or_skip
from workflows.bo_activities import evaluate_candidates, propose_initial, propose_next
from workflows.bo_campaign import BoCampaignWorkflow

warnings.filterwarnings("ignore")

_BO_ACTIVITIES: Sequence[Callable[..., Any]] = [propose_initial, propose_next, evaluate_candidates]


def test_get_objective_unknown_raises() -> None:
    """An unknown objective name is a clear error listing the known ones (G4)."""
    with pytest.raises(ValueError, match="unknown objective"):
        get_objective("does-not-exist")


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


def test_durable_campaign_runs_end_to_end() -> None:
    """The workflow runs a small Reizman campaign durably and beats the median yield."""

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
        assert result.best.value > float(load_dataset()["yld"].median())
        assert len(result.history) == 6  # 4 seed + 2 rounds x batch 1

    asyncio.run(_run())
