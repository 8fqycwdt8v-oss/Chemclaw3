"""The SSE turn stream — the one route with real concurrency machinery, kept in one place.

`POST /sessions/{id}/messages` runs a turn under guards that must compose exactly: the per-session
in-process lease and durable cross-process claim (a busy session is a line; only a full line is a
409), the per-actor concurrent-turn cap (429), the admission semaphore (queued or shed on the open
stream), and the budget (429). The rate limiter runs earlier, inside `require_principal`. The
`_turn_events` generator stays nested in the route because everything it captures is per-request
state; app-wide structures are read through `chemclaw.api.state.state(request)`.

Beside it: the stop route, the session's line (`GET /sessions/{id}/queue`, and `DELETE …/{ticket}`
to withdraw a waiting message), and `GET /sessions/{id}/turn/stream` to follow the running turn
live.
"""

import asyncio
import contextlib
import logging
import math
import random
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from typing import Any, Literal

import psycopg
from fastapi import FastAPI, HTTPException, Request
from sse_starlette.sse import EventSourceResponse, SendTimeoutError
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from chemclaw.agent.exhibit_notes import resolve_exhibit_refs
from chemclaw.agent.session_queue import QueueRefused, Refusal, TurnQueue
from chemclaw.agent.turn_remotes import Holding
from chemclaw.api.auth import (
    DEV_PRINCIPAL_OID,
    AuthError,
    IdentityProviderUnavailable,
    Principal,
    reauthorize,
)
from chemclaw.api.budget import BudgetExceeded, check_thread_size, refused_metric
from chemclaw.api.deps import CurrentSession, CurrentUser, _resolve_session, require_owner
from chemclaw.api.detach import DetachableTurn
from chemclaw.api.events import TURN_EVENT_REF, ErrorEvent, QueuedEvent, sse_frame
from chemclaw.api.middleware import AT_CAPACITY
from chemclaw.api.runner import (
    failure_event,
    run_turn,
    settle_interrupted_turns,
    transcript_settled,
)
from chemclaw.api.schemas import MessageIn, QueuedMessageOut, SessionQueueOut, session_title
from chemclaw.api.state import (
    FrontDoorState,
    LiveSession,
    SessionTurns,
    TurnLease,
    _actor_turns_in_flight,
    _claim_turn_slot,
    _hold_turn_claim,
    _release_turn_claim,
    _release_turn_slot,
    _start_turn_lease,
    _take_event_stream_slot,
    _waiting_besides,
    claim_holder,
    state,
)
from chemclaw.core.config import settings
from chemclaw.core.identity_context import get_current_correlation_id
from chemclaw.core.metrics import METRICS
from chemclaw.exhibits.models import UnknownExhibit

logger = logging.getLogger(__name__)

# : On a watch response: the correlation id of the turn being watched (its sender's own
# : `POST …/messages` id), distinct from the watch request's `X-Chemclaw-Correlation-Id`.
TURN_CORRELATION_HEADER = "X-Chemclaw-Turn-Correlation-Id"


