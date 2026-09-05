"""The push-back mailbox's two readers: the job stream, and the standing-query digest.

`session_events` is one durable mailbox with two kinds of addressee, and this module is where both
are read from. `GET /sessions/{id}/events` streams a finished durable job back into the chat that
launched it (F3-T3). `GET /digests` claims the standing-query digests
(`durable/digest.py`) the daily job left for the caller. They sit together because they claim the
same table through the same kind-scoped claim, and apart in every other respect — one is an
unbounded SSE stream over a session the caller owns, the other a single read of a mailbox addressed
by the caller's own identity:

| | `/sessions/{id}/events` | `/digests` |
| --- | --- | --- |
| addressed by | a session id in the path | the authenticated `oid`, never the request |
| authorized by | `resolve_session` (stored owner) | nothing to authorize — no id is accepted |
| shape | SSE, polls for the session's life | one claim, one JSON body |

**Why the digest read is not a second SSE stream**, since it would have reused every bound here:
the digest job's cadence is `digest_schedule_minutes` (a day by default), so a stream would hold a
per-user slot and a poller on the loop for hours to carry one row; and a digest is not a turn event,
so streaming it would want a member in the turn contract (`api/events.py`) that no turn ever emits.

Beside the job route stay the two closures that are nested in it on purpose:
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
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse, SendTimeoutError
from starlette.types import Receive, Scope, Send

from chemclaw.agent.session_events import claim_unconsumed
from chemclaw.api import app as front_door
from chemclaw.api.deps import CurrentUser, resolve_session
from chemclaw.api.events import (
    AwaitingAnswerEvent,
    ErrorEvent,
    JobCompletedEvent,
    JobFailedEvent,
)
from chemclaw.api.state import state
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from chemclaw.durable.awaiting import AWAITING_KIND
from chemclaw.durable.digest import DIGEST_KIND, digest_channel

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

    **It is unbounded in *time*, and it was also unbounded in one *send*, which is a different
    thing and was an oversight.** `EventSourceResponse` defaults `send_timeout` to `None`
    (verified against sse-starlette 3.4.8), so a half-open connection — a laptop closed mid-poll,
    a proxy that stopped reading without closing — parked this generator on a write that would
    never complete, holding one of the user's five slots and one poller on the loop with no
    deadline of any kind above it. The turn stream has passed one since D-159 for exactly this
    reason (`routes/turns._TurnStream`); this one had the identical hole and none of the turn
    stream's other bounds to catch it, since a push-back stream has no wall clock at all.
    """

    def __init__(
        self,
        content: AsyncIterator[dict[str, str]],
        *,
        release: Callable[[], None],
        ping: int,
        send_timeout: float,
        session_id: str,
    ) -> None:
        """Wrap `content`, bounding each send and releasing the slot when the response ends."""
        super().__init__(content, ping=ping, send_timeout=send_timeout)
        self._release = release
        self._session_id = session_id

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the stream, returning the admission slot however it ends.

        `SendTimeoutError` is caught here rather than allowed to escape, mirroring `_TurnStream`:
        letting it out would trade a parked generator for an unhandled-ASGI traceback, and here is
        where the session id is still in scope to name. The slot is returned either way — that is
        what the `finally` below has always been for.

        **Counted as well as logged, on its own series.** `_TurnStream` has
        `chemclaw_turn_send_timeouts_total` beside its identical log line, and that declaration's
        own comment forbids the obvious shortcut: reusing it here would put two populations —
        turns cut mid-answer and push-back streams cut mid-poll — under one denominator nobody can
        interpret. So this has `chemclaw_event_stream_send_timeouts_total` instead, which is the
        counter that makes "clients keep dropping off the push-back channel" a rate an operator can
        watch rather than a log line somebody has to think to grep for.
        """
        try:
            await super().__call__(scope, receive, send)
        except SendTimeoutError:
            METRICS.increment("chemclaw_event_stream_send_timeouts_total")
            logger.warning(
                "the push-back client of session %s stopped reading for %ss; the stream was closed",
                self._session_id,
                settings.service_sse_send_timeout_seconds,
            )
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
                session_id, kinds=("job_completed", "job_failed", AWAITING_KIND)
            ):
                # **A question the agent is waiting on is news on this channel too.**
                # `AwaitAnswerWorkflow._push` has always written this row; nothing ever claimed it,
                # because the tuple above named two kinds and this is a third. So the notification
                # was written, never delivered, and aged out under retention — and the only thing a
                # chemist saw was the `job_started` recorded beside it, of a kind no surface knows,
                # which reads as a durable job that runs for seven days and then expires.
                #
                # Widening the claim steals from nobody: `AWAITING_KIND` had **no consumer at all**,
                # and the claim is kind-scoped precisely so a selective consumer leaves other kinds
                # for theirs. Named from `durable.awaiting` rather than written out again here — a
                # second spelling of a wire constant is the drift this route would not notice.
                if pushed.kind == AWAITING_KIND:
                    yield _awaiting_event(pushed.payload)
                    continue
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
            send_timeout=settings.service_sse_send_timeout_seconds,
            session_id=session_id,
        )
        handed_off = True
        return response
    finally:
        # Mirrors the turn route: any BaseException before the response is handed off must
        # return the slot, or the user's stream budget leaks toward a permanent 429. The two
        # sites are mutually exclusive — once handed off, only the response releases.
        if not handed_off:
            _release_stream_slot()


class Digest(BaseModel):
    """One standing query's new matches, as the digest job left them in the caller's mailbox."""

    query: str = ""
    note_ids: list[str] = Field(default_factory=list)


def _awaiting_event(payload: dict[str, Any]) -> dict[str, str]:
    """Read one claimed `awaiting-answer` row into the SSE frame the contract declares.

    Lenient in the same way `_digest` is, and for a stronger version of the same reason: the row is
    already claimed by the time this runs, so there is no re-delivery. A payload that failed
    validation would take the notification with it, and the notification is the whole point — the
    request itself is still open, still in `GET /pending`, and still on its deadline.

    The two pushes carry different fields. The open (and every reminder) sends `kind`, `asked_of`
    and `due_at`; the expiry sends `subject`, `state` and `reminders` and nothing else. `.get` with
    the model's own defaults is what lets one reader take both without asking which it is.
    """
    event = AwaitingAnswerEvent(
        request_id=str(payload.get("request_id", "")),
        state=str(payload.get("state", "waiting")),
        subject=str(payload.get("subject", "")),
        kind=str(payload.get("kind", "")),
        asked_of=str(payload.get("asked_of", "")),
        due_at=str(payload.get("due_at", "")),
        # `int()` on whatever arrived rather than a cast: a reminder count is a number in every
        # payload this workflow writes, and a string there is a row from a build that is not this
        # one — which is not a reason to lose the notification.
        reminders=int(payload.get("reminders", 0) or 0),
    )
    return {"event": event.type, "data": event.model_dump_json()}


def _digest(payload: dict[str, Any]) -> Digest:
    """Read one claimed mailbox row, tolerating a payload an older job wrote.

    Deliberately lenient, and the claim is the reason: by the time this runs the row is already
    marked consumed and the subscription's watermark is long past the notes it names, so a payload
    that failed validation would take the digest with it and there would be nothing to re-deliver.
    A missing key costs one blank field; a raised `ValidationError` costs the whole digest.
    """
    note_ids = payload.get("note_ids")
    return Digest(
        query=str(payload.get("query", "")),
        note_ids=[str(note_id) for note_id in note_ids] if isinstance(note_ids, list) else [],
    )


async def read_digests(principal: CurrentUser) -> list[Digest]:
    """Claim and return the standing-query digests waiting for the caller (gap IDEA-1).

    **The caller cannot name a mailbox, so there is nothing to authorize.** `digest-<owner>` is a
    synthetic session id no `session_owners` row backs, so `resolve_session` — the gate every
    session-scoped route uses — cannot decide it, and gating a path segment against the principal
    would be a check that has to be got right rather than one that cannot be got wrong. This route
    therefore takes no id at all: it derives the channel from the authenticated principal with the
    writer's own `digest_channel`, exactly as `GET /sessions` scopes its listing to
    `principal.oid`. One chemist reading another's digest would take a forged token, not a crafted
    request.

    **The read is the consume**, scoped to `DIGEST_KIND`. The mailbox claim is destructive by
    design (COR-4) — claiming everything and filtering afterwards would silently destroy the job
    push-back rows of whatever channel it ran against — and consuming is what lets
    `durable/retention.py` age the row out, since its predicate is `consumed_at IS NOT NULL`. The
    residual window is the one every consumer of this mailbox has: a row claimed here whose
    response never reaches the client is not re-delivered. That is the at-most-once contract the
    channel documents, and its cost is bounded to the *notification* — the notes it names are
    merged knowledge, and the query that found them is saved, so `list_watches` plus a search
    re-finds them. Losing the notification is not losing the knowledge.

    **The answer is deliberately unbounded**, and a page here would be worse than none: the claim
    has already run by the time a slice could be taken, so dropping the tail would destroy it
    rather than defer it. The bound is upstream and is the cadence — one row per subscription per
    `digest_schedule_minutes` (a day), for as long as the owner has not read them.

    Nothing is counted here on purpose: `session_events.consumed_at` already records, per row,
    whether a digest was read, which is the same evidence the runbook has operators read for the
    eval-drift channel — and a counter would need a declaration in `core/metrics.py` to say less.
    """
    claimed = await claim_unconsumed(digest_channel(principal.oid), kinds=(DIGEST_KIND,))
    return [_digest(event.payload) for event in claimed]


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
    # No `dependencies=[Depends(resolve_session)]`, and that absence is the authorization model
    # rather than a gap in it: this route accepts no session id to resolve. See `read_digests`.
    app.get("/digests")(read_digests)
