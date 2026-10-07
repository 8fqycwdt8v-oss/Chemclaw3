"""Bounded growth for the artifact store, ordered by what a blob is worth.

Never deletes a `calculation_results` row (D-011). It reclaims blobs in `artifact_blobs`; for a
row that holds its answer inline, a reclaimed blob costs at most recomputing a by-product.
`science.calc.artifacts.ArrayOffloadingStore` is the exception: it treats a missing blob as a
full cache miss, so reclaiming one evicts that answer — an accepted trade.

Blobs are ordered by `compute_seconds` (the cost of the run that produced them) over idle time:
cheap, long-unread blobs go first. Two independent triggers, each disabled by zero:

- `artifact_store_max_bytes` — a size ceiling; evict the least valuable blobs until it fits.
- `artifact_evict_idle_days` — an idle floor; a blob unread that long goes regardless.

Link rows go with the blob via `ON DELETE CASCADE`, so no dangling reference survives.
"""

from datetime import timedelta

from pydantic import BaseModel
from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from chemclaw.core.config import settings
    from chemclaw.core.db import connection
    from chemclaw.durable.registry import durable_activity, durable_workflow

from chemclaw.durable.heartbeat import beating
from chemclaw.durable.publish import BAD_DATA_RETRY, queue_wait_timeout

# Blobs nobody has opened in `artifact_evict_idle_days`. Unconditional: an artifact that has not
# been read in months is not paying for the space it occupies, whatever the store's total size.
_EVICT_IDLE = """
    DELETE FROM artifact_blobs
    WHERE last_access_at < now() - make_interval(days => %s)
    RETURNING stored_bytes
"""

# The size ceiling, least valuable first. `value` is the most expensive calculation feeding a
# blob over its idle time; a blob with no recorded cost sorts last-valued. The window sums the
# sizes of everything more valuable, so the selected rows are exactly those past the ceiling, in
# one statement so nothing races between deciding and deleting.
_EVICT_TO_FIT = """
    WITH ranked AS (
        SELECT
            b.content_hash,
            b.stored_bytes,
            SUM(b.stored_bytes) OVER (
                ORDER BY
                    COALESCE(
                        MAX(a.compute_seconds) / GREATEST(
                            EXTRACT(EPOCH FROM (now() - b.last_access_at)) / 86400.0, 1.0
                        ),
                        0
                    ) DESC,
                    b.last_access_at DESC,
                    b.content_hash
                ROWS UNBOUNDED PRECEDING
            ) AS cumulative
        FROM artifact_blobs AS b
        LEFT JOIN calculation_artifacts AS a ON a.content_hash = b.content_hash
        GROUP BY b.content_hash, b.stored_bytes, b.last_access_at
    )
    DELETE FROM artifact_blobs
    WHERE content_hash IN (SELECT content_hash FROM ranked WHERE cumulative > %s)
    RETURNING stored_bytes
"""


class EvictionOutcome(BaseModel):
    """What one eviction pass reclaimed — the job's own audit record.

    Counts and bytes are reported separately per trigger so an operator can tell a store that is
    over its ceiling from one that is merely accumulating stale blobs; the two want different
    responses.
    """

    idle_blobs: int = 0
    idle_bytes: int = 0
    oversize_blobs: int = 0
    oversize_bytes: int = 0
    skipped: list[str] = []


def _reclaimed(rows: list[tuple[int]]) -> tuple[int, int]:
    """`(blob count, bytes)` from an eviction statement's `RETURNING stored_bytes` rows."""
    return len(rows), sum(int(row[0]) for row in rows)


@durable_activity("background")
@activity.defn
async def evict_cold_artifacts() -> EvictionOutcome:
    """Reclaim artifact blobs, heartbeating while the pass runs so a dead worker is noticed.

    The two `DELETE`s have no unit boundary to report progress at, so the beat only says "still
    running". Budgeted by `retention_timeout_seconds`; settings validation keeps the heartbeat
    timeout below it.
    """
    return await beating(
        _evict_cold_artifacts(),
        "artifact eviction sweep",
        settings.background_activity_heartbeat_timeout_seconds,
    )


async def _evict_cold_artifacts() -> EvictionOutcome:
    """Reclaim artifact blobs by idle time and by size ceiling; return what was removed.

    Idle eviction runs first so the size pass only ranks blobs still present.
    """
    outcome = EvictionOutcome()
    idle_days = settings.artifact_evict_idle_days
    ceiling = settings.artifact_store_max_bytes
    if idle_days <= 0 and ceiling <= 0:
        outcome.skipped.append("artifact eviction disabled (no idle window, no size ceiling)")
        return outcome

    async with connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            if idle_days > 0:
                await cur.execute(_EVICT_IDLE, (idle_days,))
                outcome.idle_blobs, outcome.idle_bytes = _reclaimed(await cur.fetchall())
            else:
                outcome.skipped.append("idle eviction disabled")
            if ceiling > 0:
                await cur.execute(_EVICT_TO_FIT, (ceiling,))
                outcome.oversize_blobs, outcome.oversize_bytes = _reclaimed(await cur.fetchall())
            else:
                outcome.skipped.append("size ceiling disabled")
        await conn.commit()
    return outcome


@durable_workflow("background")
# Deliberately left able to park: reached only from the `artifact-eviction` Schedule, so a run is
# bounded by `schedule_run_timeout_seconds`, nothing reads its result, and a skipped pass is
# caught up by the next. `ScheduleHealth.last_outcome` reports a parked run as `TIMED_OUT`.
@workflow.defn
class ArtifactEvictionWorkflow:
    """Keep the artifact store within its cost policy on a cadence."""

    @workflow.run
    async def run(self) -> EvictionOutcome:
        """Run one eviction pass and return what it reclaimed."""
        return await workflow.execute_activity(
            evict_cold_artifacts,
            start_to_close_timeout=timedelta(seconds=settings.retention_timeout_seconds),
            schedule_to_start_timeout=queue_wait_timeout(),
            # Without a heartbeat timeout the beats detect nothing; a dead worker would surface only
            # when the
            # start-to-close budget expired.
            heartbeat_timeout=timedelta(
                seconds=settings.background_activity_heartbeat_timeout_seconds
            ),
            retry_policy=BAD_DATA_RETRY,
        )