class _TurnStream(EventSourceResponse):
    """A turn stream that ends *itself* when the client stops reading, rather than being collected.

    `asyncio.timeout` in the generator bounds a stalled model but not a stalled transport: when
    `send` blocks on a client that stopped reading, the cancellation lands in sse-starlette, which
    does not `aclose()` the body, leaving teardown to the async-generator GC finalizer in a
    different `Context`. `send_timeout` makes sse-starlette `aclose()` the generator in the serving
    task, so the permit, lease and token booking are released promptly in the right context. The
    resulting `SendTimeoutError` is caught and logged here with the session id.
    """

    def __init__(
        self,
        content: AsyncIterator[dict[str, str]],
        *,
        session_id: str,
        ping: int,
        send_timeout: float,
        release: Callable[[], None] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Wrap `content`, bounding each send and remembering whose turn this is.

        `release`, when given, runs however the response ends — including a client gone before the
        first byte, when the body generator never starts. A watcher's place and stream slot are held
        by the socket, so they are returned here.
        """
        super().__init__(content, ping=ping, send_timeout=send_timeout, headers=headers)
        self._session_id = session_id
        self._release = release

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
        finally:
            if self._release is not None:
                self._release()


def _retry_after_hint() -> str:
    """Seconds to suggest before a refused caller tries again — a cadence, with jitter.

    The base is `service_turn_admission_timeout_seconds`, the existing answer to how long waiting
    for a permit is reasonable. Jitter of up to one interval keeps refused clients from
    re-converging on one cadence. At least 1, because `Retry-After: 0` means retry immediately.
    """
    base = settings.service_turn_admission_timeout_seconds
    return str(max(1, math.ceil(base + random.random() * base)))


def _invalid_exhibit_ref(message: str) -> dict[str, str]:
    """The 422 detail for an `exhibit_refs` this turn cannot resolve: a `code` and the sentence.

    The code is what a surface acts on, so it never has to match the sentence.
    """
    return {"code": "invalid_exhibit_ref", "message": message}


def _queue_refusal(reason: Refusal) -> dict[str, str]:
    """The 409 detail for a line that cannot take this message: a `code` and the sentence.

    A client acts differently on a full line (`queue_full` — wait and send again) and on a sender
    who already has a message waiting (`already_waiting` — withdraw it or wait), so the code
    distinguishes them.
    """
    if reason == "waiting":
        return {
            "code": "already_waiting",
            "message": "you already have a message waiting in this session; withdraw it or "
            "wait for it to run",
        }
    return {
        "code": "queue_full",
        "message": "a turn is already running for this session and "
        f"{settings.service_turn_queue_max} message(s) are already waiting",
    }


async def post_message(
    request: Request,
    session_id: str,
    body: MessageIn,
    principal: CurrentUser,
    live: CurrentSession,
) -> EventSourceResponse:
    """Run one turn for the session and stream its events as SSE.

    Admission-controlled: the turn holds one of the process's turn permits for its whole run. The
    permit is taken inside the stream, so a wait is reported as a `queued` event and a timeout ends
    with an error event rather than an HTTP 503. Two wall clocks bound it:
    `service_turn_timeout_seconds` bounds the turn (one `turn_timeout` error event), and
    `service_sse_send_timeout_seconds` bounds one send to a client that stopped reading
    (`_TurnStream`).

    One turn at a time per session, claimed twice as leases: the in-process `active_turns` map (no
    I/O, no race window) and, under `session_store="postgres"`, the durable `session_turns` claim
    that covers other replicas.

    A busy session is a line, not a 409: the message takes a ticket (`agent/session_queue`), this
    request waits and reports its place, and at the head it takes both claims and runs as this
    request's principal. Tickets run in database admission order. Still refused: a full line or a
    sender already waiting (409, with `code`), and a process at its waiter budget (429). A message
    that waited is re-authorized at the head (`_refusal_at_the_head`).
    """
    front = state(request)
    active_turns: dict[str, TurnLease] = front.active_turns
    claims: SessionTurns | None = front.turn_claims
    lease = settings.service_turn_claim_lease_seconds
    semaphore = front.turn_semaphore
    # Artefact references are resolved before anything is claimed, so a refusal holds no slot, claim
    # or permit; an unresolvable one is a 422 with `code="invalid_exhibit_ref"`.
    #
    # Then the per-actor cap: the semaphore bounds the process and is actor-blind, so without this
    # one principal could hold every permit. It is a final refusal, so it gets a status code (429)
    # rather than a stream event. `Retry-After` is required because the UI reads a 429 without it as
    # an exhausted budget and locks the composer; the hint is a jittered check-back cadence, not an
    # estimate. It sits above `_claim_turn_slot`, whose reservation has no expiry until the lease
    # starts, so a raise here cannot leak a slot. Inert under the shared dev principal, where every
    # caller is one oid and there is nothing to divide. Messages waiting in other sessions' lines
    # count, since each will run as soon as its line moves.
    if body.exhibit_refs and not settings.agent_exhibits_enabled:
        # Refused rather than dropped: with artefacts off the reference would reach nobody.
        raise HTTPException(
            status_code=422,
            detail=_invalid_exhibit_ref(
                "artefacts are switched off in this deployment; send the message without "
                "exhibit_refs"
            ),
        )
    if body.exhibit_refs:
        try:
            await resolve_exhibit_refs(session_id, body.exhibit_refs)
        except UnknownExhibit as exc:
            raise HTTPException(status_code=422, detail=_invalid_exhibit_ref(str(exc))) from exc
    actor_cap = settings.service_max_concurrent_turns_per_actor
    front_waiters = front.queue_waiters
    held = (
        _actor_turns_in_flight(active_turns, principal.oid, besides=session_id)
        + _waiting_besides(front_waiters, principal.oid, besides=session_id)
        if actor_cap and principal.oid != DEV_PRINCIPAL_OID
        else 0
    )
    if actor_cap and held >= actor_cap:
        METRICS.increment("chemclaw_turns_refused_actor_cap_total")
        # The identity is logged at INFO and is never a metric label: `/metrics` is unauthenticated,
        # and a capped label set would stop counting during exactly the flood worth reading. Log the
        # measured `held`, not the cap: a count above the cap is the symptom of a lease that
        # outlived its turn.
        logger.info(
            "refusing a turn for %s: holding %d concurrent turn(s) against a per-actor cap of %d",
            principal.oid,
            held,
            actor_cap,
        )
        raise HTTPException(
            status_code=429,
            detail="too many concurrent turns for this user; wait for one to finish",
            headers={"Retry-After": _retry_after_hint()},
        )
    # A busy session is a line, not a refusal: a message that finds another turn running (here, on
    # another replica, or with somebody already waiting) joins the line, and the two claims below
    # still decide who runs. The fast path is taken only when nobody is waiting, so a message never
    # jumps an existing line.
    queue = front.turn_queue
    signal = front.queue_signal
    busy: str | None = "queue" if await queue.waiting(session_id) else None
    # Nothing may sit between this claim and the `try` below — no `await`, nothing that can raise:
    # the reservation does not expire until `_start_turn_lease`, and until then only that `finally`
    # gives it back.
    slot = None if busy else _claim_turn_slot(active_turns, session_id, actor=principal.oid)
    if busy is None and slot is None:
        busy = "process"
    # Whether this turn holds the durable claim, and under which identity (`claim_holder`, keyed by
    # the turn so a late teardown cannot revoke a successor's claim). Set on the fast path here, or
    # at dispatch for a message that waited.
    holder: str | None = None
    # This message's place in the line, while it has one.
    ticket: int | None = None
    # The live session the turn runs on, re-resolved at dispatch for a message that waited, since
    # the handle may have been evicted and rehydrated meanwhile.
    current = live
    # Registered as the session's running turn when it starts, not when created, so the stop route
    # and watchers never get a waiting message.
    started: DetachableTurn | None = None
    # Who the turn runs as: the request's principal, or for a message that waited, what the same
    # credential still establishes at the head.
    runner: Principal = principal
    # Whether this message holds a place in `front.queue_waiters`, reserved before the ticket is
    # taken so concurrent POSTs cannot both pass its bound.
    counted = False
    waiter_key = (principal.oid, session_id)

    # The id the header, the audit trail and `turn_costs` are keyed on, read once here so every
    # `ErrorEvent` this module builds carries this request's correlation id.
    correlation_id = get_current_correlation_id() or ""

    # Held out here because two endings release it: the turn's own `finally`, and the detach hook,
    # since a detached turn has no waiting client to be fair to. The flag makes release idempotent,
    # and nothing between test and release can suspend.
    permit = False

    def _release_permit() -> None:
        """Give the process's admission permit back, exactly once, whoever gets here first."""
        nonlocal permit
        if permit:
            permit = False
            semaphore.release()

    def _left_line() -> None:
        """This message no longer waits: drop its ticket and its process-ledger place, once."""
        nonlocal ticket, counted
        ticket = None
        if counted:
            counted = False
            remaining = front_waiters.get(waiter_key, 1) - 1
            if remaining <= 0:
                front_waiters.pop(waiter_key, None)
            else:
                front_waiters[waiter_key] = remaining

    def _withdrawn(message: str) -> dict[str, str]:
        """The frame a waiting message ends on when it will never run."""
        METRICS.increment("chemclaw_turn_queue_withdrawn_total")
        return sse_frame(
            ErrorEvent(
                message=message,
                code="queue_cancelled",
                retryable=False,
                correlation_id=correlation_id,
            )
        )

    async def _take_the_turn() -> bool:
        """At the head of the line: take both claims for this message, or report the turn busy.

        The same claims in the same order as the fast path, so a queued message cannot run beside a
        live turn.
        """
        nonlocal slot, holder, started
        taken = _claim_turn_slot(active_turns, session_id, actor=principal.oid)
        if taken is None:
            return False
        try:
            if claims is not None and not await claims.claim(
                session_id, claim_holder(taken), lease, actor=principal.oid
            ):
                _release_turn_slot(active_turns, session_id, taken)
                return False
        except BaseException:
            _release_turn_slot(active_turns, session_id, taken)
            raise
        slot = taken
        holder = claim_holder(taken) if claims is not None else None
        _start_turn_lease(active_turns, session_id, slot)
        # Leave the line as soon as the turn is ours, so the next message becomes the head.
        if ticket is not None:
            await _leave_line(queue, session_id, ticket)
            _left_line()
        signal.notify()
        if started is not None:
            front.running_turns.register(session_id, started)
        return True

    async def _refusal_at_the_head() -> str | None:
        """Why this message may no longer run as its sender, or `None` when it still may.

        Everything the POST was admitted on is asked again, because the wait can be long:

        - **membership** — via `_resolve_session`, which also returns a current live handle;
        - **the credential** — `auth.reauthorize` re-validates the token and its roles;
        - **the per-actor cap** — the sender's turns running elsewhere now.

        Cheapest refusal first; re-checked on every look at the head.
        """
        nonlocal current, runner
        try:
            current = await _resolve_session(request, session_id, principal)
        except HTTPException:
            return "Your message did not run: you are no longer a participant in this conversation."
        try:
            runner = await reauthorize(request, principal)
        except AuthError:
            logger.info("a waiting message in session %s outlived its sender's token", session_id)
            return (
                "Your message did not run: your sign-in expired while it waited. Sign in again "
                "and resend it."
            )
        except IdentityProviderUnavailable:
            logger.warning(
                "a waiting message in session %s could not have its sender's token re-checked",
                session_id,
            )
            return (
                "Your message did not run: your sign-in could not be re-checked when its turn "
                "came. Resend it."
            )
        # Read now: the check compares against the configuration in force when the turn would start.
        cap_now = settings.service_max_concurrent_turns_per_actor
        if (
            cap_now
            and principal.oid != DEV_PRINCIPAL_OID
            and _actor_turns_in_flight(active_turns, principal.oid, besides=session_id) >= cap_now
        ):
            METRICS.increment("chemclaw_turns_refused_actor_cap_total")
            return (
                "Your message did not run: you already have as many turns running as one person "
                "may. Resend it when one finishes."
            )
        return None

    async def _wait_in_line() -> AsyncIterator[dict[str, str]]:
        """Wait for this message's turn, reporting its place whenever the place changes.

        Ends with the turn taken (`slot` set), or with the message withdrawn — its ticket vanished
        or its sender is no longer a participant — and a final `queue_cancelled` frame. Authority is
        re-read at the head (`_refusal_at_the_head`).
        """
        shown: int | None = None
        while ticket is not None:
            place = await queue.position(session_id, ticket, lease)
            if place is None:
                _left_line()
                yield _withdrawn("Your message was withdrawn before it ran.")
                return
            if place == 0:
                refusal = await _refusal_at_the_head()
                if refusal is not None:
                    await _leave_line(queue, session_id, ticket)
                    _left_line()
                    signal.notify()
                    yield _withdrawn(refusal)
                    return
                if await _take_the_turn():
                    return
            if place != shown:
                shown = place
                yield sse_frame(QueuedEvent(ticket=ticket, position=place))
            await signal.wait(settings.service_turn_queue_poll_seconds)

    async def _turn_events() -> AsyncIterator[dict[str, str]]:
        # When the turn ends — completion, error, timeout or stop — release the turn slot, the
        # durable claim, the line place and (unless detach already did) the permit, so the session
        # stays claimed exactly while work is in flight.
        heartbeat: asyncio.Task[None] | None = None
        nonlocal permit
        # Counts the turn, not its error events: one turn can yield two errors, and the timeout
        # branch sits outside the loop. One increment in the `finally` keeps the failure ratio at
        # most 1.
        turn_failed = False
        try:
            async for frame in _wait_in_line():
                yield frame
            if slot is None:
                return  # withdrawn while it waited; `_wait_in_line` said so
            if holder is not None and claims is not None:
                heartbeat = asyncio.create_task(_hold_turn_claim(claims, session_id, lease, holder))
            # `locked()` is false exactly when `acquire()` will not suspend, and nothing can
            # interleave before the acquire, so an uncontended turn emits no `queued` event.
            if semaphore.locked():
                METRICS.increment("chemclaw_turns_queued_total")
                queued_event = QueuedEvent()
                yield sse_frame(queued_event)
                try:
                    await asyncio.wait_for(
                        semaphore.acquire(),
                        timeout=settings.service_turn_admission_timeout_seconds,
                    )
                except TimeoutError:
                    # Shedding is admission control working as designed; count it so it is visible.
                    METRICS.increment("chemclaw_turns_shed_total")
                    # Retryable: shedding says "not now". `at_capacity`, not `budget_exhausted`, so
                    # a surface can tell "busy, retry shortly" from "budget gone, stop".
                    shed = ErrorEvent(
                        message=AT_CAPACITY,
                        code="at_capacity",
                        retryable=True,
                        correlation_id=correlation_id,
                    )
                    yield sse_frame(shed)
                    return
            else:
                await semaphore.acquire()
            permit = True
            # The binding budget check. The pre-response check runs at request entry, so a
            # concurrent burst passes it before any turn has booked; here a turn holds a permit, so
            # the overshoot is bounded by `service_max_concurrent_turns` plus turns that detached
            # and kept spending (`chemclaw_turns_in_flight` vs `chemclaw_turn_capacity`). An event,
            # since the response is open; not retryable until the cap is raised or the counters
            # reset.
            try:
                await front.budget.check(session_id, principal.oid)
                await check_thread_size(session_id)
            except BudgetExceeded as exc:
                METRICS.increment(refused_metric(exc))
                refused = ErrorEvent(
                    message=str(exc),
                    code="budget_exhausted",
                    retryable=False,
                    correlation_id=correlation_id,
                )
                yield sse_frame(refused)
                return
            METRICS.increment("chemclaw_turns_started_total")
            try:
                # The deadline covers the whole streamed run: a stall inside `run_turn` surfaces
                # here as `TimeoutError` and becomes one error event. It does not bound the
                # transport; `_TurnStream`'s `send_timeout` does. There is no agent lease: a graph
                # is compiled per turn around its own connectors, so there is no shared object to
                # lease.
                async with asyncio.timeout(settings.service_turn_timeout_seconds) as deadline:
                    async for event in run_turn(
                        current.session,
                        body.message,
                        # The sender, always — for a message that waited, the principal its own
                        # request authenticated.
                        actor=runner.oid,
                        roles=runner.roles,
                        budget=front.budget,
                        dry_run=body.dry_run,
                        # The profile picks both the graph and its connectors; selecting one without
                        # the other would advertise a narrowed toolset over the full connector set.
                        connectors=front.connector_factory(current.profile),
                        history=front.history,
                        profile=current.profile,
                        graph_factory=front.graph_factory,
                        # When this scope will fire, so the cost row can say `timed_out` rather than
                        # `abandoned`: inside `run_turn` a timeout is indistinguishable from a Stop,
                        # and the `except` below runs after the turn has booked itself.
                        deadline=deadline.when(),
                        exhibit_refs=body.exhibit_refs,
                    ):
                        if event.type == "error":
                            turn_failed = True
                        yield sse_frame(event)
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
                    # Not retryable unchanged: the same question will take the same time; ask a
                    # narrower one.
                    retryable=False,
                    correlation_id=correlation_id,
                )
                yield sse_frame(timeout_event)
        except Exception as exc:
            # The stream's catch-all, for failures one frame above `run_turn`'s own guard
            # (evaluating `front.connector_factory`, `front.history`, `front.graph_factory`) or
            # while waiting in line. Once the response has started no exception handler can run, so
            # without this the stream would end with zero events. A stream always ends with an
            # answer or an error; `failure_event` is the runner's own classifier, so both report a
            # failure the same way.
            turn_failed = True
            logger.exception("turn stream failed for session %s", session_id)
            failed = failure_event(exc, session_id, correlation_id or uuid.uuid4().hex)
            yield sse_frame(failed)
        finally:
            if turn_failed:
                METRICS.increment("chemclaw_turns_failed_total")
            if heartbeat is not None:
                heartbeat.cancel()
            # A no-op when the reader already went and the detach hook gave it back.
            _release_permit()
            if ticket is not None:
                # Stopped, failed or cancelled while waiting: give the place up now.
                await _leave_line(queue, session_id, ticket)
                _left_line()
            try:
                # Settle the turn's question before giving up the claim, so the next message never
                # finds it `running` and calls it interrupted (`runner.transcript_settled`).
                await transcript_settled(session_id)
            finally:
                if slot is not None:
                    _release_turn_slot(active_turns, session_id, slot)
                    if holder is not None and claims is not None:
                        await _release_turn_claim(claims, session_id, holder)
                # Whatever ended — a turn, or a place in line — the next message may now be up.
                signal.notify()

    handed_off = False
    try:
        # Name the session after its first message, so `GET /sessions` can render a conversation
        # list. Here because the message is still a plain string; before the stream, so a failed
        # turn still names it; a no-op once a title exists. Inside this `try` because the store
        # round trip can raise or be cancelled, and the `finally` must still return the slot.
        if front.session_owners is not None:
            await front.session_owners.set_title_if_absent(session_id, session_title(body.message))
        # Budget fast path: refuse with a clean 429 before taking a permit if the budget is already
        # exhausted. The binding check is inside the stream.
        try:
            await front.budget.check(session_id, principal.oid)
            await check_thread_size(session_id)
        except BudgetExceeded as exc:
            METRICS.increment(refused_metric(exc))
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        # The durable claim, on the fast path. A failed checkout raises `ConnectionError` and is
        # shed as a 503 (fail closed, retryably). A turn on another replica sends this message into
        # the line below.
        if slot is not None and claims is not None:
            if await claims.claim(session_id, claim_holder(slot), lease, actor=principal.oid):
                holder = claim_holder(slot)
            else:
                _release_turn_slot(active_turns, session_id, slot)
                slot = None
                busy = "durable"
        if slot is None:
            METRICS.increment("chemclaw_turns_conflict_total", labels={"scope": busy or "queue"})
            # Refused (still before the response exists) only when the line is full or the sender
            # already has a message in it, or when this process cannot hold another waiter: a
            # waiting message holds its stream open here, and the socket budget charges
            # `service_max_concurrent_turns` × `service_turn_queue_max` waiters per process.
            # Reserved with no `await` between test and write.
            if sum(front_waiters.values()) >= (
                settings.service_max_concurrent_turns * settings.service_turn_queue_max
            ):
                METRICS.increment("chemclaw_turn_queue_refused_total")
                raise HTTPException(
                    status_code=429,
                    detail="this server holds as many waiting messages as it can; retry shortly",
                    headers={"Retry-After": _retry_after_hint()},
                )
            front_waiters[waiter_key] = front_waiters.get(waiter_key, 0) + 1
            counted = True
            try:
                ticket = await queue.enqueue(
                    session_id,
                    principal.oid or "",
                    capacity=settings.service_turn_queue_max,
                    lease_seconds=lease,
                )
            except QueueRefused as exc:
                _left_line()
                METRICS.increment("chemclaw_turn_queue_refused_total")
                raise HTTPException(status_code=409, detail=_queue_refusal(exc.reason)) from exc
        else:
            # The lease clock starts here: from now on the `finally` below no longer owns cleanup,
            # so the slot needs its own expiry.
            _start_turn_lease(active_turns, session_id, slot)
        # The turn runs on its own pump task: the SSE response is a view, so a disconnect detaches
        # it and the turn runs to completion, releasing lease and claim at its true end. The permit,
        # which belongs to the replica, goes back at detach (`_release_permit`). Stopping is the
        # explicit route below. A waiting message waits on the same pump, so a detach keeps its
        # place.
        turn = DetachableTurn(
            _turn_events(),
            session_id=session_id,
            survive_disconnect=settings.service_turn_survives_disconnect,
            on_detach=_release_permit,
            correlation_id=correlation_id,
        )
        if slot is not None:
            front.running_turns.register(session_id, turn)
        else:
            started = turn
        response = _TurnStream(
            turn.events(),
            session_id=session_id,
            ping=settings.service_sse_ping_seconds,
            send_timeout=settings.service_sse_send_timeout_seconds,
        )
        handed_off = True
        return response
    finally:
        # try/finally, not `except Exception`: cancellation is a BaseException, and missing it would
        # leak the session's active-turns entry. Until hand-off this owns cleanup; afterwards the
        # generator does, and the lease (and the queue's own lease for a ticket) bounds the window
        # where neither runs.
        if not handed_off:
            if slot is not None:
                _release_turn_slot(active_turns, session_id, slot)
            if holder is not None and claims is not None:
                await _release_turn_claim(claims, session_id, holder)
            if ticket is not None:
                await _leave_line(queue, session_id, ticket)
            _left_line()


