"""Durable crawl of every mounted document share: crawl → diff → parse → embed → sweep.

The Temporal wrapper over `chemclaw.ingest.documents.sync`, on `background-jobs`, driven by a
Schedule. Documents are evidence retrieved with a citation; no note is written.

- **Bounding.** Each attempt considers `document_sync_batch_size` candidates and returns a
  cursor; the workflow loops and continues as new every `document_sync_max_iterations` chunks.
- **The sweep.** Deletion runs once per source after its whole crawl drained, and only if no
  root failed: a dropped CIFS mount looks like an empty directory.
- **Whose clock.** The mark is a database `now()`, so the sweep reference is read from the
  database too.
- **Stale vectors first.** Re-embedding vectors from a superseded model runs before the crawl,
  since those are wrong now; it reads stored chunk text, so it works with every mount down.
"""

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

from chemclaw.durable.heartbeat import beating
from chemclaw.durable.publish import BAD_DATA_RETRY, queue_wait_timeout

# Module-level indirection so tests swap the Postgres backend for the in-memory one.
_document_index = default_document_index


def share_sources() -> dict[str, DocumentShareSource]:
    """Every active retrieve source that carries a crawlable share, by name.

    Also decides whether this job gets a Schedule at all.
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
    # How many activities this run may schedule before continuing as new. Captured in the planning
    # activity and carried on the state, because it decides the command count: reading it live would
    # break replay after a redeploy.
    max_iterations: int


class DocumentSyncOutcome(BaseModel):
    """What one run did, including whether it actually finished what it started.

    `reembedded` alone reads as success at any value, including zero — which is why the third
    field exists.
    """

    shares: list[SyncReport] = Field(default_factory=list)
    reembedded: int = 0
    # Distinguishes "nothing left to do" from "nothing could be done": `reembed_stale` returns
    # `has_more=False` in both cases, and an embedding outage must not report as completion.
    reembed_stalled: bool = False


class DocumentSyncState(BaseModel):
    """A run's position, carried across `continue_as_new` so a huge share drains over many runs."""

    started_at: datetime
    # The bound this drain started with, carried so every run of the chain uses the value the
    # first one recorded — see `DocumentSyncPlan.max_iterations`.
    max_iterations: int
    # Sources still to drain; the first is the one in progress.
    remaining: list[str]
    # The crawl cursor within the source in progress: the last path its previous chunk examined.
    after: str = ""
    # No `degraded` flag: whether a drain may sweep is read off its merged report (`prune_share`).
    reports: list[SyncReport] = Field(default_factory=list)
    # Whether the re-embedding drain finished. Carried, because a corpus large enough to need
    # `continue_as_new` mid-re-embed must not restart that drain from the top on the next run.
    reembed_done: bool = False
    # Set when a whole re-embed batch failed to embed. The drain stops on it without marking the
    # re-embed done, so the next scheduled run retries.
    reembed_stalled: bool = False
    reembedded: int = 0


@durable_activity("background")
@activity.defn
async def plan_document_sync() -> DocumentSyncPlan:
    """Name the shares to crawl, read the sweep reference off the index's own clock, fix the bound.

    All three are live reads, so they belong in an activity and are recorded in history once.
    """
    index: DocumentIndex = _document_index()
    return DocumentSyncPlan(
        sources=sorted(share_sources()),
        started_at=await index.clock(),
        max_iterations=settings.document_sync_max_iterations,
    )


# One chunk is minutes of share reads and parsing with no natural progress point, so liveness is
# time-based via `durable.heartbeat.beating`, whose interval has a one-second floor. The eager
# pre-beat covers a chunk that finishes before the first interval.


@durable_activity("background")
@activity.defn
async def sync_document_share(source: str, after: str) -> SyncReport:
    """Index one bounded slice of `source`, resuming past `after`."""
    share = share_sources().get(source)
    if share is None:  # names come from `plan_document_sync`, so this is a wiring bug
        raise ChemclawError(f"data source {source!r} carries no document share")
    activity.heartbeat()
    return await beating(
        sync_share(
            source,
            share.share_binding(),
            _document_index(),
            after=after,
            limit=settings.document_sync_batch_size,
        ),
        f"document share {source}",
        settings.document_sync_heartbeat_timeout_seconds,
    )


