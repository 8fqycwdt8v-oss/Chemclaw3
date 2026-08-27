"""The front door's per-process state: what `create_app` seeds onto `app.state`, typed once.

`app.state` is the seam the whole `api/` split rests on (R3.2): every route and dependency reads
the process's live structures — the session cache, the turn leases, the admission semaphore —
through `request.app.state` rather than through lexical capture, which is what lets the routes
live in `chemclaw/api/routes/` while `create_app` (`api/app.py`) stays the only factory. Tests
lean on the same seam from the other side (`tests/test_service.py` replaces `app.state.
turn_semaphore` and `app.state.live_sessions` wholesale), so nothing here may cache a snapshot of
a state attribute: `FrontDoorState` reads through to `app.state` on every access.

This module holds the *shapes* of that state — the live-session cache and its record type, the
durable ownership/turn-claim Protocols and their config-gated default constructors, the process's
claim-holder identity, and the in-process turn lease — plus the `state(request)` accessor that
contains Starlette's untyped `app.state` in one place instead of leaking `Any` into every route
(the D-117 lesson).
"""

import asyncio
import logging
import math
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from fastapi import FastAPI, Request

from chemclaw.agent.plan_approval_store import ApprovalStore
from chemclaw.api.budget import BudgetTracker
from chemclaw.api.detach import RunningTurns
from chemclaw.connectors.health import ConnectorHealth
from chemclaw.core.bounded import BoundedLru
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LiveSession:
    """One live conversation: its turn session, who owns it, and which profile it runs under.

    A record rather than a tuple because it grew a third field and a fourth is plausible —
    unpacking `(session, owner)` at five call sites was already the kind of thing that breaks
    silently when the shape changes.
    """

    session: Any
    owner: str | None
    profile: str | None = None


class _LiveSessions:
    """A bounded, LRU cache of the front door's live in-process sessions with their owner (COR-3).

    The service keeps the live `TurnSession` handle per session id; without a bound this map grows
    for the pod's whole lifetime (a memory leak). This caps it and evicts the least-recently-used
    entry when full — an evicted session's durable history still lives in the session store,
    only the live in-process handle is dropped, so the worst case under memory pressure is a
    client starting a new session. Session, owner and profile are stored together so they can
    never drift: the profile decides which agent runs the turn *and* which connectors it gets,
    so a session that lost it would silently change agent mid-conversation.

    `pinned` names the sessions eviction must skip: a session with a turn in flight. Evicting one
    does not stop its turn — the turn holds the `TurnSession` handle directly — it makes the next
    request rehydrate a *second* handle over the same durable history, and the two then diverge in
    `session.state` (the hazard `chemclaw.api.deps._rehydrate_session`'s docstring names). The pin
    source is the in-process turn lease, which expires (see `_claim_turn_slot`), so a leaked pin
    releases the cache by itself rather than wedging eviction for the pod's lifetime.

    The bookkeeping itself is `chemclaw.core.bounded.BoundedLru` — this class keeps the
    session-shaped API (`add` takes the record's three fields and returns the stored entry) that
    `create_app` and the tests drive.
    """

    def __init__(self, capacity: int, pinned: Callable[[str], bool] | None = None) -> None:
        """Create a registry holding at most `capacity` live sessions.

        `pinned` says which session ids must not be evicted right now (default: none). It is
        consulted at eviction time, not stored per entry, so a pin needs no bookkeeping to clear.
        """
        self._entries: BoundedLru[str, LiveSession] = BoundedLru(capacity, pinned=pinned)

    def __len__(self) -> int:
        """How many live sessions are held — the source for the `live_sessions` gauge (DEP-4)."""
        return len(self._entries)

    def add(
        self, session_id: str, session: Any, owner: str | None, profile: str | None = None
    ) -> LiveSession:
        """Register a live session (most-recently-used), evicting the oldest past capacity.

        Returns the entry it stored, so a caller that needs the handle back does not have to
        `get` what it just `add`ed — a round trip that reads as if the entry might be missing
        when it cannot be, and whose `None` branch was previously silenced with a type ignore.

        Eviction (see `BoundedLru.put`) takes the least-recently-used entry that is neither
        pinned (a turn in flight) nor the entry just added (its handle is being handed to the
        caller, so dropping it would leave a live handle writing outside the cache — the exact
        divergence the pin prevents). When every candidate is pinned the map briefly holds more
        than `capacity`: turns in flight are bounded by the admission semaphore, orders of
        magnitude below the cache cap, so honoring the bound by corrupting a running conversation
        would be the wrong trade.
        """
        entry = LiveSession(session=session, owner=owner, profile=profile)
        self._entries.put(session_id, entry)
        return entry

    def get(self, session_id: str) -> "LiveSession | None":
        """Return the live entry for `session_id` (marking it recently used), or None."""
        return self._entries.get(session_id)


