"""Durable re-embedding of the stored corpus after an embedding model or width change.

Started on demand (`python -m chemclaw.cli.reembed`), never scheduled: a model change is a decision,
not a cadence. One workflow on `background-jobs`, resumable from the database alone because the
state is the rows' `embedding_key` rather than anything held here:

1. **Documents** — a walk through the stored chunks in primary-key order, each batch read, embedded
   and then written only where the row is still not under the live key. No row is locked across the
   embedding call, so the crawl, the scheduled sync and a peer pass are never made to wait; the
   price is that two passes reaching one chunk may both embed it. A chunk that will not embed is
   passed once and counted, and the walk stops early only when several batches in a row all fail.
2. **Notes** — the incremental note reindex, which already treats a different key as changed.
3. **Verify** — what is still not under the live key, so "done" is a measurement.

Emptying the superseded width's vectors is a separate run (`reclaim`), because it ends the rollback
path: the previous image reads those vectors, and without them it must re-embed.

The live column must already exist (the migrator creates it from `embedding_dim`); the plan refuses
otherwise rather than failing every batch.
"""

from datetime import timedelta

from psycopg import sql
from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from pydantic import BaseModel

    from chemclaw.core import db
    from chemclaw.core.config import settings
    from chemclaw.core.embeddings import embedding_config_key, note_embedding_key
    from chemclaw.core.errors import ChemclawError
    from chemclaw.core.vector_schema import (
        VECTOR_TABLES,
        embedding_column,
        embedding_columns,
    )
    from chemclaw.durable.document_sync import share_sources
    from chemclaw.durable.heartbeat import beating
    from chemclaw.durable.note_index import reindex_notes_activity
    from chemclaw.durable.registry import ActivityClass, durable_activity, durable_workflow
    from chemclaw.ingest.documents.index import (
        ChunkCursor,
        PostgresDocumentIndex,
        default_document_index,
    )
    from chemclaw.ingest.documents.sync import ReembedReport, reembed_after
    from chemclaw.retrieval.vector_index import (
        REINDEX_SKIPPED,
        PostgresNoteIndex,
        default_note_index,
    )

from chemclaw.durable.publish import queue_wait_timeout
from chemclaw.durable.retry import retry_for

# Module-level indirection so tests swap the Postgres backend for another.
_document_index = default_document_index


class ReembedPlan(BaseModel):
    """The configuration a run converges the corpus to, fixed once so a replay cannot move it."""

    key: str
    column: str
    dim: int
    # Activities one run may schedule before continuing as new, captured for the reason
    # `durable/document_sync.py::DocumentSyncPlan` gives.
    max_iterations: int
    # Consecutive wholly failed batches that end the documents phase as stalled.
    max_failed_batches: int


class ReembedProgress(BaseModel):
    """What a run has done so far; carried across `continue_as_new`."""

    plan: ReembedPlan
    phase: str = "documents"
    cursor: ChunkCursor | None = None
    failed_streak: int = 0
    documents_embedded: int = 0
    documents_failed: int = 0
    notes_indexed: int = 0
    notes_skipped: bool = False
    vectors_cleared: int = 0
    stalled: bool = False


class ReembedRequest(BaseModel):
    """The workflow's input: what to do, and (after `continue_as_new`) where it had got to."""

    reclaim: bool = False
    progress: ReembedProgress | None = None


class ReembedRemaining(BaseModel):
    """Rows still not under the live configuration (documents: enabled chunkings only)."""

    documents: int
    notes: int


class ReembedOutcome(BaseModel):
    """What a run did and what it left; `complete` is the measurement, not the absence of errors."""

    documents_embedded: int
    documents_failed: int
    notes_indexed: int
    # Another worker held the note reindex, so this run indexed none: re-run to cover the notes.
    notes_skipped: bool
    vectors_cleared: int
    stalled: bool
    remaining: ReembedRemaining

    @property
    def complete(self) -> bool:
        """Every row is under the live key: nothing remains and nothing was refused."""
        return not self.stalled and not self.remaining.documents and not self.remaining.notes


