"""Walk every enabled reaction corpus into the label index, one bounded page at a time.

The counterpart of `eln_sync.py` for the other kind of reaction source, and the differences are all
consequences of one thing: a corpus is *literature*, not this organisation's own record. So it
writes no transcription of its own, it carries a patent citation rather than a note id, and it is
drained by keyset rather than by a datetime watermark — a release is a versioned load, not a live
feed. The
reasoning is in `ingest/labels/corpus.py` and in
`D-2026-08-25-a-corpus-is-evidence-not-an-eln`.

Shaped like `document_sync.py`: a planning activity that reads the live values once, a bounded page
per activity, `continue_as_new` so a multi-million-row corpus drains over many runs without an
event history that cannot be replayed.

**Whether the cursor outlives the run is the binding's call, not this file's**
(`D-2026-08-28-a-feed-is-a-corpus-that-does-not-stop`). For a *release* — the default, and what this
job was written for — it is intra-run only and rides the state, exactly as the document crawl's
does: a re-drain of an unchanged release is a no-op (every write is an id-keyed upsert of the record
phase) and a *new* release must be walked from the top, so there is nothing worth storing. For a
source whose binding declares `append_only: true` it is persisted in `corpus_cursors` and the next
fire resumes there, because a live feed is the case where re-walking means reading the whole corpus
daily to find yesterday's rows. Not `sync_cursors`: that column is a datetime and this is a keyset
key in the source's own domain.
"""

from datetime import timedelta

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from pydantic import BaseModel, Field

    from chemclaw.core.config import settings
    from chemclaw.core.errors import ChemclawError
    from chemclaw.durable.registry import durable_activity, durable_workflow
    from chemclaw.ingest.eln.warehouse.binding import CorpusBinding, load_binding
    from chemclaw.ingest.eln.warehouse.connect import open_warehouse
    from chemclaw.ingest.eln.warehouse.driver import Warehouse
    from chemclaw.ingest.labels.corpus import CorpusReport, drain_corpus
    from chemclaw.ingest.labels.cursor import load_corpus_cursor, store_corpus_cursor
    from chemclaw.ingest.sources.registry import active_manifests
    from chemclaw.science.labels.molecules import CorpusMolecules
    from chemclaw.science.labels.reactions import corpus_reactions
    from chemclaw.science.labels.store import default_label_index

import logging

from chemclaw.durable.heartbeat import beating
from chemclaw.durable.publish import BAD_DATA_RETRY, queue_wait_timeout

logger = logging.getLogger(__name__)

# Module-level indirections so tests swap the production stores for in-memory ones.
_label_index = default_label_index
_corpus_molecules = CorpusMolecules
_corpus_reactions = corpus_reactions


def corpus_sources() -> dict[str, CorpusBinding]:
    """Every active source whose warehouse binding declares a drainable reaction corpus, by name.

    Also what `durable/schedules.py` asks to decide whether this job earns a Schedule at all — the
    `share_sources()` twin, and asked of the manifests rather than of a `*_enabled` setting for the
    reason that file records three times over.

    **Read off the manifest rather than off the built retrieve half**, which is the shape
    `share_sources()` uses and is deliberately *not* what this did first. A corpus and a vector
    index are two seams onto one table — Pistachio carries both — and a source declares exactly one
    `retrieve:` callable, so "which sources have a corpus" cannot be answered by asking what the
    retrieve half happens to be an instance of. It is also cheaper: a manifest is YAML, and this
    runs on every Schedule reconcile.

    A malformed binding is skipped rather than raised on: `make datasource-validate --construct` is
    where a bad binding is reported, and a worker that refuses to start because one disabled-ish
    source has a typo would take every other drain down with it.
    """
    found: dict[str, CorpusBinding] = {}
    for manifest in active_manifests():
        raw = manifest.config.get("binding")
        if not isinstance(raw, dict):
            continue
        try:
            corpus = load_binding(raw).corpus
        except ValueError:
            logger.warning(
                "data source %s has a warehouse binding that does not load; it will not be "
                "drained. Run `make datasource-validate --construct` for the reason.",
                manifest.name,
            )
            continue
        if corpus is not None:
            found[manifest.name] = corpus
    return found


# One warehouse connection per source per worker process. `open_warehouse` builds a driver, and a
# page is one query — reconnecting per page would pay a handshake for every thousand rows.
_WAREHOUSES: dict[str, Warehouse] = {}


def _warehouse_for(source: str) -> Warehouse:
    """The open warehouse for `source`, built once per process."""
    if source not in _WAREHOUSES:
        manifest = next(m for m in active_manifests() if m.name == source)
        _WAREHOUSES[source] = open_warehouse(load_binding(manifest.config["binding"]).connection)
    return _WAREHOUSES[source]


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
    unfingerprintable: int = 0


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
    """Read one page of `source`, resuming after `after`, and record it.

    **The persisted cursor is read and written here, not in the workflow**, for the reason every
    other IO in this file is in an activity: a workflow must replay deterministically, and a
    database read cannot. An empty `after` means "the start of this source" — the workflow spells
    it that way both on the first page and after it pops a finished source — so it is the one
    moment a stored position is worth consulting. For a release-mode binding there is none, and the
    drain begins at the top exactly as it always has.
    """
    binding = corpus_sources().get(source)
    if binding is None:  # names come from `plan_corpus_sync`, so this is a wiring bug
        raise ChemclawError(f"data source {source!r} carries no reaction corpus")
    if binding.append_only and not after:
        after = await load_corpus_cursor(source)
    activity.heartbeat()
    report = await beating(
        drain_corpus(
            _warehouse_for(source),
            binding,
            _label_index(),
            source,
            molecules=_corpus_molecules(),
            reactions=_corpus_reactions(),
            after=after,
            limit=settings.corpus_page_size,
        ),
        f"reaction corpus {source}",
        settings.corpus_sync_heartbeat_timeout_seconds,
    )
    # Every page, not only the last one: a run that is interrupted between pages must resume where
    # it stopped rather than at the position the previous *run* left, and the write is one indexed
    # upsert against thousands of rows of work.
    if binding.append_only:
        await store_corpus_cursor(source, report.cursor)
    return report


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
                plan_corpus_sync,
                start_to_close_timeout=timeout,
                schedule_to_start_timeout=queue_wait_timeout(),
                retry_policy=BAD_DATA_RETRY,
            )
            state = CorpusSyncState(max_iterations=plan.max_iterations, remaining=plan.sources)
        iterations = 0
        while state.remaining:
            source = state.remaining[0]
            page: CorpusReport = await workflow.execute_activity(
                drain_reaction_corpus,
                args=[source, state.after],
                start_to_close_timeout=timeout,
                schedule_to_start_timeout=queue_wait_timeout(),
                heartbeat_timeout=timedelta(seconds=settings.corpus_sync_heartbeat_timeout_seconds),
                retry_policy=BAD_DATA_RETRY,
            )
            state.read += page.read
            state.recorded += page.recorded
            state.skipped += page.skipped
            state.unfingerprintable += page.unfingerprintable
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
            reports=[
                CorpusReport(
                    read=state.read,
                    recorded=state.recorded,
                    skipped=state.skipped,
                    unfingerprintable=state.unfingerprintable,
                )
            ]
        )
