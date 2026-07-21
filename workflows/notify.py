"""The job-side entry point for session push-back (plan Phase F3-T2/T3).

A completing workflow cannot touch the front-door process, so it records a `session_events` row via
this activity; the front-door tailer (`agents.session_events.stream_new_events`) then wakes the
session. Keeping the write in an activity (not the workflow) is the layer rule: workflows stay
deterministic and side-effect-free, activities do the I/O. The activity is a thin wrapper over
`agents.session_events.record_session_event`, so the channel's write logic lives in one place.
"""

from typing import Any

from pydantic import BaseModel, Field
from temporalio import activity

from agents.session_events import record_session_event


class SessionEventInput(BaseModel):
    """The typed argument for `record_session_event_activity` (a durable workflow→session note)."""

    session_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)


@activity.defn
async def record_session_event_activity(event: SessionEventInput) -> None:
    """Persist a push-back event for a session (called by a completing workflow).

    Best-effort by construction: the caller schedules it only when a session id is known, and a
    failure here must not fail the job whose result was already stored — the workflow treats it as a
    notification, not a durable side effect (durability stays in the job's own result path).
    """
    await record_session_event(event.session_id, event.kind, event.payload)