class SessionOwners(Protocol):
    """The durable session-ownership registry the front door rehydrates from after a restart (F3).

    Kept as a Protocol so the concrete `chemclaw.agent.session_store.SessionOwnerStore` (which
    needs a
    database) is imported only on the durable path, and a test can inject an in-memory fake.
    """

    async def record(self, session_id: str, owner: str | None, profile: str | None = None) -> None:
        """Record a session's owner and profile at creation (idempotent)."""
        ...

    async def lookup(self, session_id: str) -> tuple[bool, str | None, str | None]:
        """Return `(found, owner, profile)` for a session id — all-None when unknown."""
        ...

    async def set_title_if_absent(self, session_id: str, title: str) -> None:
        """Name a session after its opening question; a no-op once it has a name."""
        ...

    async def list_for_owner(
        self, owner: str | None
    ) -> list[tuple[str, datetime, datetime, str | None]]:
        """`(session_id, created_at, updated_at, title)`, newest activity first.

        Sessions with no messages are not listed — see `_OWNER_LIST` in
        `chemclaw.agent.session_store` for both that and why the order is `updated_at`.
        """
        ...


class SessionTurns(Protocol):
    """The durable "who is running a turn on this session" claim (D-121).

    A Protocol for the same reason `SessionOwners` is one: the concrete
    `chemclaw.agent.session_store.SessionTurnClaims` needs a database, so it is imported only on the
    durable path and a test injects an in-memory fake.
    """

    async def claim(self, session_id: str, holder: str, lease_seconds: float) -> bool:
        """Take the session's turn slot for `lease_seconds`; False if someone else holds it."""
        ...

    async def refresh(self, session_id: str, holder: str, lease_seconds: float) -> bool:
        """Extend this holder's claim; False once the claim is no longer this holder's."""
        ...

    async def release(self, session_id: str, holder: str) -> None:
        """Give the slot back when the turn ends."""
        ...


# This process's identity as a claim holder. A fresh id per process (each uvicorn worker imports
# this module in its own interpreter), so a claim can never be refreshed or released by anyone but
# the process that took it — including a *previous* incarnation of this pod, whose leftover claims
# must age out rather than be inherited.
_WORKER_ID = uuid.uuid4().hex

# The claim is refreshed this many times per lease. Three, so two consecutive refreshes can fail —
# a slow query, one blocked moment on the loop — before the lease is genuinely at risk. Not a
# config knob: it is a property of how the lease is maintained, not something a deployment tunes
# independently of `service_turn_claim_lease_seconds`.
_CLAIM_REFRESHES_PER_LEASE = 3


@dataclass(frozen=True)
class TurnLease:
    """One session's in-process turn slot: which turn holds it, and until when.

    `token` is what makes a release *identity-checked*. Both teardown paths used to remove
    whatever entry sat under the session id, so once one lease had lapsed and a successor had
    claimed the slot, the first turn's teardown revoked the successor's claim and a third turn was
    admitted beside a live one — a guard undoing itself.

    `deadline` is `math.inf` for as long as `post_message`'s own `try/finally` owns the cleanup,
    and a real wall clock from the moment it stops (see `_start_turn_lease`). A record rather than
    a bare float because those two fields are one fact and had to be read together.
    """

    token: str
    deadline: float


