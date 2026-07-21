"""The approval-hold seam starts, signals, and queries a real workflow (plan step 5.5).

Server-backed: runs on Temporal's time-skipping server in CI, skips offline. Proves the
frontend reference caller drives `InteractionApprovalWorkflow` end-to-end — start surfaces
the candidate, a Yes signal opens exactly one PR, and the status query tracks the state.
The PR-gate submitter is faked via the module factory so no git is touched.
"""

import asyncio

import pytest
from temporalio.client import Client
from temporalio.worker import Worker

import agents.interaction_tools as interaction_tools
import workflows.interaction_approval as approval
from agents.interaction_tools import approval_status, decide_approval, start_approval
from chemclaw.config import settings
from tests.conftest import FakeSubmitter
from tests.temporal_env import pydantic_client, start_env_or_skip
from workflows.interaction_approval import (
    InteractionApprovalWorkflow,
    InteractionCandidate,
    propose_confirmed_answer_activity,
)

_CANDIDATE = InteractionCandidate(
    interaction_id="q-77",
    question="Preferred base for the amidation?",
    answer="DIPEA, 2 equiv.",
    evidence_note_ids=["reaction-eln-2026-001"],
)


async def _ready(client: Client) -> Client:
    """A coroutine yielding an already-connected client (matches `connect()`'s shape)."""
    return client


def test_start_signal_query_drives_the_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start → status(pending) → decide(Yes) → status(approved) → exactly one PR."""
    fake = FakeSubmitter()
    monkeypatch.setattr(approval, "default_submitter", lambda: fake)

    async def _run() -> None:
        async with await start_env_or_skip() as env:
            client: Client = pydantic_client(env)
            monkeypatch.setattr(interaction_tools, "connect", lambda: _ready(client))
            async with Worker(
                client,
                task_queue=settings.background_task_queue,
                workflows=[InteractionApprovalWorkflow],
                activities=[propose_confirmed_answer_activity],
            ):
                approval_id = await start_approval(_CANDIDATE)
                assert approval_id == "approval-q-77"
                assert await approval_status(approval_id) == "pending"

                # Surfacing the same candidate again is idempotent (same hold id).
                assert await start_approval(_CANDIDATE) == approval_id

                await decide_approval(approval_id, True)  # the Yes click
                await client.get_workflow_handle(approval_id).result()
                assert await approval_status(approval_id) == "approved"

        assert len(fake.submissions) == 1  # Yes opened exactly one PR
        assert "reaction-eln-2026-001" in fake.submissions[0].content

    asyncio.run(_run())


def test_decide_unknown_hold_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Signalling a non-existent hold surfaces a clear error, not a raw RPC crash."""

    async def _run() -> None:
        async with await start_env_or_skip() as env:
            client: Client = pydantic_client(env)
            monkeypatch.setattr(interaction_tools, "connect", lambda: _ready(client))
            with pytest.raises(ValueError, match="no approval hold"):
                await decide_approval("approval-does-not-exist", True)

    asyncio.run(_run())
