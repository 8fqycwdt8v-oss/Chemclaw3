"""Session push-back: the job-side activity and the workflow-side calls.

A completing workflow cannot touch the front-door process, so it records a `session_events` row
via `record_session_event_activity`; the front-door tailer
(`chemclaw.agent.session_events.stream_new_events`) then wakes the session. The write is an
activity so workflows stay deterministic. `notify_session_best_effort` never fails a job whose
result is already done; `notify_session` is the must-deliver variant.
"""

import asyncio
import hashlib
import json
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, Field
from temporalio import activity, workflow
from temporalio.exceptions import ActivityError
from temporalio.exceptions import CancelledError as TemporalCancelledError

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
    # Deterministic identity of this logical event, derived in workflow code (`_dedupe_key`), so an
    # at-least-once retry after a committed insert does not deliver twice.
    dedupe_key: str | None = None


def _dedupe_key(workflow_id: str, run_id: str, kind: str, payload: dict[str, Any]) -> str:
    """The deterministic identity of one logical push-back event, for the at-most-once insert.

    Derived from the run (a re-execution of the workflow id is a new event), the kind and a payload
    digest (one run may emit several events of one kind). Every input is replay-stable.
    """
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"{workflow_id}:{run_id}:{kind}:{digest}"


@durable_activity("background")
@activity.defn
async def record_session_event_activity(event: SessionEventInput) -> None:
    """Persist a push-back event for a session (called by a completing workflow).

    A thin wrapper over `chemclaw.agent.session_events.record_session_event`.
    """
    await record_session_event(
        event.session_id, event.kind, event.payload, dedupe_key=event.dedupe_key
    )


async def notify_session(session_id: str, kind: str, payload: dict[str, Any]) -> None:
    """Record a session push-back event, letting a delivery failure fail the caller.

    For a notification that is the workflow's only operator-facing output (the eval-drift alert), a
    dropped delivery must be visible as a failed workflow. Callers whose result is a durable
    calculation use `notify_session_best_effort`.
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
        # `start_to_close` bounds only the work once a worker picks the task up; this bounds the
        # wait for a slot on a busy or unserved `background-jobs` queue, separately, so slow pickup
        # does not eat the retry budget. Shorter than core's hour because this runs at the end of a
        # job, and a finished job should not wait an hour to tell anyone.
        schedule_to_start_timeout=light_write_queue_wait_timeout(),
        retry_policy=BAD_DATA_RETRY,
    )


async def notify_session_best_effort(session_id: str, kind: str, payload: dict[str, Any]) -> bool:
    """Record a session push-back event, but never fail the caller on a delivery failure.

    For a workflow whose real result is the calculation: a failed notification must not fail the
    job. Returns whether the event was recorded; a caller that advances a watermark past what it
    sent must check it (`durable/digest.py`).

    A cancellation (`ActivityError(cause=CancelledError)`) is not a delivery failure: it is
    re-raised as `asyncio.CancelledError` so the caller's cleanup clause runs (e.g. a durable wait
    settling its `pending_requests` row).
    """
    try:
        await notify_session(session_id, kind, payload)
    except ActivityError as exc:
        if isinstance(exc.cause, TemporalCancelledError):
            raise asyncio.CancelledError(
                f"the push-back to session {session_id} was cancelled with its workflow"
            ) from exc
        # Name the cause: `SCHEDULE_TO_START` means nothing is polling `background-jobs`, which
        # needs a worker, not a retry.
        workflow.logger.warning(
            "session push-back failed for %s: %s", session_id, activity_failure_reason(exc)
        )
        # Counted so a fleet-wide push-back outage shows on a dashboard; guarded so a replay does
        # not re-count.
        if not workflow.unsafe.is_replaying():
            record_metric(lambda m: m.increment("chemclaw_pushback_dropped_total"))
        return False
    return True
