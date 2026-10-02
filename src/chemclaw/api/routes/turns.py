"""The SSE turn stream — the one route with real concurrency machinery, kept in one place.

`POST /sessions/{id}/messages` runs a turn under five guards that must compose exactly: the
per-session in-process lease and the durable cross-process claim (a busy session is a *line* — the
message waits in it and runs as its sender when its turn comes, and only a full line is a 409:
`D-2026-10-01-a-queued-message-waits-in-its-senders-request`), the per-actor
concurrent-turn cap (429, above the claim — `D-2026-09-19-a-pod-wide-cap-is-not-a-fair-one`), the
admission semaphore (queued/shed on the open stream, D-166), and the budget (429). A sixth is
spent before this module is reached at all: `api/rate_limit.py`'s token bucket, inside
`require_principal`. The `_turn_events`
generator stays **nested in the route on purpose**: everything it captures — the turn's session,
body, principal, lease bookkeeping — is per-request state that exists nowhere but this request's
frame, so hoisting it would mean re-threading eight arguments to move code that has exactly one
caller. The app-wide structures it touches are read through `chemclaw.api.state.state(request)`
at request time, which is the seam that let this route leave `create_app` unchanged (R3.2).

Beside it: the stop route, the session's line (`GET /sessions/{id}/queue`, and `DELETE …/{ticket}`
to withdraw a waiting message), and `GET /sessions/{id}/turn/stream`, which lets any other
participant follow the running turn live.
"""

import asyncio
import contextlib
import logging
import math
import random
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable

