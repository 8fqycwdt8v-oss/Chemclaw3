"""Server-backed test for the durable ELN sync workflow (plan step 4.5).

Runs the real `ElnSyncWorkflow` on Temporal's time-skipping server (CI; skips offline),
proving the durable path ingests the seed ELN corpus end-to-end: fetch → map → validate →
index (in-memory here) → record store (in-memory here). Stores are swapped via the module
factories so no database or git is needed. The per-source-cursor behavior (D-054) is proven by
a second server test with an in-memory cursor store, plus offline unit tests of the named-source
activity and the summary fold.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from temporalio.client import Client
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Worker

import chemclaw.durable.eln_sync as eln_sync
from chemclaw.core.config import settings
from chemclaw.durable.eln_sync import (
    ElnSyncState,
    ElnSyncWorkflow,
    _absorb,
    _BoundedIngest,
    load_sync_cursor,
    plan_eln_sync,
    store_sync_cursor,
    sync_eln_entries,
)
from chemclaw.ingest.eln.adapter import RawEntry
from chemclaw.ingest.eln.ord import OrdReaction
from chemclaw.ingest.eln.records import InMemoryReactionRecordStore
from chemclaw.ingest.eln.sync import IngestSummary, RejectedEntry, sync_entries
from chemclaw.ingest.sources.registry import active_ingest_source_names
from chemclaw.science.fingerprints.store import InMemoryFingerprintStore
from chemclaw.science.labels.store import InMemoryLabelIndex
from tests.temporal_env import pydantic_client, start_env_or_skip

_EPOCH = datetime.min.replace(tzinfo=UTC)


def _swap_stores(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[InMemoryReactionRecordStore, InMemoryFingerprintStore]:
    """Point the sync at in-memory stores; return the record store and the reaction index."""
    record_store = InMemoryReactionRecordStore()
    reaction_store = InMemoryFingerprintStore()
    molecule_store = InMemoryFingerprintStore()
    monkeypatch.setattr(eln_sync, "_reaction_store", lambda: reaction_store)
    monkeypatch.setattr(eln_sync, "_molecule_store", lambda: molecule_store)
    monkeypatch.setattr(eln_sync, "_label_index", InMemoryLabelIndex)
    monkeypatch.setattr(eln_sync, "_record_store", lambda: record_store)
    return record_store, reaction_store


def test_active_ingest_source_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only sources with an ingest half are cursored; `graph` (retrieve-only) is excluded."""
    monkeypatch.setattr(settings, "data_sources", "graph,eln-json,eln-ord")
    assert active_ingest_source_names() == ["eln-json", "eln-ord"]
    monkeypatch.setattr(settings, "data_sources", "graph,eln-json")
    assert active_ingest_source_names() == ["eln-json"]


def test_sync_eln_entries_ingests_one_named_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """The activity syncs exactly the named source (offline; no Temporal server needed)."""
    records, reaction_store = _swap_stores(monkeypatch)
    beats: list[object] = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *details: beats.append(details)
    chunk = asyncio.run(env.run(sync_eln_entries, "eln-json", _EPOCH))
    # The JSON seed corpus (data/eln-exports) has two valid reactions.
    assert set(chunk.summary.ingested) == {"eln-2026-001", "eln-2026-002"}
    assert chunk.summary.rejected == []
    assert chunk.has_more is False  # nothing beyond the batch bound remains
    assert len(asyncio.run(records.all_records())) == 2
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


