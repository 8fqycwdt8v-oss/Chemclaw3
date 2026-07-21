"""Server-backed test for the durable confirmed-answer approval hold (plan step 5.5).

Proves the async "save this knowledge? [Yes]/[No]" seam: a candidate is held on the
time-skipping Temporal server until a `decide` signal (the button click), and only a Yes
opens a PR through the gate. Runs in CI; skips offline. The PR-gate submitter is faked via
the module factory so no git is touched; the timeout path uses the server's time-skipping so
the 7-day hold resolves instantly.
"""

import asyncio

import pytest
from temporalio.client import Client
from temporalio.worker import Worker

import workflows.interaction_approval as approval
from tests.conftest import FakeSubmitter
from tests.temporal_env import pydantic_client, start_env_or_skip
from workflows.interaction_approval import (
    ApprovalOutcome,
    InteractionApprovalWorkflow,
    InteractionCandidate,
    propose_confirmed_answer_activity,
)

_CANDIDATE = InteractionCandidate(
    interaction_id="q-42",
    question="Best solvent for the coupling?",
    answer="Aqueous dioxane at 90 C.",
    evidence_note_ids=["reaction-eln-2026-002"],
)


def test_yes_proposes_reject_and_timeout_do_not(monkeypatch: pytest.MonkeyPatch) -> None:
    """Yes opens exactly one PR; No and an unanswered hold open none — all durable."""
    fake = FakeSubmitter()
    monkeypatch.setattr(approval, "default_submitter", lambda: fake)

    async def _run() -> None:
        async with await start_env_or_skip() as env:
            client: Client = pydantic_client(env)
            async with Worker(
                client,
                task_queue="test-approval",
                workflows=[InteractionApprovalWorkflow],
                activities=[propose_confirmed_answer_activity],
            ):
                # Yes → the click is delivered as a signal, then the note is proposed.
                approved = await client.start_workflow(
                    InteractionApprovalWorkflow.run,
                    _CANDIDATE,
                    id="approval-yes",
                    task_queue="test-approval",
                )
                await approved.signal(InteractionApprovalWorkflow.decide, True)
                yes: ApprovalOutcome = await approved.result()

                # No → held candidate is dropped, no PR.
                rejected = await client.start_workflow(
                    InteractionApprovalWorkflow.run,
                    _CANDIDATE,
                    id="approval-no",
                    task_queue="test-approval",
                )
                await rejected.signal(InteractionApprovalWorkflow.decide, False)
                no: ApprovalOutcome = await rejected.result()

                # No click at all → the server skips the whole hold; it expires.
                expired_handle = await client.start_workflow(
                    InteractionApprovalWorkflow.run,
                    _CANDIDATE,
                    id="approval-timeout",
                    task_queue="test-approval",
                )
                expired: ApprovalOutcome = await expired_handle.result()
                # A UI polling after the hold times out must see `expired`, not `pending`.
                expired_status = await expired_handle.query(InteractionApprovalWorkflow.status)

        assert yes.status == "approved"
        assert yes.reference == "pr://note/interaction-q-42"
        assert no.status == "rejected" and no.reference == ""
        assert expired.status == "expired" and expired.reference == ""
        assert expired_status == "expired"
        # Only the Yes reached the PR-gate, citing its evidence.
        assert len(fake.submissions) == 1
        assert "reaction-eln-2026-002" in fake.submissions[0].content

    asyncio.run(_run())


def test_decision_state_machine_first_click_wins() -> None:
    """The signal/query state machine (no server): pending → decided, first click sticks."""
    wf = InteractionApprovalWorkflow()
    assert wf.status() == "pending"
    wf.decide(True)
    assert wf.status() == "approved"
    wf.decide(False)  # a second click must not flip an already-recorded decision
    assert wf.status() == "approved"


def test_background_worker_registers_interaction_approval() -> None:
    """The approval workflow/activity are wired onto the background worker (regression)."""
    from workers.background_worker import BACKGROUND_ACTIVITIES, BACKGROUND_WORKFLOWS

    assert InteractionApprovalWorkflow in BACKGROUND_WORKFLOWS
    assert propose_confirmed_answer_activity in BACKGROUND_ACTIVITIES
