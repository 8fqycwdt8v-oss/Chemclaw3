"""Durable ELN sync (plan step 4.5): fetch → validate → index → transcribe, on the bg queue.

A thin Temporal wrapper over `chemclaw.ingest.eln.sync.sync_entries`: the activity wires the
production
adapter and fingerprint stores and does all the I/O (ELN read, DB writes); the workflow invokes
it with the high-water cursor. It runs on the
`background-jobs` queue (light, periodic work), and a Temporal Schedule drives it
(`durable/schedules.py`). The sync is **self-cursoring and per-source**: each active ingest
source carries its own cursor in `sync_cursors` (keyed by the registry source name). A
scheduled run (no `since`) loads each source's cursor, syncs from it, and stores the advanced
value — so two ingest sources whose newest entries differ never let one skip the other's
lagging entries (the per-source cursor fix, D-054). An explicit `since` (a manual backfill)
runs every source from that point and does not touch any stored cursor. Each source is
drained in bounded, heartbeating chunks (`eln_sync_batch_size` new entries per activity
attempt, cursor persisted per chunk), so an arbitrarily large backlog makes durable forward
progress instead of wedging one over-window attempt forever; only the first chunk reaches
into the late-file overlap window, so a drain never replays it once per chunk. One *run* is
bounded too (`eln_sync_max_iterations`), continuing as new with its position so that history
length is a function of the bound and not of the backlog. Factories are module-level so tests
swap them for in-memory stores.
"""

from datetime import UTC, datetime, timedelta

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from pydantic import BaseModel

    from chemclaw.core.config import settings
    from chemclaw.core.errors import ChemclawError
    from chemclaw.durable.registry import durable_activity, durable_workflow
    from chemclaw.ingest.eln.adapter import RawEntry, fetch_was_truncated
    from chemclaw.ingest.eln.cursor import load_cursor, store_cursor
    from chemclaw.ingest.eln.ord import OrdReaction
    from chemclaw.ingest.eln.records import default_record_store
    from chemclaw.ingest.eln.sync import IngestSummary, sync_entries
    from chemclaw.ingest.sources.base import IngestHalf
    from chemclaw.ingest.sources.registry import active_ingest_source_names, make_data_source
    from chemclaw.science.fingerprints.store import default_molecule_store, default_reaction_store
    from chemclaw.science.labels.store import default_label_index

from chemclaw.durable.heartbeat import beating
from chemclaw.durable.publish import BAD_DATA_RETRY, queue_wait_timeout

# Module-level indirection so tests swap the production stores for in-memory ones.
_reaction_store = default_reaction_store
_molecule_store = default_molecule_store
_label_index = default_label_index
_record_store = default_record_store


class ElnSyncOutcome(BaseModel):
    """What one drain did, in counters — the shape that survives `continue_as_new`.

    **Counters, not the per-chunk `IngestSummary`.** That model carries one entry id per ingested
    and per skipped entry, which is exactly right for one chunk and impossible to carry across a
    chain of runs: a backfill large enough to need `continue_as_new` is a backfill whose id lists
    outgrow Temporal's payload limit, so the thing that bounds the history would be defeated by
    the thing it hands forward. Nothing is lost that an operator can reach: `sync_entries` already
    logs every rejection with its reason at WARNING, and the counts are what the CLI printed.

    `next_cursor` is the max seen across sources and is informational — the real cursors are stored
    per source — falling back to the run's `since` when no source ran.
    """

    ingested: int = 0
    skipped_existing: int = 0
    # Reported separately because a run that ingests thousands and rejects thousands is a broken
    # source reporting healthy progress, and one total cannot say so.
    rejected: int = 0
    next_cursor: datetime


class ElnSyncPlan(BaseModel):
    """The two live values one drain is fixed to, read once and recorded in history."""

    sources: list[str]
    # Read here rather than in workflow code because it decides how many activity commands the run
    # emits, so a replaying worker must see the value the *first* attempt recorded and not whatever
    # the config says now — the same argument `plan_label_sync` and `plan_document_sync` make.
    max_iterations: int