def test_sync_eln_entries_applies_overlap_only_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`apply_overlap=False` fetches from the cursor itself; True replays the window.

    This is the per-chunk seam: the workflow passes False for every chunk after the first,
    so a backlog drain fetches (and re-checks) the overlap window once per run, not once
    per chunk.
    """
    _swap_stores(monkeypatch)
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))  # no merged notes
    monkeypatch.setattr(settings, "eln_sync_overlap_seconds", 90 * 86400.0)
    between = datetime(2026, 2, 1, tzinfo=UTC)  # after seed 001 (Jan 15), before 002 (Feb 3)
    no_overlap = asyncio.run(
        ActivityEnvironment().run(sync_eln_entries, "eln-json", between, False)
    )
    assert set(no_overlap.summary.ingested) == {"eln-2026-002"}  # nothing behind the cursor
    with_overlap = asyncio.run(
        ActivityEnvironment().run(sync_eln_entries, "eln-json", between, True)
    )
    assert set(with_overlap.summary.ingested) == {"eln-2026-001", "eln-2026-002"}


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


def test_absorb_folds_every_chunk_counter_and_takes_the_max_cursor() -> None:
    """`_absorb` counts every per-entry list across chunks and sources, and never regresses.

    Every list, not the ones a reader remembers: the run's whole outcome arrives through this fold
    (the workflow syncs each source in chunks and folds each one), so a field left out here is a
    field that silently reports zero for the entire deployment.

    Counters rather than the id lists themselves, because this state is what
    `continue_as_new` carries — see `ElnSyncOutcome`. A backfill large enough to need a continued
    run is one whose id lists would outgrow Temporal's payload limit, which would defeat the bound
    that exists to keep the run alive.
    """
    early, late = datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC)
    state = ElnSyncState(max_iterations=100, remaining=["eln-json"])
    _absorb(
        state,
        IngestSummary(ingested=["a1"], skipped_existing=["a0"], rejected=[], next_cursor=late),
    )
    reject = RejectedEntry(entry_id="b-bad", reason="nope", created_at=late)
    _absorb(state, IngestSummary(ingested=["b1"], rejected=[reject], next_cursor=early))
    assert (state.ingested, state.skipped_existing, state.rejected) == (2, 1, 1)
    # The max seen, not the last seen — sources are drained one after another and each has its own.
    assert state.next_cursor == late
    # Nothing folded in yet → the workflow falls back to the run's floor.
    assert ElnSyncState(max_iterations=100, remaining=[]).next_cursor is None


def test_eln_sync_workflow_ingests_seed_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    """The workflow ingests every seed ELN entry and reports them, durably."""
    records, reaction_store = _swap_stores(monkeypatch)

    async def _run() -> None:
        async with await start_env_or_skip() as env:
            client: Client = pydantic_client(env)
            async with Worker(
                client,
                task_queue="test-eln",
                workflows=[ElnSyncWorkflow],
                activities=[plan_eln_sync, sync_eln_entries],
            ):
                # An explicit `since` is a manual backfill: it touches no stored cursor, so the
                # cursor activities are never called and no database is needed.
                summary = await client.execute_workflow(
                    ElnSyncWorkflow.run,
                    _EPOCH,
                    id="eln-sync-test",
                    task_queue="test-eln",
                )
        # The seed corpus (data/eln-exports) has two valid reactions. The workflow reports counts
        # (`ElnSyncOutcome`); *which* entries landed is asserted against the stores below, which is
        # the stronger claim anyway — a count is a report, a record is the thing a chemist reads.
        assert summary.ingested == 2
        assert summary.rejected == 0
        assert {record.reaction_id for record in await records.all_records()} == {
            "eln-2026-001",
            "eln-2026-002",
        }
        assert len(await reaction_store.all_records()) == 2

    asyncio.run(_run())


def test_eln_sync_workflow_cursors_each_source_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scheduled run stores a separate cursor per active ingest source (D-054)."""
    records, _ = _swap_stores(monkeypatch)
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
                    plan_eln_sync,
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
        assert summary.ingested >= 2
        assert {"eln-2026-001", "eln-2026-002"} <= {
            record.reaction_id for record in await records.all_records()
        }

    asyncio.run(_run())


