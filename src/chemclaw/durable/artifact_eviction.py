"""Bounded growth for the artifact store, ordered by what a blob is worth (STO-6).

`durable/retention.py` prunes by age and explicitly refuses to touch `calculation_results`,
because D-011 ("never compute twice") is a correctness and cost guarantee and an age cutoff is the
wrong instrument for a cache. That refusal stands and this job does not weaken it: **nothing here
deletes a `calculation_results` row.** It reclaims *blobs* — the by-products in `artifact_blobs` —
and for a result whose row holds the whole answer with only a by-product offloaded, a reclaimed
blob costs at most a recomputation of the by-product, while the answer itself stays cached.

That is not every calculation, though, and the one exception is exactly the reason this store
exists: `science.calc.artifacts.ArrayOffloadingStore` (built for `hessian()`, D-124) stores *only*
a content hash for each packed array in the row and treats a missing blob as a full cache miss —
`ArrayOffloadingStore.get` returns `None`, not a partial result — so reclaiming that blob evicts
the answer along with it, not merely a by-product of it. "The answer itself stays cached forever"
is the design for a row that carries its answer inline; it is not what this job's own contract
promises for a row whose answer *is* an offloaded array.

A result row is the answer; an ordinary artifact is an optimization on top of it, and
`chemclaw.science.calc.artifacts` states in its own contract that such an artifact may be absent —
a reader that finds one gone treats it as a miss and recomputes, so this job can reclaim space
without any reader having to learn about it. `ArrayOffloadingStore` is the one place that contract
is load-bearing for the answer rather than for an optimization, and it is a deliberate, accepted
trade (D-2026-08-16, "the physics leaves, the cache stays") rather than an oversight here.

**Its largest producer left, and the policy is unchanged by that.**
`D-2026-08-16-the-physics-leaves-the-cache-stays` moved the Hessians — the multi-megabyte blobs this
job was sized against — to `Chemclaw3-mcp`, which persists nothing and returns them on the wire. So
what remains here is whatever the QM path and any later producer store. The mechanism is the same
and the ordering below still applies; what changed is how much there is to reclaim.

**What it orders by.** `compute_seconds` — the wall time of the run that produced the blob, which
D-124 started recording precisely so this job would not have to guess — divided into how long the
blob has sat unread. A cheap artifact nobody has opened in months goes first; a Hessian that cost
four minutes and was read yesterday goes last. This is the cost policy `retention.py` said a cache
needs and correctly declined to fake.

Two independent triggers, either of which may be disabled by setting it to zero:

- `artifact_store_max_bytes` — a size ceiling. Evict the least valuable blobs until the store fits.
- `artifact_evict_idle_days` — an idle floor. A blob nobody has opened in that long goes regardless
  of the ceiling, so a store that never reaches its limit still does not accumulate forever.

A blob is deleted, not its links: `calculation_artifacts.content_hash` is `ON DELETE CASCADE`, so
the link rows go with it and no dangling reference survives.
"""

from datetime import timedelta

from pydantic import BaseModel
from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from chemclaw.core.config import settings
    from chemclaw.core.db import connection
    from chemclaw.durable.registry import durable_activity, durable_workflow

from chemclaw.durable.publish import BAD_DATA_RETRY, queue_wait_timeout

# Blobs nobody has opened in `artifact_evict_idle_days`. Unconditional: an artifact that has not
# been read in months is not paying for the space it occupies, whatever the store's total size.
_EVICT_IDLE = """
    DELETE FROM artifact_blobs
    WHERE last_access_at < now() - make_interval(days => %s)
    RETURNING stored_bytes
"""

# The size ceiling, least valuable first. `value` is the cost of losing a blob per day it has gone
# unread: the most expensive calculation feeding it, over its idle time. `COALESCE(..., 0)` puts a
# blob with no recorded cost at the bottom — it arrived before costs were recorded, or from a path
# that does not time itself, and either way there is nothing to argue it is worth keeping.
#
# The window sums the sizes of everything *more* valuable, so the rows selected are exactly those
# past the point where the store still fits. One statement, so nothing races between deciding and
# deleting.
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
    """Reclaim artifact blobs by idle time and by size ceiling; return what was removed.

    Idle eviction runs first so the size pass only has to consider blobs still worth ranking. Both
    are single statements against `artifact_blobs`, and `calculation_results` is never touched —
    see the module docstring for why that distinction is the whole point.
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
# **Deliberately left able to park** (D-2026-08-27). Reached only from the `artifact-eviction`
# Schedule, so a parked run is bounded by `schedule_run_timeout_seconds`; nothing reads its
# result; and one pass is an unconditional policy sweep, so a fire it skips costs a day of
# blobs that the next pass reclaims with the rest. There is no party a parked run misleads —
# the harm D-2026-08-16 measured was a chemist told `running`, and no chemist is here — and
# both terminal states are equally unreported by `ScheduleHealth`. Changing it because the
# neighbours changed is how a per-workflow decision turns into a sweep.
@workflow.defn
class ArtifactEvictionWorkflow:
    """Keep the artifact store within its cost policy on a cadence (STO-6)."""

    @workflow.run
    async def run(self) -> EvictionOutcome:
        """Run one eviction pass and return what it reclaimed."""
        return await workflow.execute_activity(
            evict_cold_artifacts,
            start_to_close_timeout=timedelta(seconds=settings.retention_timeout_seconds),
            schedule_to_start_timeout=queue_wait_timeout(),
            retry_policy=BAD_DATA_RETRY,
        )