from fastapi import FastAPI, HTTPException, Request
from sse_starlette.sse import EventSourceResponse, SendTimeoutError
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from chemclaw.agent.exhibit_notes import resolve_exhibit_refs
from chemclaw.agent.session_queue import QueueRefused, Refusal, TurnQueue
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
from chemclaw.api.runner import failure_event, run_turn
from chemclaw.api.schemas import MessageIn, QueuedMessageOut, SessionQueueOut, session_title
from chemclaw.api.state import (
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
        release: Callable[[], None] | None = None,
    ) -> None:
        """Wrap `content`, bounding each send and remembering whose turn this is.

        `release`, when given, runs when the response ends — every way it can end, including a
        client gone before the first byte, where the body generator never starts and runs no
        `finally` (`routes/streams._SlotBoundEventStream` measured that window). A watcher's place
        and stream slot are held by the *socket*, so this is the scope they are returned in.
        """
        super().__init__(content, ping=ping, send_timeout=send_timeout)
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

    The base is `service_turn_admission_timeout_seconds`, this system's existing answer to how long
    waiting for a turn permit is reasonable, rather than a number chosen here. The jitter is up to
    one further interval and exists for one reason: every client refused by this guard would
    otherwise be handed the same constant and re-converge on a single cadence, arriving together at
    the pod they were refused by. Ceilinged to at least 1, because `Retry-After: 0` means "retry
    immediately" and would turn the hint into a spin.
    """
    base = settings.service_turn_admission_timeout_seconds
    return str(max(1, math.ceil(base + random.random() * base)))


def _queue_refusal(reason: Refusal) -> dict[str, str]:
    """The 409 detail for a line that cannot take this message: a `code` and the sentence.

    An object rather than the sentence alone, as the protocol routes answer their 409s, because
    this status means two things on this route and a client has to act differently on each: a
    full line (`queue_full` — wait for it to move and send again) and a sender who already has a
    message waiting (`already_waiting` — withdraw that one or wait for it). With the sentence alone
    `Chemclaw3_ui` could tell them apart only by matching it, and offered "start a fresh session",
    the remedy for the 409 this route sent before a busy session queued (Chemclaw3 #503).
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
    conversation thread. The durable half is present only under `session_store="postgres"` — with
    the in-memory store there is no shared history for two processes to corrupt.

    **Neither answers 409 any more; a busy session is a line**
    (`D-2026-10-01-a-queued-message-waits-in-its-senders-request`). Several people share a session
    now, and a second person's question is not a double-submit. A message that finds the session
    busy takes a ticket (`agent/session_queue`), and *this request* waits in the line: its stream
    reports `queued` with the ticket and its place, and when the ticket reaches the head it takes
    both claims exactly as an uncontended turn does — then runs with this request's principal, so
    it is the sender's turn and nobody else's. The order is the database's admission order of the
    tickets, and a message never overtakes one already waiting. What is still refused, with 409, is
    a line that is full or a sender who already has a message in it — each named by its own `code`
    in the detail (`_queue_refusal`) — and, with 429, a process already holding as many waiters as
    its socket budget charges for.

    **A message that waited is re-authorized at the head**, not trusted from when it was sent
    (`D-2026-10-02-a-queued-message-is-re-authorized-at-the-head-of-the-line`): membership, the
    token and the per-actor cap are asked again before it takes the turn (`_refusal_at_the_head`).
    """
    front = state(request)
    active_turns: dict[str, TurnLease] = front.active_turns
    claims: SessionTurns | None = front.turn_claims
    lease = settings.service_turn_claim_lease_seconds
    semaphore = front.turn_semaphore
    # **Per actor — the half of a pair `src/chemclaw/api/routes/streams.py` already ships
    # whole.** The semaphore below bounds this *process* and is actor-blind, so one principal
    # opening
    # `service_max_concurrent_turns` sessions holds every permit on the replica and every other
    # chemist is shed `at_capacity` (`chemclaw.api.detach` has the measurement: one hang-up per
    # permit). One bound does not imply the other.
    #
    # **Refused here rather than beside `semaphore.acquire()`, and that is not a re-litigation of
    # D-166.** What D-166 moved onto the stream was the *wait*, which was invisible — up to
    # `service_turn_admission_timeout_seconds` with no response at all. This is a refusal: decided
    # by a dict scan, final for this request, and identical on a retry a millisecond later. So it
    # gets a status code, like the durable 409 below and like the stream cap's own 429. Answering
    # it with `at_capacity` would also name the wrong full resource — the replica may be idle; the
    # caller's own turns are the limit — and `retryable=True` would tell a UI to keep hammering a
    # condition only that client can clear.
    #
    # **`Retry-After` is sent, and the reason is the client rather than the server.** This process
    # cannot predict when one of the caller's turns ends, so on its own terms the honest answer is
    # "no number" — which is what this refusal shipped until the client was read. `Chemclaw3_ui`'s
    # `errorFromStatus` splits 429 on the *presence* of the header: with one it renders a transient
    # `rate_limited` banner with a countdown, without one it renders `budget_exhausted` — "the usage
    # budget for this service is exhausted" — which locks the composer, is false here, and which
    # that module's own comment says nothing in the UI clears. A machine-readable `code` cannot
    # carry it either: `streamTurn.ts` does not pass `errorFromStatus` its `code` argument at all,
    # so an older client would still lock. The admission timeout is the right hint because it is
    # already this system's answer to "how long is it reasonable to wait for a turn permit" — it is
    # a configured number rather than an invented one.
    #
    # **It is a "check back" cadence and not an estimate, and saying so matters**: what clears this
    # is one of the caller's *own* turns ending, which is bounded by `service_turn_timeout_seconds`
    # (600 s) rather than by the admission timeout (5 s). A compliant client can therefore retry
    # many times before the condition can plausibly lift. That is the right trade only because the
    # refusal is the cheapest thing this route does — it is raised above `set_title_if_absent`,
    # above the durable claim and above `semaphore.acquire()`, so a refused retry takes no permit,
    # no turn slot and no Postgres claim — and because a chemist's turn may finish in two seconds,
    # which a 600-second countdown in the banner would hide. What is *not* defensible is a constant
    # every refused client in a deployment shares, so it carries jitter: without it they re-converge
    # on one cadence and arrive together.
    #
    # **Above `_claim_turn_slot`, and the line order is the guard.** That claim's reservation
    # carries `deadline=math.inf` until `_start_turn_lease` starts its clock, so a raise between it
    # and the `try` below leaks the session's slot with no expiry — 409-bricking that session for
    # the pod's lifetime.
    #
    # **Inert under the shared dev principal, because there "per actor" means "everybody".** With
    # `entra_required` false every caller is one fixed oid (`auth.DEV_PRINCIPAL_OID`), so this would
    # stop dividing the pod between chemists and start capping the pod itself at this number — one
    # client holding its share would refuse every other client, which is the starvation the guard
    # exists to prevent, inverted. That configuration is reachable (`service_allow_insecure`), and a
    # deployment fronting the API with one service credential for many humans is the same shape:
    # the honest answer in both is that this guard has nothing to divide.
    #
    # **A message waiting in another session's line counts too**
    # (`D-2026-10-02-a-queued-message-is-re-authorized-at-the-head-of-the-line`): it is a turn this
    # actor will run the moment its line moves, so leaving it out let one chemist park a message in
    # every shared session they belong to and run them all at once, past the cap. The same count is
    # taken again when a waiting message reaches the head (`_refusal_at_the_head`), because what
    # was true when it was sent need not be true when it starts.
    # **The artefacts this message points at, resolved before anything is claimed.** A reference
    # to an artefact the session does not hold is a 422 here rather than a turn that quietly runs
    # without it — the chemist pressed "Ask about this" on something specific — and resolving it
    # first means a refusal holds no slot, no claim and no permit.
    if body.exhibit_refs and not settings.agent_exhibits_enabled:
        # Refused rather than validated and dropped: with artefacts off the turn note is not
        # composed, so a reference would be checked here and then reach nobody.
        raise HTTPException(
            status_code=422,
            detail="artefacts are switched off in this deployment; send the message without "
            "exhibit_refs",
        )
    if body.exhibit_refs:
        try:
            await resolve_exhibit_refs(session_id, body.exhibit_refs)
        except UnknownExhibit as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
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
        # The identity is logged at INFO and deliberately not a label. `/metrics` is
        # unauthenticated, and an `oid`'s domain is unbounded — not *caller*-chosen, which this
        # comment claimed until it was checked: the value is a tenant-issued claim off a validated
        # token, so minting many needs tenant identities or a multi-tenant `entra_tenant_id`. The
        # conclusion is unchanged, because the series cap is what decides it: a labelled counter
        # would stop counting past its limit, exactly when a flood is what you are trying to read.
        # INFO rather than WARNING matches `api/rate_limit.py`'s sibling refusal, and matters
        # because the rate is the refused client's to choose while the request limiter that would
        # bound it ships off in code.
        # The *measured* count beside the cap, not the cap twice. `held` can legitimately read
        # higher than `actor_cap` — the predicate is `>=` — and a count above it is the one
        # observable symptom of a lease that outlived its turn, so logging the configured number
        # in its place would hide exactly the failure this line exists to attribute.
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
    # **A busy session is a line, not a refusal**
    # (`D-2026-10-01-a-queued-message-waits-in-its-senders-request`). A message that finds another
    # turn running — here, on another replica, or already somebody waiting — joins the session's
    # line and this request waits in it; its stream reports the place, and when the ticket reaches
    # the head and the claims come free the turn runs as *this* principal. So the two claims below
    # stay the only thing that decides who runs, and a queued message takes them exactly as an
    # uncontended one does, only later.
    #
    # **A message never jumps a line that already exists.** The fast path is taken only when nobody
    # is waiting; otherwise the message joins behind them even if the claim happens to be free at
    # this instant, because the head may be one poll away from taking it.
    queue = front.turn_queue
    signal = front.queue_signal
    busy: str | None = "queue" if await queue.waiting(session_id) else None
    # Nothing may sit between this claim and the `try` below — no `await`, and nothing that can
    # raise — because the reservation it takes does not expire until `_start_turn_lease` starts its
    # clock, and until then only that `try`'s `finally` gives it back.
    slot = None if busy else _claim_turn_slot(active_turns, session_id, actor=principal.oid)
    if busy is None and slot is None:
        busy = "process"
    # Whether this turn holds the durable claim, and under which identity (`api/state.claim_holder`:
    # keyed by the *turn*, not by the process, so a teardown arriving after its own lease has
    # lapsed cannot revoke the successor's claim). Set when the claim is taken — here on the fast
    # path, at dispatch for a message that waited.
    holder: str | None = None
    # This message's place in the line, while it has one.
    ticket: int | None = None
    # The live session the turn runs on. A message that waited re-resolves it at dispatch: the
    # handle this request resolved may have been evicted and rehydrated while it waited, and two
    # handles over one thread diverge (`api/deps._rehydrate_session`).
    current = live
    # The turn object, registered as the session's running turn when it starts rather than when it
    # is created — a waiting message is not the running turn, and registering it early would hand
    # the stop route and every watcher the wrong one.
    started: DetachableTurn | None = None
    # Who the turn runs as. The request's principal on the fast path; for a message that waited,
    # what the same credential still establishes at the head of the line (`_refusal_at_the_head`).
    runner: Principal = principal
    # Whether this message holds a place in the process's waiter ledger (`front.queue_waiters`) —
    # reserved before the ticket is taken, so two concurrent POSTs cannot both pass its bound.
    counted = False
    waiter_key = (principal.oid, session_id)

    # **The id the header, the audit trail and `turn_costs` are all keyed on.** Read once, here,
    # rather than in the generator: the observability middleware minted it for this request and
    # stamped it as an ambient, and the generator runs in this request's context, so both resolve
    # to the same string — but reading it at the top is what makes that a fact of the code rather
    # than of the runtime. Every `ErrorEvent` this module builds carries it, because
    # `ErrorEvent.correlation_id` is the join key an operator is asked to quote and three of the
    # four events built here used to default it to `""` while the answer sat on the response
    # header. `run_turn`'s own events already carry the same id through `ledger.correlation_id`.
    correlation_id = get_current_correlation_id() or ""

    # **Held out here, because two different endings give it back and only one of them is the
    # turn's.** The turn's own `finally` releases it at the pump's true end; `_release_permit` is
    # also handed to `DetachableTurn` as its detach hook, so a client that hangs up stops charging
    # *admission* for work nobody is watching. Admission is fairness to a waiting client and a
    # detached turn has none — see `chemclaw.api.detach`'s module docstring for the measurement
    # (eight hang-ups, 0 permits free, every other chemist shed). The flag makes it idempotent, and
    # nothing between the test and the release can suspend, so whichever ending arrives first wins.
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

        The same two claims the fast path takes, in the same order and under the same rules — the
        in-process slot first (no `await` between its test and its write), then the durable row —
        so a queued message cannot run beside a live turn any more than a double-submit could.
        """
        nonlocal slot, holder, started
        taken = _claim_turn_slot(active_turns, session_id, actor=principal.oid)
        if taken is None:
            return False
        try:
            if claims is not None and not await claims.claim(
                session_id, claim_holder(taken), lease
            ):
                _release_turn_slot(active_turns, session_id, taken)
                return False
        except BaseException:
            _release_turn_slot(active_turns, session_id, taken)
            raise
        slot = taken
        holder = claim_holder(taken) if claims is not None else None
        _start_turn_lease(active_turns, session_id, slot)
        # Out of the line the moment the turn is ours, so the next message becomes the head and
        # starts asking the claim instead of the queue.
        if ticket is not None:
            await _leave_line(queue, session_id, ticket)
            _left_line()
        signal.notify()
        if started is not None:
            front.running_turns.register(session_id, started)
        return True

    async def _refusal_at_the_head() -> str | None:
        """Why this message may no longer run as its sender, or `None` when it still may.

        Everything the POST was admitted on is asked again, because the wait can last about
        `service_turn_queue_max` × `service_turn_timeout_seconds`
        (`D-2026-10-02-a-queued-message-is-re-authorized-at-the-head-of-the-line`):

        - **membership** — an owner who removes a member while their message waits must not have
          it run anyway. `_resolve_session` is the gate every request passes, and it also hands
          back a live handle current *now*;
        - **the credential** — `auth.reauthorize` re-validates the same token, so a turn never
          starts on one that expired in the line, and runs on the roles it still vouches for;
        - **the per-actor cap** — the turns this sender has running elsewhere *now*, which a burst
          of concurrent POSTs can have pushed past what each saw on arrival.

        Asked in that order, cheapest refusal first; each is read on every look at the head, so a
        message held there by a busy claim is re-checked each poll rather than once.
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
        # Read now rather than taken from the POST's frame: this is the check *at the head*, and
        # what it compares against is the configuration in force when the turn would start.
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

        Ends in one of two ways: the turn is taken (`slot` is set), or the message is withdrawn —
        its ticket vanished (its sender or the owner withdrew it, its sender was erased, the
        session was deleted) or its sender is no longer a participant — and a `queue_cancelled`
        frame is the last thing yielded.

        **Authority is read again at the head** (`_refusal_at_the_head`), because the principal was
        authorized when the message was *sent* and the turn runs later.
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
        # Release the session's turn slot, its durable claim, this message's place in the line and
        # — unless the detach hook already did — the admission permit when the *turn* ends: normal
        # completion, error, timeout, or a stop. Since the pump, that is the turn's true end rather
        # than the reader's, so this is what keeps the session claimed for exactly as long as work
        # is in flight.
        heartbeat: asyncio.Task[None] | None = None
        nonlocal permit
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
            async for frame in _wait_in_line():
                yield frame
            if slot is None:
                return  # withdrawn while it waited; `_wait_in_line` said so
            if holder is not None and claims is not None:
                heartbeat = asyncio.create_task(_hold_turn_claim(claims, session_id, lease, holder))
            # Admission, inside the stream (D-166). `locked()` is the whole reason the common
            # case costs nothing: it is false exactly when `acquire()` will return without
            # suspending, and there is no await between the test and the acquire for another
            # turn to slip through, so an uncontended turn takes its permit and emits no
            # `queued` event at all.
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
                    # retrying". `AT_CAPACITY` was already the one literal for this condition;
                    # now the code names the same thing the wording does.
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
            # The budget, again — and this is the check that binds. The one before the response
            # was handed off runs at *request entry*, so every turn in a concurrent burst passes
            # it before any of them has recorded a thing: measured with production-shaped values
            # (8 permits, 40 concurrent POSTs, a 1-turn cap) as 40 answers and 40,000 tokens
            # booked, against a documented overshoot bound of 8. Re-checking here is what makes
            # that bound a small number rather than the request concurrency: a turn reaching this
            # line holds a permit, so it is one of at most `service_max_concurrent_turns` *newly
            # admitted* turns, and every turn that finished ahead of it has already been booked by
            # `record`. It is no longer exactly 8, because a detached turn gives its permit back
            # and keeps spending (see `_release_permit`) — the bound is that number plus whatever
            # detached before this turn was admitted, which `chemclaw_turns_in_flight` shows
            # against `chemclaw_turn_capacity`.
            #
            # An event rather than a status code (D-166): the response is open by now, and the
            # shed branch above answers the same way for the same reason. Not retryable — the
            # budget is spent, so the next attempt fails identically until an operator raises the
            # cap or the counters reset.
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
                        current.session,
                        body.message,
                        # **The sender, always** (`D-2026-09-27-in-a-shared-session-the-sender-
                        # governs`) — and for a message that waited in line, still the principal
                        # *its own* request authenticated, never whoever's turn ran before it.
                        actor=runner.oid,
                        roles=runner.roles,
                        budget=front.budget,
                        dry_run=body.dry_run,
                        # The session's profile picks both halves of its surface: the graph the
                        # chemist talks to and the connectors that graph gets. Selecting one
                        # without the other would advertise a narrowed toolset over the full
                        # connector set.
                        connectors=front.connector_factory(current.profile),
                        history=front.history,
                        profile=current.profile,
                        graph_factory=front.graph_factory,
                        # The reading this scope will fire at, so the turn's own cost row can say
                        # `timed_out` rather than `abandoned`. The cancellation is indistinguishable
                        # from a Stop inside `run_turn`, and this route learns which it was only in
                        # the `except TimeoutError` below — which runs *after* the turn has booked
                        # itself. See `run_turn`'s `deadline` argument.
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
                    # Not retryable unchanged: the same question will take the same time. The
                    # useful next step is a narrower question, not another wait.
                    retryable=False,
                    correlation_id=correlation_id,
                )
                yield sse_frame(timeout_event)
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
            # A failure while *waiting* (the queue's store unreachable) lands here too, and is the
            # same account: the message did not run.
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
                # Stopped, failed or cancelled while still waiting: give the place up now rather
                # than leave the message behind it waiting one lease for a waiter that is gone.
                await _leave_line(queue, session_id, ticket)
                _left_line()
            if slot is not None:
                _release_turn_slot(active_turns, session_id, slot)
                if holder is not None and claims is not None:
                    await _release_turn_claim(claims, session_id, holder)
            # Whatever ended — a turn, or a place in line — the next message may now be up.
            signal.notify()

    handed_off = False
    try:
        # Name the session after the message that opened it, so `GET /sessions` can render a
        # conversation list rather than a column of ids. Here rather than in the history provider
        # because here the message is still a plain string — the provider stores an opaque payload
        # it is not allowed to interpret. Before the stream, so a turn that fails mid-answer still
        # leaves the conversation named. `set_title_if_absent` is a no-op once there is a title,
        # which is every turn after the first — and every message that waits, since a line only
        # forms behind a turn that has already named the session.
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
            await front.budget.check(session_id, principal.oid)
            await check_thread_size(session_id)
        except BudgetExceeded as exc:
            METRICS.increment(refused_metric(exc))
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        # The durable claim, on the fast path. A failed checkout raises `ConnectionError` and is
        # shed as a 503 by `_database_unavailable` — the guard fails closed, retryably. A turn
        # already running on another replica is not a refusal any more: this message joins the
        # line below, behind it.
        if slot is not None and claims is not None:
            if await claims.claim(session_id, claim_holder(slot), lease):
                holder = claim_holder(slot)
            else:
                _release_turn_slot(active_turns, session_id, slot)
                slot = None
                busy = "durable"
        if slot is None:
            METRICS.increment("chemclaw_turns_conflict_total", labels={"scope": busy or "queue"})
            # **Refused only when the line itself cannot take it** — a status code, because this
            # is still before the response exists. The two limits say different things: the line
            # is full (wait for it to move), or this sender already has a message in it (one each,
            # so a member cannot crowd the others out and a retried POST queues one duplicate,
            # not a stream of them).
            #
            # **And only while this process can hold another waiter.** A waiting message holds its
            # sender's stream open *here* whichever replica runs the turn ahead of it, so the
            # socket budget (`core/config/__init__.py`) cannot charge waiters per local turn; it
            # charges `service_max_concurrent_turns` × `service_turn_queue_max` waiters per process,
            # and this is where that number stops being an assumption. Reserved before the
            # `await`, with nothing between the test and the write, so a burst cannot overshoot.
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
            # The lease clock starts *here*, not at the claim: from the next statement on, the
            # `finally` below no longer owns the cleanup and the slot needs an expiry of its own
            # (see `_start_turn_lease`).
            _start_turn_lease(active_turns, session_id, slot)
        # The turn runs on a pump task of its own from this moment
        # (`D-2026-08-27-a-disconnect-is-a-detach-not-a-stop`): the SSE response is a *view* of
        # it, so a client disconnect detaches the view and the turn runs to completion — its
        # answer lands in the transcript, its teardown releases the lease and the claim at the
        # turn's true end. The **permit** goes back earlier, at the detach itself, because it is
        # the one thing here that belongs to the replica rather than to the session; see
        # `_release_permit`. Stopping is the explicit route below, which cancels the pump and
        # delivers the same `CancelledError` a disconnect used to. A message still waiting in
        # line waits on the same pump, so a detach does not lose its place either.
        turn = DetachableTurn(
            _turn_events(),
            session_id=session_id,
            survive_disconnect=settings.service_turn_survives_disconnect,
            on_detach=_release_permit,
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
        # try/finally, not `except Exception`: cancellation (a client gone mid-admission) is
        # a BaseException, and missing it here leaked the session's active-turns entry —
        # 409-bricking the session until restart. Until the streaming response is handed
        # off, this owns the cleanup; afterwards the generator's own finally does — except
        # for the one window neither covers (handed off, never advanced), which the lease
        # in `_claim_turn_slot` bounds instead, and the queue's own lease bounds for a ticket.
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

    Shielded for `api/state._release_turn_claim`'s reason: the callers are `finally` blocks that run
    *because* their task was cancelled, and a bare `await` there raises at its first suspension. A
    leave that never lands costs the message behind it one lease — the ticket stops counting as
    ahead of anybody once it lapses — which is why the failure is logged rather than raised.
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
) -> dict[str, bool]:
    """Stop the session's running turn — the explicit act a disconnect no longer performs.

    Closing the SSE stream used to be how a turn was stopped, which made the Stop button and a
    network blip the same event; now the stream only *detaches*
    (`D-2026-08-27-a-disconnect-is-a-detach-not-a-stop`) and this is the one way to cancel work
    in flight. Guarded by the same session dependency as the turn route itself, and in a shared
    session by one more rule: a member stops only their own turn, and the owner any.

    404 when no turn is running rather than a silent 200: "there was nothing to stop" and
    "stopped" are different facts, and a client that raced the turn's own completion should know
    which happened. Only this process's turns are stoppable — the pump lives here — so on a
    multi-replica deployment the client calls the same origin its stream was on, which it always
    does, because the stream *is* how it knows a turn is running.
    """
    front = state(request)
    turn = front.running_turns.get(session_id)
    if turn is None:
        raise HTTPException(status_code=404, detail="no turn is running for this session")
    # **In a shared session, a turn is its sender's to stop — or the owner's**
    # (`D-2026-09-27-in-a-shared-session-the-sender-governs`). The session gate admits every member,
    # and one member ending another's work in flight is not a standing a membership grants; the
    # owner keeps it, as the person who decides who is in the conversation at all.
    lease = front.active_turns.get(session_id)
    sender = lease.actor if lease is not None else None
    if sender is not None and sender != principal.oid:
        require_owner(live, principal, session_id, "stop somebody else's turn")
    await turn.stop()
    METRICS.increment("chemclaw_turns_stopped_total")
    logger.info("session %s's turn was stopped by request", session_id)
    return {"stopped": True}


async def session_queue(
    request: Request,
    session_id: str,
    principal: CurrentUser,
    live: CurrentSession,
) -> SessionQueueOut:
    """The session's line — who is waiting for the running turn to end, first in line first.

    Any participant may read it, as any participant may read the transcript: the line names senders
    and places and nothing they said (`D-2026-10-01-a-queued-message-waits-in-its-senders-request`).
    """
    front = state(request)
    waiting = await front.turn_queue.waiting(session_id)
    return SessionQueueOut(
        running=front.running_turns.get(session_id) is not None,
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

    The same rule the stop route applies to a running turn, one step earlier: a message is its
    sender's, a member may not remove somebody else's, and the owner keeps the standing to clear the
    line of a conversation they own. 404 for a ticket this session's line does not hold, which is
    also what a ticket from another session gets — the lookup is by session, so a participant of one
    session cannot reach into another's line by guessing a number.

    The waiting request notices on its next look at its place — at once on this replica, within
    `service_turn_queue_poll_seconds` on another — and ends its stream with `queue_cancelled`.
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

    **Fan-out** (`D-2026-10-01-a-queued-message-waits-in-its-senders-request`): the sender's own
    `POST` stream is one view of a turn; this is another, with a buffer of its own, so a watcher who
    stops reading is cut off (`stream_lagged`) without slowing the turn or anybody else's view. The
    turn is resolved from *this* session's entry in the registry after the session gate has admitted
    the caller, so a participant of one conversation can never be handed another's.

    404 when no turn is running here — including one running on another replica, whose pump this
    process cannot reach (the stop route's scope, for the same reason). A late joiner sees events
    from the moment it attaches; what came earlier is in the transcript once the answer lands.
    429 when the turn already has `service_turn_max_watchers` watchers, or when the caller already
    holds `service_max_event_streams_per_user` long-lived streams — a followed turn is held open as
    long as a push-back stream is, so it is charged to the same ledger
    (`api/state._take_event_stream_slot`). Both places are held until the *socket* closes, not until
    the pump stops feeding the view (`api/detach.Watch`).

    **Membership is re-read while watching** (`_while_a_participant`): an owner who removes a
    member mid-turn stops that member's view rather than leaving it open to the turn's end.
    """
    front = state(request)
    turn = front.running_turns.get(session_id)
    if turn is None:
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
    watch = turn.watch()
    if watch is None:
        release_slot()
        raise HTTPException(status_code=404, detail="no turn is running for this session")

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
    )


