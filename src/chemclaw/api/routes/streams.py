"""The job push-back stream: a finished durable job wakes the chat that launched it (F3-T3).

One route, `GET /sessions/{id}/events`, plus the two closures that stay nested in it on purpose:
`_release_stream_slot` and `_events` capture this request's principal and admission bookkeeping —
per-request state with exactly one consumer — so hoisting them would thread arguments to move code
nowhere. The per-user/per-pod stream ledger they mutate is app-wide and is read through
`chemclaw.api.state.state(request)`, the seam of the R3.2 split.
"""

from collections.abc import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from chemclaw.agent.harness_todo import defer_job_completion
from chemclaw.api import app as front_door
from chemclaw.api.deps import CurrentUser, resolve_session
from chemclaw.api.events import JobCompletedEvent
from chemclaw.api.state import state
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS


async def session_events(
    request: Request,
    session_id: str,
    principal: CurrentUser,
) -> EventSourceResponse:
    """Stream async job push-back for the session (F3-T3): a finished job wakes the chat.

    Bounded twice, because one bound does not imply the other: per user
    (`service_max_event_streams_per_user`) so no single client can fan out, and across all
    users on this process (`service_max_event_streams_total`) because 50 chemists each within
    their per-user cap is still 250 forever-polling tasks on one event loop. Each stream
    polls the database for its whole lifetime, so unbounded streams are a load vector (429
    past either cap). The claim is scoped to `job_completed` in the SQL itself — the claim is
    destructive (at-most-once), so filtering after it would silently destroy events of any
    other kind meant for another consumer.
    """
    streams: dict[str, int] = state(request).event_streams
    at_user_cap = streams.get(principal.oid, 0) >= settings.service_max_event_streams_per_user
    at_pod_cap = sum(streams.values()) >= settings.service_max_event_streams_total
    if at_user_cap or at_pod_cap:
        METRICS.increment("chemclaw_event_streams_rejected_total")
        raise HTTPException(
            status_code=429, detail="too many concurrent event streams; close one and retry"
        )
    streams[principal.oid] = streams.get(principal.oid, 0) + 1

    def _release_stream_slot() -> None:
        """Return this stream's per-user slot — exactly once, whoever owns cleanup."""
        remaining = streams.get(principal.oid, 1) - 1
        if remaining <= 0:
            streams.pop(principal.oid, None)
        else:
            streams[principal.oid] = remaining

    async def _events() -> AsyncIterator[dict[str, str]]:
        try:
            # Through the front-door module so the suite's patch seam
            # (`chemclaw.api.app.stream_new_events`) keeps reaching the tailer this route runs.
            async for pushed in front_door.stream_new_events(session_id, kinds=("job_completed",)):
                job_id = str(pushed.payload.get("job_id", ""))
                # Record the completion for the harness todo waiting on this job (F3-T3
                # follow-up) — recorded, not applied. This stream runs concurrently with
                # whatever turn the session has in flight, and flipping the todo here was a
                # load-modify-save over the live `session.state` under a running writer: a
                # flip landing mid-turn was silently un-done when a disconnect teardown
                # restored that turn's pre-turn snapshot (`chemclaw.api.runner`). The next
                # turn applies it at its start (`apply_deferred_completions`), where nothing
                # else writes; the notification itself was already durably claimed above,
                # so nothing durable rides on this process surviving.
                if settings.harness_enabled:
                    defer_job_completion(session_id, job_id, reason=f"QM job {job_id} completed")
                event = JobCompletedEvent(job_id=job_id, summary=pushed.payload)
                yield {"event": event.type, "data": event.model_dump_json()}
        finally:
            _release_stream_slot()

    handed_off = False
    try:
        response = EventSourceResponse(_events(), ping=settings.service_sse_ping_seconds)
        handed_off = True
        return response
    finally:
        # Mirrors the turn route: any BaseException before the response is handed off must
        # return the slot, or the user's stream budget leaks toward a permanent 429.
        if not handed_off:
            _release_stream_slot()


def register(app: FastAPI) -> None:
    """Attach this module's route to `app` — called once, by `create_app` only.

    Registered with the app's own decorators rather than an `APIRouter` + `include_router`:
    since FastAPI 0.139 `include_router` is lazy — `app.routes` would hold opaque
    `_IncludedRouter` nodes, invisible to everything that walks the route table by type
    (`tests/test_route_auth_coverage.py`, the session-scope inventory in
    `tests/test_service.py`) — and a standalone router's routes carry no
    `dependency_overrides_provider`, which silently disables `app.dependency_overrides`.
    Registering on the app keeps both exactly as they were when these handlers lived in
    `create_app`.
    """
    app.get("/sessions/{session_id}/events", dependencies=[Depends(resolve_session)])(
        session_events
    )
