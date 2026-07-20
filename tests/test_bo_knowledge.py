"""Tests for the BO recommendation → knowledge-graph bridge (plan step 1d.5)."""

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

import pytest
from temporalio.client import Client
from temporalio.worker import Worker

import workflows.bo_knowledge as bo_knowledge
from bo.problem import CampaignResult, CampaignSpec, Observation
from chemclaw.config import settings
from kg.pr_gate import NoteSubmission
from tests.temporal_env import pydantic_client, start_env_or_skip
from workflows.bo_activities import evaluate_candidates, propose_initial, propose_next
from workflows.bo_campaign import BoCampaignWorkflow
from workflows.bo_knowledge import note_from_campaign_result, write_campaign_node

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


def test_write_campaign_node_uses_the_pr_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The activity proposes the mapped note through the (fake) submitter."""
    captured: list[NoteSubmission] = []

    class _Fake:
        async def submit(self, submission: NoteSubmission) -> str:
            captured.append(submission)
            return f"pr://{submission.branch}"

    monkeypatch.setattr(bo_knowledge, "default_submitter", lambda: _Fake())
    ref = asyncio.run(write_campaign_node("reizman_suzuki", _RESULT))

    assert ref.startswith("pr://note/bo-reizman_suzuki-")
    assert captured[0].path.startswith("knowledge/bo-candidate/bo-reizman_suzuki-")


def test_campaign_publishes_recommendation_to_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """With publish_to_graph, a finished campaign proposes a bo-candidate note (bg queue)."""
    captured: list[NoteSubmission] = []

    class _Fake:
        async def submit(self, submission: NoteSubmission) -> str:
            captured.append(submission)
            return f"pr://{submission.branch}"

    monkeypatch.setattr(bo_knowledge, "default_submitter", lambda: _Fake())

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
                    activities=[write_campaign_node],
                ),
            ):
                await client.execute_workflow(
                    BoCampaignWorkflow.run,
                    spec,
                    id="bo-publish-test",
                    task_queue="test-bo-pub",
                )
        assert len(captured) == 1  # the recommendation was proposed as a note
        assert captured[0].path.startswith("knowledge/bo-candidate/bo-")

    asyncio.run(_run())
