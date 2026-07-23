"""Session push-back: the job-side activity + the workflow-side best-effort call (F3-T2/T3).

A completing workflow cannot touch the front-door process, so it records a `session_events` row via
`record_session_event_activity`; the front-door tailer (`agents.session_events.stream_new_events`)
then wakes the session. Keeping the write in an activity (not the workflow) is the layer rule:
workflows stay deterministic, activities do the I/O. `notify_session_best_effort` is the
workflow-side wrapper that schedules it on the light background queue and never fails the job whose
scientific result is already done — the push-back is a notification, not a durable side effect
(durability stays in the job's own result path).
"""

from datetime import timedelta
from typing import Any

from pydantic import BaseModel, Field
from temporalio import activity, workflow
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from agents.session_events import record_session_event
    from chemclaw.config import settings
    from workflows.publish import BAD_DATA_RETRY


class SessionEventInput(BaseModel):
    """The typed argument for `record_session_event_activity` (a durable workflow→session note)."""

    session_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)


@activity.defn
async def record_session_event_activity(event: SessionEventInput) -> None:
    """Persist a push-back event for a session (called by a completing workflow).

    A thin wrapper over `agents.session_events.record_session_event`, so the channel's write logic
    lives in one place.
    """
    await record_session_event(event.session_id, event.kind, event.payload)


async def notify_session(session_id: str, kind: str, payload: dict[str, Any]) -> None:
    """Record a session push-back event, letting a delivery failure fail the caller.

    The must-deliver half of the push-back seam (shares the one activity + input model so the write
    logic still lives in one place). For a notification that is the workflow's *only* operator-
    facing output — the eval-drift alert, whose whole point is to surface a silent regression — a
    dropped delivery would defeat the feature, so the failure must be visible (a failed workflow),
    not swallowed. Callers whose result is a durable calculation use `notify_session_best_effort`.
    """
    await workflow.execute_activity(
        record_session_event_activity,
        SessionEventInput(session_id=session_id, kind=kind, payload=payload),
        task_queue=settings.background_task_queue,
        start_to_close_timeout=timedelta(seconds=settings.qm_activity_timeout_seconds),
        retry_policy=BAD_DATA_RETRY,
    )


async def notify_session_best_effort(session_id: str, kind: str, payload: dict[str, Any]) -> None:
    """Record a session push-back event, but never fail the caller on a delivery failure.

    For a workflow whose real result is the calculation (QM, BO): the science is done and cached, so
    a failed notification must not fail the job — the same discipline as `publish_note_best_effort`
    for the note write. It runs on the light background queue (a small DB insert, not HPC).
    """
    try:
        await notify_session(session_id, kind, payload)
    except ActivityError:
        workflow.logger.warning("session push-back failed for %s", session_id)
