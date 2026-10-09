"""The front door's per-process state: what `create_app` seeds onto `app.state`, typed once.

Routes read live structures through `request.app.state`, so they can live in `chemclaw/api/routes/`
with `create_app` the only factory. Holds the live-session cache, the durable ownership and
turn-claim Protocols, the claim-holder identity and the in-process turn lease, plus
`state(request)`, a read-through typed view (tests replace attributes wholesale).
"""

import asyncio
import logging
import math
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from fastapi import FastAPI, Request

from chemclaw.agent.plan_approval_store import ApprovalStore
from chemclaw.agent.session_queue import InMemoryTurnQueue, SessionTurnQueue, TurnQueue
from chemclaw.api.budget import BudgetTracker
from chemclaw.api.detach import RunningTurns
from chemclaw.connectors.health import ConnectorHealth
from chemclaw.core.bounded import BoundedLru
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from chemclaw.core.turn_fence import TurnFence

if TYPE_CHECKING:  # `api/turn_relay` reads this module's lease and holder identity at import
    from chemclaw.api.turn_relay import TurnRelay

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LiveSession:
    """One live conversation: its turn session, who owns it, and which profile it runs under."""

    session: Any
    owner: str | None
    profile: str | None = None


class _LiveSessions:
    """A bounded LRU cache of the front door's live in-process sessions with their owner.

    Eviction drops only the live handle; history stays durable. Session, owner and profile are
    stored together so they cannot drift. Sessions with a turn in flight are `pinned`, since
    re-hydrating a second handle would split the in-memory store's thread (`session.state`) and
    the turn's in-process lease; pins come from expiring turn leases. Built on
    `chemclaw.core.bounded.BoundedLru`.
    """

    def __init__(self, capacity: int, pinned: Callable[[str], bool] | None = None) -> None:
        """Create a registry holding at most `capacity` live sessions.

        `pinned` says which session ids must not be evicted right now (default: none); consulted at
        eviction time, so a pin needs no clearing.
        """
        self._entries: BoundedLru[str, LiveSession] = BoundedLru(capacity, pinned=pinned)

    def __len__(self) -> int:
        """How many live sessions are held — the source for the `live_sessions` gauge (DEP-4)."""
        return len(self._entries)

    def add(
        self, session_id: str, session: Any, owner: str | None, profile: str | None = None
    ) -> LiveSession:
        """Register a live session (most-recently-used), evicting the oldest past capacity.

        Returns the stored entry. Eviction skips pinned entries and the one just added (its handle
        is going to the caller). When every candidate is pinned the map briefly exceeds `capacity`:
        turns in flight are bounded far below the cap by admission, and evicting one would corrupt a
        running conversation.
        """
        entry = LiveSession(session=session, owner=owner, profile=profile)
        self._entries.put(session_id, entry)
        return entry

    def get(self, session_id: str) -> "LiveSession | None":
        """Return the live entry for `session_id` (marking it recently used), or None."""
        return self._entries.get(session_id)


