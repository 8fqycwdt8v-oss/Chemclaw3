"""Tests for the BO recommendation → knowledge-graph bridge (plan step 1d.5)."""

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

import pytest
from temporalio.client import Client
from temporalio.worker import Worker

import workflows.memory_jobs as memory_jobs
from bo.problem import CampaignResult, CampaignSpec, Observation
from chemclaw.config import settings
from connectors.bo.activities import evaluate_candidates, propose_initial, propose_next
from connectors.bo.knowledge import note_from_campaign_result
from connectors.bo.workflows import BoCampaignWorkflow
from tests.conftest import FakeSubmitter
from tests.temporal_env import pydantic_client, start_env_or_skip
from workflows.connector_job import ConnectorJobInput, ConnectorJobWorkflow
from workflows.memory_jobs import publish_memory_note_activity

_BO_ACTIVITIES: Sequence[Callable[..., Any]] = [propose_initial, propose_next, evaluate_candidates]

_RESULT = CampaignResult(
    best=Observation(
        params={"catalyst": "P1", "temperature": 90.0}, value=98.7, provenance="measured"
    ),
    history=[
        Observation(params={"catalyst": "P2", "temperature": 30.0}, value=12.0),
        Observation(
            params={"catalyst": "P1", "temperature": 90.0}, value=98.7, provenance="measured"
        ),
    ],
)


def test_note_from_campaign_result_maps_fields() -> None:
    """The recommendation becomes an agent `bo-candidate` note with conditions + provenance."""
    note = note_from_campaign_result("reizman_suzuki", _RESULT)
    assert note.type == "bo-candidate"
    assert note.created_by == "agent"
    assert note.source == "bo:reizman_suzuki"
    assert note.id.startswith("bo-reizman_suzuki-")
    assert "catalyst: P1" in note.body and "temperature: 90" in note.body
    assert "98.7" in note.body and "measured" in note.body
    assert "2 evaluation" in note.body  # cites how many evaluations backed it
    # No dangling wikilink (would fail kg-validate on this PR).
    assert note.outgoing_links() == []


def test_note_id_is_stable_for_the_same_recommendation() -> None:
    """The id is a hash of the recommended params, so re-proposing is idempotent."""
    assert (
        note_from_campaign_result("obj", _RESULT).id == note_from_campaign_result("obj", _RESULT).id
    )


@pytest.mark.timeout(600)
def test_campaign_publishes_recommendation_to_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """With publish_to_graph, a finished campaign proposes a bo-candidate note (bg queue).

    Overrides the 180s per-test cap rather than raising it globally. This is the one test that
    fits a real BoTorch GP and runs a real acquisition optimisation *inside* a Temporal worker, and
    on a shared runner that optimisation retries: CI logs show
    `Optimization failed in gen_candidates_scipy ... Trying again with a new set of initial
    conditions`, several times, each a fresh multi-start. It is slow, not hung — and the global cap
    exists to catch hangs, so weakening it for everything to accommodate one genuinely heavy test
    would trade the guard away for nothing.
    """
    fake = FakeSubmitter()
    # The gate is core's now, so the submitter is patched where core publishes from.
    monkeypatch.setattr(memory_jobs, "default_submitter", lambda: fake)

    async def _run() -> None:
        from bo.benchmarks.reizman_suzuki import build_problem, load_dataset

        spec = CampaignSpec(
            problem=build_problem(load_dataset()),
            objective_name="reizman_suzuki",
            n_initial=3,
            n_rounds=1,
            publish_to_graph=True,
        )
        async with await start_env_or_skip() as env:
            client: Client = pydantic_client(env)
            async with (
                Worker(
                    client,
                    task_queue="test-bo-pub",
                    workflows=[BoCampaignWorkflow],
                    activities=_BO_ACTIVITIES,
                ),
                Worker(
                    client,
                    task_queue=settings.background_task_queue,
                    activities=[publish_memory_note_activity],
                ),
            ):
                # The campaign now *builds* the note and core *publishes* it, so this drives the
                # whole path: the connector's workflow as a child of core's wrapper, which PR-gates
                # whatever note the envelope carries (D-093).
                await client.execute_workflow(
                    ConnectorJobWorkflow.run,
                    ConnectorJobInput(
                        connector="bo",
                        job="start_optimization_campaign",
                        workflow="BoCampaignWorkflow",
                        task_queue="test-bo-pub",
                        payload=spec.model_dump(mode="json"),
                        requested_by="tester",
                        publish_to_graph=True,
                    ),
                    id="bo-publish-test",
                    task_queue=settings.background_task_queue,
                )
        assert len(fake.submissions) == 1  # the recommendation was proposed as a note
        assert fake.submissions[0].path.startswith("knowledge/bo-candidate/bo-")

    asyncio.run(_run())
