"""Session push-back: the job-side activity + the workflow-side best-effort call (F3-T2/T3).

A completing workflow cannot touch the front-door process, so it records a `session_events` row via
`record_session_event_activity`; the front-door tailer
(`chemclaw.agent.session_events.stream_new_events`)
then wakes the session. Keeping the write in an activity (not the workflow) is the layer rule:
workflows stay deterministic, activities do the I/O. `notify_session_best_effort` is the
workflow-side wrapper that schedules it on the light background queue and never fails the job whose
scientific result is already done — the push-back is a notification, not a durable side effect
(durability stays in the job's own result path).
"""

import hashlib
import json
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, Field
from temporalio import activity, workflow
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from chemclaw.agent.session_events import record_session_event
    from chemclaw.core.config import settings
    from chemclaw.core.metrics_bridge import record_metric
    from chemclaw.durable.publish import (
        BAD_DATA_RETRY,
        activity_failure_reason,
        light_write_queue_wait_timeout,
    )
    from chemclaw.durable.registry import durable_activity


class SessionEventInput(BaseModel):
    """The typed argument for `record_session_event_activity` (a durable workflow→session note)."""

    session_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    # Deterministic identity of this logical event, derived in workflow code (`_dedupe_key`):
    # the activity runs at-least-once, so without it a retry after a committed-but-unacked
    # insert would deliver the same notification twice.
    dedupe_key: str | None = None


def _dedupe_key(workflow_id: str, run_id: str, kind: str, payload: dict[str, Any]) -> str:
    """The deterministic identity of one logical push-back event, for the at-most-once insert.

    Derived from the *run* (not just the workflow id — a later re-execution of the same workflow
    id is genuinely a new event) plus the kind and a payload digest, because one run may emit
    several events of the same kind (e.g. one eval-drift alert per drifted metric) that must not
    dedupe each other. Every input is replay-stable, so an activity retry recomputes the same key
    and lands on the unique index instead of duplicating the notification.
    """
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"{workflow_id}:{run_id}:{kind}:{digest}"


@durable_activity("background")
@activity.defn
async def record_session_event_activity(event: SessionEventInput) -> None:
    """Persist a push-back event for a session (called by a completing workflow).

    A thin wrapper over `chemclaw.agent.session_events.record_session_event`, so the channel's
    write logic
    lives in one place.
    """
    await record_session_event(
        event.session_id, event.kind, event.payload, dedupe_key=event.dedupe_key
    )


async def notify_session(session_id: str, kind: str, payload: dict[str, Any]) -> None:
    """Record a session push-back event, letting a delivery failure fail the caller.

    The must-deliver half of the push-back seam (shares the one activity + input model so the write
    logic still lives in one place). For a notification that is the workflow's *only* operator-
    facing output — the eval-drift alert, whose whole point is to surface a silent regression — a
    dropped delivery would defeat the feature, so the failure must be visible (a failed workflow),
    not swallowed. Callers whose result is a durable calculation use `notify_session_best_effort`.
    """
    info = workflow.info()
    await workflow.execute_activity(
        record_session_event_activity,
        SessionEventInput(
            session_id=session_id,
            kind=kind,
            payload=payload,
            dedupe_key=_dedupe_key(info.workflow_id, info.run_id, kind, payload),
        ),
        task_queue=settings.background_task_queue,
        start_to_close_timeout=timedelta(seconds=settings.activity_timeout_seconds),
        # **`start_to_close` alone is not a bound on this call, and that is what made "best effort"
        # able to block a finished job forever.** It runs only once a worker has *picked the task
        # up*; a task nobody polls — the background fleet scaled to zero, a rolling update, a queue
        # named in config but served by no pod — simply waits. Measured: a workflow calling
        # `notify_session_best_effort` against an unserved queue was still RUNNING after 75 s with
        # this timeout at 30. So the caller whose contract is "never fail the job whose scientific
        # result is already done" was instead holding it open indefinitely, and the `except
        # ActivityError` below was unreachable in precisely the case it exists for.
        #
        # **It was bounded by `schedule_to_close_timeout = activity_timeout_seconds * 2`, and that
        # is the wrong quantity — a *total* budget spent almost entirely on a wait this call does
        # not control.** `background-jobs` also carries 900 s template agent steps, 300 s report
        # sections and the hourly sweeps, on eight slots. Measured on the real broker: with those
        # slots held by long activities a 50 ms activity waited 41.6 s, and the shipped shape (30 s
        # start-to-close, 60 s schedule-to-close, 5 attempts) behind eight 120 s activities was
        # dropped at 60.1 s with `ActivityError: Activity task timed out`. At target load the
        # expected wait for a slot is ~150 s, so *essentially every* completion push-back was
        # dropped and the chemist's session showed "running" forever — a queue-pressure reopening
        # of the exact defect `D-2026-08-04-a-failure-that-says-nothing-is-read-as-proceed` closed.
        #
        # So the two quantities are separated, which is what Temporal has these timeouts for: the
        # bound above is the *work* (one small insert), and the bound below is the *wait*. The
        # retry budget comes back with it — schedule-to-close capped all five attempts together, so
        # a single slow pickup spent the lot. It is `light_write_queue_wait_timeout` rather than
        # core's hour because this call is at the *end* of a job: an hour of patience here is an
        # hour in which a finished job has told nobody. `durable/publish.py` states the number and
        # what it is derived from.
        schedule_to_start_timeout=light_write_queue_wait_timeout(),
        retry_policy=BAD_DATA_RETRY,
    )


async def notify_session_best_effort(session_id: str, kind: str, payload: dict[str, Any]) -> bool:
    """Record a session push-back event, but never fail the caller on a delivery failure.

    For a workflow whose real result is the calculation (QM, BO): the science is done and cached, so
    a failed notification must not fail the job — the same discipline as `publish_note_best_effort`
    for the note write. It runs on the light background queue (a small DB insert, not a
    calculation).

    Returns whether the event was recorded. Most callers ignore it, exactly because the science is
    the result and the notification is not. A caller that advances a *watermark* past what it just
    tried to send must not: for it, "delivered" and "swallowed" are different facts, and treating
    them alike loses the matches the failed send covered forever (`durable/digest.py`).
    """
    try:
        await notify_session(session_id, kind, payload)
    except ActivityError as exc:
        # **Named, not just counted.** Every drop used to read the same whatever caused it, so the
        # two states an operator has to tell apart — "the background queue is unserved" and "the
        # insert failed" — arrived as one line. `TimeoutType.SCHEDULE_TO_START` is the first one,
        # and it is the one no amount of retrying inside this workflow can help: it means nothing
        # is polling `background-jobs`, so the chemist's completion is lost for a fleet reason and
        # the fix is a worker, not a redelivery.
        workflow.logger.warning(
            "session push-back failed for %s: %s", session_id, activity_failure_reason(exc)
        )
        # Counted, because the log line above is workflow-scoped and swallowed by every caller: a
        # fleet-wide push-back outage — a dead background queue, a full mailbox table — was
        # invisible on any dashboard while every job's completion silently reached nobody. Guarded
        # against replay so a workflow history rebuild does not re-count a drop that happened once.
        if not workflow.unsafe.is_replaying():
            record_metric(lambda m: m.increment("chemclaw_pushback_dropped_total"))
        return False
    return True