class SessionOwners(Protocol):
    """The durable session-ownership registry the front door rehydrates from after a restart.

    A Protocol, so the database-backed `SessionOwnerStore` is imported only on the durable path and
    tests can inject a fake.
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
    ) -> list[tuple[str, datetime, datetime, str | None, str | None]]:
        """`(session_id, created_at, updated_at, title, profile)`, newest activity first.

        Sessions with no messages are not listed (see `_OWNER_LIST` in
        `chemclaw.agent.session_store`). `profile` is what `GET /plans/pending` filters on; `None`
        means the default profile.
        """
        ...


class SessionTurns(Protocol):
    """The durable "who is running a turn on this session" claim.

    A Protocol for the same reason as `SessionOwners`.
    """

    async def claim(
        self, session_id: str, holder: str, lease_seconds: float, *, actor: str | None = None
    ) -> bool:
        """Take the session's turn slot for `lease_seconds`, as `actor`'s turn; False if taken."""
        ...

    async def refresh(self, session_id: str, holder: str, lease_seconds: float) -> bool:
        """Extend this holder's claim; False once the claim is no longer this holder's."""
        ...

    async def release(self, session_id: str, holder: str) -> None:
        """Give the slot back when the turn ends."""
        ...


@runtime_checkable
class TurnAdmission(Protocol):
    """The deployment-wide limits on running turns, kept where every replica sees them.

    Offered by a claim store that holds its leases in a shared database; a store without these
    methods leaves the per-process permits as the only limit.
    """

    async def admit(
        self, session_id: str, holder: str, *, fleet_cap: int, actor: str | None, actor_cap: int
    ) -> bool:
        """Take a concurrent-turn slot for this claim, atomically; False when a limit is reached."""
        ...

    async def actor_turns(self, actor: str, besides: str) -> int:
        """The live turn claims `actor` holds anywhere, on sessions other than `besides`."""
        ...


# This process's identity as a claim holder, fresh per process, so a previous incarnation's leftover
# claims age out rather than being inherited.
_WORKER_ID = uuid.uuid4().hex


def claim_holder(token: str) -> str:
    """This *turn's* identity as a durable claim holder: the process id plus its slot token.

    The token comes from `_claim_turn_slot`. Per-process identity is not enough: a turn whose lease
    lapsed could refresh or release a successor's claim in the same process. With the token the
    durable claim is identity-checked like `_release_turn_slot`.
    """
    return f"{_WORKER_ID}:{token}"


# Refreshes per lease: three, so two consecutive refreshes can fail before the lease is at risk. A
# property of lease maintenance, not a deployment knob.
_CLAIM_REFRESHES_PER_LEASE = 3


@dataclass(frozen=True)
class TurnLease:
    """One session's in-process turn slot: which turn holds it, and until when.

    `token` identity-checks releases. `deadline` is `math.inf` while `post_message`'s `finally` owns
    cleanup, then a wall clock (`_start_turn_lease`). `actor` feeds the per-actor cap (`None` for
    fork/delete holds). `claimed_at` lets that cap age out an un-started reservation stuck on a
    store call, so one wedged call cannot lock a chemist out everywhere.
    """

    token: str
    deadline: float
    actor: str | None
    claimed_at: float


def _claim_turn_slot(
    active_turns: dict[str, TurnLease], session_id: str, *, actor: str | None
) -> str | None:
    """Reserve the in-process one-turn-per-session slot, or report that a live turn holds it.

    Returns this turn's token, or `None` when another turn holds the session. `actor` is
    keyword-only with no default (maintenance holds pass `None`). A lease, not a latch, because one
    window runs no releasing `finally`; its clock starts at hand-off (`_start_turn_lease`). Expired
    entries are swept here. No `await` between test and write.
    """
    now = time.monotonic()
    for stale_id, lease in list(active_turns.items()):
        if lease.deadline <= now:
            del active_turns[stale_id]
    if session_id in active_turns:
        return None
    token = uuid.uuid4().hex
    active_turns[session_id] = TurnLease(
        token=token, deadline=math.inf, actor=actor, claimed_at=now
    )
    return token


def _widest_turn_width() -> float:
    """The widest wall clock a live turn can hold a slot: its timeout plus its admission wait."""
    return settings.service_turn_timeout_seconds + settings.service_turn_admission_timeout_seconds


def _still_holding(lease: TurnLease, now: float) -> bool:
    """Whether `lease` may still belong to a running turn, for the per-actor count only.

    A started lease answers from its `deadline`. An un-started one (`deadline=math.inf`) is aged
    from `claimed_at` at the width `_start_turn_lease` would stamp, so it never expires sooner than
    the lease it would become, and one wedged store call cannot lock a chemist out indefinitely. The
    session's own guard is unchanged.
    """
    if lease.deadline != math.inf:
        return lease.deadline > now
    return lease.claimed_at + _widest_turn_width() > now


def _actor_turns_in_flight(active_turns: dict[str, TurnLease], actor: str, *, besides: str) -> int:
    """How many live turns `actor` is running on sessions other than `besides`.

    Read off the lease map, which sweeps itself. Detached turns count: they gave their admission
    permit back but still spend. `besides` excludes this request's session so a double-submit gets
    the session's own answer rather than a 429; it cannot exceed the cap, since that path creates no
    lease. Expired entries are filtered, not deleted (`_claim_turn_slot` sweeps).
    """
    now = time.monotonic()
    return sum(
        1
        for held_id, lease in active_turns.items()
        if held_id != besides and lease.actor == actor and _still_holding(lease, now)
    )


def _waiting_besides(waiters: dict[tuple[str, str], int], actor: str, *, besides: str) -> int:
    """How many of `actor`'s messages wait in this process, in lines other than `besides`'s.

    A waiting message is a turn its sender will get, so it counts against
    `service_max_concurrent_turns_per_actor`; otherwise messages parked in many shared sessions
    could all start at once. `besides` for `_actor_turns_in_flight`'s reason.
    """
    return sum(
        count
        for (sender, session_id), count in waiters.items()
        if sender == actor and session_id != besides
    )


def _take_event_stream_slot(streams: dict[str, int], actor: str) -> Callable[[], None] | None:
    """Take one of `actor`'s long-lived stream slots, or `None` when a cap is already reached.

    One ledger for every stream a person can hold open indefinitely (push-back streams and followed
    turns), bounded per user (`service_max_event_streams_per_user`) and per pod
    (`service_max_event_streams_total`). No `await` between test and take. Returns an idempotent
    release, so a double end cannot free two slots.
    """
    at_user_cap = streams.get(actor, 0) >= settings.service_max_event_streams_per_user
    if at_user_cap or sum(streams.values()) >= settings.service_max_event_streams_total:
        return None
    streams[actor] = streams.get(actor, 0) + 1
    held = True

    def _release() -> None:
        """Give the slot back, once; drop the actor's entry with their last stream."""
        nonlocal held
        if not held:
            return
        held = False
        remaining = streams.get(actor, 1) - 1
        if remaining <= 0:
            streams.pop(actor, None)
        else:
            streams[actor] = remaining

    return _release


