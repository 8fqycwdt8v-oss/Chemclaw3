"""Draining the result outbox to whatever external stores a deployment enabled.

`publish/outbox.py` writes a projected record locally when a result is produced; this job carries
those rows to their destination, retries what fails, and gives up loudly once a row has spent its
attempt budget. One activity pass per sink, since each destination is its own failure domain. A
failed batch leaves its rows `pending` and the run still succeeds: `result_publications` already
records the failure, and failing the workflow would add a second backoff. The run returns a
per-sink account.
"""

import logging
from datetime import timedelta

from pydantic import BaseModel, Field
from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from chemclaw.core.config import settings
    from chemclaw.durable.heartbeat import beating
    from chemclaw.durable.registry import durable_activity, durable_workflow
    from chemclaw.publish import outbox
    from chemclaw.publish.driver import ResultSink, SinkUnavailableError
    from chemclaw.publish.record import ResultRecord
    from chemclaw.publish.registry import ResultSinkError, build, enabled

from chemclaw.durable.publish import BAD_DATA_RETRY, queue_wait_timeout

logger = logging.getLogger(__name__)


class SinkOutcome(BaseModel):
    """What one drain pass achieved against one sink."""

    sink: str
    delivered: int = 0
    failed: int = 0
    # Why the last batch failed, when one did. Carried into the workflow result so a scheduled
    # run's own history says what is wrong, rather than only that something is.
    reason: str = ""


class PublishOutcome(BaseModel):
    """What one drain pass achieved overall."""

    sinks: list[SinkOutcome] = Field(default_factory=list)
    # Sinks that were skipped, and why — a disabled subsystem, an unbuildable driver. Named rather
    # than counted, because "nothing was published" is ambiguous and this is what disambiguates it.
    skipped: list[str] = Field(default_factory=list)

    @property
    def delivered(self) -> int:
        """Total records delivered across every sink."""
        return sum(outcome.delivered for outcome in self.sinks)


async def _drain_one(manifest_name: str, sink: ResultSink, batch_size: int) -> SinkOutcome:
    """Claim and deliver one batch for one sink.

    One batch per run, not drain-to-empty: it bounds how long one activity holds a connection and
    how much one failure re-attempts. The backfill CLI drains faster.
    """
    outcome = SinkOutcome(sink=manifest_name)
    claimed = await outbox.claim(manifest_name, batch_size)
    if not claimed:
        return outcome
    # Parsed per row, so one unreadable record (from an older writer) does not fail its neighbours.
    # The lease travels with the row so every `mark_*` call is fenced (see `outbox.Lease`).
    leases: list[outbox.Lease] = []
    records: list[ResultRecord] = []
    unreadable: list[outbox.Lease] = []
    for row in claimed:
        try:
            records.append(ResultRecord.model_validate(row.document))
        except Exception as exc:
            # Will not fix itself on a retry, so it spends an attempt rather than looping forever.
            unreadable.append(row.lease)
            outcome.reason = str(exc)[:500]
            continue
        leases.append(row.lease)
    if unreadable:
        await outbox.mark_failed(
            unreadable, f"stored document is not a readable record: {outcome.reason}"
        )
        outcome.failed = len(unreadable)
    if not records:
        return outcome

    try:
        await sink.deliver(records)
    except SinkUnavailableError as exc:
        # An outage is batch-wide: the whole claim spends one attempt and stays claimable. Never
        # re-raised; `result_publications.last_error` records it.
        await outbox.mark_failed(leases, str(exc))
        outcome.failed += len(leases)
        outcome.reason = str(exc)[:500]
        return outcome
    except Exception as exc:
        # A refusal is about one record, so the batch is re-attempted one record at a time: a single
        # poison record must not mark its neighbours failed or hold the head of the queue.
        # Re-sending a record that already landed is a no-op, because every far-side write is an
        # upsert onto a content hash.
        outcome.reason = str(exc)[:500]
        delivered: list[outbox.Lease] = []
        refused: list[outbox.Lease] = []
        for lease, record in zip(leases, records, strict=True):
            try:
                await sink.deliver([record])
            except SinkUnavailableError as outage:
                # The destination went away mid-replay: everything not yet delivered is the
                # outage's, not the poison's, and must stay claimable.
                refused.extend(leases[len(delivered) + len(refused) :])
                outcome.reason = str(outage)[:500]
                break
            except Exception as refusal:
                refused.append(lease)
                outcome.reason = str(refusal)[:500]
            else:
                delivered.append(lease)
        if refused:
            await outbox.mark_failed(refused, outcome.reason)
        leases = delivered
        outcome.failed += len(refused)

    await outbox.mark_delivered(leases)
    outcome.delivered = len(leases)
    return outcome