async def _leave_line(queue: TurnQueue, session_id: str, ticket: int) -> None:
    """Give a place in the line back, surviving the cancellation that usually causes it.

    Shielded because callers are `finally` blocks running under cancellation. A failed leave costs
    the next message at most one lease, so it is logged rather than raised.
    """

    async def _leave() -> None:
        """The delete itself, owning its own failure so the shielded task cannot raise unseen."""
        try:
            await queue.leave(session_id, ticket)
        except Exception:
            logger.warning(
                "could not take ticket %s out of session %s's line; it lapses on its own",
                ticket,
                session_id,
                exc_info=True,
            )

    await asyncio.shield(_leave())


async def stop_turn(
    request: Request,
    session_id: str,
    principal: CurrentUser,
    live: CurrentSession,
    reason: Literal["unload"] | None = None,
) -> dict[str, bool]:
    """Stop the session's running turn — a disconnect only detaches, so this is the one way to cancel
    work in flight.

    Guarded by the session dependency; in a shared session a member stops only their own turn, the
    owner any. 404 when nothing is running, so a client that raced completion knows which happened.
    Any replica answers: a turn held elsewhere is authorized here and delivered to its holder as a
    polled request (`_stop_elsewhere`); 503 if the holder does not answer within
    `service_turn_relay_lease_seconds`.

    `?reason=unload` defers the stop: the page is being discarded and a reload cannot be told from a
    close, so the turn stops only if neither its sender nor the requester reattaches through `GET
    /sessions/{id}/turn/stream` within `service_turn_unload_grace_seconds`. Answers `{"stopped":
    false, "deferred": true}`. Without the reason the stop is immediate, including over a pending
    deferral.
    """
    front = state(request)
    turn = front.running_turns.get(session_id)
    if turn is None:
        return await _stop_elsewhere(front, session_id, principal, live, reason)
    # In a shared session a turn is its sender's to stop, or the owner's; membership does not grant
    # ending another member's work.
    lease = front.active_turns.get(session_id)
    sender = lease.actor if lease is not None else None
    if sender is not None and sender != principal.oid:
        require_owner(live, principal, session_id, "stop somebody else's turn")
    grace = settings.service_turn_unload_grace_seconds
    if (
        reason == "unload"
        and grace > 0
        and turn.defer_stop(
            grace,
            resumers=frozenset(oid for oid in (principal.oid, sender) if oid),
            max_deferrals=settings.service_turn_unload_grace_max_deferrals,
        )
    ):
        METRICS.increment("chemclaw_turns_stop_deferred_total")
        logger.info(
            "session %s's turn will be stopped in %ss unless its page reattaches (unload stop)",
            session_id,
            grace,
        )
        return {"stopped": False, "deferred": True}
    await turn.stop()
    METRICS.increment("chemclaw_turns_stopped_total")
    logger.info("session %s's turn was stopped by request", session_id)
    return {"stopped": True}


