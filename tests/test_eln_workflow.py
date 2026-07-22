"""Server-backed test for the durable ELN sync workflow (plan step 4.5).

Runs the real `ElnSyncWorkflow` on Temporal's time-skipping server (CI; skips offline),
proving the durable path ingests the seed ELN corpus end-to-end: fetch → map → validate →
index (in-memory here) → PR-gate (fake). Stores and submitter are swapped via the module
factories so no database or git is needed.
"""

import asyncio
from datetime import UTC, datetime

import pytest
from temporalio.client import Client
from temporalio.worker import Worker

import workflows.eln_sync as eln_sync
from chemclaw.config import settings
from mcp_servers.fpstore import InMemoryFingerprintStore
from tests.conftest import FakeSubmitter
from tests.temporal_env import pydantic_client, start_env_or_skip
from workflows.eln_sync import (
    ElnSyncWorkflow,
    load_sync_cursor,
    store_sync_cursor,
    sync_eln_entries,
)


def test_sync_rejects_multiple_ingest_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two active ingest sources fail fast — the shared cursor tracks only one (DEFERRED guard)."""
    from chemclaw.errors import ChemclawError

    # Two ingest sources active: the single shared high-water cursor would skip the lagging one.
    monkeypatch.setattr(settings, "data_sources", "graph,eln-json,eln-ord")
    with pytest.raises(ChemclawError, match="one shared high-water cursor"):
        asyncio.run(sync_eln_entries(datetime.min.replace(tzinfo=UTC)))


def test_eln_sync_workflow_ingests_seed_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    """The workflow ingests every seed ELN entry and reports them, durably."""
    fake = FakeSubmitter()
    reaction_store = InMemoryFingerprintStore()
    molecule_store = InMemoryFingerprintStore()
    monkeypatch.setattr(eln_sync, "_reaction_store", lambda: reaction_store)
    monkeypatch.setattr(eln_sync, "_molecule_store", lambda: molecule_store)
    monkeypatch.setattr(eln_sync, "default_submitter", lambda: fake)

    async def _run() -> None:
        async with await start_env_or_skip() as env:
            client: Client = pydantic_client(env)
            async with Worker(
                client,
                task_queue="test-eln",
                workflows=[ElnSyncWorkflow],
                activities=[sync_eln_entries],
            ):
                summary = await client.execute_workflow(
                    ElnSyncWorkflow.run,
                    datetime.min.replace(tzinfo=UTC),
                    id="eln-sync-test",
                    task_queue="test-eln",
                )
        # The seed corpus (eln/exports) has two valid reactions.
        assert set(summary.ingested) == {"eln-2026-001", "eln-2026-002"}
        assert summary.rejected == []
        assert len(fake.submissions) == 2  # both proposed a reaction note
        assert len(await reaction_store.all_records()) == 2

    asyncio.run(_run())


def test_background_worker_registers_eln_sync() -> None:
    """The ELN sync activity/workflow are wired onto the background worker (regression)."""
    from workers.background_worker import BACKGROUND_ACTIVITIES, BACKGROUND_WORKFLOWS

    assert ElnSyncWorkflow in BACKGROUND_WORKFLOWS
    assert sync_eln_entries in BACKGROUND_ACTIVITIES
    # The self-cursoring activities must be registered too, or a scheduled (no-`since`) run
    # would fail to load/store its high-water mark.
    assert load_sync_cursor in BACKGROUND_ACTIVITIES
    assert store_sync_cursor in BACKGROUND_ACTIVITIES
    assert settings.background_task_queue  # the queue the sync runs on