@durable_activity("background")
@activity.defn
async def reembed_stale_documents() -> ReembedReport:
    """Refresh one bounded batch of vectors whose embedding configuration is superseded.

    Scoped to the chunkings the enabled shares use; a chunk cut under a superseded chunking will be
    re-cut by the crawl anyway.
    """
    activity.heartbeat()
    chunkings = {share.share_binding().chunking_key for share in share_sources().values()}
    return await beating(
        reembed_stale(_document_index(), chunkings, settings.document_reembed_batch_size),
        "document re-embed",
        settings.document_sync_heartbeat_timeout_seconds,
    )


@durable_activity("background")
@activity.defn
async def prune_document_share(source: str, before: datetime, report: SyncReport) -> int:
    """Sweep `source` rows unseen since `before` — a no-op unless the drain evidences absence."""
    return await prune_share(source, _document_index(), before, report)


@durable_workflow("background")
# Deliberately left able to park: reached only from the `document-sync` Schedule (bounded by
# `schedule_run_timeout_seconds`), nothing reads its result, and the next fire re-walks from the
# top.
@workflow.defn
class DocumentShareSyncWorkflow:
    """Crawl every mounted share into the document index, one bounded chunk at a time.

    Each run starts from the top of each share: the sweep needs a complete pass, and the stat-only
    crawl makes re-walking an unchanged share cheap. So no row is kept in `sync_cursors`.
    """

    @workflow.run
    async def run(self, state: DocumentSyncState | None = None) -> DocumentSyncOutcome:
        """Refresh stale vectors, then drain and sweep each share; report what happened.

        `state` is passed only by `continue_as_new`; a scheduled or manual run passes nothing.
        """
        timeout = timedelta(seconds=settings.document_sync_timeout_seconds)
        if state is None:
            plan: DocumentSyncPlan = await workflow.execute_activity(
                plan_document_sync,
                start_to_close_timeout=timeout,
                schedule_to_start_timeout=queue_wait_timeout(),
                retry_policy=BAD_DATA_RETRY,
            )
            state = DocumentSyncState(
                started_at=plan.started_at,
                remaining=plan.sources,
                max_iterations=plan.max_iterations,
            )
        iterations = 0
        # Before the crawl: superseded vectors are actively wrong, and this needs no share.
        while not state.reembed_done:
            refresh: ReembedReport = await workflow.execute_activity(
                reembed_stale_documents,
                start_to_close_timeout=timeout,
                schedule_to_start_timeout=queue_wait_timeout(),
                heartbeat_timeout=timedelta(
                    seconds=settings.document_sync_heartbeat_timeout_seconds
                ),
                retry_policy=BAD_DATA_RETRY,
            )
            state.reembedded += refresh.embedded
            iterations += 1
            if refresh.stalled:
                # Stop without marking the drain done, so the next run retries the failed batch.
                state.reembed_stalled = True
                break
            if not refresh.has_more:
                state.reembed_done = True
                break
            if iterations >= state.max_iterations:
                state.reports = _merge_by_source(state.reports)
                workflow.continue_as_new(state)
        while state.remaining:
            source = state.remaining[0]
            chunk: SyncReport = await workflow.execute_activity(
                sync_document_share,
                args=[source, state.after],
                start_to_close_timeout=timeout,
                schedule_to_start_timeout=queue_wait_timeout(),
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
                if iterations >= state.max_iterations:
                    # Compacted first, so the carried state does not grow with every chunk of a
                    # large first crawl.
                    state.reports = _merge_by_source(state.reports)
                    workflow.continue_as_new(state)
                continue
            if chunk.has_more:
                # Unreachable with a well-behaved crawl; a bug stops one source with a warning
                # rather than looping forever. `has_more` survives into the merged report, which
                # blocks the sweep.
                workflow.logger.warning(
                    "document sync for %s reported more entries but no cursor advance; stopping",
                    source,
                )
            drained = merge_reports([r for r in state.reports if r.source == source], source)
            pruned = await workflow.execute_activity(
                prune_document_share,
                args=[source, state.started_at, drained],
                start_to_close_timeout=timeout,
                schedule_to_start_timeout=queue_wait_timeout(),
                retry_policy=BAD_DATA_RETRY,
            )
            state.reports[-1].pruned = pruned
            state.remaining.pop(0)
            state.after = ""
        return DocumentSyncOutcome(
            shares=_merge_by_source(state.reports),
            reembedded=state.reembedded,
            reembed_stalled=state.reembed_stalled,
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