async def _held_elsewhere(front: FrontDoorState, session_id: str) -> Holding | None:
    """The live claim on a session whose turn is not running in this process, if there is one."""
    relay = front.turn_relay
    if relay is None:
        return None
    holding = await relay.holding(session_id)
    # The claim can name this process just before its turn registers or just after it ended; neither
    # is a turn elsewhere.
    if holding is None or holding.holder in _local_holders(front, session_id):
        return None
    return holding


def _local_holders(front: FrontDoorState, session_id: str) -> set[str]:
    """The claim holder this process would name for the session's in-flight slot, if any."""
    lease = front.active_turns.get(session_id)
    return {claim_holder(lease.token)} if lease is not None else set()


async def _stop_elsewhere(
    front: FrontDoorState,
    session_id: str,
    principal: Principal,
    live: LiveSession,
    reason: Literal["unload"] | None,
) -> dict[str, bool]:
    """Stop a turn another replica holds: authorize here, then ask the holder and wait for it.

    Sender-or-owner is applied from the claim's recorded sender before anything is written, so the
    holder only executes allowed stops. A claim with no recorded sender is treated as someone else's
    turn (owner only).
    """
    holding = await _held_elsewhere(front, session_id)
    relay = front.turn_relay
    if holding is None or relay is None:
        raise HTTPException(status_code=404, detail="no turn is running for this session")
    if holding.actor is None or holding.actor != principal.oid:
        require_owner(live, principal, session_id, "stop somebody else's turn")
    answer = await relay.stop(session_id, holding, principal.oid or "", unload=reason == "unload")
    if answer.state is None:
        raise HTTPException(status_code=404, detail="no turn is running for this session")
    if answer.state == "asked":
        raise HTTPException(
            status_code=503,
            detail="the replica running this turn did not answer; retry the stop",
            headers={"Retry-After": _retry_after_hint()},
        )
    if answer.state == "deferred":
        return {"stopped": False, "deferred": True}
    logger.info("session %s's turn on another replica was stopped by request", session_id)
    return {"stopped": True}


