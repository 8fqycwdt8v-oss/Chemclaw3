"""The SSE turn stream — the one route with real concurrency machinery, kept in one place.

`POST /sessions/{id}/messages` runs a turn under four guards that must compose exactly: the
per-session in-process lease and the durable cross-process claim (both 409), the admission
semaphore (queued/shed on the open stream, D-166), and the budget (429). The `_turn_events`
generator stays **nested in the route on purpose**: everything it captures — the turn's session,
body, principal, lease bookkeeping — is per-request state that exists nowhere but this request's
frame, so hoisting it would mean re-threading eight arguments to move code that has exactly one
caller. The app-wide structures it touches are read through `chemclaw.api.state.state(request)`
at request time, which is the seam that let this route leave `create_app` unchanged (R3.2).
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from sse_starlette.sse import EventSourceResponse, SendTimeoutError
from starlette.types import Receive, Scope, Send

from chemclaw.api.budget import BudgetExceeded
from chemclaw.api.deps import CurrentSession, CurrentUser
from chemclaw.api.detach import DetachableTurn
from chemclaw.api.events import ErrorEvent, QueuedEvent
from chemclaw.api.middleware import _AT_CAPACITY
from chemclaw.api.runner import failure_event, run_turn
from chemclaw.api.schemas import MessageIn, session_title
from chemclaw.api.state import (
    _WORKER_ID,
    SessionTurns,
    TurnLease,
    _claim_turn_slot,
    _hold_turn_claim,
    _release_turn_claim,
    _release_turn_slot,
    _start_turn_lease,
    state,
)
from chemclaw.core.config import settings
from chemclaw.core.identity_context import get_current_correlation_id
from chemclaw.core.metrics import METRICS

logger = logging.getLogger(__name__)


class _TurnStream(EventSourceResponse):
    """A turn stream that ends *itself* when the client stops reading, rather than being collected.

    `asyncio.timeout` inside the generator bounds a stalled **model**: the cancellation lands in the
    frame that entered the scope, becomes a `TimeoutError`, and the turn gets one error event. It
    cannot bound a stalled **transport**. When `await send(...)` blocks on a client that has stopped
    reading, the generator is parked at a `yield` and the cancellation lands in
    `sse_starlette._stream_response` instead: `asyncio.timeout.__aexit__` never runs, no event can
    be written (nobody is reading), and sse-starlette does not `aclose()` the body iterator on that
    path — so the permit, the lease and the token booking were left to asyncio's async-generator GC
    finalizer, which runs the teardown in a *different* `Context` (see `runner._turn_ambient`).

    `send_timeout` is the bound sse-starlette answers by calling `aclose()` **in the task that was
    serving the stream**, which is the one place the turn's teardown belongs: the same context that
    stamped the ambients, promptly rather than whenever the collector runs. What it then raises is
    `SendTimeoutError`, and letting that escape would trade a GC traceback for an unhandled-ASGI
    one — so it is caught here, where the session id is still in scope to name in the log. This is
    the same "the response's lifetime is the right scope" argument `_SlotBoundEventStream` makes in
    `chemclaw.api.routes.streams`.
    """

    def __init__(
        self,
        content: AsyncIterator[dict[str, str]],
        *,
        session_id: str,
        ping: int,
        send_timeout: float,
    ) -> None:
        """Wrap `content`, bounding each send and remembering whose turn this is."""
        super().__init__(content, ping=ping, send_timeout=send_timeout)
        self._session_id = session_id

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve the stream; a client that stopped reading ends it quietly, not as a crash."""
        try:
            await super().__call__(scope, receive, send)
        except SendTimeoutError:
            METRICS.increment("chemclaw_turn_send_timeouts_total")
            logger.warning(
                "the client of session %s stopped reading for %ss; the stream was closed and "
                "the turn detached",
                self._session_id,
                settings.service_sse_send_timeout_seconds,
            )


