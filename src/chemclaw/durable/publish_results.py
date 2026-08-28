"""Draining the result outbox to whatever external stores a deployment enabled.

The half of the publish path that runs *away* from the calculation. `publish/outbox.py` writes a
projected record locally in the same act that produces it; this job carries those rows to their
destination, retries what fails, and gives up loudly rather than silently once a row has spent its
attempt budget.

**One workflow, one activity per sink.** Per sink, because two enabled destinations are two failure
domains: one being unreachable must not hold up the other, and a batch that fails for one must not
mark the other's rows. That is also why `result_publications` carries a row per (sink, calculation)
rather than one row with a set of destinations.

**A failed batch leaves its rows `pending` and the run succeeds.** The alternative — failing the
workflow — would put a destination's outage into Temporal's retry loop as well as into this table's
`attempts` column, two backoffs for one problem, and would make an operator read a workflow failure
to learn something `result_publications` already says more precisely. What the run returns instead
is a per-sink account, so a scheduled run's history is the record of how publishing is going.
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

    One batch per run rather than draining to empty, deliberately. A backlog then takes several
    scheduled passes to clear, which is the right shape: it bounds how long a single activity holds
    a connection and how much a single failure re-attempts, and the schedule is frequent enough
    that a real backlog still drains steadily. An operator in a hurry runs the backfill CLI.
    """
    outcome = SinkOutcome(sink=manifest_name)
    claimed = await outbox.claim(manifest_name, batch_size)
    if not claimed:
        return outcome
    # **Parsed per row, never per batch.** One row projected by an older writer whose record shape
    # this release cannot read is one row's problem: validating the batch inside a single `try`
    # meant a single unreadable document marked every id in the claim failed, retiring up to
    # `batch_size - 1` perfectly deliverable rows once they had spent their attempts. A poison row
    # must not take its neighbours with it, and which neighbours it took would depend only on the
    # order `claim` happened to return.
    ids: list[int] = []
    records: list[ResultRecord] = []
    unreadable: list[int] = []
    for row_id, _, document in claimed:
        try:
            records.append(ResultRecord.model_validate(document))
        except Exception as exc:
            # Will not fix itself on a retry, so it spends an attempt rather than looping forever.
            unreadable.append(row_id)
            outcome.reason = str(exc)[:500]
            continue
        ids.append(row_id)
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
        # **An outage is genuinely batch-wide**, and is the one case that stays so: nothing in this
        # batch reached the destination and nothing in it would on a second try, so the whole claim
        # spends one attempt and stays claimable until the budget runs out.
        #
        # Never re-raised. A destination's failure is data about that destination; putting it into
        # Temporal's retry loop as well would be two backoffs for one problem, and would make an
        # operator read a workflow failure to learn what `result_publications.last_error` says more
        # precisely.
        await outbox.mark_failed(ids, str(exc))
        outcome.failed += len(ids)
        outcome.reason = str(exc)[:500]
        return outcome
    except Exception as exc:
        # **A refusal is about one record, so it is re-attempted one record at a time.** This used
        # to share the handler above, which made the delivery side do the opposite of the parse
        # side ten lines up: one record the sink would not take marked *every* id in the claim
        # failed. Because `SqlResultSink` writes record-by-record on an autocommit connection, the
        # records before the poison were already durable at the far end while being booked
        # `failed`, and the ones after it were never attempted at all — and because `_CLAIM` is
        # `ORDER BY enqueued_at`, the poison stayed at the head of the queue and re-collected the
        # same neighbours every pass until the whole group had spent its attempts. At a batch size
        # of 100 that is up to 99 good records retired per poison, recoverable only by an operator
        # running `--requeue`.
        #
        # The replay is free of duplication because every write on the far side is an upsert onto a
        # content hash, so re-sending a record that already landed is a no-op. It costs one
        # delivery per record for the one pass in which a refusal occurs, which is bounded by the
        # batch size and is the smaller harm by far.
        outcome.reason = str(exc)[:500]
        delivered: list[int] = []
        refused: list[int] = []
        for row_id, record in zip(ids, records, strict=True):
            try:
                await sink.deliver([record])
            except SinkUnavailableError as outage:
                # The destination went away mid-replay: everything not yet delivered is the
                # outage's, not the poison's, and must stay claimable.
                refused.extend(ids[len(delivered) + len(refused) :])
                outcome.reason = str(outage)[:500]
                break
            except Exception as refusal:
                refused.append(row_id)
                outcome.reason = str(refusal)[:500]
            else:
                delivered.append(row_id)
        if refused:
            await outbox.mark_failed(refused, outcome.reason)
        ids = delivered
        outcome.failed += len(refused)

    await outbox.mark_delivered(ids)
    outcome.delivered = len(ids)
    return outcome