def _start_turn_lease(active_turns: dict[str, TurnLease], session_id: str, token: str) -> None:
    """Start this turn's lease clock, at the moment the request stops owning its cleanup.

    Called just before the streaming response is handed off; from here the deadline is the widest
    wall clock a live turn can hold (admission wait plus turn timeout), so an expired entry belongs
    to no running turn. Identity-checked like the release, and carries `actor` across the restamp so
    the per-actor cap keeps counting the turn.
    """
    lease = active_turns.get(session_id)
    if lease is None or lease.token != token:
        return
    active_turns[session_id] = TurnLease(
        token=token,
        deadline=time.monotonic() + _widest_turn_width(),
        actor=lease.actor,
        claimed_at=lease.claimed_at,
    )


def _release_turn_slot(active_turns: dict[str, TurnLease], session_id: str, token: str) -> None:
    """Give back the slot *this* turn holds — never a successor's.

    Compares the token first, so a teardown arriving after its lease lapsed is a no-op.
    """
    lease = active_turns.get(session_id)
    if lease is not None and lease.token == token:
        del active_turns[session_id]


async def _hold_turn_claim(
    claims: SessionTurns,
    session_id: str,
    lease_seconds: float,
    holder: str,
    fence: TurnFence | None = None,
) -> None:
    """Keep this turn's claim alive for as long as the turn streams.

    Cancelled by the stream's `finally`. A failed refresh is logged and counted, not fatal until a
    whole lease has passed without a successful one: then, like a refresh that shows another worker
    took the session, it loses the `fence`, which ends the turn — a turn that cannot show it holds
    its session must not go on beside whoever resumed it. A refresh is bounded to one interval, so
    that loss comes at most a lease and two intervals after the last success (the lease is counted
    from the refresh that succeeded, the database's expiry from the one before it). This timer ends
    a turn that is doing nothing; an effect is stopped by the ownership check before it and a
    checkpoint by the lock on the claim row, neither of which waits for the timer.
    """
    interval = lease_seconds / _CLAIM_REFRESHES_PER_LEASE
    last_refreshed = time.monotonic()
    while True:
        await asyncio.sleep(interval)
        try:
            if not await asyncio.wait_for(
                claims.refresh(session_id, holder, lease_seconds), timeout=interval
            ):
                # The claim lapsed and another worker took the session (the UPDATE matched no row).
                # No later refresh can succeed.
                METRICS.increment("chemclaw_turn_claims_lost_total")
                logger.warning(
                    "the turn claim for session %s was taken over while the turn was running; "
                    "the turn is ended",
                    session_id,
                )
                if fence is not None:
                    fence.lose()
                return
            last_refreshed = time.monotonic()
        except Exception:
            # Broad: this task is only ever cancelled, never awaited, so an unnamed exception (e.g.
            # `psycopg.Error`) would kill the heartbeat silently and surface as an
            # unretrieved-exception traceback.
            METRICS.increment("chemclaw_turn_claim_refresh_failures_total")
            logger.warning(
                "could not refresh the turn claim for session %s; if this keeps failing the "
                "claim lapses after %ss and another worker may start a turn on this session",
                session_id,
                lease_seconds,
                exc_info=True,
            )
            if fence is not None and time.monotonic() - last_refreshed >= lease_seconds:
                fence.lose()
                return