async def post_message(
    request: Request,
    session_id: str,
    body: MessageIn,
    principal: CurrentUser,
    live: CurrentSession,
) -> EventSourceResponse:
    """Run one turn for the session and stream its events as SSE.

    Admission-controlled (AG-15): the turn takes one of the process's turn permits for its
    whole streamed run, so a burst of concurrent turns cannot pile onto the shared internal
    LLM endpoint. That permit is taken **inside the stream** (D-166): a turn that has to wait
    reports the wait as a `queued` event and, if no permit frees within the admission timeout,
    ends with an error event on an open stream rather than an HTTP 503. The wait was
    previously invisible — up to `service_turn_admission_timeout_seconds` with no response at
    all — which is the one thing a busy front door and a dead one must not have in common.
    The permit hold is wall-clock bounded twice, because one bound cannot cover both stalls.
    `service_turn_timeout_seconds` bounds the turn: a hung model stream ends with one
    `turn_timeout` error event on the open stream and the permit released.
    `service_sse_send_timeout_seconds` bounds one *send*: a client that has stopped reading gets
    no event — it is not reading, so there is nowhere to put one — and its stream is closed in the
    task that was serving it, which is what returns the permit, the lease and the token booking
    promptly instead of leaving them to a garbage collector (`_TurnStream`).

    **One turn at a time per session**, claimed twice, and both claims are *leases*. The
    in-process `active_turns` map answers a double-submit that lands on this same process
    with no I/O and no race window (`_claim_turn_slot`: no `await` between the test and the
    write, and an entry expires rather than outliving a turn whose teardown never ran). The
    durable claim in `session_turns` answers the case that map cannot see: the shipped chart
    runs two front-door replicas, so the second POST may arrive at a different process
    entirely, and both would otherwise be admitted and interleave their messages into one
    conversation thread. Both answer 409. The durable half is present only under
    `session_store="postgres"` — with the in-memory store there is no shared history for two
    processes to corrupt.
    """
    front = state(request)
    active_turns: dict[str, TurnLease] = front.active_turns
    claims: SessionTurns | None = front.turn_claims
    lease = settings.service_turn_claim_lease_seconds
    semaphore = front.turn_semaphore
    # Nothing may sit between this claim and the `try` below — no `await`, and nothing that can
    # raise — because the reservation it takes does not expire until `_start_turn_lease` starts its
    # clock, and until then only that `try`'s `finally` gives it back.
    slot = _claim_turn_slot(active_turns, session_id)
    if slot is None:
        METRICS.increment("chemclaw_turns_conflict_total", labels={"scope": "process"})
        raise HTTPException(status_code=409, detail="a turn is already running for this session")

    # **The id the header, the audit trail and `turn_costs` are all keyed on.** Read once, here,
    # rather than in the generator: the observability middleware minted it for this request and
    # stamped it as an ambient, and the generator runs in this request's context, so both resolve
    # to the same string — but reading it at the top is what makes that a fact of the code rather
    # than of the runtime. Every `ErrorEvent` this module builds carries it, because
    # `ErrorEvent.correlation_id` is the join key an operator is asked to quote and three of the
    # four events built here used to default it to `""` while the answer sat on the response
    # header. `run_turn`'s own events already carry the same id through `ledger.correlation_id`.
    correlation_id = get_current_correlation_id() or ""

    async def _turn_events() -> AsyncIterator[dict[str, str]]:
        # Release the permit and the session's turn slot when the stream ends — normal
        # completion, error, timeout, or client disconnect (the generator is closed, running
        # this finally) — so neither is ever leaked.
        heartbeat = (
            None
            if claims is None
            else asyncio.create_task(_hold_turn_claim(claims, session_id, lease))
        )
        permit = False
        # **The turn, not its error events** (M7). This used to be one increment per `error` event
        # inside the loop below, and `runner.py` can yield *two* for one turn: the loop cap and the
        # empty answer are independent predicates and a runaway turn satisfies both — so
        # `chemclaw_turns_failed_total / chemclaw_turns_started_total`, which reads as a failure
        # *rate*, could exceed 1.0. A flag plus one increment in the `finally` counts each turn
        # once, and the `finally` is also what makes the timeout branch below count at all: it is
        # outside the `async for`, so a timed-out turn moved this counter zero times and an
        # all-timeout deployment showed a **zero** failure ratio (M8).
        turn_failed = False
        try:
            # Admission, inside the stream (D-166). `locked()` is the whole reason the common
            # case costs nothing: it is false exactly when `acquire()` will return without
            # suspending, and there is no await between the test and the acquire for another
            # turn to slip through, so an uncontended turn takes its permit and emits no
            # `queued` event at all.
            if semaphore.locked():
                METRICS.increment("chemclaw_turns_queued_total")
                queued_event = QueuedEvent()
                yield {"event": queued_event.type, "data": queued_event.model_dump_json()}
                try:
                    await asyncio.wait_for(
                        semaphore.acquire(),
                        timeout=settings.service_turn_admission_timeout_seconds,
                    )
                except TimeoutError:
                    # Shedding is the admission control working as designed — and was
                    # completely invisible from outside until this counter existed.
                    METRICS.increment("chemclaw_turns_shed_total")
                    # Retryable and honestly so: shedding says "not now", not "not ever",
                    # and it is the one failure where trying again shortly is exactly right.
                    # **`at_capacity`, not `budget_exhausted`.** Both used to be the second,
                    # with opposite `retryable` values — two populations with opposite remedies
                    # under one code, on a taxonomy whose whole contract is that each member is a
                    # different thing for the user to do. A surface switching on `code` could not
                    # tell "we are busy, retry in a moment" from "your budget is gone, stop
                    # retrying". `_AT_CAPACITY` was already the one literal for this condition;
                    # now the code names the same thing the wording does.
                    shed = ErrorEvent(
                        message=_AT_CAPACITY,
                        code="at_capacity",
                        retryable=True,
                        correlation_id=correlation_id,
                    )
                    yield {"event": shed.type, "data": shed.model_dump_json()}
                    return
            else:
                await semaphore.acquire()
            permit = True
            # The budget, again — and this is the check that binds. The one before the response
            # was handed off runs at *request entry*, so every turn in a concurrent burst passes
            # it before any of them has recorded a thing: measured with production-shaped values
            # (8 permits, 40 concurrent POSTs, a 1-turn cap) as 40 answers and 40,000 tokens
            # booked, against a documented overshoot bound of 8. Re-checking here is what makes
            # that bound true, because a turn holding a permit is one of at most
            # `service_max_concurrent_turns`, and every turn that finished ahead of it has
            # already been booked by `record`.
            #
            # An event rather than a status code (D-166): the response is open by now, and the
            # shed branch above answers the same way for the same reason. Not retryable — the
            # budget is spent, so the next attempt fails identically until an operator raises the
            # cap or the counters reset.
            try:
                front.budget.check(session_id, principal.oid)
            except BudgetExceeded as exc:
                METRICS.increment("chemclaw_turns_refused_budget_total")
                refused = ErrorEvent(
                    message=str(exc),
                    code="budget_exhausted",
                    retryable=False,
                    correlation_id=correlation_id,
                )
                yield {"event": refused.type, "data": refused.model_dump_json()}
                return
            METRICS.increment("chemclaw_turns_started_total")
            try:
                # The deadline covers the whole streamed run (AG-15's missing wall-clock half).
                # A stall inside `run_turn` is cancelled in the frame that entered this scope, so
                # it surfaces here as `TimeoutError` and becomes one user-safe error event.
                # **It does not bound the transport**, which used to be claimed here: the
                # generator is suspended at a `yield` while the send blocks, so the cancellation
                # lands in sse-starlette instead and this `__aexit__` never runs. That half is
                # `_TurnStream`'s `send_timeout`, which ends such a stream in the task serving it
                # — and it is what makes this `finally` run at all in that case.
                # There is no agent lease here any more, and its absence is the point of D-123
                # rather than a regression against it. Two turns streaming through one shared
                # chat client interleaved its tool-call bookkeeping and emitted a `tool_use`
                # block with an empty name — 20% of turns in a live 50-user run — which is why a
                # pooled agent had to be leased exclusively. A graph is compiled per turn around
                # that turn's own connectors, so there is no shared object to lease: the defect
                # has no surface left to occur on.
                async with asyncio.timeout(settings.service_turn_timeout_seconds) as deadline:
                    async for event in run_turn(
                        live.session,
                        body.message,
                        actor=principal.oid,
                        roles=principal.roles,
                        budget=front.budget,
                        dry_run=body.dry_run,
                        # The session's profile picks both halves of its surface: the graph the
                        # chemist talks to and the connectors that graph gets. Selecting one
                        # without the other would advertise a narrowed toolset over the full
                        # connector set.
                        connectors=front.connector_factory(live.profile),
                        history=front.history,
                        profile=live.profile,
                        graph_factory=front.graph_factory,
                        # The reading this scope will fire at, so the turn's own cost row can say
                        # `timed_out` rather than `abandoned`. The cancellation is indistinguishable
                        # from a Stop inside `run_turn`, and this route learns which it was only in
                        # the `except TimeoutError` below — which runs *after* the turn has booked
                        # itself. See `run_turn`'s `deadline` argument.
                        deadline=deadline.when(),
                    ):
                        if event.type == "error":
                            turn_failed = True
                        yield {"event": event.type, "data": event.model_dump_json()}
            except TimeoutError:
                turn_failed = True
                METRICS.increment("chemclaw_turn_timeouts_total")
                logger.warning(
                    "turn timed out after %ss for session %s",
                    settings.service_turn_timeout_seconds,
                    session_id,
                )
                timeout_event = ErrorEvent(
                    message=(
                        "The turn exceeded the "
                        f"{settings.service_turn_timeout_seconds:g}s time limit and was "
                        f"cancelled (session {session_id})."
                    ),
                    code="turn_timeout",
                    # Not retryable unchanged: the same question will take the same time. The
                    # useful next step is a narrower question, not another wait.
                    retryable=False,
                    correlation_id=correlation_id,
                )
                yield {"event": timeout_event.type, "data": timeout_event.model_dump_json()}
        except Exception as exc:
            # **The stream's own catch-all, and it covers what `run_turn`'s cannot.** `run_turn`
            # turns any `Exception` into one user-safe `ErrorEvent`, but that guard starts inside
            # it — while everything this route *evaluates to call it* (`front.connector_factory`,
            # `front.history`, `front.graph_factory`) and `run_turn`'s own pre-`try` statements run
            # one frame above it. A failure there used to end the stream with an HTTP 200, an SSE
            # content-type and zero events, with the exception escaping the ASGI app: by then
            # `EventSourceResponse` has written `http.response.start`, so Starlette's
            # `ExceptionMiddleware` cannot run a handler any more. The reachable trigger is an
            # ordinary configuration change — a session whose stored profile the deployment no
            # longer ships rehydrates unvalidated (deliberately, REV-14) and `connector_factory`
            # raises `ValueError` on every turn, forever, silently.
            #
            # So the invariant `events.py` states — a stream ends with an answer or an error — is
            # the *stream's*, not only `run_turn`'s. `failure_event` is the same classifier the
            # runner uses, so a client cannot get two different accounts of one kind of failure.
            turn_failed = True
            logger.exception("turn stream failed for session %s", session_id)
            failed = failure_event(exc, session_id, correlation_id or uuid.uuid4().hex)
            yield {"event": failed.type, "data": failed.model_dump_json()}
        finally:
            if turn_failed:
                METRICS.increment("chemclaw_turns_failed_total")
            if heartbeat is not None:
                heartbeat.cancel()
            if permit:
                semaphore.release()
            _release_turn_slot(active_turns, session_id, slot)
            if claims is not None:
                await _release_turn_claim(claims, session_id)

    claimed = False
    handed_off = False
    try:
        # Name the session after the message that opened it, so `GET /sessions` can render a
        # conversation list rather than a column of ids. Here rather than in the history provider
        # because here the message is still a plain string — the provider stores an opaque payload
        # it is not allowed to interpret. After the turn claim, so a rejected double-submit does
        # not write; before the stream, so a turn that fails mid-answer still leaves the
        # conversation named. `set_title_if_absent` is a no-op once there is a title, which is
        # every turn after the first.
        #
        # **Inside this `try`, which is where it belongs and is now load-bearing.** It is a store
        # round trip: it can raise (a failed checkout is shed 503) and it can be cancelled, and
        # from outside the block neither path gave the session's slot back — a leak the old
        # claim-time deadline merely time-boxed and the reservation would hold for good.
        if front.session_owners is not None:
            await front.session_owners.set_title_if_absent(session_id, session_title(body.message))
        # Runaway-cost guard (budget #3), first pass: refuse before taking a permit if this
        # session/user has *already* exhausted its budget — a clean 429, not a queued turn that
        # was never going to run. It is a fast path, not the guard: the binding check is the one
        # inside the stream, after the permit (see there for the measurement).
        try:
            front.budget.check(session_id, principal.oid)
        except BudgetExceeded as exc:
            METRICS.increment("chemclaw_turns_refused_budget_total")
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        # Claimed here rather than inside the stream because it is a *refusal*, not a wait:
        # a turn already running elsewhere must be told 409 by a status code, which only
        # exists before the response is handed off. A failed checkout raises `ConnectionError`
        # and is shed as a 503 by `_database_unavailable` — the guard fails closed, retryably.
        if claims is not None and not await claims.claim(session_id, _WORKER_ID, lease):
            METRICS.increment("chemclaw_turns_conflict_total", labels={"scope": "durable"})
            raise HTTPException(
                status_code=409, detail="a turn is already running for this session"
            )
        claimed = claims is not None
        # The lease clock starts *here*, not at the claim: from the next statement on, the
        # `finally` below no longer owns the cleanup and the slot needs an expiry of its own
        # (see `_start_turn_lease`).
        _start_turn_lease(active_turns, session_id, slot)
        # The turn runs on a pump task of its own from this moment
        # (`D-2026-08-27-a-disconnect-is-a-detach-not-a-stop`): the SSE response is a *view* of
        # it, so a client disconnect detaches the view and the turn runs to completion — its
        # answer lands in the transcript, its teardown releases the permit, the lease and the
        # claim at the turn's true end. Stopping is the explicit route below, which cancels the
        # pump and delivers the same `CancelledError` a disconnect used to.
        turn = DetachableTurn(
            _turn_events(),
            session_id=session_id,
            survive_disconnect=settings.service_turn_survives_disconnect,
        )
        front.running_turns.register(session_id, turn)
        response = _TurnStream(
            turn.events(),
            session_id=session_id,
            ping=settings.service_sse_ping_seconds,
            send_timeout=settings.service_sse_send_timeout_seconds,
        )
        handed_off = True
        return response
    finally:
        # try/finally, not `except Exception`: cancellation (a client gone mid-admission) is
        # a BaseException, and missing it here leaked the session's active-turns entry —
        # 409-bricking the session until restart. Until the streaming response is handed
        # off, this owns the cleanup; afterwards the generator's own finally does — except
        # for the one window neither covers (handed off, never advanced), which the lease
        # in `_claim_turn_slot` bounds instead.
        if not handed_off:
            _release_turn_slot(active_turns, session_id, slot)
            if claimed and claims is not None:
                await _release_turn_claim(claims, session_id)