def _claim_turn_slot(active_turns: dict[str, TurnLease], session_id: str) -> str | None:
    """Reserve the in-process one-turn-per-session slot, or report that a live turn holds it.

    Returns this turn's token (its identity for `_start_turn_lease` and `_release_turn_slot`), or
    `None` when another turn holds the session.

    The slot is a *lease*, not a latch — the same semantics D-121 gave its durable counterpart
    (`session_turns`), for the same reason: every release site can be skipped. Both of this
    guard's releases live in a `finally` (the SSE generator's and `post_message`'s, exchanged via
    `handed_off` — see `chemclaw.api.routes.turns`), and one real window runs neither — a client
    gone after the streaming response is handed off but before its generator is first advanced.
    An async generator that never started runs no `finally` at all, so a latch then answered 409
    for the pod's whole lifetime. A leased entry instead stops refusing once its deadline passes.

    **The clock does not start here, and that is the correction.** The deadline used to be stamped
    at this moment and justified as "the widest wall clock a *live* turn can hold the slot", but
    two store round trips run between here and the streamed run — the title write and the durable
    claim — so the real ceiling was `(store latency) + admission + turn timeout`, strictly larger.
    Measured on the real app with a slow store, a second POST was admitted with 200 while the
    first turn was still being set up, and both drove the same `TurnSession`. What actually holds
    for that phase is stronger than any deadline: `post_message`'s `finally` releases the slot on
    every exit, exception and cancellation alike, so the reservation needs no expiry until the
    response is handed off and that `finally` stops owning it.

    Expired entries (this session's or any other's) are swept here rather than by a timer: the
    map stays bounded, the `turns_in_flight` gauge stays honest, and a leaked entry stops
    pinning its session in the live cache (`_LiveSessions`) at the same moment it stops 409ing.
    Check-and-set stays atomic on the event loop — no `await` between the test and the write —
    so the gate has no race window.
    """
    now = time.monotonic()
    for stale_id, lease in list(active_turns.items()):
        if lease.deadline <= now:
            del active_turns[stale_id]
    if session_id in active_turns:
        return None
    token = uuid.uuid4().hex
    active_turns[session_id] = TurnLease(token=token, deadline=math.inf)
    return token


def _start_turn_lease(active_turns: dict[str, TurnLease], session_id: str, token: str) -> None:
    """Start this turn's lease clock, at the moment the request stops owning its cleanup.

    Called immediately before the streaming response is handed off, which is exactly where the
    unguarded window opens: from here on `post_message`'s `finally` no longer releases the slot,
    and a client that vanishes before the generator's first advance runs no `finally` at all. So
    from *here* the deadline is the widest wall clock a live turn can hold the slot — the
    admission wait plus the streamed run's own timeout — and an expired entry again provably
    belongs to no running turn.

    Identity-checked like the release, so this can only ever restamp the entry it was given a
    token for.
    """
    lease = active_turns.get(session_id)
    if lease is None or lease.token != token:
        return
    active_turns[session_id] = TurnLease(
        token=token,
        deadline=(
            time.monotonic()
            + settings.service_turn_timeout_seconds
            + settings.service_turn_admission_timeout_seconds
        ),
    )


def _release_turn_slot(active_turns: dict[str, TurnLease], session_id: str, token: str) -> None:
    """Give back the slot *this* turn holds — never a successor's.

    The two teardown paths popped by key, so a turn whose lease had already lapsed removed
    whatever entry it found and let a third turn in beside the second. Comparing the token first
    makes a late teardown a no-op, which is the only correct thing it can be.
    """
    lease = active_turns.get(session_id)
    if lease is not None and lease.token == token:
        del active_turns[session_id]


