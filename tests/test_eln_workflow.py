"""Server-backed test for the durable ELN sync workflow (plan step 4.5).

Runs the real `ElnSyncWorkflow` on Temporal's time-skipping server (CI; skips offline),
proving the durable path ingests the seed ELN corpus end-to-end: fetch → map → validate →
index (in-memory here) → PR-gate (fake). Stores and submitter are swapped via the module
factories so no database or git is needed. The per-source-cursor behavior (D-054) is proven by
a second server test with an in-memory cursor store, plus offline unit tests of the named-source
activity and the summary fold.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from temporalio.client import Client
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Worker

import workflows.eln_sync as eln_sync
from chemclaw.config import settings
from eln.adapter import RawEntry
from eln.ord import OrdReaction
from eln.sync import IngestSummary, RejectedEntry
from mcp_servers.fpstore import InMemoryFingerprintStore
from sources.registry import active_ingest_source_names
from tests.conftest import FakeSubmitter
from tests.temporal_env import pydantic_client, start_env_or_skip
from workflows.eln_sync import (
    ElnSyncWorkflow,
    _BoundedIngest,
    _merge,
    list_ingest_sources,
    load_sync_cursor,
    store_sync_cursor,
    sync_eln_entries,
)

_EPOCH = datetime.min.replace(tzinfo=UTC)


def _swap_stores(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeSubmitter, InMemoryFingerprintStore]:
    """Point the sync at in-memory stores + a fake submitter; return the submitter and rxn store."""
    fake = FakeSubmitter()
    reaction_store = InMemoryFingerprintStore()
    molecule_store = InMemoryFingerprintStore()
    monkeypatch.setattr(eln_sync, "_reaction_store", lambda: reaction_store)
    monkeypatch.setattr(eln_sync, "_molecule_store", lambda: molecule_store)
    monkeypatch.setattr(eln_sync, "default_submitter", lambda: fake)
    return fake, reaction_store


def test_active_ingest_source_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only sources with an ingest half are cursored; `graph` (retrieve-only) is excluded."""
    monkeypatch.setattr(settings, "data_sources", "graph,eln-json,eln-ord")
    assert active_ingest_source_names() == ["eln-json", "eln-ord"]
    monkeypatch.setattr(settings, "data_sources", "graph,eln-json")
    assert active_ingest_source_names() == ["eln-json"]


def test_sync_eln_entries_ingests_one_named_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """The activity syncs exactly the named source (offline; no Temporal server needed)."""
    fake, reaction_store = _swap_stores(monkeypatch)
    beats: list[object] = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *details: beats.append(details)
    chunk = asyncio.run(env.run(sync_eln_entries, "eln-json", _EPOCH))
    # The JSON seed corpus (eln/exports) has two valid reactions.
    assert set(chunk.summary.ingested) == {"eln-2026-001", "eln-2026-002"}
    assert chunk.summary.rejected == []
    assert chunk.has_more is False  # nothing beyond the batch bound remains
    assert len(fake.submissions) == 2
    # The activity heartbeats while it ingests, so a dead worker is caught within the
    # heartbeat timeout instead of only at the (much larger) start-to-close.
    assert beats


def test_sync_eln_entries_bounds_one_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """One attempt ingests at most `eln_sync_batch_size` new entries and reports the remainder."""
    _swap_stores(monkeypatch)
    monkeypatch.setattr(settings, "eln_sync_batch_size", 1)
    chunk = asyncio.run(ActivityEnvironment().run(sync_eln_entries, "eln-json", _EPOCH))
    assert len(chunk.summary.ingested) == 1  # bounded: one new entry per attempt
    assert chunk.has_more is True  # the second seed entry remains for the next chunk
    # The next chunk resumes from the advanced cursor and drains the rest.
    follow_up = asyncio.run(
        ActivityEnvironment().run(sync_eln_entries, "eln-json", chunk.summary.next_cursor)
    )
    assert follow_up.has_more is False
    ingested = set(chunk.summary.ingested) | set(follow_up.summary.ingested)
    assert {"eln-2026-001", "eln-2026-002"} <= ingested