@durable_activity("background")
@activity.defn
async def drain_result_publications() -> PublishOutcome:
    """Drain the outbox, heartbeating: this is delivery to somebody else's database.

    A thin wrapper for the reason `retention.prune_expired_rows` is one — a per-sink boundary would
    report progress through a batch, and the thing that actually hangs is one HTTP or driver call
    inside a sink. The budget it sits under is `result_publish_timeout_seconds` multiplied by the
    number of configured sinks, which is the longest of the three core background activities and
    the only one whose slow part is *outside* this deployment.
    """
    return await beating(
        _drain_result_publications(),
        "result publication drain",
        settings.background_activity_heartbeat_timeout_seconds,
    )


async def _drain_result_publications() -> PublishOutcome:
    """Deliver one batch to each enabled sink, and report what happened.

    Never raises for a destination's own failure — see the module docstring. It *does* raise if the
    outbox itself is unreadable, because that is this deployment's database rather than someone
    else's service, and a job that cannot read its own queue has nothing useful to report.
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
            # **Built per run means closed per run.** Building a sink each pass is deliberate (a
            # rotated credential takes effect on the next run, not the next restart) and it is
            # exactly what makes an unclosed connection unbounded: one leaked per pass, every
            # `result_publish_schedule_minutes`, reaching a stock `max_connections` inside a day
            # and then failing the whole worker rather than the publish. In a `finally`, because a
            # sink that failed its batch is holding the same connection as one that succeeded.
            await sink.aclose()

    # **The backlog gauges are refreshed here, once, after every row of every sink has been
    # marked.** Three things put it at exactly this point:
    #
    # - *After the marking*, because a claim does not remove a row from the backlog. `_CLAIM` only
    #   increments `attempts`; the state stays `pending` until `mark_delivered`/`mark_failed` runs.
    #   The refresh used to sit inside `outbox.claim` with a comment saying it was taken after the
    #   claim so the reading "excludes the rows this pass is about to deliver" — measured, three
    #   rows and one `claim()` left `chemclaw_outbox_pending{sink="probe"} 3.0` with all three
    #   still pending. The gauge published the pre-drain depth and held it for a whole pass.
    # - *Once per pass rather than once per sink*, because `refresh_backlog` reads every sink in
    #   two `GROUP BY sink` statements. Inside `claim` it ran N times per pass for N sinks, N-1 of
    #   them redundant — and one of the two is a sequential scan of the whole table (see
    #   `publish/outbox._DEAD_LETTERED`, ~20 ms on 200k rows), which is not a read to repeat per
    #   destination for the same answer.
    # - *Outside the per-sink loop*, so a sink whose driver would not build, or whose batch failed,
    #   does not cost the other sinks their reading.
    #
    # It never raises — see `refresh_backlog` — so telemetry cannot fail a pass that just published.
    await outbox.refresh_backlog()
    return outcome


@durable_workflow("background")
@workflow.defn
class PublishResultsWorkflow:
    """Carry queued results to their external stores on a cadence."""

    @workflow.run
    async def run(self) -> PublishOutcome:
        """Run one drain pass and return the per-sink account."""
        return await workflow.execute_activity(
            drain_result_publications,
            start_to_close_timeout=timedelta(
                seconds=settings.result_publish_timeout_seconds
                * max(1, len(settings.result_sink_list))
            ),
            schedule_to_start_timeout=queue_wait_timeout(),
            # Without a heartbeat timeout the beats the activity now sends do nothing for failure
            # detection, and the budget above is the longest of the three core background
            # activities — `result_publish_timeout_seconds` times the number of sinks. A worker
            # that dies while delivering to an external store would otherwise be invisible for all
            # of it. The beat is derived from this same number, so the two cannot drift.
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
    # The result model's own name. A composite's `calc_type` is `<connector>.<job>`, which no
    # projector prefix matches, so this is the *only* thing that routes one — see
    # `ConnectorJobResult.payload_kind`.
    payload_kind: str = ""
    payload: dict[str, object] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    actor: str = ""
    session_id: str = ""
    correlation_id: str = ""
    job_id: str = ""
    rationale: str = ""


@durable_activity("background")
@activity.defn
async def publish_job_result(request: JobPublishInput) -> int:
    """Queue one finished job's composite result. Returns how many rows were written.

    Never raises: `outbox.enqueue_payload` is best-effort by construction, and a completed durable
    job must not be failed by a publish that could not be queued.
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
        ),
    )
