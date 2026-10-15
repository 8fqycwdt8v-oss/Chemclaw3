"""Shared workflow-side queue-wait bounds and note-publish helpers for durable activities.

The note and result publish helpers run on the light background queue and, for best-effort
publishes, never fail a completed scientific result. The queue-wait bounds say how long an activity
may sit unclaimed, in three sizes: `queue_wait_timeout` (core's hour),
`connector_queue_wait_timeout` (a bundle's, where a wait is backpressure) and
`light_write_queue_wait_timeout` (end-of-job writes). How an activity is retried is
`durable/retry.py`'s.
"""

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ActivityError, TimeoutType
from temporalio.exceptions import TimeoutError as TemporalTimeoutError

with workflow.unsafe.imports_passed_through():
    from chemclaw.core.config import settings
    from chemclaw.core.metrics_bridge import record_metric
    from chemclaw.durable.retry import retry_for


def queue_wait_timeout() -> timedelta:
    """How long a core activity may sit unclaimed on its queue, as `schedule_to_start_timeout`.

    `start_to_close_timeout` starts only once a worker picks the task up, so on an unserved queue
    it bounds nothing, and a Schedule under SKIP then skips every later fire. Schedule-to-start is
    not retried and leaves the retry budget intact, unlike schedule-to-close, which caps all
    attempts together. A function so the setting is read at run time, not import time.

    Returns:
        The `schedule_to_start_timeout` every core activity call passes.
    """
    return timedelta(seconds=settings.activity_queue_wait_seconds)


def light_write_queue_wait_timeout() -> timedelta:
    """How long a *small* write may wait on the shared background queue, before it is a fault.

    For the end-of-job writes (session push-back, job record, outbound copy), which callers swallow;
    an hour of patience there is an hour a finished or failed job reports nothing.
    `tests/test_activity_queue_bound.py` holds every dispatch site to its bound.

    Equal to `template_step_timeout_seconds`, the longest single activity this queue runs and so
    the worst a slot holder can put in front of a small write. Derived rather than configured so the
    relationship holds. The caller's `start_to_close_timeout` bounds the work separately.

    Returns:
        The `schedule_to_start_timeout` every end-of-job write passes.
    """
    return timedelta(seconds=settings.template_step_timeout_seconds)


def fan_out_queue_wait_timeout() -> timedelta:
    """How long a **fan-out child's** activity may sit unclaimed before the child gives up.

    The wait precedes the work, so the child's ceiling `C` must contain wait plus work. This takes
    the ceiling's headroom, `C - w - overhead`, where `w` is `longest_fan_out_activity`, so the
    composite fits by construction and an unserved queue surfaces as a named activity timeout rather
    than a child execution timeout no workflow code sees.

    Returns:
        The `schedule_to_start_timeout` a fan-out child's activity passes. Strictly positive by
        construction: `_the_fan_out_ceiling_covers_the_section_it_bounds` refuses a ceiling that
        does not exceed the longest fan-out activity plus one activity's overhead.
    """
    longest, _ = settings.longest_fan_out_activity
    return timedelta(
        seconds=settings.fan_out_child_timeout_seconds - longest - settings.activity_timeout_seconds
    )


def connector_queue_wait_timeout() -> timedelta:
    """How long a **connector bundle's** activity may sit unclaimed on its own queue.

    On a bundle queue a long wait is ordinary backpressure, but an unbounded one makes "no worker is
    serving `connector-calc`" indistinguishable from "every worker is busy" until the child's
    execution timeout, which is delivered to nobody.

    The bound is the ceiling's headroom, `C - w - overhead`, with `w` = `longest_bundle_activity`,
    so wait plus one attempt always fits the parent's execution budget; a fraction of `C` cannot
    guarantee that. A ScheduleToStart expiry is not retried: an unserved queue stays unserved.

    This holds for one activity. A child dispatching a sequence (`BoCampaignWorkflow`) must use
    `remaining_queue_wait_timeout` instead.

    Returns:
        The `schedule_to_start_timeout` a single-activity connector-bundle child passes. Strictly
        positive by construction: `Settings` refuses a ceiling that does not exceed the longest
        activity plus one activity's overhead.
    """
    longest, _ = settings.longest_bundle_activity
    return timedelta(seconds=_queue_wait_seconds(settings.connector_job_timeout_seconds, longest))


