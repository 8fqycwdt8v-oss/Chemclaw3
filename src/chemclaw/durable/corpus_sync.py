"""Walk every enabled reaction corpus into the label index, one bounded page at a time.

The counterpart of `eln_sync.py` for the other kind of reaction source, and the differences are all
consequences of one thing: a corpus is *literature*, not this organisation's own record. So it
never reaches the PR-gate, it carries a patent citation rather than a note id, and it is drained by
keyset rather than by a datetime watermark — a release is a versioned load, not a live feed. The
reasoning is in `ingest/labels/corpus.py` and in
`D-2026-08-25-a-corpus-is-evidence-not-an-eln`.

Shaped like `document_sync.py`: a planning activity that reads the live values once, a bounded page
per activity, `continue_as_new` so a multi-million-row corpus drains over many runs without an
event history that cannot be replayed. The cursor is intra-run only and rides the state, exactly as
the document crawl's does — there is no `sync_cursors` row, because a re-drain of an unchanged
release is a no-op (every write is an id-keyed upsert of the record phase) and a *new* release must
be walked from the top.
"""

from datetime import timedelta

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from pydantic import BaseModel, Field

    from chemclaw.core.config import settings
    from chemclaw.core.errors import ChemclawError
    from chemclaw.durable.registry import durable_activity, durable_workflow
    from chemclaw.ingest.labels.corpus import CorpusReport, drain_corpus
    from chemclaw.ingest.labels.retriever import WarehouseCorpusRetriever
    from chemclaw.ingest.sources.registry import active_retrieve_sources
    from chemclaw.science.labels.molecules import CorpusMolecules
    from chemclaw.science.labels.store import default_label_index

from chemclaw.durable.heartbeat import beating
from chemclaw.durable.publish import BAD_DATA_RETRY

# Module-level indirections so tests swap the production stores for in-memory ones.
_label_index = default_label_index
_corpus_molecules = CorpusMolecules


def corpus_sources() -> dict[str, WarehouseCorpusRetriever]:
    """Every active retrieve source that carries a drainable reaction corpus, by name.

    Also what `durable/schedules.py` asks to decide whether this job earns a Schedule at all — the
    `share_sources()` twin, and asked of the manifests rather than of a `*_enabled` setting for the
    reason that file records three times over.
    """
    return {
        source.name: source
        for source in active_retrieve_sources()
        if isinstance(source, WarehouseCorpusRetriever)
    }


class CorpusSyncPlan(BaseModel):
    """What one run will drain, and the bound it is fixed to."""

    sources: list[str]
    # Captured in the activity rather than read in the workflow: this decides how many commands the
    # run emits, so a redeploy that lowers it mid-drain would replay `continue_as_new` earlier than
    # history records — a non-determinism error, which retries forever and wedges the run (D-093).
    max_iterations: int


class CorpusSyncOutcome(BaseModel):
    """What one run did, per source."""

    reports: list[CorpusReport] = Field(default_factory=list)


class CorpusSyncState(BaseModel):
    """A run's position, carried across `continue_as_new`."""

    max_iterations: int
    remaining: list[str]
    # The keyset cursor within the source in progress: the last key its previous page saw.
    after: str = ""
    read: int = 0
    recorded: int = 0
    skipped: int = 0


@durable_activity("background")
@activity.defn
async def plan_corpus_sync() -> CorpusSyncPlan:
    """Name the corpora to drain and fix the run's iteration bound."""
    return CorpusSyncPlan(
        sources=sorted(corpus_sources()),
        max_iterations=settings.corpus_sync_max_iterations,
    )


# One page is thousands of rows out of a warehouse and a fingerprint per distinct structure —
# minutes of work with no natural progress point — so liveness is time-based, the same shape every
# other drain in this package uses. The eager pre-beat is kept because `beating()` waits one
# interval before its first, and a small page may finish before that.
@durable_activity("background")
@activity.defn
async def drain_reaction_corpus(source: str, after: str) -> CorpusReport:
    """Read one page of `source`, resuming after `after`, and record it."""
    corpus = corpus_sources().get(source)
    if corpus is None:  # names come from `plan_corpus_sync`, so this is a wiring bug
        raise ChemclawError(f"data source {source!r} carries no reaction corpus")
    activity.heartbeat()
    return await beating(
        drain_corpus(
            corpus.open(),
            corpus.corpus_binding(),
            _label_index(),
            source,
            molecules=_corpus_molecules(),
            after=after,
            limit=settings.corpus_page_size,
        ),
        f"reaction corpus {source}",
        settings.corpus_sync_heartbeat_timeout_seconds,
    )


@durable_workflow("background")
# Without `failure_exception_types` this workflow cannot fail — it hangs. The SDK parks a plain
# exception in an infinite workflow-task-failure loop, so a genuine bad-data failure looks like a
# run that is still going, forever (measured; `connector_job.py` records it).
@workflow.defn(failure_exception_types=[Exception])
class ReactionCorpusWorkflow:
    """Drain each enabled reaction corpus into the label index's record phase.

    The labelling itself is `ReactionLabelWorkflow`'s job and runs on its own Schedule: a row lands
    here unlabelled and leaves the stale set when the labeller reaches it. Splitting them is what
    lets a corpus be re-drained without re-labelling it, and a labeller upgraded without re-reading
    the warehouse.
    """

    @workflow.run
    async def run(self, state: CorpusSyncState | None = None) -> CorpusSyncOutcome:
        """Drain pages until every corpus is exhausted or the run's bound is spent."""
        timeout = timedelta(seconds=settings.corpus_sync_timeout_seconds)
        if state is None:
            plan: CorpusSyncPlan = await workflow.execute_activity(
                plan_corpus_sync, start_to_close_timeout=timeout, retry_policy=BAD_DATA_RETRY
            )
            state = CorpusSyncState(max_iterations=plan.max_iterations, remaining=plan.sources)
        iterations = 0
        while state.remaining:
            source = state.remaining[0]
            page: CorpusReport = await workflow.execute_activity(
                drain_reaction_corpus,
                args=[source, state.after],
                start_to_close_timeout=timeout,
                heartbeat_timeout=timedelta(seconds=settings.corpus_sync_heartbeat_timeout_seconds),
                retry_policy=BAD_DATA_RETRY,
            )
            state.read += page.read
            state.recorded += page.recorded
            state.skipped += page.skipped
            iterations += 1
            if page.has_more and page.cursor and page.cursor != state.after:
                state.after = page.cursor
                if iterations >= state.max_iterations:
                    # The carried state is three counters and two strings, so unlike the document
                    # drain there is nothing to compact — the payload cannot grow with the corpus.
                    workflow.continue_as_new(state)
                continue
            if page.has_more:
                # Unreachable with a well-behaved binding (a truncated page always advances the
                # cursor), but a mis-declared `order_by` must stop one source with a warning rather
                # than spin this loop — and Temporal's event history — forever.
                workflow.logger.warning(
                    "reaction corpus %s reported more rows but no cursor advance; stopping. Check "
                    "that its `order_by` column is unique and stable across the release.",
                    source,
                )
            state.remaining.pop(0)
            state.after = ""
        return CorpusSyncOutcome(
            reports=[CorpusReport(read=state.read, recorded=state.recorded, skipped=state.skipped)]
        )
