"""Mirror committed work in from the systems that own it (F4).

One activity per source, driven by a Schedule, cursored in `sync_cursors` exactly as the ELN sync
is — same table, same per-source keying, same discipline that two sources advance independently and
neither's furthest cursor can skip the other's lagging rows.

**Deliberately thin, and the thinness is the decision.** Nothing here plans, schedules, levels
resources or computes a critical path: the organisation already runs a portfolio tool and that tool
is the truth. A mirror that started deriving dates would give the deployment two answers to "when
does this land", and the second one would be wrong more often — see
`D-2026-08-29-a-mirror-is-not-a-plan`.

**And no Schedule here opens a pull request**, which is why this is safe to run on a timer at all.
`D-2026-08-25-an-eln-transcription-is-data-not-a-claim` established the rule: a deterministic
transcription is data and is not gated, while anything agent-*asserted* waits for a person. A
portfolio row copied from an export asserts nothing, so it lands like an ELN transcription rather
than like a note.
"""

from datetime import datetime, timedelta
from typing import cast

from pydantic import BaseModel, Field
from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from chemclaw.core.config import settings
    from chemclaw.core.db import connection
    from chemclaw.durable.registry import durable_activity, durable_workflow
    from chemclaw.ingest.commitments.store import record_commitments
    from chemclaw.ingest.eln.cursor import load_cursor, store_cursor
    from chemclaw.ingest.sources.registry import active_commitment_sources, make_data_source

from chemclaw.durable.publish import BAD_DATA_RETRY, queue_wait_timeout


class CommitmentSyncResult(BaseModel):
    """What one source's mirror pass did."""

    source: str
    mirrored: int = 0
    #: Of those, how many say what chemistry they are waiting on. The number that says whether this
    #: mirror is worth keeping: a commitment with no link is a row the portfolio tool already holds
    #: and holds better.
    linked_to_science: int = 0
    #: Rows this pass removed because the source stopped saying them. Reported rather than silent
    #: because a deletion from a mirror is the one thing here a reader cannot reconstruct: the row
    #: existed only in this table and in a source that no longer mentions it.
    withdrawn: int = 0


# The sweep half of the mark-and-sweep below. `observed_at` is stamped `now()` by every write, so a
# row still carrying a stamp from before this pass began is a row the pass did not restate.
#
# SQL here rather than in `ingest/commitments/store.py` for the reason `retention.py` issues its
# own: a sweep is a *disposal policy*, and disposal is this layer's job — the store owns what a
# reader and a writer of one commitment do, and neither of them deletes.
_SWEEP = "DELETE FROM commitments WHERE source = %s AND observed_at < %s"

# The mark half, and it is the *same* `now()` the upsert stamps `observed_at` with, on the same
# server, because a comparison between two clocks is not a comparison. See `pass_mark`.
_MARK = "SELECT now()"


def _commitments_dsn() -> str:
    """The database the mirror lives in — the **store's**, not core's.

    `ingest/commitments/store.py` connects to `session_store_dsn or postgres_dsn`, so a deployment
    that splits the two would have the mark read from one database and the rows stamped by another
    — the sweep then silently succeeding against a table the mirror was never written to.
    """
    return settings.session_store_dsn or settings.postgres_dsn


async def pass_mark() -> datetime:
    """Stamp the start of one mirror pass, from the clock that will stamp the rows it compares to.

    **The mark and the stamp have to come from one machine.** This used to be
    `activity.info().started_time`, which the *Temporal server* stamps, while `observed_at` is
    stamped by `now()` in Postgres — a worker pod, a temporal-history pod and a database in any
    real deployment. The sweep then compared two clocks: measured against real Postgres, a broker
    clock a quarter of a second ahead made the pass delete every row it had just mirrored
    (`mirrored=3, withdrawn=3`, mirror empty), on every pass, permanently, while
    `CommitmentSyncResult` reported a healthy sync. A tolerance would not have fixed it — it would
    have kept the two clocks and guessed at the gap.

    Read per attempt rather than carried, which is what the old `started_time` was chosen for: a
    retry marks from its own start instead of inheriting the first attempt's.
    """
    async with connection(_commitments_dsn(), operation="commitments") as conn:
        cursor = await conn.execute(_MARK)
        marked = await cursor.fetchone()
    if marked is None:  # pragma: no cover - `SELECT now()` always answers with a row
        raise RuntimeError("the commitments database did not answer with its clock")
    return cast(datetime, marked[0])