class ElnSyncState(BaseModel):
    """A run's position, carried across `continue_as_new` so a huge backfill drains over many runs.

    Every field is bounded: source names, one cursor, one flag and four numbers. That is the whole
    reason the counters above exist.
    """

    max_iterations: int
    # Sources still to drain; the first is the one in progress.
    remaining: list[str]
    # The manual-backfill floor. `None` is the scheduled path, which reads and writes a stored
    # cursor per source instead.
    since: datetime | None = None
    # The cursor within the source in progress, carried so a continued run resumes mid-source.
    source_since: datetime | None = None
    # The late-file overlap window is a per-*run-chain* re-check, not a per-chunk one, so this
    # stays False across `continue_as_new` — reaching behind the cursor once per chunk is
    # quadratic over a backlog.
    apply_overlap: bool = True
    ingested: int = 0
    skipped_existing: int = 0
    rejected: int = 0
    next_cursor: datetime | None = None


def _absorb(state: ElnSyncState, summary: IngestSummary) -> None:
    """Fold one chunk's summary into the drain's carried counters (max cursor, summed counts)."""
    state.ingested += len(summary.ingested)
    state.skipped_existing += len(summary.skipped_existing)
    state.rejected += len(summary.rejected)
    state.next_cursor = (
        summary.next_cursor
        if state.next_cursor is None
        else max(state.next_cursor, summary.next_cursor)
    )


@durable_activity("background")
@activity.defn
async def plan_eln_sync() -> ElnSyncPlan:
    """Name the active ingest sources and fix the run chain's iteration bound.

    Both are live reads that belong in an activity: the source set because it is deployment
    configuration, and `max_iterations` because it decides a command count — see `ElnSyncPlan`.
    """
    return ElnSyncPlan(
        sources=active_ingest_source_names(),
        max_iterations=settings.eln_sync_max_iterations,
    )


class SyncChunk(BaseModel):
    """One bounded sync attempt's outcome: the summary, plus whether newer entries remain.

    `has_more` is what lets the workflow loop chunk by chunk instead of the activity ingesting an
    unbounded backlog in one attempt — the failure mode where a large first backfill can never fit
    the start-to-close window and the scheduled sync wedges with zero forward progress.
    """

    summary: IngestSummary
    has_more: bool


class _BoundedIngest:
    """An `ElnAdapter` wrapper that caps how many *new* entries one sync attempt sees.

    Entries at or before the run's cursor (`since`) — the overlap window's idempotent re-ingest —
    pass through uncapped: they are cheap re-writes and never advance the cursor. Entries after it
    are sorted oldest-first and truncated to `limit`, so one activity attempt does a bounded amount
    of ingest work no matter how large the backlog, and `truncated` tells the workflow to come
    back for the rest with the advanced cursor. Because the cap applies only past `since`, every
    kept chunk that was truncated strictly advances the cursor — the loop always makes progress.

    **The source's own truncation counts too** (`fetch_was_truncated`), and this is the half that
    was missing. This cap sees only what the fetch handed over, so a source that cut its page short
    of what the caller asked for — a warehouse whose `fetch_limit` landed inside a block of rows
    sharing one watermark — looked exactly like a source with nothing new: `has_more` was `False`,
    the workflow stopped, and the guard below it was never reached. Asking the adapter turns that
    into "come back", which either advances the cursor or trips the guard out loud.
    """

    def __init__(self, inner: IngestHalf, since: datetime, limit: int) -> None:
        self._inner = inner
        self._since = since
        self._limit = limit
        self.truncated = False

    @property
    def inner(self) -> IngestHalf:
        """The adapter this bounds — the seam's wrapper contract, not a convenience.

        `adapter._wrapper_chain` walks this chain to find an optional capability, and a
        `runtime_checkable` Protocol is structural: a wrapper that does not expose what it wraps
        simply does not have the capability, and answers in silence. That is exactly how
        `fetch_truncated` read `False` for every source in every deployment. This wrapper asks
        `fetch_was_truncated` of its inner directly, below, so the gap surfaced on the *other*
        capability — `fetch_refusals`, where a file the fetch could not read left no
        rejection-ledger row at all. `DatedIngest` already exposed `inner` for this reason; this
        is the same rule on the wrapper one layer out.
        """
        return self._inner

    async def fetch_new_entries(self, since: datetime) -> list[RawEntry]:
        """Fetch from the wrapped adapter: the overlap plus the oldest `limit` new entries."""
        entries = sorted(
            await self._inner.fetch_new_entries(since),
            key=lambda entry: (entry.created_at, entry.entry_id),
        )
        overlap = [entry for entry in entries if entry.created_at <= self._since]
        new = [entry for entry in entries if entry.created_at > self._since]
        self.truncated = len(new) > self._limit or fetch_was_truncated(self._inner)
        return overlap + new[: self._limit]

    def map_to_ord(self, raw: RawEntry) -> OrdReaction:
        """Delegate mapping unchanged — bounding is purely a fetch concern."""
        return self._inner.map_to_ord(raw)


