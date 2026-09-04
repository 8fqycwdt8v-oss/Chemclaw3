"""Server-backed test for the durable ELN sync workflow (plan step 4.5).

Runs the real `ElnSyncWorkflow` on Temporal's time-skipping server (CI; skips offline),
proving the durable path ingests the seed ELN corpus end-to-end: fetch → map → validate →
index (in-memory here) → record store (in-memory here). Stores are swapped via the module
factories so no database or git is needed. The per-source-cursor behavior (D-054) is proven by
a second server test with an in-memory cursor store, plus offline unit tests of the named-source
activity and the summary fold.
"""

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Worker

import chemclaw.durable.eln_sync as eln_sync
from chemclaw.core.config import settings
from chemclaw.durable.eln_sync import (
    ElnSyncOutcome,
    ElnSyncState,
    ElnSyncWorkflow,
    SyncChunk,
    _absorb,
    _BoundedIngest,
    load_sync_cursor,
    plan_eln_sync,
    store_sync_cursor,
    sync_eln_entries,
)
from chemclaw.ingest.eln.adapter import RawEntry, entry_window
from chemclaw.ingest.eln.ord import OrdReaction
from chemclaw.ingest.eln.records import InMemoryReactionRecordStore
from chemclaw.ingest.eln.sync import IngestSummary, RejectedEntry, sync_entries
from chemclaw.ingest.sources.registry import active_ingest_source_names
from chemclaw.science.fingerprints.store import InMemoryFingerprintStore
from chemclaw.science.labels.store import InMemoryLabelIndex
from tests.temporal_env import pydantic_client, start_env_or_skip, start_local_env_or_skip

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


def test_the_bound_truncates_on_the_same_stamp_the_cursor_advances_on() -> None:
    """Two orderings of one batch is silent data loss, and this is where they were different.

    `sync_entries` advances the cursor on `entry_window(created_at, modified_at)` — the later of
    the two, because the fetch filters on that and an amended entry counts as new. `_BoundedIngest`
    sorted, split and truncated on `created_at` alone. So the chunk that was *kept* could contain
    an entry whose window is later than the window of an entry that was *dropped*: the cursor
    advances past the dropped one, the next fetch asks for entries after that cursor, and the
    dropped entry is never seen again. Nothing reports it — there is no `ingest_rejections` row for
    an entry that was fetched, silently discarded by the cap, and then filtered out by a cursor.

    The corpus this builds is a scientific record, so an entry lost this way is a real experiment a
    chemist ran that nobody can find. Driven with amendments ordered *against* creation, which is
    the ordinary shape of the case — old entries corrected recently — rather than a contrived one.

    Asserted as "the kept chunk's cursor does not overrun any dropped entry" rather than as a count,
    because the count is a property of this fixture and the invariant is what has to hold for any.
    """
    since = datetime(2026, 6, 1, tzinfo=UTC)
    total, limit = 150, 100
    # Entry i was created at +i minutes and amended at +(total - i) minutes: the oldest entries
    # carry the newest amendments, so the two orderings are inverted.
    entries = [
        RawEntry(
            entry_id=f"e-{i:03d}",
            created_at=since + timedelta(minutes=i + 1),
            modified_at=since + timedelta(minutes=total - i),
            payload={},
        )
        for i in range(total)
    ]

    class _AmendedAdapter:
        async def fetch_new_entries(self, _since: datetime) -> list[RawEntry]:
            return list(entries)

        def map_to_ord(self, raw: RawEntry) -> OrdReaction:  # pragma: no cover - never reached
            raise AssertionError("not used")

    bounded = _BoundedIngest(_AmendedAdapter(), since, limit=limit)
    kept = asyncio.run(bounded.fetch_new_entries(since))
    assert bounded.truncated is True
    assert len(kept) == limit

    kept_ids = {entry.entry_id for entry in kept}
    dropped = [entry for entry in entries if entry.entry_id not in kept_ids]
    # What `sync_entries` will store as the next cursor, from the chunk it was handed.
    cursor = max(entry_window(entry.created_at, entry.modified_at) for entry in kept)
    lost = [
        entry.entry_id
        for entry in dropped
        if entry_window(entry.created_at, entry.modified_at) <= cursor
    ]
    assert lost == [], (
        f"{len(lost)} of {total} entries are unreachable after this chunk: the cap dropped them "
        f"and the cursor advanced to {cursor.isoformat()}, past their own fetch window, so the "
        f"next fetch filters them out. First few: {lost[:5]}"
    )


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


