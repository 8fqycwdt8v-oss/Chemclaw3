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

from datetime import timedelta

from pydantic import BaseModel, Field
from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from chemclaw.core.config import settings
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


@durable_activity("background")
@activity.defn
async def mirror_commitments_activity(source: str) -> CommitmentSyncResult:
    """Fetch one source's commitments since its cursor and upsert them.

    The cursor is advanced only after the write commits, the ordering every sync here uses: a crash
    between fetching and storing must cause a re-read, not a silent skip. A re-read is free, because
    the upsert is keyed on `(source, external_id)` — which is also why a source that cannot answer
    incrementally may return its whole snapshot.
    """
    since = await load_cursor(source)
    adapter = make_data_source(source).commitments
    if adapter is None:  # pragma: no cover - guarded by `active_commitment_sources`
        return CommitmentSyncResult(source=source)
    commitments = await adapter.fetch_commitments(since)
    written = await record_commitments(commitments)
    await store_cursor(source, activity.info().started_time)
    return CommitmentSyncResult(
        source=source,
        mirrored=written,
        linked_to_science=sum(1 for row in commitments if row.links_to_science),
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