async def session_queue(
    request: Request,
    session_id: str,
    principal: CurrentUser,
    live: CurrentSession,
) -> SessionQueueOut:
    """The session's line — who is waiting for the running turn to end, first in line first.

    Any participant may read it: it names senders and places, not what they said.
    """
    front = state(request)
    waiting = await front.turn_queue.waiting(session_id)
    return SessionQueueOut(
        running=(
            front.running_turns.get(session_id) is not None
            or await _held_elsewhere(front, session_id) is not None
        ),
        waiting=[
            QueuedMessageOut(
                ticket=entry.ticket,
                sender=entry.sender,
                enqueued_at=entry.enqueued_at,
                position=place,
                mine=entry.sender == principal.oid,
            )
            for place, entry in enumerate(waiting)
        ],
    )


async def withdraw_queued(
    request: Request,
    session_id: str,
    ticket: int,
    principal: CurrentUser,
    live: CurrentSession,
) -> Response:
    """Withdraw a waiting message before it runs — its sender's, or the owner's for any.

    404 for a ticket this session's line does not hold, including another session's, so tickets
    cannot be guessed across sessions. The waiting request notices on its next look (within
    `service_turn_queue_poll_seconds` on another replica) and ends with `queue_cancelled`.
    """
    front = state(request)
    entry = next(
        (item for item in await front.turn_queue.waiting(session_id) if item.ticket == ticket),
        None,
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="no such message is waiting in this session")
    if entry.sender != principal.oid:
        require_owner(live, principal, session_id, "withdraw somebody else's message")
    await front.turn_queue.leave(session_id, ticket)
    front.queue_signal.notify()
    logger.info("a waiting message in session %s was withdrawn by request", session_id)
    return Response(status_code=204)