async def sweep_withdrawn(source: str, marked_at: datetime) -> int:
    """Delete this source's rows that the pass beginning at `marked_at` did not restate.

    Only ever called for an adapter that declares itself a `snapshot`, because only there does an
    absent row *mean* withdrawn. For an incremental source an absent row means "unchanged", and
    this would empty the mirror on the first quiet pass.

    `marked_at` must come from `pass_mark` — the database's own clock — or this deletes what the
    pass just wrote.

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

    The cursor is advanced only after the write commits, the ordering every sync here uses: a crash
    between fetching and storing must cause a re-read, not a silent skip. A re-read is free, because
    the upsert is keyed on `(source, external_id)` — which is also why a source that cannot answer
    incrementally may return its whole snapshot.

    **Mark and sweep, because that key can converge upward and never downward.** A snapshot says
    "this is no longer committed" by omitting the row, and an upsert has no way to hear that: a
    withdrawn milestone kept a live state and stayed in `outstanding()` for the life of the
    deployment, inside a list stamped with the *refreshed* rows' freshness — so it read as current
    work that nobody was doing. The mark is the mirror database's own clock (`pass_mark`) and the
    sweep removes what the pass did not restate, under the two conditions the call site states.
    """
    # **Namespaced, because `sync_cursors` is keyed on the source name alone and one source may
    # declare both halves.** Nothing in `DataSourceManifest` forbids an `ingest:` and a
    # `commitments:` on one manifest — the model requires *at least* one — and both syncs would then
    # read and write the same row. The commitment mirror stores wall-clock now; the next ELN sync
    # would load it and fetch only entries newer than that, silently skipping every unread entry.
    # That is precisely the failure `ingest/eln/cursor.py` argues cannot happen ("it can never move
    # it past an entry nobody read"), and that argument assumes one writer per source.
    cursor_key = f"{source}:commitments"
    since = await load_cursor(cursor_key)
    adapter = make_data_source(source).commitments
    if adapter is None:  # pragma: no cover - guarded by `active_commitment_sources`
        return CommitmentSyncResult(source=source)
    # **The mark, taken before the fetch, from the database that stamps the rows.** Every row this
    # pass restates is stamped `now()` by the upsert, so anything still older than this is
    # something the source stopped saying — an inference that only holds while both stamps come
    # from one clock, which is the whole of `pass_mark`'s docstring.
    marked_at = await pass_mark()
    commitments = await adapter.fetch_commitments(since)
    written = await record_commitments(commitments)
    # **The sweep, and the two conditions on it.** `snapshot` is the adapter promising that a fetch
    # is the whole picture, so an absent row means withdrawn rather than unchanged; without it a
    # quiet incremental pass would delete the entire mirror. `getattr` because an adapter reaches
    # this through a `module:callable` a site wrote, so it is duck-typed and may predate the
    # property — and the safe reading of "did not say" is the default that sweeps nothing.
    #
    # And not on an empty answer, which is the asymmetry worth stating: a portfolio export that
    # returns nothing is far likelier to be a broken export than a programme with no committed work
    # left, and the two mistakes do not cost the same. A row kept too long is visible in the mirror
    # and corrected by the next good pass; a mirror deleted wholesale is gone, because these rows
    # exist nowhere else this system can reach. A source that genuinely empties converges on the
    # pass after it reports its first remaining row, and never converging on *that* case is the
    # cheaper of the two errors.
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
# **No `failure_exception_types`, deliberately, unlike the job wrappers.** This is a periodic job
# nobody waits on: nothing reads its return value, its only starter is a Schedule that already
# bounds the run, and its work is idempotent, so a plain exception should park until somebody ships
# a fix rather than fail a run there is no caller to tell
# (`D-2026-08-27-a-periodic-job-decides-for-itself-whether-a-bug-should-park-it`). The failure that
# matters here is visible without the workflow failing: a mirror that stops refreshing reports its
# own staleness through `observed_at`, and every reading leads with it.
@workflow.defn
class CommitmentSyncWorkflow:
    """Mirror every enabled source's committed work, one source at a time."""

    @workflow.run
    async def run(self) -> CommitmentSyncReport:
        """Sync each source independently; a failing export does not stop the others.

        Reject-and-continue, the discipline the ELN sync and the digest already use. One portfolio
        system being down must not leave every other programme's mirror a week stale.
        """
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
                        retry_policy=BAD_DATA_RETRY,
                    )
                )
            except Exception:
                workflow.logger.warning("commitment mirror failed for source %s", source)
                report.results.append(CommitmentSyncResult(source=source))
        return report


@durable_activity("background")
@activity.defn
async def list_commitment_sources_activity() -> list[str]:
    """Which sources hold committed work.

    An activity rather than a workflow-side read for the reason every configuration read here is
    one: a workflow that read `settings` directly would emit a different set of commands on replay
    when the enable list changed mid-flight.
    """
    return active_commitment_sources()
