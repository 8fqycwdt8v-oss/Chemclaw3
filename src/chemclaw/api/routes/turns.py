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
from collections.abc import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from chemclaw.api.budget import BudgetExceeded
from chemclaw.api.deps import CurrentSession, CurrentUser
from chemclaw.api.events import ErrorEvent, QueuedEvent
from chemclaw.api.middleware import _AT_CAPACITY
from chemclaw.api.runner import run_turn
from chemclaw.api.schemas import MessageIn
from chemclaw.api.state import (
    _WORKER_ID,
    SessionTurns,
    _claim_turn_slot,
    _hold_turn_claim,
    _release_turn_claim,
    state,
)
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS

logger = logging.getLogger(__name__)


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
    The permit hold is wall-clock bounded (`service_turn_timeout_seconds`): a hung model
    stream or a slow-reading client cannot pin a permit forever — on expiry the client gets
    one error event and the permit is released.

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
    active_turns: dict[str, float] = front.active_turns
    claims: SessionTurns | None = front.turn_claims
    lease = settings.service_turn_claim_lease_seconds
    if not _claim_turn_slot(active_turns, session_id):
        METRICS.increment("chemclaw_turns_conflict_total", labels={"scope": "process"})
        raise HTTPException(status_code=409, detail="a turn is already running for this session")
    semaphore = front.turn_semaphore

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
                    shed = ErrorEvent(message=_AT_CAPACITY, code="budget_exhausted", retryable=True)
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
                refused = ErrorEvent(message=str(exc), code="budget_exhausted", retryable=False)
                yield {"event": refused.type, "data": refused.model_dump_json()}
                return
            METRICS.increment("chemclaw_turns_started_total")
            try:
                # The deadline covers the whole streamed run *including* client consumption:
                # the generator is suspended inside this scope at each `yield`, so a stalled
                # model stream and a slow-reading client are both bounded (AG-15's missing
                # wall-clock half). A stall inside `run_turn` surfaces here as TimeoutError
                # and becomes one user-safe error event; a stall in the transport tears the
                # stream down, and the `finally` still frees the permit either way.
                async with (
                    asyncio.timeout(settings.service_turn_timeout_seconds),
                    # Exclusive for this turn (D-123). Two turns streaming through one chat
                    # client interleave its tool-call bookkeeping and emit a `tool_use` block
                    # with an empty name, which Anthropic rejects — 20% of turns in a live
                    # 50-user run. The lease is returned even if the turn raises or the client
                    # disconnects, so a pod cannot bleed capacity.
                    front.turn_agent(live.profile) as turn_agent,
                ):
                    async for event in run_turn(
                        # The session's profile picks both halves of its surface: the agent
                        # it talks to and the connectors that agent gets. Selecting one
                        # without the other would advertise a narrowed toolset over the full
                        # connector set.
                        turn_agent,
                        live.session,
                        body.message,
                        actor=principal.oid,
                        roles=principal.roles,
                        budget=front.budget,
                        dry_run=body.dry_run,
                        connectors=front.connector_factory(live.profile),
                        history=front.history,
                        profile=live.profile,
                        # The other engine's half of the same selection: `turn_agent` above hands
                        # over a pooled MAF agent, this hands over the builder for a graph that
                        # cannot exist until the turn's connectors do. Exactly one of the two is
                        # used, and which one is `run_turn`'s business rather than this route's.
                        graph_factory=front.graph_factory,
                    ):
                        if event.type == "error":
                            METRICS.increment("chemclaw_turns_failed_total")
                        yield {"event": event.type, "data": event.model_dump_json()}
            except TimeoutError:
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
                )
                yield {"event": timeout_event.type, "data": timeout_event.model_dump_json()}
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
            if permit:
                semaphore.release()
            active_turns.pop(session_id, None)
            if claims is not None:
                await _release_turn_claim(claims, session_id)

    claimed = False
    handed_off = False
    try:
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
        response = EventSourceResponse(_turn_events(), ping=settings.service_sse_ping_seconds)
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
            active_turns.pop(session_id, None)
            if claimed and claims is not None:
                await _release_turn_claim(claims, session_id)


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