async def watch_turn(
    request: Request,
    session_id: str,
    principal: CurrentUser,
    live: CurrentSession,
) -> EventSourceResponse:
    """Follow the session's running turn live — any participant, from this moment on.

    Fan-out: each watcher has its own buffer, so one that stops reading is cut off (`stream_lagged`)
    without slowing the turn. The turn is resolved from this session's registry entry after the
    session gate. A turn on another replica is relayed through rows by its holder
    (`_watch_elsewhere`). A late joiner sees events from the moment it attaches.

    404 when no turn is running; 410 `turn_interrupted` when the latest turn died with its process,
    so the client can offer to resend. 429 at `service_turn_max_watchers`, or when the caller holds
    `service_max_event_streams_per_user` long-lived streams (the same ledger as push-back streams);
    both are held until the socket closes.

    `TURN_CORRELATION_HEADER` names the turn being watched. The sender reattaching cancels a pending
    unload stop (`DetachableTurn.resume`). Membership is re-read while watching
    (`_while_a_participant`).
    """
    front = state(request)
    turn = front.running_turns.get(session_id)
    if turn is None:
        holding = await _held_elsewhere(front, session_id)
        if holding is not None:
            return await _watch_elsewhere(request, session_id, principal, holding)
        if await _interrupted(front.history, session_id, live.session.state):
            raise HTTPException(
                status_code=410,
                detail={
                    "code": "turn_interrupted",
                    "message": _INTERRUPTED_MESSAGE,
                },
            )
        raise HTTPException(status_code=404, detail="no turn is running for this session")
    if turn.watchers >= settings.service_turn_max_watchers:
        raise HTTPException(
            status_code=429,
            detail=(
                "this turn already has as many watchers as it accepts; read the answer in the "
                "conversation once it lands"
            ),
            headers={"Retry-After": _retry_after_hint()},
        )
    release_slot = _take_event_stream_slot(front.event_streams, principal.oid)
    if release_slot is None:
        METRICS.increment("chemclaw_event_streams_rejected_total")
        raise HTTPException(
            status_code=429,
            detail="too many concurrent event streams; close one and retry",
            headers={"Retry-After": "1"},
        )
    watch = turn.watch(principal.oid)
    if watch is None:
        release_slot()
        raise HTTPException(status_code=404, detail="no turn is running for this session")
    # A page reloaded mid-turn: its sender reattaching cancels its own pending unload stop; `resume`
    # checks who.
    turn.resume(principal.oid)

    def _release() -> None:
        """Give back the watcher's place and the caller's stream slot, when the socket is gone."""
        watch.close()
        release_slot()

    return _TurnStream(
        _while_a_participant(request, session_id, principal, watch.events),
        session_id=session_id,
        ping=settings.service_sse_ping_seconds,
        send_timeout=settings.service_sse_send_timeout_seconds,
        release=_release,
        # Which turn this is, so a reloaded page can tell it from another participant's turn.
        headers={TURN_CORRELATION_HEADER: turn.correlation_id} if turn.correlation_id else None,
    )