def test_one_failing_source_does_not_take_the_rest_of_the_sync_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A drain over N sources must be N independent drains, not one that shares a fate.

    `sync_eln_entries` is called per source with no handler around it, so a source that raises
    after `BAD_DATA_RETRY` is exhausted — a warehouse that is down, a credential that expired, a
    binding that no longer matches the site's schema — failed the whole workflow. Every source
    *after* it in the plan was then never synced, and its cursor never advanced: a run that looked
    like one broken source was silently a run where the healthy ones stopped ingesting too, for as
    long as the broken one stayed broken.

    Bad data was never the case at issue and still is not — that rejects and continues *inside*
    `sync_entries`, and the retry policy exists to keep it there. What this covers is the source
    itself being unreachable, which no amount of retrying inside one activity can fix.

    Asserted on the healthy source's own outcome, because "the run did not raise" is much weaker
    than what has to hold: the second source must actually have been synced and its cursor stored.
    """
    records, _ = _swap_stores(monkeypatch)
    monkeypatch.setattr(settings, "data_sources", "eln-json,eln-ord")
    cursors: dict[str, datetime] = {}

    async def fake_load(source: str) -> datetime:
        return cursors.get(source, _EPOCH)

    async def fake_store(source: str, cursor: datetime) -> None:
        cursors[source] = cursor

    monkeypatch.setattr(eln_sync, "load_cursor", fake_load)
    monkeypatch.setattr(eln_sync, "store_cursor", fake_store)

    @activity.defn(name="sync_eln_entries")
    async def failing_first_source(
        source: str, since: datetime, apply_overlap: bool = True
    ) -> SyncChunk:
        """The first source in the plan is unreachable; the second is the real one."""
        if source == "eln-json":
            raise ApplicationError(f"{source} is unreachable", non_retryable=True)
        return await sync_eln_entries(source, since, apply_overlap)

    async def _run() -> ElnSyncOutcome:
        async with await start_env_or_skip() as env:
            client: Client = pydantic_client(env)
            async with Worker(
                client,
                task_queue="test-eln-one-source-down",
                workflows=[ElnSyncWorkflow],
                activities=[
                    plan_eln_sync,
                    failing_first_source,
                    load_sync_cursor,
                    store_sync_cursor,
                ],
            ):
                return await client.execute_workflow(
                    ElnSyncWorkflow.run,
                    id="eln-sync-one-source-down",
                    task_queue="test-eln-one-source-down",
                )

    outcome = asyncio.run(_run())

    assert outcome.failed_sources == ["eln-json"], (
        f"the run reports {outcome.failed_sources} as failed; a source that could not be reached "
        "has to be named in the outcome, or the run reads as healthy while a source is dark"
    )
    assert "eln-ord" in cursors, (
        "the source after the broken one never ran: one unreachable warehouse stopped every other "
        "source from ingesting, and none of their cursors advanced"
    )
    assert {record.reaction_id for record in asyncio.run(records.all_records())}, (
        "the healthy source ingested nothing"
    )


def test_cancelling_a_drain_stops_it_instead_of_skipping_the_source_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancel must end the run, and the skip-one-source clause is exactly where that can be lost.

    Temporal delivers a workflow cancellation to the awaiting `execute_activity` as an
    `ActivityError` whose cause is `temporalio.exceptions.CancelledError` — the same type the
    clause above catches to drop one unreachable source. Caught rather than re-raised, `cancel`
    becomes "skip whichever source is in flight and carry on": the run finishes COMPLETED, the
    remaining sources are synced, and the SDK's `uncancel` after the absorbed cancel means no
    later activity is cancelled either. `cli.live_data.backfill` awaits `handle.result()`, so an
    operator's cancel of a wrong-`since` backfill silently mutilated one source instead of
    stopping the drain.

    Driven on the **real-time** server for the reason `start_local_env_or_skip` records: this is a
    test about a wall-clock event reaching a run that is still going, and time skipping would
    fast-forward the in-flight activity instead of letting the cancel arrive during it.

    Asserted on the run's *status* rather than on "it raised", because the failure being pinned is
    a run that ends successfully, and on the second source's cursor, because the harm is the work
    that happened after the cancel rather than the exception that did not.
    """
    _swap_stores(monkeypatch)
    monkeypatch.setattr(settings, "data_sources", "eln-json,eln-ord")
    cursors: dict[str, datetime] = {}

    async def fake_load(source: str) -> datetime:
        return cursors.get(source, _EPOCH)

    async def fake_store(source: str, cursor: datetime) -> None:
        cursors[source] = cursor

    monkeypatch.setattr(eln_sync, "load_cursor", fake_load)
    monkeypatch.setattr(eln_sync, "store_cursor", fake_store)
    in_flight = asyncio.Event()

    @activity.defn(name="sync_eln_entries")
    async def hangs_on_the_first_source(
        source: str, since: datetime, apply_overlap: bool = True
    ) -> SyncChunk:
        """First source heartbeats forever so the cancel reaches it; the second is the real one."""
        if source == "eln-json":
            in_flight.set()
            while True:
                activity.heartbeat()
                await asyncio.sleep(0.05)
        return await sync_eln_entries(source, since, apply_overlap)

    async def _run() -> WorkflowExecutionStatus | None:
        async with await start_local_env_or_skip() as env:
            client: Client = pydantic_client(env)
            async with Worker(
                client,
                task_queue="test-eln-cancel",
                workflows=[ElnSyncWorkflow],
                activities=[
                    plan_eln_sync,
                    hangs_on_the_first_source,
                    load_sync_cursor,
                    store_sync_cursor,
                ],
            ):
                handle = await client.start_workflow(
                    ElnSyncWorkflow.run,
                    id="eln-sync-cancelled",
                    task_queue="test-eln-cancel",
                )
                await asyncio.wait_for(in_flight.wait(), timeout=60)
                await handle.cancel()
                with contextlib.suppress(WorkflowFailureError):
                    await asyncio.wait_for(handle.result(), timeout=60)
                return (await handle.describe()).status

    status = asyncio.run(_run())

    assert status == WorkflowExecutionStatus.CANCELED, (
        f"the cancelled drain ended {status!r}; a cancel that is absorbed by the per-source skip "
        "makes this run unstoppable — it drops the source in flight and syncs the rest"
    )
    assert "eln-ord" not in cursors, (
        "the source after the cancelled one was synced anyway: the cancel skipped one source "
        "instead of stopping the run"
    )


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