@durable_activity("background")
@activity.defn
async def drain_result_publications() -> PublishOutcome:
    """Drain the outbox, heartbeating: this is delivery to somebody else's database.

    What hangs is one HTTP or driver call inside a sink, so liveness is time-based. Budgeted at
    `result_publish_timeout_seconds` times the number of configured sinks.
    """
    return await beating(
        _drain_result_publications(),
        "result publication drain",
        settings.background_activity_heartbeat_timeout_seconds,
    )


async def _drain_result_publications() -> PublishOutcome:
    """Deliver one batch to each enabled sink, and report what happened.

    Never raises for a destination's own failure; does raise if the local outbox is unreadable.
    """
    outcome = PublishOutcome()
    try:
        manifests = enabled()
    except ResultSinkError as exc:
        outcome.skipped.append(f"sink configuration is invalid: {exc}")
        return outcome
    if not manifests:
        outcome.skipped.append("no result sink enabled (CHEMCLAW_RESULT_SINKS is empty)")
        return outcome

    for manifest in manifests:
        try:
            # Built per run rather than cached, so a rotated credential takes effect on the next
            # pass instead of the next restart.
            sink = build(manifest)
        except ResultSinkError as exc:
            outcome.skipped.append(f"{manifest.name}: {exc}")
            continue
        try:
            outcome.sinks.append(
                await _drain_one(manifest.name, sink, settings.result_publish_batch_size)
            )
        finally:
            # A sink is built per run (so rotated credentials take effect) and therefore closed per
            # run, in a
            # `finally`, or each pass would leak a connection.
            await sink.aclose()

    # Refresh the backlog gauges once per pass, after every row of every sink is marked (a claim
    # alone leaves rows `pending`), outside the per-sink loop so one sink's failure does not cost
    # the others their reading. Never raises.
    await outbox.refresh_backlog()
    return outcome


@durable_workflow("background")
# Deliberately left able to park: rows stay `pending` and `result_publications` reports the problem,
# it runs only from the `result-publish` Schedule (bounded by `schedule_run_timeout_seconds`), and
# the durable outbox loses nothing to a run that never finishes.
@workflow.defn
class PublishResultsWorkflow:
    """Carry queued results to their external stores on a cadence."""

    @workflow.run
    async def run(self) -> PublishOutcome:
        """Run one drain pass and return the per-sink account."""
        return await workflow.execute_activity(
            drain_result_publications,
            # The same number `publish/outbox.py` leases a claimed row for, so the lease ends
            # exactly when this
            # activity can no longer hold the row.
            start_to_close_timeout=timedelta(seconds=settings.result_publish_lease_seconds),
            schedule_to_start_timeout=queue_wait_timeout(),
            # Without a heartbeat timeout a dead worker would go unnoticed for the whole (long)
            # budget above; the beat interval derives from this value.
            heartbeat_timeout=timedelta(
                seconds=settings.background_activity_heartbeat_timeout_seconds
            ),
            retry_policy=BAD_DATA_RETRY,
        )


class JobPublishInput(BaseModel):
    """What a finished connector job hands the publish activity.

    A model rather than positional arguments because it crosses the Temporal wire: an argument
    added later is additive here and a signature change there.
    """

    calc_ref: str
    calc_type: str
    # The result model's own name; for a composite (`calc_type` is `<connector>.<job>`) this is the
    # only thing that routes it to a projector.
    payload_kind: str = ""
    payload: dict[str, object] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    actor: str = ""
    session_id: str = ""
    correlation_id: str = ""
    job_id: str = ""
    rationale: str = ""
    # The note the job's envelope produced, which `job_records.note_id` stores from the same value.
    note_id: str = ""


@durable_activity("background")
@activity.defn
async def publish_job_result(request: JobPublishInput) -> int:
    """Queue one finished job's composite result. Returns how many rows were written.

    Never raises: a completed durable job must not be failed by a publish that could not be queued.
    """
    from chemclaw.publish.record import Publication

    return await outbox.enqueue_payload(
        calc_ref=request.calc_ref,
        calc_type=request.calc_type,
        payload_kind=request.payload_kind,
        payload=dict(request.payload),
        depends_on=list(request.depends_on),
        publication=Publication(
            tenant_id="",  # filled from the sink's manifest at write time
            actor=request.actor,
            session_id=request.session_id,
            correlation_id=request.correlation_id,
            job_id=request.job_id,
            rationale=request.rationale,
            note_id=request.note_id,
        ),
    )