async def stop_turn(
    request: Request,
    session_id: str,
    principal: CurrentUser,
    live: CurrentSession,
) -> dict[str, bool]:
    """Stop the session's running turn — the explicit act a disconnect no longer performs.

    Closing the SSE stream used to be how a turn was stopped, which made the Stop button and a
    network blip the same event; now the stream only *detaches*
    (`D-2026-08-27-a-disconnect-is-a-detach-not-a-stop`) and this is the one way to cancel work
    in flight. Guarded by the same session-ownership dependency as the turn route itself, so
    stopping a turn requires exactly the standing that starting one does.

    404 when no turn is running rather than a silent 200: "there was nothing to stop" and
    "stopped" are different facts, and a client that raced the turn's own completion should know
    which happened. Only this process's turns are stoppable — the pump lives here — so on a
    multi-replica deployment the client calls the same origin its stream was on, which it always
    does, because the stream *is* how it knows a turn is running.
    """
    turn = state(request).running_turns.get(session_id)
    if turn is None:
        raise HTTPException(status_code=404, detail="no turn is running for this session")
    await turn.stop()
    METRICS.increment("chemclaw_turns_stopped_total")
    logger.info("session %s's turn was stopped by request", session_id)
    return {"stopped": True}


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
    app.post("/sessions/{session_id}/messages")(post_message)
    app.post("/sessions/{session_id}/turn/stop")(stop_turn)
