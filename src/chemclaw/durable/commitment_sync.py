"""Mirror committed work in from the systems that own it.

One activity per source, driven by a Schedule, cursored in `sync_cursors` like the ELN sync, so
sources advance independently. Deliberately thin: nothing here plans, schedules or derives
dates, because the portfolio tool is the truth (D-2026-08-29-a-mirror-is-not-a-plan). A
copied portfolio row asserts nothing, so it lands like an ELN transcription and is safe to run
on a timer.
"""

from datetime import datetime, timedelta
from typing import cast

from pydantic import BaseModel, Field
from temporalio import activity, workflow
from temporalio.exceptions import is_cancelled_exception

with workflow.unsafe.imports_passed_through():
    from chemclaw.core.config import settings
    from chemclaw.core.db import connection
    from chemclaw.durable.registry import durable_activity, durable_workflow
    from chemclaw.ingest.commitments.store import record_commitments
    from chemclaw.ingest.eln.cursor import load_cursor, store_cursor
    from chemclaw.ingest.sources.registry import active_commitment_sources, make_data_source

from chemclaw.durable.heartbeat import beating
from chemclaw.durable.publish import BAD_DATA_RETRY, queue_wait_timeout


class CommitmentSyncResult(BaseModel):
    """What one source's mirror pass did."""

    source: str
    mirrored: int = 0
    #: Of those, how many say what chemistry they are waiting on — the measure of whether this
    #: mirror adds anything over the portfolio tool.
    linked_to_science: int = 0
    #: Rows this pass removed because the source stopped stating them; reported because a deletion
    #: from a mirror cannot be reconstructed.
    withdrawn: int = 0


# The sweep half of the mark-and-sweep: every write stamps `observed_at = now()`, so a row older
# than the pass's mark was not restated. A disposal policy, so it lives in this layer rather than
# in the store.
_SWEEP = "DELETE FROM commitments WHERE source = %s AND observed_at < %s"

# The mark half, and it is the *same* `now()` the upsert stamps `observed_at` with, on the same
# server, because a comparison between two clocks is not a comparison. See `pass_mark`.
_MARK = "SELECT now()"


def _commitments_dsn() -> str:
    """The database the mirror lives in — the **store's**, not core's.

    The store uses `session_store_dsn or postgres_dsn`; reading the mark elsewhere would let the
    sweep run against a table the mirror was never written to.
    """
    return settings.session_store_dsn or settings.postgres_dsn


async def pass_mark() -> datetime:
    """Stamp the start of one mirror pass, from the clock that will stamp the rows it compares to.

    The mark and `observed_at` must come from one clock (the database's); comparing against the
    broker's clock lets a small skew delete every freshly mirrored row. Read per attempt, so a
    retry marks from its own start.
    """
    async with connection(_commitments_dsn(), operation="commitments") as conn:
        cursor = await conn.execute(_MARK)
        marked = await cursor.fetchone()
    if marked is None:  # pragma: no cover - `SELECT now()` always answers with a row
        raise RuntimeError("the commitments database did not answer with its clock")
    return cast(datetime, marked[0])


async def sweep_withdrawn(source: str, marked_at: datetime) -> int:
    """Delete this source's rows that the pass beginning at `marked_at` did not restate.

    Only for an adapter that declares itself a `snapshot`; for an incremental source an absent row
    means "unchanged". `marked_at` must come from `pass_mark`.

    Returns:
        How many rows were removed.
    """
    async with connection(_commitments_dsn(), operation="commitments") as conn:
        cursor = await conn.execute(_SWEEP, (source, marked_at))
        return cursor.rowcount


@durable_activity("background")
@activity.defn
async def mirror_commitments_activity(source: str) -> CommitmentSyncResult:
    """Fetch one source's commitments since its cursor, upsert them, and sweep what it withdrew.

    Heartbeats while the opaque export and upsert run, so a dead worker is noticed before the whole
    start-to-close lapses; the eager pre-beat covers passes shorter than one interval.
    """
    activity.heartbeat()
    return await beating(
        _mirror_one_source(source),
        f"commitment mirror {source}",
        settings.commitment_sync_heartbeat_timeout_seconds,
    )