async def _release_turn_claim(claims: SessionTurns, session_id: str, holder: str) -> None:
    """Give a session's turn slot back, surviving the cancellation that usually causes it.

    `holder` is this turn's (`claim_holder`), so a late teardown cannot revoke a successor.
    Shielded: callers are `finally` blocks running because their task was cancelled, where a bare
    `await` raises at its first suspension and the release would never land, refusing the session's
    owner for a whole lease. Cancellation still propagates to the caller. The lease remains the
    backstop for what shielding cannot cover (a killed process).
    """

    async def _release() -> None:
        """The release itself — the part that must survive, so it owns its own error handling.

        Errors are handled inside the shielded task: once the caller is cancelled nobody retrieves
        its exception, which would surface as an unattributed `Task exception was never retrieved`.
        """
        try:
            await claims.release(session_id, holder)
        except Exception:
            # `Exception`, not a tuple: nobody awaits this task, so anything uncaught (e.g.
            # `psycopg.errors.AdminShutdown` when Postgres stops mid-disconnect) would become an
            # unattributed traceback. A release that cannot happen costs one lease and a log line.
            logger.warning(
                "could not release the turn claim for session %s; it expires on its own",
                session_id,
                exc_info=True,
            )

    await asyncio.shield(_release())


class QueueSignal:
    """Wakes this process's waiting messages the moment the turn ahead of them may have moved.

    Remote waiters poll every `service_turn_queue_poll_seconds`; local events (a turn ending, a
    message leaving or withdrawn) wake local waiters at once. A wake is only a hint to re-ask the
    queue. The event is created lazily per loop, because an `asyncio` primitive binds to its first
    loop and tests drive `app.state` from several; `notify` drops the event it set.
    """

    def __init__(self) -> None:
        """No waiter yet, so no event yet."""
        self._event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def notify(self) -> None:
        """Wake every waiter currently parked in `wait`."""
        if self._event is not None:
            self._event.set()
            self._event = None

    async def wait(self, timeout: float) -> None:
        """Park until the next `notify` or for `timeout` seconds, whichever is first."""
        loop = asyncio.get_running_loop()
        if self._event is None or self._loop is not loop:
            self._event = asyncio.Event()
            self._loop = loop
        event = self._event
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except TimeoutError:
            return


def _default_turn_queue() -> TurnQueue:
    """Each session's line of waiting messages, durable exactly where the turn claim is.

    Durable under `session_store="postgres"`, where replicas share sessions and must share one
    order; in-process otherwise. Never `None`: a second message must wait for the first either way.
    """
    if settings.session_store != "postgres":
        return InMemoryTurnQueue()
    return SessionTurnQueue()