# The sync activity's real work happens inside `sync_entries`, which this layer must not modify
# (the loop is backend-agnostic core, G6) — so liveness is time-based: something beats while the
# sync runs, letting Temporal detect a dead worker within `eln_sync_heartbeat_timeout_seconds`
# rather than waiting out the whole start-to-close.
#
# That something is `durable.heartbeat.beating`, the helper extracted for exactly this shape. The
# copy this file used to carry derived its interval as `timeout / 3` with **no floor**, so an
# ENV-set timeout of a second — permitted, the field is only `gt=0` — beat three times a second
# against the Temporal server for the whole chunk. `beating()` uses `max(1.0, timeout / 4)`, and
# that floor is the difference. The eager pre-beat at the call site stays: `beating()` waits one
# interval before its first beat and a fast sync may finish before it.


@durable_activity("background")
@activity.defn
async def sync_eln_entries(source: str, since: datetime, apply_overlap: bool = True) -> SyncChunk:
    """Ingest a bounded chunk of entries newer than `since` from the one named ingest source.

    Bounded (`eln_sync_batch_size`) and heartbeating, so a large backlog can neither blow the
    activity's start-to-close window in one giant attempt nor hide a dead worker until it lapses.
    `apply_overlap` is True only for a run's first chunk: the late-file overlap window is a
    per-run re-check, so subsequent chunks of the same drain fetch from the advancing cursor
    instead of replaying the whole window once per chunk (quadratic during a backlog drain).
    """
    data_source = make_data_source(source)
    ingest = data_source.ingest
    if ingest is None:  # names come from the ingest-filtered set, so this is a wiring bug
        raise ChemclawError(f"data source {source!r} has no ingest half")
    bounded = _BoundedIngest(ingest, since, settings.eln_sync_batch_size)
    # First beat immediately (a fast sync may finish before `beating()`'s first interval elapses),
    # then it keeps beating for as long as the chunk actually takes.
    activity.heartbeat()
    summary = await beating(
        sync_entries(
            bounded,
            _reaction_store(),
            _molecule_store(),
            _record_store(),
            since,
            label_index=_label_index(),
            source=source,
            apply_overlap=apply_overlap,
        ),
        f"eln sync {source}",
        settings.eln_sync_heartbeat_timeout_seconds,
    )
    return SyncChunk(summary=summary, has_more=bounded.truncated)


@durable_activity("background")
@activity.defn
async def load_sync_cursor(source: str) -> datetime:
    """Return the persisted high-water cursor for `source` (epoch if it has never synced)."""
    return await load_cursor(source)


@durable_activity("background")
@activity.defn
async def store_sync_cursor(source: str, cursor: datetime) -> None:
    """Persist the advanced high-water cursor for `source` after a scheduled run."""
    await store_cursor(source, cursor)