async def _mirror_one_source(source: str) -> CommitmentSyncResult:
    """The pass itself, so the activity above is the heartbeat wrapper and nothing else.

    The cursor advances only after the write commits, so a crash causes a re-read rather than a
    skip; re-reads are free because the upsert is keyed on `(source, external_id)`. An upsert
    cannot hear a withdrawal, so a snapshot source is marked and swept (see the call site).
    """
    # Namespaced: one source may declare both `ingest:` and `commitments:`, and sharing the
    # `sync_cursors` row would make the ELN sync skip unread entries.
    cursor_key = f"{source}:commitments"
    since = await load_cursor(cursor_key)
    adapter = make_data_source(source).commitments
    if adapter is None:  # pragma: no cover - guarded by `active_commitment_sources`
        return CommitmentSyncResult(source=source)
    # The mark, taken before the fetch, from the database that stamps the rows.
    marked_at = await pass_mark()
    commitments = await adapter.fetch_commitments(since)
    written = await record_commitments(commitments)
    # Sweep only when the adapter declares `snapshot` (duck-typed; absent means no sweep), and never
    # on an empty answer: an empty export is likelier broken than truthful, and a wrongly kept row
    # is corrected next pass while a deleted mirror cannot be recovered.
    withdrawn = 0
    if commitments and getattr(adapter, "snapshot", False):
        withdrawn = await sweep_withdrawn(source, marked_at)
    await store_cursor(cursor_key, marked_at)
    return CommitmentSyncResult(
        source=source,
        mirrored=written,
        linked_to_science=sum(1 for row in commitments if row.links_to_science),
        withdrawn=withdrawn,
    )


class CommitmentSyncReport(BaseModel):
    """Every source's pass, so one broken export is visible rather than absorbed."""

    results: list[CommitmentSyncResult] = Field(default_factory=list)

    @property
    def mirrored(self) -> int:
        """How many rows this pass wrote in total."""
        return sum(result.mirrored for result in self.results)


@durable_workflow("background")
# No `failure_exception_types`: a periodic, idempotent job nobody waits on parks on a bug until a
# fix ships. A stale mirror shows itself through `observed_at`.
@workflow.defn
class CommitmentSyncWorkflow:
    """Mirror every enabled source's committed work, one source at a time."""

    @workflow.run
    async def run(self) -> CommitmentSyncReport:
        """Sync each source independently; a failing export does not stop the others."""
        timeout = timedelta(seconds=settings.commitment_sync_timeout_seconds)
        sources = await workflow.execute_activity(
            list_commitment_sources_activity,
            start_to_close_timeout=timeout,
            schedule_to_start_timeout=queue_wait_timeout(),
            retry_policy=BAD_DATA_RETRY,
        )
        report = CommitmentSyncReport()
        for source in sources:
            try:
                report.results.append(
                    await workflow.execute_activity(
                        mirror_commitments_activity,
                        source,
                        start_to_close_timeout=timeout,
                        schedule_to_start_timeout=queue_wait_timeout(),
                        heartbeat_timeout=timedelta(
                            seconds=settings.commitment_sync_heartbeat_timeout_seconds
                        ),
                        retry_policy=BAD_DATA_RETRY,
                    )
                )
            except Exception as exc:
                # A workflow cancellation arrives as `ActivityError(cause=CancelledError)`; re-raise
                # it rather than booking it as a failed source and completing.
                if is_cancelled_exception(exc):
                    raise
                workflow.logger.warning("commitment mirror failed for source %s", source)
                report.results.append(CommitmentSyncResult(source=source))
        return report


@durable_activity("background")
@activity.defn
async def list_commitment_sources_activity() -> list[str]:
    """Which sources hold committed work.

    An activity because reading `settings` in the workflow would change its commands on replay.
    """
    return active_commitment_sources()