def _default_owner_store() -> SessionOwners | None:
    """The durable session-ownership store, but only when durable sessions are on (else None).

    Without durable history there is nothing to reattach to, so a cache miss stays a 404. Imported
    lazily so the dev/test path never loads psycopg.
    """
    if settings.session_store != "postgres":
        return None
    from chemclaw.agent.session_store import SessionOwnerStore

    return SessionOwnerStore()


def _default_turn_claims() -> SessionTurns | None:
    """The durable turn claim, but only where two processes can share one session (else None).

    Gated on `session_store="postgres"`; under the in-memory store the in-process slot covers
    everything.
    """
    if settings.session_store != "postgres":
        return None
    from chemclaw.agent.session_store import SessionTurnClaims

    return SessionTurnClaims()


class FrontDoorState:
    """A typed, read-through view over the front door's `app.state`.

    Keeps Starlette's untyped `app.state` (`Any`) in one module. Every property reads at access
    time, never a snapshot, because tests replace whole attributes. The two connector-health fields
    have setters for the readiness route; everything else is written only by `create_app`.
    """

    def __init__(self, app: FastAPI) -> None:
        """Wrap `app`, whose `state` this view types."""
        self._app = app

    @property
    def connector_factory(self) -> Callable[[str | None], list[Any]]:
        """Builds one turn's connectors for a profile — called per turn, never cached.

        Typed `list[Any]`: the representation is chosen in
        `chemclaw.agent.chemclaw_agent.connector_specs`, and `run_turn` opens whatever it is handed.
        """
        factory: Callable[[str | None], list[Any]] = self._app.state.connector_factory
        return factory

    @property
    def graph_factory(self) -> Callable[..., Any]:
        """Build one turn's compiled graph; called per turn, never cached."""
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
        """The plan-approval store — the same instance `chemclaw.agent.plan_gate` reads."""
        store: ApprovalStore = self._app.state.plan_approvals
        return store

    @property
    def history(self) -> Any:
        """The session-history provider the agent writes turns through, shared for reads."""
        history: Any = self._app.state.history
        return history

    @property
    def turn_semaphore(self) -> asyncio.Semaphore:
        """The admission-control permit set capping concurrent turns."""
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
    def turn_admission(self) -> TurnAdmission | None:
        """The deployment-wide turn limits, or None where the claim store cannot share them."""
        claims = self.turn_claims
        return claims if isinstance(claims, TurnAdmission) else None

    @property
    def turn_relay(self) -> "TurnRelay | None":
        """How a turn held by another replica is followed and stopped from here, and vice versa.

        `None` exactly where `turn_claims` is: under the in-memory store no other replica holds a
        turn.
        """
        relay: TurnRelay | None = self._app.state.turn_relay
        return relay

    @property
    def turn_queue(self) -> TurnQueue:
        """Each session's line of messages waiting for its running turn to end."""
        queue: TurnQueue = self._app.state.turn_queue
        return queue

    @property
    def queue_signal(self) -> QueueSignal:
        """What wakes this process's waiting messages when the line may have moved."""
        signal: QueueSignal = self._app.state.queue_signal
        return signal

    @property
    def running_turns(self) -> "RunningTurns":
        """The live turns themselves — what the explicit stop route resolves a session against."""
        turns: RunningTurns = self._app.state.running_turns
        return turns

    @property
    def queue_waiters(self) -> dict[tuple[str, str], int]:
        """This process's waiting messages, per `(sender, session)` — the line's local ledger."""
        waiters: dict[tuple[str, str], int] = self._app.state.queue_waiters
        return waiters

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

        Started and awaited by `chemclaw/api/routes/ops.py`. Per app rather than a module global,
        since probes belong to one app and one event loop.
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
    def schema_current(self) -> bool:
        """Whether the schema carries the newest migration this image ships (True until asked).

        Separate from `database_reachable` because "database down" and "image ahead of schema" are
        different outages, and `/readyz`'s body is the operator's diagnosis.
        """
        current: bool = self._app.state.schema_current
        return current

    @schema_current.setter
    def schema_current(self, current: bool) -> None:
        """Store the schema verdict from the same round trip the reachability probe made."""
        self._app.state.schema_current = current

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