async def _hold_turn_claim(claims: SessionTurns, session_id: str, lease_seconds: float) -> None:
    """Keep this turn's claim alive for as long as the turn streams.

    Cancelled by the stream's `finally`, so it lives exactly as long as the turn does. A refresh
    that fails is logged and counted rather than fatal: killing a chemist's turn because one small
    UPDATE did not land would trade a real answer for a hazard that also needs a second turn on
    the same session to arrive inside the remaining lease. It is *counted* because this branch
    already learned that lesson the expensive way — a guard that quietly switches itself off
    (D-107's rollback watermark) is worse than one that fails loudly.
    """
    interval = lease_seconds / _CLAIM_REFRESHES_PER_LEASE
    while True:
        await asyncio.sleep(interval)
        try:
            if not await claims.refresh(session_id, _WORKER_ID, lease_seconds):
                # The claim is no longer ours: it lapsed and another worker took the session while
                # this turn was still running. Nothing raised — the UPDATE simply matched no row —
                # so before the 2026-08-05 review this was indistinguishable from a healthy
                # refresh, and the warning below was unreachable in the one case it names.
                #
                # Stop rather than keep trying: every further refresh would match no row either,
                # and a heartbeat that cannot succeed is a timer burning a connection every few
                # seconds. The turn itself continues — cancelling a chemist's answer because a
                # lease lapsed would trade a real result for a race that has already happened.
                METRICS.increment("chemclaw_turn_claims_lost_total")
                logger.warning(
                    "the turn claim for session %s was taken over while the turn was running; "
                    "another worker may already have started a turn on this session",
                    session_id,
                )
                return
        except Exception:
            # Widened for the reason the release below it was (D-130): this runs in a task the
            # turn only ever cancels, never awaits, so an exception the tuple did not name would
            # kill the heartbeat silently *and* surface later as an unretrieved-exception
            # traceback. `psycopg.Error` is the concrete case — the store raises it and the old
            # tuple did not cover it.
            METRICS.increment("chemclaw_turn_claim_refresh_failures_total")
            logger.warning(
                "could not refresh the turn claim for session %s; if this keeps failing the "
                "claim lapses after %ss and another worker may start a turn on this session",
                session_id,
                lease_seconds,
                exc_info=True,
            )


async def _release_turn_claim(claims: SessionTurns, session_id: str) -> None:
    """Give a session's turn slot back, surviving the cancellation that usually causes it.

    **Shielded, and that is the entire point of this function** (D-130). Both callers reach it from
    a `finally` that runs *because* their task was cancelled — a chemist closed the tab mid-turn —
    and a bare `await` inside a cancelled task raises at its first suspension point. The release
    therefore started on every abandoned turn and finished on none: measured on the real path, the
    session then answered 409 to its own owner for the **full 60-second lease**, so reopening a
    closed tab was refused for a minute. `shield` runs the release as an independent task that
    outlives this frame, which is what makes the DELETE actually land.

    Cancellation still propagates out of here — the caller's task is being torn down and must
    continue to be. Only the *release* is protected, not the caller.

    The lease remains the backstop for what shielding cannot cover (the process being killed, the
    loop closing under it): a release that never lands costs the session one lease of
    unavailability, not a permanent 409 — which is precisely why the claim expires at all.
    """

    async def _release() -> None:
        """The release itself — the part that must survive, so it owns its own error handling.

        Handling the failure *inside* the shielded task rather than around the `await` is not a
        style choice: when the caller is cancelled, `shield` drops its bookkeeping callback on the
        inner task, so an exception raised there afterwards is never retrieved and asyncio reports
        it as a bare `Task exception was never retrieved` traceback with nothing tying it to a
        session. A task that cannot fail cannot produce one.
        """
        try:
            await claims.release(session_id, _WORKER_ID)
        except Exception:
            # `Exception`, not a tuple of the connection errors. The narrow tuple was written when
            # a failure here could only propagate into a `finally` that was about to be discarded
            # anyway; shielding turned it into a task nobody awaits, where anything uncaught
            # becomes an unattributed `Task exception was never retrieved`. Chaos scenario C4 —
            # Postgres stopped at the instant of the disconnect — produced exactly that, because
            # the store raises `psycopg.errors.AdminShutdown`, which is a `psycopg.Error` and
            # matched none of `(ConnectionError, OSError, RuntimeError)`.
            #
            # Breadth is the correct contract here rather than a concession: this function's whole
            # promise is that a release which cannot happen costs the session one lease, and there
            # is no failure mode for which crashing an orphan task is a better answer than saying
            # so in the log.
            logger.warning(
                "could not release the turn claim for session %s; it expires on its own",
                session_id,
                exc_info=True,
            )

    await asyncio.shield(_release())