@durable_activity("background", ActivityClass.IO)
@activity.defn
async def plan_embedding_reembed() -> ReembedPlan:
    """Fix the target configuration, refusing a run whose column the migrator has not built.

    A missing column fails every batch the same way, so it is refused here, once, naming the fix.
    """
    dim = settings.embedding_dim
    column = embedding_column(dim)
    if settings.vector_store_provider == "pgvector":
        async with db.connection(settings.postgres_dsn) as conn:
            for table in VECTOR_TABLES:
                if (await embedding_columns(conn, table)).get(column) != dim:
                    raise ChemclawError(
                        f"{table}.{column} does not exist at width {dim}: run the migration "
                        "(`make db-migrate`, or the chart's pre-upgrade hook) with the new "
                        "embedding_dim before re-embedding"
                    )
    return ReembedPlan(
        key=embedding_config_key(),
        column=column,
        dim=dim,
        max_iterations=settings.document_sync_max_iterations,
        max_failed_batches=settings.document_reembed_max_failed_batches,
    )


@durable_activity("background", ActivityClass.REMOTE)
@activity.defn
async def reembed_document_batch(after: ChunkCursor | None = None) -> ReembedReport:
    """Re-embed the next batch of stored chunks after `after` whose vector is not the live one.

    Scoped to the chunkings the enabled shares use. Heartbeats while the embedding endpoint
    answers, since a batch is one opaque round trip.
    """
    activity.heartbeat()
    chunkings = {share.share_binding().chunking_key for share in share_sources().values()}
    return await beating(
        reembed_after(_document_index(), chunkings, settings.document_reembed_batch_size, after),
        "document re-embed",
        settings.document_sync_heartbeat_timeout_seconds,
    )


# Empties the superseded width's vector on rows already stamped with the live key. The picking
# select locks rows and skips locked ones, and runs once (`MATERIALIZED`) so a plan cannot repeat
# it per outer row (`tests/test_claim_statements.py`). Run only by an explicit reclaim.
_CLEAR_SUPERSEDED = """
    WITH picked AS MATERIALIZED (
        SELECT ctid AS row_id FROM {table}
        WHERE {column} IS NOT NULL AND embedding_key = %(key)s
        LIMIT %(k)s FOR UPDATE SKIP LOCKED
    )
    UPDATE {table} AS t SET {column} = NULL FROM picked WHERE t.ctid = picked.row_id
"""


@durable_activity("background", ActivityClass.IO)
@activity.defn
async def clear_superseded_vectors(plan: ReembedPlan) -> int:
    """Empty, in one bounded batch, vectors of another width on rows already under the live key.

    Returns how many it emptied; zero means nothing is left to reclaim. A row's live vector is in
    the column its key's width names, so a vector beside it in another column is storage only:
    reads never rank it. Irreversible: the previous image reads those vectors.
    """
    budget = settings.document_reembed_batch_size
    keys = {"note_index": note_embedding_key(), "document_chunks": plan.key}
    async with db.connection(settings.postgres_dsn) as conn:
        for table in VECTOR_TABLES:
            for column in await embedding_columns(conn, table):
                if column == plan.column or budget <= 0:
                    continue
                cursor = await conn.execute(
                    sql.SQL(_CLEAR_SUPERSEDED).format(
                        table=sql.Identifier(table), column=sql.Identifier(column)
                    ),
                    {"key": keys[table], "k": budget},
                )
                budget -= cursor.rowcount
        await conn.commit()
    return settings.document_reembed_batch_size - budget


@durable_activity("background", ActivityClass.IO)
@activity.defn
async def count_unembedded() -> ReembedRemaining:
    """How many rows are still not under the live key, for the verdict a run ends with."""
    chunkings = {share.share_binding().chunking_key for share in share_sources().values()}
    index = _document_index()
    notes = default_note_index()
    return ReembedRemaining(
        documents=(
            await index.count_stale(embedding_config_key(), chunkings)
            if isinstance(index, PostgresDocumentIndex)
            else 0
        ),
        notes=(
            await notes.count_unembedded(note_embedding_key())
            if isinstance(notes, PostgresNoteIndex)
            else 0
        ),
    )