def test_bounded_ingest_keeps_overlap_and_truncates_new() -> None:
    """The bound applies only past the cursor: overlap re-ingests pass through uncapped.

    The overlap window exists to re-pick-up late-landing files (idempotent, never advances the
    cursor), so capping it would starve it; capping only the *new* tail guarantees a truncated
    chunk always advances the cursor — the workflow loop's progress condition.
    """

    def entry(entry_id: str, ts: datetime) -> RawEntry:
        return RawEntry(entry_id=entry_id, created_at=ts, payload={})

    since = datetime(2026, 6, 1, tzinfo=UTC)
    older = [entry("old-1", since - timedelta(days=1)), entry("old-2", since)]
    newer = [entry(f"new-{i}", since + timedelta(hours=i)) for i in range(1, 5)]

    class _FakeAdapter:
        async def fetch_new_entries(self, _since: datetime) -> list[RawEntry]:
            return newer + older  # deliberately unsorted

        def map_to_ord(self, raw: RawEntry) -> OrdReaction:  # pragma: no cover - never reached
            raise AssertionError("not used")

    bounded = _BoundedIngest(_FakeAdapter(), since, limit=2)
    kept = asyncio.run(bounded.fetch_new_entries(since - timedelta(days=30)))
    assert [e.entry_id for e in kept] == ["old-1", "old-2", "new-1", "new-2"]
    assert bounded.truncated is True
    # Under the limit: everything passes and nothing is reported as remaining.
    roomy = _BoundedIngest(_FakeAdapter(), since, limit=10)
    assert len(asyncio.run(roomy.fetch_new_entries(since))) == 6
    assert roomy.truncated is False


def test_merge_folds_per_source_summaries() -> None:
    """`_merge` unions ingested/rejected across sources and takes the max cursor."""
    early, late = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC)
    a = IngestSummary(ingested=["a1"], rejected=[], next_cursor=early)
    reject = RejectedEntry(entry_id="b-bad", reason="nope", created_at=late)
    b = IngestSummary(ingested=["b1"], rejected=[reject], next_cursor=late)
    merged = _merge([a, b], _EPOCH)
    assert merged.ingested == ["a1", "b1"]
    assert merged.rejected == [reject]
    assert merged.next_cursor == late
    # No source ran → the cursor holds at the passed floor.
    assert _merge([], late).next_cursor == late


def test_eln_sync_workflow_ingests_seed_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    """The workflow ingests every seed ELN entry and reports them, durably."""
    fake, reaction_store = _swap_stores(monkeypatch)

    async def _run() -> None:
        async with await start_env_or_skip() as env:
            client: Client = pydantic_client(env)
            async with Worker(
                client,
                task_queue="test-eln",
                workflows=[ElnSyncWorkflow],
                activities=[list_ingest_sources, sync_eln_entries],
            ):
                # An explicit `since` is a manual backfill: it touches no stored cursor, so the
                # cursor activities are never called and no database is needed.
                summary = await client.execute_workflow(
                    ElnSyncWorkflow.run,
                    _EPOCH,
                    id="eln-sync-test",
                    task_queue="test-eln",
                )
        # The seed corpus (eln/exports) has two valid reactions.
        assert set(summary.ingested) == {"eln-2026-001", "eln-2026-002"}
        assert summary.rejected == []
        assert len(fake.submissions) == 2  # both proposed a reaction note
        assert len(await reaction_store.all_records()) == 2

    asyncio.run(_run())