@durable_workflow("background")
# Declared, where its near-twin `DocumentShareSyncWorkflow` is not, and the difference is not
# the shape of the drain — it is that this one has a starter that waits. `cli.live_data.
# backfill` starts it with an explicit `since`, **no `execution_timeout`**, and then awaits
# `handle.result()`: a plain exception parks a run nothing will ever end while the bring-up
# blocks on it. Failing costs at most the chunk in flight, because every chunk persists its
# cursor through `store_sync_cursor` — which is the same reason `schedule_run_timeout_seconds`
# is safe to state at all. D-2026-08-27.
@workflow.defn(failure_exception_types=[Exception])
class ElnSyncWorkflow:
    """Run one ELN sync durably, returning what was ingested across every active ingest source.

    Scheduled runs pass no `since`: for each active ingest source the workflow loads its stored
    cursor, syncs, and stores the advanced one — so consecutive firings never re-do or skip work,
    and each source advances on its own timeline. A manual run may pass an explicit `since` to
    backfill every source from a chosen point without disturbing any stored cursor.
    """

    @workflow.run
    async def run(
        self, since: datetime | None = None, state: ElnSyncState | None = None
    ) -> ElnSyncOutcome:
        """Sync each active source from its cursor (or `since`); advance cursors when scheduled.

        Each source is synced in bounded chunks (`eln_sync_batch_size` new entries per activity
        attempt), the cursor advancing — and, when scheduled, being persisted — after every chunk.
        A large backfill therefore makes durable forward progress chunk by chunk instead of
        retrying one over-window batch forever.

        **And the run itself is bounded.** After `eln_sync_max_iterations` chunks the drain hands
        its position to a fresh execution with `continue_as_new`, so one run's history is a
        function of the bound rather than of the backlog. Without it this was the only drain in the
        package whose history grew without limit: at a measured 12.2 events per chunk, a first
        backfill hit Temporal's 51,200-event ceiling around 420,000 entries and was *terminated* —
        not failed, so nothing retried and nothing was pushed back.

        `state` is passed only by `continue_as_new`; a scheduled or manual run passes nothing.
        """
        activity_timeout = timedelta(seconds=settings.eln_sync_timeout_seconds)
        if state is None:
            plan: ElnSyncPlan = await workflow.execute_activity(
                plan_eln_sync,
                start_to_close_timeout=activity_timeout,
                schedule_to_start_timeout=queue_wait_timeout(),
                retry_policy=BAD_DATA_RETRY,
            )
            state = ElnSyncState(
                max_iterations=plan.max_iterations, remaining=plan.sources, since=since
            )
        iterations = 0
        while state.remaining:
            source = state.remaining[0]
            if state.source_since is None:
                # Scheduled (no `since`): resume from this source's own cursor. Manual backfill:
                # run every source from the explicit `since` and leave the stored cursors alone.
                if state.since is None:
                    state.source_since = await workflow.execute_activity(
                        load_sync_cursor,
                        source,
                        start_to_close_timeout=activity_timeout,
                        schedule_to_start_timeout=queue_wait_timeout(),
                        retry_policy=BAD_DATA_RETRY,
                    )
                else:
                    state.source_since = state.since
            chunk: SyncChunk = await workflow.execute_activity(
                sync_eln_entries,
                args=[source, state.source_since, state.apply_overlap],
                start_to_close_timeout=activity_timeout,
                schedule_to_start_timeout=queue_wait_timeout(),
                heartbeat_timeout=timedelta(seconds=settings.eln_sync_heartbeat_timeout_seconds),
                # Bad data must reject-and-continue inside the sync, never retry the batch.
                retry_policy=BAD_DATA_RETRY,
            )
            # The overlap window is a per-drain re-check for late-landing files: only the first
            # chunk reaches behind the cursor; later chunks — including those in a continued run —
            # fetch from the advancing cursor, or every chunk would replay the window (quadratic).
            state.apply_overlap = False
            _absorb(state, chunk.summary)
            iterations += 1
            if state.since is None:
                await workflow.execute_activity(
                    store_sync_cursor,
                    args=[source, chunk.summary.next_cursor],
                    start_to_close_timeout=activity_timeout,
                    schedule_to_start_timeout=queue_wait_timeout(),
                    retry_policy=BAD_DATA_RETRY,
                )
            if chunk.has_more and chunk.summary.next_cursor > state.source_since:
                state.source_since = chunk.summary.next_cursor
            else:
                if chunk.has_more:
                    # Unreachable with a well-behaved adapter (a truncated chunk always advances
                    # the cursor), but a buggy source must wedge one source with a warning, not
                    # spin this loop — and Temporal's event history — forever.
                    workflow.logger.warning(
                        "eln sync for %s reported more entries but no cursor advance; stopping",
                        source,
                    )
                # This source is drained: move to the next one, from its own cursor.
                state.remaining = state.remaining[1:]
                state.source_since = None
                state.apply_overlap = True
            if state.remaining and iterations >= state.max_iterations:
                # The carried state is bounded by construction — source names, one cursor, one
                # flag, four counters — so unlike the document drain there is nothing to compact.
                workflow.continue_as_new(args=[since, state])
        # `state.since` rather than the parameter: a continued run is handed both, and the state
        # is the one that is true for the whole chain by construction.
        floor = state.since if state.since is not None else datetime.min.replace(tzinfo=UTC)
        return ElnSyncOutcome(
            ingested=state.ingested,
            skipped_existing=state.skipped_existing,
            rejected=state.rejected,
            next_cursor=state.next_cursor if state.next_cursor is not None else floor,
        )