def test_eln_sync_workflow_drains_a_backlog_in_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a batch bound of 1 the workflow loops, persisting the cursor after every chunk.

    This is the fix for the wedged-backfill failure mode: progress must be durable per chunk,
    so a backlog larger than one activity window completes across attempts instead of retrying
    one giant attempt forever from the same cursor. The drain must also replay the late-file
    overlap window only on its first chunk — per-chunk replay is quadratic over a backlog.
    """
    records, _ = _swap_stores(monkeypatch)
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))  # no merged notes
    monkeypatch.setattr(settings, "eln_sync_batch_size", 1)
    cursors: dict[str, datetime] = {}
    stored: list[datetime] = []
    overlap_flags: list[bool] = []

    async def recording_sync(*args: Any, **kwargs: Any) -> IngestSummary:
        overlap_flags.append(bool(kwargs.get("apply_overlap", True)))
        return await sync_entries(*args, **kwargs)

    monkeypatch.setattr(eln_sync, "sync_entries", recording_sync)

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
                    plan_eln_sync,
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
        assert summary.ingested >= 2
        assert {"eln-2026-001", "eln-2026-002"} <= {
            record.reaction_id for record in await records.all_records()
        }
        assert len(stored) >= 2  # one persisted cursor per chunk, not one per run
        assert stored == sorted(stored)  # the cursor only ever advances
        # The overlap window is fetched by the first chunk only; later chunks of the same
        # drain fetch from the advancing cursor (no per-chunk window replay).
        assert overlap_flags[0] is True
        assert overlap_flags[1:] and all(flag is False for flag in overlap_flags[1:])

    asyncio.run(_run())


def test_background_worker_registers_eln_sync() -> None:
    """The ELN sync activity/workflow are wired onto the background worker (regression)."""
    from chemclaw.durable.background_worker import BACKGROUND_ACTIVITIES, BACKGROUND_WORKFLOWS

    assert ElnSyncWorkflow in BACKGROUND_WORKFLOWS
    assert sync_eln_entries in BACKGROUND_ACTIVITIES
    # The source-listing + self-cursoring activities must be registered too, or a scheduled
    # (no-`since`) run would fail to enumerate sources or load/store its per-source high-water mark.
    assert plan_eln_sync in BACKGROUND_ACTIVITIES
    assert load_sync_cursor in BACKGROUND_ACTIVITIES
    assert store_sync_cursor in BACKGROUND_ACTIVITIES
    assert settings.background_task_queue  # the queue the sync runs on


# Temporal terminates a workflow execution whose history passes this many events. It is a server
# limit, not a setting, so it is transcribed here rather than imported.
_HISTORY_EVENT_LIMIT = 51_200


def test_a_long_eln_drain_continues_as_new_instead_of_growing_one_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one drain in this package with no iteration bound — so a big backfill is terminated.

    Each chunk emits two activities (`sync_eln_entries`, `store_sync_cursor`) and nothing bounded
    how many chunks one run could take, so history grew linearly with the backlog until the server
    killed the run at its 51,200-event ceiling. Measured against a live broker on the identical
    two-activity loop: 12.2 events per chunk, i.e. ~4,200 chunks — about 420,000 entries at the
    default batch size, against a warehouse ELN this repository sizes at ~700,000. A termination
    is not a failure, so nothing retries and nothing is pushed back; and a *manual* backfill
    (`since` supplied) stores no cursor, so it loses everything it had drained.

    The three sibling drains — `ReactionLabelWorkflow`, `ReactionCorpusWorkflow`,
    `DocumentShareSyncWorkflow` — all capture a bound in their planning activity and
    `continue_as_new`. This asserts the ELN sync now does the same, by measuring what one run's
    history actually costs and what the configured bound therefore buys.
    """
    _swap_stores(monkeypatch)
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    # One entry per chunk and one chunk per run: the smallest drain that must span runs.
    monkeypatch.setattr(settings, "eln_sync_batch_size", 1)
    monkeypatch.setattr(settings, "eln_sync_max_iterations", 1)
    cursors: dict[str, datetime] = {}

    async def fake_load(source: str) -> datetime:
        return cursors.get(source, _EPOCH)

    async def fake_store(source: str, cursor: datetime) -> None:
        cursors[source] = cursor

    monkeypatch.setattr(eln_sync, "load_cursor", fake_load)
    monkeypatch.setattr(eln_sync, "store_cursor", fake_store)

    async def _run() -> tuple[Any, int, int]:
        async with await start_env_or_skip() as env:
            client: Client = pydantic_client(env)
            async with Worker(
                client,
                task_queue="test-eln-can",
                workflows=[ElnSyncWorkflow],
                activities=[
                    plan_eln_sync,
                    sync_eln_entries,
                    load_sync_cursor,
                    store_sync_cursor,
                ],
            ):
                handle = await client.start_workflow(
                    ElnSyncWorkflow.run,
                    id="eln-sync-continue-as-new",
                    task_queue="test-eln-can",
                )
                first_run = handle.first_execution_run_id
                outcome = await handle.result()
                first = await client.get_workflow_handle(
                    handle.id, run_id=first_run
                ).fetch_history()
        events = list(first.events)
        continued = sum(
            1
            for event in events
            if event.HasField("workflow_execution_continued_as_new_event_attributes")
        )
        return outcome, len(events), continued

    outcome, first_run_events, continued = asyncio.run(_run())

    assert continued == 1, (
        f"the first run of the drain ended without continuing as new ({first_run_events} events); "
        "history therefore grows with the backlog until the server terminates the run"
    )
    # What the bound is worth: one run's history is roughly `max_iterations` chunks' worth of
    # events, so the shipped default has to stay clear of the ceiling by a wide margin.
    per_chunk = first_run_events  # this run drained exactly one chunk (max_iterations=1)
    projected = per_chunk * 100  # the shipped `eln_sync_max_iterations`
    assert projected < _HISTORY_EVENT_LIMIT, (
        f"{per_chunk} events per chunk x the shipped bound is {projected} events in one run, "
        f"against Temporal's {_HISTORY_EVENT_LIMIT}-event ceiling"
    )
    # The chain still reports the whole drain, not just its last run.
    assert outcome.ingested == 2  # the seed corpus's two valid reactions, drained one per run