async def _watch_elsewhere(
    request: Request, session_id: str, principal: Principal, holding: Holding
) -> EventSourceResponse:
    """Follow a turn another replica holds, through the frames its holder relays.

    The stream slot is charged here as for a local watch; the watcher cap is the holder's, since
    only it can count every view, and comes back as the same 429.
    """
    front = state(request)
    relay = front.turn_relay
    if relay is None:  # `_held_elsewhere` found a holding, so there is a relay
        raise HTTPException(status_code=404, detail="no turn is running for this session")
    release_slot = _take_event_stream_slot(front.event_streams, principal.oid)
    if release_slot is None:
        METRICS.increment("chemclaw_event_streams_rejected_total")
        raise HTTPException(
            status_code=429,
            detail="too many concurrent event streams; close one and retry",
            headers={"Retry-After": "1"},
        )
    try:
        request_id, answer = await relay.follow(session_id, holding, principal.oid or "")
    except BaseException:
        release_slot()
        raise
    if answer.state != "watching":
        release_slot()
        if answer.state == "refused":
            raise HTTPException(
                status_code=429,
                detail=(
                    "this turn already has as many watchers as it accepts; read the answer in the "
                    "conversation once it lands"
                ),
                headers={"Retry-After": _retry_after_hint()},
            )
        if answer.state == "asked":
            raise HTTPException(
                status_code=503,
                detail="the replica running this turn did not answer; reattach in a moment",
                headers={"Retry-After": _retry_after_hint()},
            )
        raise HTTPException(status_code=404, detail="no turn is running for this session")

    def _release() -> None:
        """Withdraw the follow and give back the caller's stream slot, when the socket is gone."""
        relay.forget(request_id)
        release_slot()

    return _TurnStream(
        _while_a_participant(
            request, session_id, principal, relay.view(request_id, session_id, holding.holder)
        ),
        session_id=session_id,
        ping=settings.service_sse_ping_seconds,
        send_timeout=settings.service_sse_send_timeout_seconds,
        release=_release,
        headers=(
            {TURN_CORRELATION_HEADER: answer.correlation_id} if answer.correlation_id else None
        ),
    )