async def _while_a_participant(
    request: Request,
    session_id: str,
    principal: Principal,
    view: AsyncGenerator[dict[str, str], None],
) -> AsyncIterator[dict[str, str]]:
    """Relay a watcher's view for as long as the watcher is still in the conversation.

    Membership is reach (`D-2026-09-27-in-a-shared-session-the-sender-governs`) and it is read per
    request — but a watch is one request that lasts a whole turn, so it is read again, before the
    next event goes out, once `service_turn_watch_recheck_seconds` have passed since the last
    look. Per event rather than on a timer, because a removed member is owed nothing *until* there
    is something to withhold, and a quiet turn costs no lookups at all.

    A watcher found removed is closed **without a final event**: they are a stranger to this
    conversation now, and a stranger gets the same nothing a session they never belonged to gives
    them (`api/deps._resolve_session`'s 404 posture). Their client falls back to the transcript,
    which answers them 404 — the account of what happened that every other route already gives.
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

    Registered with the app's own decorators rather than an `APIRouter` + `include_router`:
    since FastAPI 0.139 `include_router` is lazy — `app.routes` would hold opaque
    `_IncludedRouter` nodes, invisible to everything that walks the route table by type
    (`tests/test_route_auth_coverage.py`, the session-scope inventory in
    `tests/test_service.py`) — and a standalone router's routes carry no
    `dependency_overrides_provider`, which silently disables `app.dependency_overrides`.
    Registering on the app keeps both exactly as they were when these handlers lived in
    `create_app`.
    """
    app.post(
        "/sessions/{session_id}/messages",
        # The SSE body is `text/event-stream`, which FastAPI cannot infer from the
        # return annotation — so without this the one artefact `Chemclaw3_ui` reads
        # says nothing at all about what this route streams
        # (`D-2026-09-14-a-contract-the-client-cannot-read-is-a-contract-one-side-remembers`).
        # `create_app` is what merges the referenced components.
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
