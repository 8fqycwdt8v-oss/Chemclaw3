"""Durable crawl of every mounted document share: crawl → diff → parse → embed → sweep.

The Temporal wrapper over `chemclaw.ingest.documents.sync`, and the sibling of
`durable/note_index.py`: both keep a *derived* index in step with a source of truth that lives
somewhere else, on the `background-jobs` queue, driven by a Schedule. Nothing here is knowledge —
the share's documents are evidence retrieved with a citation, so no note is proposed and the
PR-gate is not involved.

Three things this file exists to get right, none of which belong in the sync loop itself:

**Bounding.** A first crawl of a TB share is not one activity. Each attempt considers
`document_sync_batch_size` candidates and returns a cursor; the workflow loops on it, and
continues as new every `document_sync_max_iterations` chunks so event history stays bounded no
matter how large the share is. This is the shape `ElnSyncWorkflow` uses, with a path where it has
a timestamp.

**The sweep, and when it is allowed.** Deletion runs once per source, after its whole crawl
drained, and only if no root failed anywhere in that drain. A CIFS mount that dropped mid-run
presents as an empty directory, and the sweep must not read that as "everything was deleted".

**Whose clock.** The mark is a database `now()`, so the sweep's reference is read from the
database too — not from this worker. A database a minute behind the worker would otherwise make
freshly-marked rows look older than the run that marked them.

**Stale vectors go first.** A run drains re-embedding before it crawls, because a chunk embedded
by a superseded model is *wrong now* — it is being compared against freshly embedded queries and
the comparison is meaningless — whereas a document not yet crawled is merely absent. The re-embed
pass reads stored chunk text, so it touches no share and runs even when every mount is down.
"""

import asyncio
from datetime import datetime, timedelta

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from pydantic import BaseModel, Field

    from chemclaw.core.config import settings
    from chemclaw.core.errors import ChemclawError
    from chemclaw.durable.registry import durable_activity, durable_workflow
    from chemclaw.ingest.documents.index import DocumentIndex, default_document_index
    from chemclaw.ingest.documents.sync import (
        DocumentShareSource,
        ReembedReport,
        SyncReport,
        merge_reports,
        prune_share,
        reembed_stale,
        sync_share,
    )
    from chemclaw.ingest.sources.registry import active_retrieve_sources

from chemclaw.durable.publish import BAD_DATA_RETRY

# Module-level indirection so tests swap the Postgres backend for the in-memory one.
_document_index = default_document_index


def share_sources() -> dict[str, DocumentShareSource]:
    """Every active retrieve source that carries a crawlable share, by name.

    Also what `durable/schedules.py` asks to decide whether this job earns a Schedule at all — a
    deployment with no share mounted must not fire a crawl of nothing every six hours.
    """
    return {
        source.name: source
        for source in active_retrieve_sources()
        if isinstance(source, DocumentShareSource)
    }


class DocumentSyncPlan(BaseModel):
    """What one run will crawl, and the reference its sweep will be measured against."""

    sources: list[str]
    # Read from the index backend's own clock, never this worker's — see the module docstring.
    started_at: datetime


class DocumentSyncOutcome(BaseModel):
    """What one run did: per-share reports, and how many stale vectors it refreshed."""

    shares: list[SyncReport] = Field(default_factory=list)
    reembedded: int = 0


class DocumentSyncState(BaseModel):
    """A run's position, carried across `continue_as_new` so a huge share drains over many runs."""

    started_at: datetime
    # Sources still to drain; the first is the one in progress.
    remaining: list[str]
    # The crawl cursor within the source in progress: the last path its previous chunk examined.
    after: str = ""
    # No `degraded` flag: whether a drain may sweep is read off the drain's own merged report
    # (`prune_share`), which already carries the failed roots and the unfinished tail. A flag beside
    # the evidence is a second copy of the rule, and the copy the CLI kept was the wrong one.
    reports: list[SyncReport] = Field(default_factory=list)
    # Whether the re-embedding drain finished. Carried, because a corpus large enough to need
    # `continue_as_new` mid-re-embed must not restart that drain from the top on the next run.
    reembed_done: bool = False
    reembedded: int = 0


@durable_activity("background")
@activity.defn
async def plan_document_sync() -> DocumentSyncPlan:
    """Name the shares to crawl and read the sweep reference off the index's own clock."""
    index: DocumentIndex = _document_index()
    return DocumentSyncPlan(sources=sorted(share_sources()), started_at=await index.clock())


async def _heartbeat_forever() -> None:
    """Beat until cancelled, several times per heartbeat-timeout window (the usual margin).

    One chunk is hundreds of files read off a network share and parsed — minutes of work with no
    natural progress point to report. A sibling task beats while it runs so Temporal detects a dead
    worker within the heartbeat timeout instead of waiting out the whole start-to-close.
    """
    # A third of the timeout is the conventional margin: two beats may be lost to scheduling or
    # network delay before Temporal wrongly declares the worker dead.
    interval = settings.document_sync_heartbeat_timeout_seconds / 3
    while True:
        activity.heartbeat()
        await asyncio.sleep(interval)