def test_eln_sync_workflow_cursors_each_source_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scheduled run stores a separate cursor per active ingest source (D-054)."""
    _swap_stores(monkeypatch)
    monkeypatch.setattr(settings, "data_sources", "graph,eln-json,eln-ord")
    # In-memory cursor store so the scheduled path needs no Postgres.
    cursors: dict[str, datetime] = {}

    async def fake_load(source: str) -> datetime:
        return cursors.get(source, _EPOCH)

    async def fake_store(source: str, cursor: datetime) -> None:
        cursors[source] = cursor

    monkeypatch.setattr(eln_sync, "load_cursor", fake_load)
    monkeypatch.setattr(eln_sync, "store_cursor", fake_store)

    async def _run() -> None:
        async with await start_env_or_skip() as env:
            client: Client = pydantic_client(env)
            async with Worker(
                client,
                task_queue="test-eln-cursors",
                workflows=[ElnSyncWorkflow],
                activities=[
                    list_ingest_sources,
                    sync_eln_entries,
                    load_sync_cursor,
                    store_sync_cursor,
                ],
            ):
                # No `since` → the scheduled path: load each cursor, sync, store each advanced one.
                summary = await client.execute_workflow(
                    ElnSyncWorkflow.run,
                    id="eln-sync-cursors",
                    task_queue="test-eln-cursors",
                )
        # Each ingest source got its own stored cursor — the shared-cursor skip is gone.
        assert set(cursors) == {"eln-json", "eln-ord"}
        # The JSON source's reactions still land (union across sources).
        assert {"eln-2026-001", "eln-2026-002"} <= set(summary.ingested)

    asyncio.run(_run())


def test_eln_sync_workflow_drains_a_backlog_in_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a batch bound of 1 the workflow loops, persisting the cursor after every chunk.

    This is the fix for the wedged-backfill failure mode: progress must be durable per chunk,
    so a backlog larger than one activity window completes across attempts instead of retrying
    one giant attempt forever from the same cursor.
    """
    _swap_stores(monkeypatch)
    monkeypatch.setattr(settings, "eln_sync_batch_size", 1)
    cursors: dict[str, datetime] = {}
    stored: list[datetime] = []

    async def fake_load(source: str) -> datetime:
        return cursors.get(source, _EPOCH)

    async def fake_store(source: str, cursor: datetime) -> None:
        cursors[source] = cursor
        stored.append(cursor)

    monkeypatch.setattr(eln_sync, "load_cursor", fake_load)
    monkeypatch.setattr(eln_sync, "store_cursor", fake_store)

    async def _run() -> None:
        async with await start_env_or_skip() as env:
            client: Client = pydantic_client(env)
            async with Worker(
                client,
                task_queue="test-eln-chunks",
                workflows=[ElnSyncWorkflow],
                activities=[
                    list_ingest_sources,
                    sync_eln_entries,
                    load_sync_cursor,
                    store_sync_cursor,
                ],
            ):
                summary = await client.execute_workflow(
                    ElnSyncWorkflow.run,
                    id="eln-sync-chunks",
                    task_queue="test-eln-chunks",
                )
        assert {"eln-2026-001", "eln-2026-002"} <= set(summary.ingested)
        assert len(stored) >= 2  # one persisted cursor per chunk, not one per run
        assert stored == sorted(stored)  # the cursor only ever advances

    asyncio.run(_run())


def test_background_worker_registers_eln_sync() -> None:
    """The ELN sync activity/workflow are wired onto the background worker (regression)."""
    from workers.background_worker import BACKGROUND_ACTIVITIES, BACKGROUND_WORKFLOWS

    assert ElnSyncWorkflow in BACKGROUND_WORKFLOWS
    assert sync_eln_entries in BACKGROUND_ACTIVITIES
    # The source-listing + self-cursoring activities must be registered too, or a scheduled
    # (no-`since`) run would fail to enumerate sources or load/store its per-source high-water mark.
    assert list_ingest_sources in BACKGROUND_ACTIVITIES
    assert load_sync_cursor in BACKGROUND_ACTIVITIES
    assert store_sync_cursor in BACKGROUND_ACTIVITIES
    assert settings.background_task_queue  # the queue the sync runs on
