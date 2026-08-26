"""The job push-back stream: a finished durable job wakes the chat that launched it (F3-T3).

One route, `GET /sessions/{id}/events`, plus the two closures that stay nested in it on purpose:
`_release_stream_slot` and `_events` capture this request's principal and admission bookkeeping —
per-request state with exactly one consumer — so hoisting them would thread arguments to move code
nowhere. The per-user/per-pod stream ledger they mutate is app-wide and is read through
`chemclaw.api.state.state(request)`, the seam of the R3.2 split.

The one thing that is *not* a closure is `_SlotBoundEventStream`, and that is the point: the slot
belongs to the response's lifetime rather than to the generator's, which is strictly shorter (see
its docstring).
"""

import logging
from collections.abc import AsyncIterator, Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from sse_starlette.sse import EventSourceResponse
from starlette.types import Receive, Scope, Send

from chemclaw.api import app as front_door
from chemclaw.api.deps import CurrentUser, resolve_session
from chemclaw.api.events import ErrorEvent, JobCompletedEvent, JobFailedEvent
from chemclaw.api.state import state
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS

logger = logging.getLogger(__name__)


class _SlotBoundEventStream(EventSourceResponse):
    """An SSE response that holds its admission slot for exactly as long as it is being served.

    The slot's release used to live only in the body generator's `finally`, which is a strictly
    *shorter* scope than the stream: sse-starlette starts the generator only after
    `http.response.start` has been written, and a client that vanishes while that write is still
    in flight makes the disconnect listener cancel the task group before the first `__anext__`.
    An async generator that never started runs no `finally` at all — measured, and it survives
    `gc.collect()` — so the user's slot was gone for the pod's lifetime. Five of those and an
    honest sixth connect got `429 ... close one and retry` with nothing open to close.

    Releasing in `__call__`'s `finally` is the fix rather than a second guard, because this is
    the scope the resource actually has: Starlette awaits the response exactly once for the
    request, so the block below runs on a completed stream, on an exception, and on cancellation
    alike — every way a stream can end, including the window above.

    **Not a lease** (the shape `chemclaw.api.state._claim_turn_slot` uses for the turn slot's
    identical window): a turn has a widest wall clock, so an expired entry provably belongs to no
    live turn. A push-back stream is deliberately unbounded — it polls until the client leaves —
    so any deadline short enough to clear a leak would also evict a healthy stream's accounting
    and let one user exceed the cap the ledger exists to enforce.
    """

    def __init__(
        self, content: AsyncIterator[dict[str, str]], *, release: Callable[[], None], ping: int
    ) -> None:
        """Wrap `content`, calling `release` once the response has finished being served."""
        super().__init__(content, ping=ping)
        self._release = release

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the stream, returning the admission slot however it ends."""
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._release()


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
    past either cap). The claim is scoped to the two job-outcome kinds in the SQL itself — the
    claim is destructive (at-most-once), so filtering after it would silently destroy events of any
    other kind meant for another consumer. Both outcomes are claimed here, because a job that
    failed after its turn ended has exactly the same claim on the asker's attention as one that
    succeeded, and only one of the two used to have a way to reach them.
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
        # No `finally` returning the slot here: the response owns that now
        # (`_SlotBoundEventStream`), whose scope strictly contains this generator's. Keeping
        # both would decrement twice for one stream — and the site kept would be the one that
        # does not run when a client vanishes before the first advance.
        #
        # Through the front-door module so the suite's patch seam
        # (`chemclaw.agent.session_events.stream_new_events`) keeps reaching the tailer this
        # route runs.
        try:
            async for pushed in front_door.stream_new_events(
                session_id, kinds=("job_completed", "job_failed")
            ):
                job_id = str(pushed.payload.get("job_id", ""))
                failed = pushed.kind == "job_failed"
                reason = str(pushed.payload.get("reason", ""))
                # The completion used to also be recorded against a harness todo waiting on this
                # job, deferred rather than applied because this stream runs concurrently with
                # whatever turn the session has in flight. Both halves are gone: the todo existed
                # so the previous engine's loop predicate saw "waiting" rather than re-invoking the
                # model, and the graph's loop ends when the model stops calling tools. What the
                # chemist sees is unchanged — that was always this event, not the todo.
                event: JobCompletedEvent | JobFailedEvent = (
                    JobFailedEvent(job_id=job_id, reason=reason)
                    if failed
                    else JobCompletedEvent(job_id=job_id, summary=pushed.payload)
                )
                yield {"event": event.type, "data": event.model_dump_json()}
        except Exception as exc:
            # **A stream that dies has to say so, and the registered handler cannot say it here.**
            # `create_app` turns a failed Postgres checkout into a retryable 503, but that handler
            # is structurally unreachable once a response has started: Starlette raises
            # `RuntimeError("Caught handled exception, but response already started.")` *instead*
            # of calling it. So the tailer's `ConnectionError` — Postgres rolled, or the pool
            # saturated (its own load vector, since every open tab polls) — reached the browser as
            # a truncated stream indistinguishable from "no job has finished yet", reached the log
            # as an unhandled application error, and reached `chemclaw_db_unavailable_total` not at
            # all: the counter an operator alerts on undercounted exactly the population that
            # matters most, the open tabs.
            #
            # Counted under the same name the write side uses (`middleware._database_unavailable`),
            # because it is one event seen from the read side. `ErrorEvent` rather than a new
            # member on this stream's event set: `storage_unavailable` already means precisely
            # this and is already in the closed taxonomy every surface switches on.
            #
            # `Exception`, so `GeneratorExit`/`CancelledError` — an ordinary client disconnect —
            # still tear the generator down untouched rather than being reported as an outage.
            METRICS.increment("chemclaw_db_unavailable_total")
            logger.warning("push-back stream for session %s ended: %s", session_id, exc)
            lost = ErrorEvent(
                message=(
                    "The connection to the job stream was lost; reconnect to keep receiving "
                    f"results (session {session_id})."
                ),
                code="storage_unavailable",
                retryable=True,
            )
            yield {"event": lost.type, "data": lost.model_dump_json()}

    handed_off = False
    try:
        response = _SlotBoundEventStream(
            _events(),
            release=_release_stream_slot,
            ping=settings.service_sse_ping_seconds,
        )
        handed_off = True
        return response
    finally:
        # Mirrors the turn route: any BaseException before the response is handed off must
        # return the slot, or the user's stream budget leaks toward a permanent 429. The two
        # sites are mutually exclusive — once handed off, only the response releases.
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