def _default_owner_store() -> SessionOwners | None:
    """The durable session-ownership store, but only when durable sessions are on (else None).

    Rehydration is meaningful only when there is durable history to resume, so it is gated on the
    same `session_store="postgres"` switch: under the in-memory store there is nothing to reattach
    to and a cache miss stays a 404 (today's behavior). Imported lazily so the dev/test path
    never pulls in psycopg for a store it will not use.
    """
    if settings.session_store != "postgres":
        return None
    from chemclaw.agent.session_store import SessionOwnerStore

    return SessionOwnerStore()


def _default_turn_claims() -> SessionTurns | None:
    """The durable turn claim, but only where two processes can share one session (else None).

    Gated on the same `session_store="postgres"` switch as ownership, because that switch is
    exactly the condition under which two processes share a conversation's durable history and can
    therefore corrupt it. Under the in-memory store each process has its own history and the
    in-process set already covers everything there is to cover.
    """
    if settings.session_store != "postgres":
        return None
    from chemclaw.agent.session_store import SessionTurnClaims

    return SessionTurnClaims()


class FrontDoorState:
    """A typed, read-through view over the front door's `app.state` (D-117's lesson, kept).

    `app.state` is untyped by design in Starlette, so every direct read of it returns `Any` and
    silently disables type checking on whatever it touches. Reading it through these properties
    keeps that `Any` in one module instead of leaking into each route's return type — which is
    what `api/` had been doing unchecked before it joined `make type`.

    Every property reads `app.state` **at access time**, never a snapshot: tests (and a future
    admin surface) replace whole attributes — `app.state.turn_semaphore = Semaphore(0)`,
    `app.state.live_sessions = _LiveSessions(...)` — and a cached reference would silently keep
    serving the replaced object. The two connector-health fields have setters because the
    readiness route refreshes that snapshot; everything else is written only by `create_app`.
    """

    def __init__(self, app: FastAPI) -> None:
        """Wrap `app`, whose `state` this view types."""
        self._app = app

    @property
    def connector_factory(self) -> Callable[[str | None], list[Any]]:
        """Builds one turn's connectors for a profile — called per turn, never cached.

        What comes back is the engine's own representation rather than a fixed one: the choice is
        made once, in `chemclaw.agent.chemclaw_agent.connector_specs`, and `run_turn` opens
        whichever it is handed. This property is deliberately untyped beyond `list[Any]` for that
        reason — the two engines' connectors share no base class, and naming a union here would
        put a `maf` import in the front door's type surface.
        """
        factory: Callable[[str | None], list[Any]] = self._app.state.connector_factory
        return factory

    @property
    def graph_factory(self) -> Callable[..., Any]:
        """Builds one turn's compiled graph on the LangGraph engine — called per turn, never cached.

        Read through this view rather than off `app.state` directly for the reason every property
        here exists: `run_turn` needs it as an argument, and a route reaching into `app.state`
        would take an `Any` with it.
        """
        factory: Callable[..., Any] = self._app.state.graph_factory
        return factory

    @property
    def live_sessions(self) -> _LiveSessions:
        """The bounded cache of live in-process sessions (see `_LiveSessions`)."""
        sessions: _LiveSessions = self._app.state.live_sessions
        return sessions

    @property
    def session_owners(self) -> SessionOwners | None:
        """The durable ownership registry, or None under the in-memory session store."""
        owners: SessionOwners | None = self._app.state.session_owners
        return owners

    @property
    def plan_approvals(self) -> ApprovalStore:
        """The plan-approval store — the same instance `chemclaw.agent.plan_gate` reads (D-167)."""
        store: ApprovalStore = self._app.state.plan_approvals
        return store

    @property
    def history(self) -> Any:
        """The session-history provider the agent writes turns through, shared for reads."""
        history: Any = self._app.state.history
        return history

    @property
    def turn_semaphore(self) -> asyncio.Semaphore:
        """The admission-control permit set capping concurrent turns (AG-15)."""
        semaphore: asyncio.Semaphore = self._app.state.turn_semaphore
        return semaphore

    @property
    def active_turns(self) -> dict[str, TurnLease]:
        """Session id → the lease held by the turn in flight (see `_claim_turn_slot`)."""
        turns: dict[str, TurnLease] = self._app.state.active_turns
        return turns

    @property
    def turn_claims(self) -> SessionTurns | None:
        """The durable cross-process turn claim, or None under the in-memory session store."""
        claims: SessionTurns | None = self._app.state.turn_claims
        return claims

    @property
    def running_turns(self) -> "RunningTurns":
        """The live turns themselves — what the explicit stop route resolves a session against."""
        turns: RunningTurns = self._app.state.running_turns
        return turns

    @property
    def event_streams(self) -> dict[str, int]:
        """Per-user count of open push-back event streams (the DB-load cap's ledger)."""
        streams: dict[str, int] = self._app.state.event_streams
        return streams

    @property
    def budget(self) -> BudgetTracker:
        """The runaway-cost guard metering turns and tokens per session and per user."""
        budget: BudgetTracker = self._app.state.budget
        return budget

    @property
    def connector_health(self) -> list[ConnectorHealth]:
        """The last connector sweep's result — refreshed by readiness, read by a gauge."""
        health: list[ConnectorHealth] = self._app.state.connector_health
        return health

    @connector_health.setter
    def connector_health(self, health: list[ConnectorHealth]) -> None:
        """Store a fresh sweep result (the readiness route and the startup probe write here)."""
        self._app.state.connector_health = health

    @property
    def connector_health_at(self) -> float:
        """When the snapshot was taken (`time.monotonic`); -inf means "never, treat as stale"."""
        return float(self._app.state.connector_health_at)

    @connector_health_at.setter
    def connector_health_at(self, at: float) -> None:
        """Record the moment of the sweep the snapshot came from."""
        self._app.state.connector_health_at = at

    @property
    def readiness_probes(self) -> dict[str, "asyncio.Task[Any]"]:
        """The readiness probes currently in flight, one entry per probe.

        Started and awaited by `chemclaw/api/routes/ops.py`. On `app.state` rather than in a
        module global for the reason every other structure here is: the probes belong to one app
        and one event loop, and a global would be shared by two apps in one test process — and by
        a task bound to a loop that has since closed.
        """
        probes: dict[str, asyncio.Task[Any]] = self._app.state.readiness_probes
        return probes

    @property
    def database_reachable(self) -> bool:
        """Whether the last readiness probe reached Postgres (True until one has run)."""
        reachable: bool = self._app.state.database_reachable
        return reachable

    @database_reachable.setter
    def database_reachable(self, reachable: bool) -> None:
        """Store the last probe's verdict; the readiness route writes here."""
        self._app.state.database_reachable = reachable

    @property
    def database_probed_at(self) -> float:
        """When the database was last probed (`time.monotonic`); -inf means "never"."""
        return float(self._app.state.database_probed_at)

    @database_probed_at.setter
    def database_probed_at(self, at: float) -> None:
        """Record the moment of the probe the verdict came from."""
        self._app.state.database_probed_at = at


def state(request: Request) -> FrontDoorState:
    """The typed view over this request's `app.state` — how every route reads process state."""
    return FrontDoorState(request.app)