@durable_activity("background")
@activity.defn
async def sync_document_share(source: str, after: str) -> SyncReport:
    """Index one bounded slice of `source`, resuming past `after`."""
    share = share_sources().get(source)
    if share is None:  # names come from `plan_document_sync`, so this is a wiring bug
        raise ChemclawError(f"data source {source!r} carries no document share")
    activity.heartbeat()
    heartbeater = asyncio.create_task(_heartbeat_forever())
    try:
        return await sync_share(
            source,
            share.share_binding(),
            _document_index(),
            after=after,
            limit=settings.document_sync_batch_size,
        )
    finally:
        heartbeater.cancel()


@durable_activity("background")
@activity.defn
async def reembed_stale_documents() -> ReembedReport:
    """Refresh one bounded batch of vectors whose embedding configuration is superseded."""
    activity.heartbeat()
    heartbeater = asyncio.create_task(_heartbeat_forever())
    try:
        return await reembed_stale(_document_index(), settings.document_reembed_batch_size)
    finally:
        heartbeater.cancel()


@durable_activity("background")
@activity.defn
async def prune_document_share(source: str, before: datetime, report: SyncReport) -> int:
    """Sweep `source` rows unseen since `before` — a no-op unless the drain evidences absence."""
    return await prune_share(source, _document_index(), before, report)


@durable_workflow("background")
@workflow.defn
class DocumentShareSyncWorkflow:
    """Crawl every mounted share into the document index, one bounded chunk at a time.

    Each run starts from the top of each share rather than from a stored cursor: the sweep needs a
    complete pass to be safe, and the crawl is stat-only, so a re-walk of an unchanged share costs
    a `scandir` and no reads. There is nothing to cursor between runs, which is why — unlike the
    ELN sync — this job keeps no row in `sync_cursors`.
    """

    @workflow.run
    async def run(self, state: DocumentSyncState | None = None) -> DocumentSyncOutcome:
        """Refresh stale vectors, then drain and sweep each share; report what happened.

        `state` is passed only by `continue_as_new`; a scheduled or manual run passes nothing.
        """
        timeout = timedelta(seconds=settings.document_sync_timeout_seconds)
        if state is None:
            plan: DocumentSyncPlan = await workflow.execute_activity(
                plan_document_sync, start_to_close_timeout=timeout, retry_policy=BAD_DATA_RETRY
            )
            state = DocumentSyncState(started_at=plan.started_at, remaining=plan.sources)
        iterations = 0
        # Before the crawl: a vector made by a superseded model is actively wrong, and it is being
        # compared against queries embedded by the current one. Nothing here reads a share, so it
        # also makes progress on a run where every mount is unavailable.
        while not state.reembed_done:
            refresh: ReembedReport = await workflow.execute_activity(
                reembed_stale_documents,
                start_to_close_timeout=timeout,
                heartbeat_timeout=timedelta(
                    seconds=settings.document_sync_heartbeat_timeout_seconds
                ),
                retry_policy=BAD_DATA_RETRY,
            )
            state.reembedded += refresh.embedded
            iterations += 1
            if not refresh.has_more:
                state.reembed_done = True
                break
            if iterations >= settings.document_sync_max_iterations:
                state.reports = _merge_by_source(state.reports)
                workflow.continue_as_new(state)
        while state.remaining:
            source = state.remaining[0]
            chunk: SyncReport = await workflow.execute_activity(
                sync_document_share,
                args=[source, state.after],
                start_to_close_timeout=timeout,
                heartbeat_timeout=timedelta(
                    seconds=settings.document_sync_heartbeat_timeout_seconds
                ),
                # Bad data rejects-and-continues inside the pass; a bad binding or an unmounted
                # share is `DocumentShareError`, which no retry can change.
                retry_policy=BAD_DATA_RETRY,
            )
            state.reports.append(chunk)
            iterations += 1
            if chunk.has_more and chunk.cursor > state.after:
                state.after = chunk.cursor
                if iterations >= settings.document_sync_max_iterations:
                    # Compacted first: the carried state is the *input* of the next run, and a
                    # first crawl of a large share is thousands of chunks. Handing every chunk's
                    # report forward would grow the payload without bound over exactly the drains
                    # that need continue-as-new in the first place.
                    state.reports = _merge_by_source(state.reports)
                    workflow.continue_as_new(state)
                continue
            if chunk.has_more:
                # Unreachable with a well-behaved crawl (a truncated pass always advances the
                # cursor), but a bug must wedge one source with a warning rather than spin this
                # loop — and Temporal's event history — forever. `has_more` survives into the
                # merged report below, which is what stops the sweep.
                workflow.logger.warning(
                    "document sync for %s reported more entries but no cursor advance; stopping",
                    source,
                )
            drained = merge_reports([r for r in state.reports if r.source == source], source)
            pruned = await workflow.execute_activity(
                prune_document_share,
                args=[source, state.started_at, drained],
                start_to_close_timeout=timeout,
                retry_policy=BAD_DATA_RETRY,
            )
            state.reports[-1].pruned = pruned
            state.remaining.pop(0)
            state.after = ""
        return DocumentSyncOutcome(
            shares=_merge_by_source(state.reports), reembedded=state.reembedded
        )


def _merge_by_source(reports: list[SyncReport]) -> list[SyncReport]:
    """Fold a drain's per-chunk reports into one per share, in the order the shares were crawled."""
    order: list[str] = []
    grouped: dict[str, list[SyncReport]] = {}
    for report in reports:
        if report.source not in grouped:
            order.append(report.source)
            grouped[report.source] = []
        grouped[report.source].append(report)
    return [merge_reports(grouped[source], source) for source in order]