@durable_workflow("background")
# Declared failing rather than parking: started by hand (`make reembed`) with no Schedule to bound a
# park, so a bug must reach the operator waiting on it instead of leaving a run that never answers.
@workflow.defn(failure_exception_types=[Exception])
class EmbeddingReembedWorkflow:
    """Bring every stored vector to the live embedding configuration, then say what is left.

    Idempotent and safe to start twice: the work is the set of rows not under the live key, so a
    second run, a retry after a worker died, or a run beside another all converge on the same
    state. `continue_as_new` keeps a large corpus's history bounded.
    """

    @workflow.run
    async def run(self, request: ReembedRequest | None = None) -> ReembedOutcome:
        """Plan once, then drain documents, reindex notes and measure; or, on request, reclaim."""
        request = request or ReembedRequest()
        progress = request.progress
        timeout = timedelta(seconds=settings.document_sync_timeout_seconds)
        heartbeat = timedelta(seconds=settings.document_sync_heartbeat_timeout_seconds)
        if progress is None:
            plan: ReembedPlan = await workflow.execute_activity(
                plan_embedding_reembed,
                start_to_close_timeout=timeout,
                schedule_to_start_timeout=queue_wait_timeout(),
                retry_policy=retry_for(plan_embedding_reembed),
            )
            progress = ReembedProgress(
                plan=plan, phase="reclaim" if request.reclaim else "documents"
            )
        iterations = 0
        while progress.phase == "documents":
            batch: ReembedReport = await workflow.execute_activity(
                reembed_document_batch,
                progress.cursor,
                start_to_close_timeout=timeout,
                schedule_to_start_timeout=queue_wait_timeout(),
                heartbeat_timeout=heartbeat,
                retry_policy=retry_for(reembed_document_batch),
            )
            progress.documents_embedded += batch.embedded
            progress.documents_failed += batch.failed
            progress.cursor = batch.cursor or progress.cursor
            progress.failed_streak = progress.failed_streak + 1 if batch.stalled else 0
            iterations += 1
            if progress.failed_streak >= progress.plan.max_failed_batches:
                progress.stalled = True
                progress.phase = "notes"
            elif not batch.has_more:
                progress.phase = "notes"
            elif iterations >= progress.plan.max_iterations:
                workflow.continue_as_new(ReembedRequest(progress=progress))
        if progress.phase == "notes":
            indexed: int = await workflow.execute_activity(
                reindex_notes_activity,
                start_to_close_timeout=timedelta(seconds=settings.note_reindex_timeout_seconds),
                schedule_to_start_timeout=queue_wait_timeout(),
                heartbeat_timeout=timedelta(
                    seconds=settings.background_activity_heartbeat_timeout_seconds
                ),
                retry_policy=retry_for(reindex_notes_activity),
            )
            if indexed == REINDEX_SKIPPED:
                progress.notes_skipped = True
            else:
                progress.notes_indexed += indexed
            progress.phase = "verify"
        while progress.phase == "reclaim":
            cleared: int = await workflow.execute_activity(
                clear_superseded_vectors,
                progress.plan,
                start_to_close_timeout=timeout,
                schedule_to_start_timeout=queue_wait_timeout(),
                retry_policy=retry_for(clear_superseded_vectors),
            )
            progress.vectors_cleared += cleared
            iterations += 1
            if not cleared:
                progress.phase = "verify"
            elif iterations >= progress.plan.max_iterations:
                workflow.continue_as_new(ReembedRequest(reclaim=True, progress=progress))
        remaining: ReembedRemaining = await workflow.execute_activity(
            count_unembedded,
            start_to_close_timeout=timeout,
            schedule_to_start_timeout=queue_wait_timeout(),
            retry_policy=retry_for(count_unembedded),
        )
        return ReembedOutcome(
            documents_embedded=progress.documents_embedded,
            documents_failed=progress.documents_failed,
            notes_indexed=progress.notes_indexed,
            notes_skipped=progress.notes_skipped,
            vectors_cleared=progress.vectors_cleared,
            stalled=progress.stalled,
            remaining=remaining,
        )
