"""Walk every enabled reaction corpus into the label index, one bounded page at a time.

The counterpart of `eln_sync.py` for literature corpora (D-2026-08-25-a-corpus-is-evidence-
not-an-eln): no transcription of its own, a patent citation rather than a note id, and a keyset
drain rather than a datetime watermark. Shaped like `document_sync.py`: a planning activity, a
bounded page per activity, `continue_as_new` to keep history replayable.

For a release (the default), the cursor lives only in workflow state: a re-drain is an
idempotent upsert and a new release must be walked from the top. For a binding with
`append_only: true`, it persists in `corpus_cursors` so a live feed resumes where it stopped.
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

    Also decides whether this job gets a Schedule. Read off the manifest, not the built retrieve
    half, because one source may carry both a corpus and a vector index. A malformed binding is
    skipped (`make datasource-validate --construct` reports it) so one typo cannot stop every
    drain.
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
    # Captured in the activity: it decides how many commands the run emits, so reading it in the
    # workflow would break replay after a redeploy.
    max_iterations: int


class CorpusSyncOutcome(BaseModel):
    """What one run did in total, over every corpus it drained.

    **Not "per source", which is what this said and is false in three independent ways**: the list
    holds exactly one element, `ReactionCorpusWorkflow.run` accumulates into `CorpusSyncState`
    across every source and does not reset when one finishes, and `CorpusReport` carries no
    `source` field to attribute them with. A reader who believed the old sentence would have
    read a two-source run's totals as one source's.

    Per-source attribution exists, one layer down and as telemetry rather than as a return value:
    `ingest.labels.corpus.drain_corpus` books `chemclaw_ingest_records_total{source,outcome}`
    against the data source it drained. Giving this model the same split is a shape change nothing
    has asked for yet — the workflow's own caller wants the run's totals.
    """

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


# A page has no natural progress point, so liveness is time-based; the eager pre-beat covers a
# page shorter than one interval.
@durable_activity("background")
@activity.defn
async def drain_reaction_corpus(source: str, after: str) -> CorpusReport:
    """Read one page of `source`, resuming after `after`, and record it.

    The persisted cursor is read and written here because workflows cannot do IO. An empty `after`
    means "the start of this source", the one moment a stored position is consulted.
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
    # Persist after every page that advanced, so an interrupted run resumes where it stopped. Gated
    # on `advanced` so `updated_at` means "when this feed last moved" and staleness stays
    # detectable.
    if binding.append_only and report.advanced:
        await store_corpus_cursor(source, report.cursor)
    return report


@durable_workflow("background")
# Without `failure_exception_types` a bad-data failure would park in an infinite workflow-task
# retry loop and look like a run still going.
@workflow.defn(failure_exception_types=[Exception])
class ReactionCorpusWorkflow:
    """Drain each enabled reaction corpus into the label index's record phase.

    Labelling is `ReactionLabelWorkflow`'s job on its own Schedule, so a corpus can be re-drained
    without re-labelling and a labeller upgraded without re-reading the warehouse.
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
            if page.has_more and page.advanced:
                state.after = page.cursor
                if iterations >= state.max_iterations:
                    # The carried state is four counters and two strings, so unlike the document
                    # drain there is nothing to compact — the payload cannot grow with the corpus.
                    workflow.continue_as_new(state)
                continue
            if page.has_more:
                # Unreachable with a well-behaved binding; a mis-declared `order_by` stops one
                # source with a warning rather than spinning forever. Uses `page.advanced` because
                # an append-only source starts from a stored position while `state.after` is still
                # `""`.
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