def _queue_wait_seconds(budget: float, activity_seconds: float) -> float:
    """What is left of `budget` for a queue wait once one attempt and its overhead are paid for.

    The one subtraction behind the bounds above and below.

    Args:
        budget: The execution budget this wait has to fit inside, in seconds.
        activity_seconds: The start-to-close budget of the attempt that follows the wait.

    Returns:
        The wait in seconds. May be zero or negative: for the deployment-wide ceiling `Settings`
        has already refused that case; for a run partway through its budget it means nothing is
        left to fund another activity.
    """
    return budget - activity_seconds - settings.activity_timeout_seconds


def remaining_queue_wait_timeout(remaining: timedelta, activity_seconds: float) -> timedelta | None:
    """The queue wait a bundle child may still afford, given what is left of its execution budget.

    For a child that dispatches a sequence: the execution timeout spans the whole continue-as-new
    chain, so the budget is spent down rather than re-granted, keeping the sum of waits and attempts
    within the ceiling for any number of steps. `None` means the remaining budget cannot fund
    another attempt, and the caller stops with what it has (raising in workflow code would retry
    forever). `activity_seconds` is the caller's own budget; callers also apply the queue-wide
    bound and take the minimum.

    Args:
        remaining: What is left of this run's execution budget.
        activity_seconds: The start-to-close budget of the activity about to be dispatched.

    Returns:
        The `schedule_to_start_timeout` for the next dispatch, or None when the budget can no
        longer fund one.
    """
    seconds = _queue_wait_seconds(remaining.total_seconds(), activity_seconds)
    return timedelta(seconds=seconds) if seconds > 0 else None


def activity_failure_reason(exc: ActivityError) -> str:
    """A short reason for a *swallowed* activity failure, so one log line separates two states.

    A `SCHEDULE_TO_START` expiry means nobody polls the queue (fixed by a worker, not a retry);
    anything else is the write itself, reported by type.

    Args:
        exc: The swallowed activity error, whose `cause` carries what Temporal decided.

    Returns:
        A sentence fragment for the caller's own log line. Never raises.
    """
    cause = exc.cause
    if isinstance(cause, TemporalTimeoutError):
        if cause.type is TimeoutType.SCHEDULE_TO_START:
            return (
                "no worker claimed the task within the configured queue wait "
                "(schedule-to-start): nothing is serving that queue, or it is backed up past "
                "the bound"
            )
        # `type` is optional on the SDK's own model, so the unnamed case is spelled rather than
        # assumed away: a timeout that does not say which one it was is still a timeout.
        which = cause.type.name.lower() if cause.type is not None else "kind unreported"
        return f"the activity timed out ({which})"
    return type(cause).__name__ if cause is not None else "unknown"


async def publish_note(activity: Any, args: list[Any]) -> str:
    """Run a note-publish activity with the shared queue/timeout/retry discipline."""
    result: str = await workflow.execute_activity(
        activity,
        args=args,
        task_queue=settings.background_task_queue,
        start_to_close_timeout=timedelta(seconds=settings.note_write_timeout_seconds),
        schedule_to_start_timeout=queue_wait_timeout(),
        retry_policy=retry_for(activity),
    )
    return result


async def publish_note_best_effort(activity: Any, args: list[Any], label: str) -> None:
    """Publish a note but never fail the caller: log-and-swallow a failed write.

    For workflows whose real result is the calculation: a broken git remote must not fail the job.
    A failure is counted, so a dead remote is distinguishable from an idle deployment; guarded on
    `is_replaying` so a replay does not re-count.
    """
    try:
        await publish_note(activity, args)
    except ActivityError:
        workflow.logger.warning("knowledge-note publish failed for %s", label)
        if not workflow.unsafe.is_replaying():
            record_metric(lambda m: m.increment("chemclaw_notes_publish_failures_total"))


async def publish_result_best_effort(activity: Any, args: list[Any], label: str) -> None:
    """Queue a finished run's result for the external results store, never failing the caller.

    The result is already durable in `job_records`, so an unavailable outbox must not fail a
    completed job. Separate from the note publish: different timeout, counter and meaning. The
    counter is guarded on `is_replaying`.
    """
    try:
        await workflow.execute_activity(
            activity,
            args=args,
            task_queue=settings.background_task_queue,
            start_to_close_timeout=timedelta(seconds=settings.result_publish_timeout_seconds),
            schedule_to_start_timeout=queue_wait_timeout(),
            retry_policy=retry_for(activity),
        )
    except ActivityError:
        workflow.logger.warning("result publication failed to queue for %s", label)
        if not workflow.unsafe.is_replaying():
            record_metric(lambda m: m.increment("chemclaw_result_publish_failures_total"))