# : What a client following a turn that died with its process is told.
_INTERRUPTED_MESSAGE = (
    "This answer was interrupted: the service restarted while it was being written. Your question "
    "is in the conversation; send it again to get an answer."
)


async def _interrupted(history: Any, session_id: str, session_state: dict[str, Any]) -> bool:
    """Whether the session's latest turn ended `interrupted` — after first asking it to settle.

    Asked only when nothing runs here; costs one index probe and at most one row read. A store that
    cannot answer leaves the route answering 404.
    """
    await settle_interrupted_turns(history, session_id, state=session_state)
    latest = getattr(history, "latest_turn_status", None)
    if latest is None:
        return False
    try:
        return bool(await latest(session_id, state=session_state) == "interrupted")
    except (ConnectionError, psycopg.Error):
        logger.warning(
            "could not read session %s's latest turn; answering 404", session_id, exc_info=True
        )
        return False


async def _while_a_participant(
    request: Request,
    session_id: str,
    principal: Principal,
    view: AsyncGenerator[dict[str, str], None],
) -> AsyncIterator[dict[str, str]]:
    """Relay a watcher's view for as long as the watcher is still in the conversation.

    Membership is re-read before the next event once `service_turn_watch_recheck_seconds` have
    passed, so a quiet turn costs no lookups. A removed watcher is closed without a final event, as
    a stranger gets nothing; their client falls back to the transcript, which answers 404.
    """
    checked = time.monotonic()
    async with contextlib.aclosing(view):
        async for frame in view:
            if time.monotonic() - checked >= settings.service_turn_watch_recheck_seconds:
                try:
                    await _resolve_session(request, session_id, principal)
                except HTTPException:
                    METRICS.increment("chemclaw_turn_watchers_removed_total")
                    logger.info(
                        "a watcher of session %s is no longer a participant; their view was closed",
                        session_id,
                    )
                    return
                checked = time.monotonic()
            yield frame


def register(app: FastAPI) -> None:
    """Attach this module's route to `app` — called once, by `create_app` only.

    Registered on the app rather than via the lazy `include_router`, which would hide routes from
    tests that walk the route table and disable `app.dependency_overrides`.
    """
    app.post(
        "/sessions/{session_id}/messages",
        # FastAPI cannot infer a `text/event-stream` body, so the OpenAPI document states it;
        # `create_app` merges the referenced components.
        responses={
            200: {
                "description": "One SSE frame per turn event.",
                "content": {"text/event-stream": {"schema": {"$ref": TURN_EVENT_REF}}},
            }
        },
    )(post_message)
    app.post("/sessions/{session_id}/turn/stop")(stop_turn)
    app.get(
        "/sessions/{session_id}/turn/stream",
        responses={
            200: {
                "description": "One SSE frame per turn event, from the moment of attaching.",
                "content": {"text/event-stream": {"schema": {"$ref": TURN_EVENT_REF}}},
            }
        },
    )(watch_turn)
    app.get("/sessions/{session_id}/queue")(session_queue)
    app.delete("/sessions/{session_id}/queue/{ticket}", status_code=204)(withdraw_queued)
