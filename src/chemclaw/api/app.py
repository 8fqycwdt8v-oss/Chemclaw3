"""The ASGI front door (plan step F2-T1/F2-T2): a browser chat surface over the Chemclaw agent.

`create_app` builds a FastAPI app that lets a non-developer chemist open a page, start a
session, and converse with the agent — watching its plan, tool calls, and cited answer stream
in. It owns one agent instance for the process and a per-session `AgentSession` (in-memory for
F2; F3 makes the store durable and adds job→session push-back). The agent factory is injectable
so tests drive the whole app with a fake streaming agent and no live model or credentials.

Routes: `GET /healthz` (liveness), `GET /readyz` (readiness), `POST /sessions` (start a session),
`GET /sessions` (the caller's conversation list), `POST /sessions/{id}/messages` (send a turn,
Server-Sent-Events stream of `chemclaw.api.events`), `GET /sessions/{id}/messages` (read the
transcript
back), and the static chat UI at `/`. Identity (Entra OIDC on every non-health route) is layered
on in F4.
"""

import asyncio
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from fastapi import Depends, FastAPI, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
from sse_starlette.sse import EventSourceResponse
from starlette.datastructures import MutableHeaders
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from chemclaw.agent.agent_pool import AgentPool
from chemclaw.agent.attachments import STORE as ATTACHMENTS
from chemclaw.agent.attachments import AttachmentError, AttachmentSummary, parse_attachment
from chemclaw.agent.chemclaw_agent import build_agent, connector_tools, history_provider
from chemclaw.agent.durable_tools import request_note_reindex
from chemclaw.agent.harness_mode import current_plan_hash, grant_execute, session_mode
from chemclaw.agent.harness_todo import complete_awaiting_job, todo_titles
from chemclaw.agent.interaction_tools import (
    PendingApproval,
    approval_owner,
    approval_status,
    decide_approval,
    list_pending_approvals,
)
from chemclaw.agent.plan_approval_store import PlanApprovalStore
from chemclaw.agent.profile_discovery import load_profiles
from chemclaw.agent.profiles import get_profile
from chemclaw.agent.session_events import stream_new_events
from chemclaw.api.auth import Principal, require_principal
from chemclaw.api.budget import BudgetExceeded, BudgetTracker
from chemclaw.api.events import ErrorEvent, JobCompletedEvent
from chemclaw.api.metrics import CONTENT_TYPE, METRICS
from chemclaw.api.runner import run_turn
from chemclaw.cli.schedules import ScheduleHealth, describe_schedules
from chemclaw.connectors.health import (
    ConnectorHealth,
    check_connectors_at_startup,
    probe_connectors,
)
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.logging import configure_logging, configure_telemetry

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"

# Loopback interfaces: binding here keeps the unauthenticated dev mode reachable only from the
# local host, so it is not a network-exposed footgun. Anything else (notably the "0.0.0.0"
# default) is.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True)
class LiveSession:
    """One live conversation: its MAF session, who owns it, and which profile it runs under.

    A record rather than a tuple because it grew a third field and a fourth is plausible —
    unpacking `(session, owner)` at five call sites was already the kind of thing that breaks
    silently when the shape changes.
    """

    session: Any
    owner: str | None
    profile: str | None = None


class _LiveSessions:
    """A bounded, LRU cache of the front door's live in-process sessions with their owner (COR-3).

    The service keeps the live `AgentSession` object per session id; without a bound this map grows
    for the pod's whole lifetime (a memory leak). This caps it and evicts the least-recently-used
    entry when full — an evicted session's durable history still lives in the session store,
    only the live in-process handle is dropped, so the worst case under memory pressure is a
    client starting a new session. Session, owner and profile are stored together so they can
    never drift: the profile decides which agent runs the turn *and* which connectors it gets,
    so a session that lost it would silently change agent mid-conversation.
    """

    def __init__(self, capacity: int) -> None:
        """Create a registry holding at most `capacity` live sessions."""
        self._capacity = capacity
        self._entries: OrderedDict[str, LiveSession] = OrderedDict()

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
        """
        entry = LiveSession(session=session, owner=owner, profile=profile)
        self._entries[session_id] = entry
        self._entries.move_to_end(session_id)
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)
        return entry

    def get(self, session_id: str) -> "LiveSession | None":
        """Return the live entry for `session_id` (marking it recently used), or None."""
        entry = self._entries.get(session_id)
        if entry is None:
            return None
        self._entries.move_to_end(session_id)
        return entry


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

    async def list_for_owner(self, owner: str | None) -> list[tuple[str, datetime]]:
        """The owner's sessions as `(session_id, created_at)`, newest first."""
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

    async def refresh(self, session_id: str, holder: str, lease_seconds: float) -> None:
        """Extend this holder's claim, so a long turn is not declared dead and stolen from."""
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
            await claims.refresh(session_id, _WORKER_ID, lease_seconds)
        except Exception:  # noqa: BLE001 - a dead heartbeat task is worse than a logged refresh
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
        except Exception:  # noqa: BLE001 - see below; this task must not be able to fail
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


class MessageIn(BaseModel):
    """One turn's user message posted to the messages endpoint."""

    message: str
    # Plan the turn without launching anything expensive (gap IDEA-4). Every expensive path is
    # idempotent and cached, but there was no way to ask "what would you do, what would it cost"
    # without doing it — a natural primitive for a deployment whose default autonomy is
    # `plan_only`.
    dry_run: bool = False

    @field_validator("message")
    @classmethod
    def _bounded(cls, value: str) -> str:
        """Reject a message past the configured cap (SEC-4) — a clean 422, not an unbounded read.

        Read from `settings` at validation time (not as a frozen `Field(max_length=…)`) so the cap
        is genuinely config-driven and adjustable per deployment.
        """
        if len(value) > settings.service_max_message_chars:
            raise ValueError(f"message exceeds the {settings.service_max_message_chars}-char limit")
        return value


class SessionIn(BaseModel):
    """Options for a new session; all optional, so a bodyless `POST /sessions` still works."""

    # Which configured agent this conversation talks to (`agents.profile_discovery`). `None` is
    # the default profile — today's global agent — so an existing client that sends no body is
    # unaffected.
    profile: str | None = None


class SessionOut(BaseModel):
    """The identifier of a freshly created session."""

    session_id: str


class SessionSummary(BaseModel):
    """One of the caller's sessions, for the conversation list."""

    session_id: str
    created_at: datetime


class TranscriptMessage(BaseModel):
    """One stored message of a session's transcript, flattened to what a chat surface renders.

    Role plus text rather than the MAF `Message` shape: the durable row is a MAF serialization,
    and exposing it would make a MAF version bump a breaking change to the HTTP contract.
    """

    role: str
    text: str


class ApprovalDecisionIn(BaseModel):
    """The human Yes/No posted to a pending approval hold."""

    approved: bool


class ApprovalStatusOut(BaseModel):
    """A hold's handle and current state, for a polling review surface."""

    approval_id: str
    status: str


class PlanDecisionIn(BaseModel):
    """The human Yes/No on a harness plan, bound to the exact plan that was shown.

    `plan_hash` is required and is not defaulted to "whatever the plan is now": the whole point of
    the binding is that a plan which changed after being displayed is a different plan. A client
    posts back the hash it received with the plan.
    """

    approved: bool
    plan_hash: str


class PlanStatusOut(BaseModel):
    """The plan a session is currently proposing, its hash, and who (if anyone) approved it."""

    session_id: str
    plan_hash: str
    plan: list[str]
    mode: str
    approved: bool
    decided_by: str | None = None


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open this process's Postgres pool, then probe the connectors once before serving.

    The pool belongs here because it belongs to one process and one event loop, and because
    everything below `chemclaw.core.db.connection` inherits it with no plumbing: the session store,
    the ownership registry, the push-back tailer and the rollback watermark all stop paying a
    TCP+auth handshake per call. That churn — measured at ~2.7 connects per turn — was what made
    a connect fail to be scheduled inside its timeout under load, which silently disarmed the
    non-fatal rollback-watermark guard (D-107).

    The connector probe's *result* only informs (readiness reports it, a gauge counts it) — a
    missing connector costs capability, not correctness, so the default is to serve anyway.
    `connectors_required` is the opt-in inversion: it raises here, which fails startup, for a
    deployment where answering with a silently reduced tool surface is worse than not answering.
    That check belongs at startup rather than in the readiness route because refusing to *start*
    is the only way to keep a pod with degraded capability out of a rollout.
    """
    # First, so everything below is logged the way the operator asked. The front door never
    # configured either of these, so it ran on Python's default root logger (WARNING, no format)
    # while every worker honoured `CHEMCLAW_LOG_LEVEL`/`LOG_FORMAT`, and `CHEMCLAW_OTEL_ENABLED`
    # was simply inert here — the one process a chemist actually talks to was the one with no
    # observability wiring. Here rather than in `create_app` because this is the "about to serve"
    # moment, matching each worker's `main()`.
    configure_logging()
    configure_telemetry()
    # Register the file-authored profiles before any agent is built, so a session can name one
    # on its first request. Failing here is the right outcome for a malformed profile: it is a
    # deployment configuration error, and a front door that started anyway would 400 every
    # request naming that profile with no hint as to why.
    load_profiles()
    async with db.pooling():
        app.state.connector_health = await check_connectors_at_startup()
        app.state.connector_health_at = time.monotonic()
        yield


def _default_agent_factory(profile: str | None) -> Any:
    """Build the agent for one profile — `build_agent` with the profile passed by keyword.

    A named adapter rather than a lambda so the app's default factory has the same one-argument
    shape a test's fake does, and so the signature is somewhere a reader can find it.

    No `audit_sink` argument, deliberately: `chemclaw.agent.audit.default_audit_sink` supplies the
    durable
    GxP trail wherever a database is configured. This line used to be the whole of the finding —
    it passed no sink, so the compliance record was log-only in the one process chemists use.
    Fixing it *here* would have left the same trap set for the Temporal template activities and
    every future entry point, so the default moved to the one place that decides.
    """
    return build_agent(profile=profile)


def create_app(
    agent_factory: Callable[[str | None], Any] = _default_agent_factory,
    owner_store: SessionOwners | None = None,
    connector_factory: Callable[[str | None], list[Any]] = connector_tools,
    turn_claims: SessionTurns | None = None,
) -> FastAPI:
    """Build the front-door FastAPI app.

    Args:
        agent_factory: Builds the agent for one profile name (`None` = the default profile). Called
            once per distinct profile and cached, since an agent is configuration rather than
            per-conversation state. Tests pass a factory returning a fake streaming agent so the
            whole HTTP surface is exercised without a live model.
        owner_store: The durable session-ownership registry used to reattach a client to its session
            after a pod restart. Defaults to the config-gated store (present only under
            `session_store="postgres"`); tests inject an in-memory fake to exercise rehydration
            without a database.
        connector_factory: Builds *this turn's* connector tools for one profile name. A factory
            rather than a list because a connector's connection must belong to a single turn
            (see `chemclaw.agent.chemclaw_agent.connector_tools`), so the app calls it per turn; and
            per-profile because the profile narrows the connector surface as well as the
            in-process one. Injectable for the same reason `agent_factory` is: a test drives the
            whole HTTP surface without a connector server running.
        turn_claims: The durable "one turn at a time per session" claim, which is what makes that
            guard hold across processes rather than only within one (D-121). Defaults to the
            config-gated store (present only under `session_store="postgres"`); tests inject an
            in-memory fake to exercise the cross-process conflict without a database.

    Returns:
        A configured `FastAPI` application.
    """
    _refuse_unauthenticated_exposure()
    app = FastAPI(title="Chemclaw", docs_url=None, redoc_url=None, lifespan=_lifespan)
    _add_security_headers(app)
    _add_cors(app)
    # One handler rather than a try/except per route: every route that touches durable session
    # state can hit the pool, and `chemclaw.db` already funnels both "no database" and "no free
    # connection in time" into `ConnectionError` precisely because a caller cannot act on the
    # difference. See `_database_unavailable`.
    app.add_exception_handler(ConnectionError, _database_unavailable)
    # One agent per process, built lazily on first use so importing the app needs no
    # credentials; per-session threads keep conversations apart. F3 replaces the in-memory
    # session map with a durable store and wires job→session push-back. One agent per profile
    # name, built lazily on first use so importing the app needs no credentials. `None` is the
    # default profile — the key a session gets when it names none.
    app.state.agents = {}
    app.state.agent_factory = agent_factory
    # Called once per turn, not once per process — a connector connection belongs to a single turn.
    app.state.connector_factory = connector_factory
    # Bounded LRU of live sessions, each carrying its owner Entra oid so a session can only be
    # posted to / streamed by its creator (defense-in-depth beyond the unguessable uuid4 id).
    # The bound keeps the map from growing for the pod's lifetime (COR-3).
    app.state.live_sessions = _LiveSessions(settings.service_max_live_sessions)
    # Durable session-ownership registry (F3): the record a restarted front door rehydrates from
    # so a returning client reattaches to its session instead of being forced onto a new one.
    # None with the in-memory session store (nothing durable to reattach to — a cache miss stays
    # a 404).
    app.state.session_owners = owner_store if owner_store is not None else _default_owner_store()
    # The same history provider the agent writes turns through, used read-only to serve a
    # transcript back. Shared rather than re-derived per request: the Postgres provider holds
    # only a DSN and the in-memory one holds nothing at all (its messages live in
    # `session.state`), so one instance is correct for both and neither carries per-session
    # state.
    app.state.plan_approvals = PlanApprovalStore()
    app.state.history = history_provider()
    # One agent — and therefore one chat client — per concurrent turn (D-123). The cached
    # `_agent()` below still serves everything that does not stream; only a streaming turn needs
    # exclusivity, because that is where the Anthropic client keeps tool-call identity on itself.
    app.state.agent_pool = AgentPool(app.state.agent_factory, settings.service_max_concurrent_turns)
    # Admission control on concurrent turns (AG-15): a bounded permit set caps how many turns
    # hit the shared LLM endpoint at once. A permit is held for a turn's whole streamed run; a
    # turn that cannot get one within the admission timeout is shed with 503. Built here so it
    # binds to the app's event loop on first await.
    app.state.turn_semaphore = asyncio.Semaphore(settings.service_max_concurrent_turns)
    # Per-session turn serialization: session ids with a turn currently in flight. Two
    # concurrent turns on one session would drive `agent.run` against the same AgentSession
    # state at once, interleaving two turns' messages in one thread — so a second turn is
    # rejected with 409 while one runs, matching the admission semaphore's shed-don't-queue
    # semantics (a queued turn would silently pin a second permit and still interleave from the
    # user's point of view; a 409 tells the client — a double-submit or a second tab — to wait
    # for the running turn). Check-and-add is atomic on the event loop (no await between them),
    # so the gate has no race window.
    app.state.active_turns = set()
    # The same gate at the width the deployment actually has (D-121). The set above is one
    # process's view, and the chart runs the front door at two replicas, so the second POST can
    # land on a process that has never heard of the first. A leased row in `session_turns` is what
    # both processes can see; None under the in-memory session store, where two processes share no
    # history to corrupt.
    app.state.turn_claims = turn_claims if turn_claims is not None else _default_turn_claims()
    # Per-user count of open push-back event streams. The turn semaphore only guards POSTed
    # turns; each event stream polls the database for its whole lifetime, so without a cap one
    # user's scripted (or abandoned-tab) streams could pile up unbounded DB load. Entries are
    # removed when a user's last stream closes, so the map stays small.
    app.state.event_streams = {}
    # Runaway-cost guard (service.budget): meters each turn's token usage and counts turns per
    # session and per user, refusing a turn (429) that would exceed a configured cap. In-process
    # and off unless `budget_enabled`; the missing ceiling above the per-turn loop cap.
    app.state.budget = BudgetTracker()
    # Gauges read the live structures rather than a mirrored counter, so there is nothing to
    # keep in sync (gap DEP-4). In-flight turns against the cap is the saturation signal the HPA
    # should scale on — CPU is close to noise for a stream-bound, model-latency-dominated
    # service.
    METRICS.bind_gauge("chemclaw_turns_in_flight", lambda: float(len(app.state.active_turns)))
    METRICS.bind_gauge(
        "chemclaw_turn_capacity", lambda: float(settings.service_max_concurrent_turns)
    )
    METRICS.bind_gauge("chemclaw_live_sessions", lambda: float(len(app.state.live_sessions)))
    # Out-of-process capability is a new failure mode, so it gets a signal an operator can alert
    # on. Refreshed by the readiness probe (and at startup), read from the snapshot here — a
    # gauge must not perform network I/O when Prometheus scrapes it.
    app.state.connector_health = []
    # When that snapshot was taken (`time.monotonic`), so the readiness route can reuse it instead
    # of fanning out to the whole connector fleet on every unauthenticated probe. Negative
    # infinity, not 0: an empty snapshot must always be treated as stale, and 0 would be "fresh"
    # for the first `service_readiness_cache_seconds` of process uptime.
    app.state.connector_health_at = float("-inf")
    # Pool saturation (D-119). Read live from the pools rather than mirrored, like every other
    # gauge here; `requests_waiting` above zero is what "the pool is too small" looks like, and it
    # is the only reading that distinguishes it from an unreachable database.
    METRICS.bind_gauge("chemclaw_pg_pool_size", lambda: float(db.pool_stats()["pool_size"]))
    METRICS.bind_gauge(
        "chemclaw_pg_pool_available", lambda: float(db.pool_stats()["pool_available"])
    )
    METRICS.bind_gauge(
        "chemclaw_pg_pool_requests_waiting", lambda: float(db.pool_stats()["requests_waiting"])
    )
    METRICS.bind_gauge(
        "chemclaw_connectors_unhealthy",
        lambda: float(sum(1 for item in app.state.connector_health if item.state == "unreachable")),
    )

    async def _resolve_session(session_id: str, principal: Principal) -> LiveSession:
        """Return the caller's live session — from the cache, or rehydrated from durable ownership.

        A live-cache hit is authorized against its stored owner. On a miss, if durable rehydration
        is on (`session_store="postgres"`), the durable owner is looked up: a session the caller
        owns is rebuilt as a live handle over its persisted history, so a pod restart no longer
        forces the client onto a new session (orphaning its history and unconsumed push-back).
        An unknown session — or one owned by someone else — is a 404 with no existence leak
        either way.
        """
        entry = _live_sessions().get(session_id)
        if entry is not None:
            if entry.owner is not None and entry.owner != principal.oid:
                raise HTTPException(status_code=404, detail="unknown session")
            return entry
        return await _rehydrate_session(session_id, principal)

    async def _rehydrate_session(session_id: str, principal: Principal) -> LiveSession:
        """Rebuild a live session from its durable owner record, or 404 if it cannot reattach."""
        owners: SessionOwners | None = app.state.session_owners
        if owners is None:
            raise HTTPException(status_code=404, detail="unknown session")
        found, owner, profile = await owners.lookup(session_id)
        if not found or (owner is not None and owner != principal.oid):
            raise HTTPException(status_code=404, detail="unknown session")
        # Re-check the cache after the awaited lookup: two racing requests would otherwise each
        # mint a live handle over the same durable thread, and the loser's handle would keep
        # writing outside the cache. The first rehydrator's handle wins; both callers share it.
        entry = _live_sessions().get(session_id)
        if entry is not None:
            return entry
        # The durable history provider reloads the thread on the session's first use, so
        # rebuilding the handle is enough to resume the conversation; register it so later turns
        # hit the cache.
        #
        # On its own profile, not the default (REV-14). This used to come back on the default and
        # was documented as degrading gracefully — "the conversation resumes with the full tool
        # surface rather than a narrowed one". That has the direction backwards: a profile is
        # *attenuation only* (`agents.chemclaw_agent`), so restoring the full surface is a silent
        # widening, and it did not need a restart to happen. The live LRU has a capacity and no
        # TTL, so on a busy pod one session evicts another while both are in use; a chemist
        # mid-conversation regained every tool their profile had removed, having done nothing.
        session = _agent(profile).create_session(session_id=session_id)
        return _live_sessions().add(session_id, session, owner, profile)

    def _plan_approvals() -> PlanApprovalStore:
        """The durable plan-approval store, read through one annotated accessor.

        Built once on `app.state` beside the other stores: it holds only a DSN, so one instance
        serves every request and there is no per-session state to keep straight.
        """
        store: PlanApprovalStore = app.state.plan_approvals
        return store

    def _live_sessions() -> _LiveSessions:
        """The live-session cache, read through one annotated accessor.

        `app.state` is untyped by design in Starlette, so every direct read of it returns `Any` and
        silently disables type checking on whatever it touches. Reading it once here keeps that
        `Any` in a single place instead of leaking into each caller's return type — which is what
        this package had been doing unchecked, because it was missing from `make type` (D-117).
        """
        sessions: _LiveSessions = app.state.live_sessions
        return sessions

    def _agent(profile: str | None = None) -> Any:
        """The process's agent for `profile`, built once and cached under its name.

        One agent per profile rather than one per session: an `Agent` is a configuration (tools,
        instructions, providers), not per-conversation state — the thread lives in the session —
        so two sessions on the same profile share it safely, exactly as every session shared the
        single agent before profiles were selectable. Connectors are the one thing an agent must
        *not* hold
        for the process's lifetime, and it does not
        (`chemclaw.agent.chemclaw_agent.connector_tools`).
        """
        agents: dict[str | None, Any] = app.state.agents
        if profile not in agents:
            agents[profile] = app.state.agent_factory(profile)
        return agents[profile]

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness: the process is up."""
        return {"status": "ok"}

    async def _connector_health() -> list[ConnectorHealth]:
        """The connector sweep, re-probed at most once per `service_readiness_cache_seconds`.

        Monotonic, not wall-clock: a clock adjustment must not make the last sweep look
        arbitrarily fresh. A concurrent second caller inside the window reads the same snapshot;
        two callers racing past the window both probe once, which is a wasted sweep and not a
        correctness problem, so it is not worth a lock on a readiness route.
        """
        window = settings.service_readiness_cache_seconds
        now = time.monotonic()
        if window and now - float(app.state.connector_health_at) < window:
            cached: list[ConnectorHealth] = app.state.connector_health
            return cached
        health = await probe_connectors()
        app.state.connector_health = health
        app.state.connector_health_at = now
        return health

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        """Readiness: the agent can be built, plus each enabled connector's reachability.

        The connector states are *reported*, not required: an unreachable connector costs the
        agent that capability, and hiding it would leave a chemist wondering why an answer got
        worse. It is re-probed here rather than read from a startup snapshot so the answer is
        current, and the probe also refreshes the `chemclaw_connectors_unhealthy` gauge — a
        readiness probe runs on the cadence a gauge wants anyway, so one bounded sweep serves
        both. A deployment that would rather not serve at all in this state sets
        `connectors_required`, which fails startup instead.

        The sweep is cached for `service_readiness_cache_seconds`. This route is unauthenticated
        by necessity (a kubelet cannot present a token) and runs every 10 seconds per pod, so an
        uncached probe is a fan-out any caller can trigger at will — N HTTP round trips per
        request against the connector fleet. Caching does not weaken the signal: the connector
        states are reported, never gating, so the only cost is that a reported state can be up to
        one window stale. Set 0 to probe every time.
        """
        _agent()
        health = await _connector_health()
        return {
            "status": "ready",
            "connectors": ", ".join(f"{item.name}={item.state}" for item in health),
        }

    @app.post("/sessions")
    async def create_session(
        body: SessionIn | None = None,
        principal: Principal = Depends(require_principal),
    ) -> SessionOut:
        """Start a new conversation session and return its id (requires an authenticated user).

        An optional `profile` picks which configured agent the session talks to — the selection
        step that makes a filesystem-authored profile reachable by a user instead of only by a
        redeploy. It is resolved here so an unknown name is a 400 at session creation rather
        than a 500 on the first turn, and it is fixed for the session's life: a conversation
        whose instructions and tools changed underneath it would have a thread that no longer
        matches its own history.
        """
        session_id = uuid.uuid4().hex
        profile = body.profile if body is not None else None
        if profile is not None:
            try:
                # Resolved here rather than left to the factory: whether a profile name exists
                # is a property of the registry, not of how this deployment builds agents, and a
                # test's injected factory must not be able to make an unknown name look valid.
                get_profile(profile)
            except ValueError as exc:  # a caller error, not a server fault
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        agent = _agent(profile)
        # Persist ownership first (durable path only), so the session reattaches after a restart
        # even if the pod dies before the first turn writes any history.
        if app.state.session_owners is not None:
            await app.state.session_owners.record(session_id, principal.oid, profile)
        app.state.live_sessions.add(
            session_id, agent.create_session(session_id=session_id), principal.oid, profile
        )
        return SessionOut(session_id=session_id)

    @app.get("/sessions")
    async def list_sessions(
        principal: Principal = Depends(require_principal),
    ) -> list[SessionSummary]:
        """The caller's own sessions, newest first — the conversation list.

        Without this a client that lost its local state (a new browser, cleared storage, a
        second device) could not find sessions it still owns: ids are minted server-side and
        returned once into the response that created them, so an id the client forgot was
        unreachable forever
        while its durable history sat in the store.

        Read from the durable ownership registry, which is the same record `_resolve_session`
        authorizes against — so this can never list a session the caller would then be refused.
        Empty under the in-memory session store: there is no durable registry to enumerate, and
        reporting the process's live LRU instead would answer a question about the deployment
        with a partial, eviction-dependent guess.
        """
        owners: SessionOwners | None = app.state.session_owners
        if owners is None:
            return []
        return [
            SessionSummary(session_id=session_id, created_at=created_at)
            for session_id, created_at in await owners.list_for_owner(principal.oid)
        ]

    @app.get("/sessions/{session_id}/messages")
    async def get_messages(
        session_id: str,
        principal: Principal = Depends(require_principal),
    ) -> list[TranscriptMessage]:
        """One session's stored transcript, in order — what a client reads back after a reload.

        Ownership-gated by the same `_resolve_session` the turn route uses, so a transcript is
        readable only by the chemist whose session it is (a non-owner gets the same 404 as an
        unknown id, leaking nothing about which ids exist).

        Read through the agent's own history provider rather than by querying `session_messages`:
        one reader means the write path and the read path cannot drift, and the route works
        unchanged under either store — the in-memory provider keeps its messages in
        `session.state`, which is exactly what `_resolve_session` just returned.
        """
        live = await _resolve_session(session_id, principal)
        stored = await app.state.history.get_messages(session_id, state=live.session.state)
        return [TranscriptMessage(role=message.role, text=message.text) for message in stored]

    @app.post("/sessions/{session_id}/messages")
    async def post_message(
        session_id: str,
        body: MessageIn,
        principal: Principal = Depends(require_principal),
    ) -> EventSourceResponse:
        """Run one turn for the session and stream its events as SSE.

        Admission-controlled (AG-15): the turn takes one of the process's turn permits for its
        whole streamed run, and is shed with 503 if none frees within the admission timeout — so
        a burst of concurrent turns cannot pile onto the shared internal LLM endpoint. The permit
        hold is wall-clock bounded (`service_turn_timeout_seconds`): a hung model stream or a
        slow-reading client cannot pin a permit forever — on expiry the client gets one error
        event and the permit is released.

        **One turn at a time per session**, claimed twice. The in-process `active_turns` set
        answers a double-submit that lands on this same process with no I/O and no race window
        (there is no `await` between the test and the add). The durable claim in `session_turns`
        answers the case that set cannot see: the shipped chart runs two front-door replicas, so
        the second POST may arrive at a different process entirely, and both would otherwise be
        admitted and interleave their messages into one conversation thread. Both answer 409.
        The durable half is present only under `session_store="postgres"` — with the in-memory
        store there is no shared history for two processes to corrupt.
        """
        live = await _resolve_session(session_id, principal)
        active_turns: set[str] = app.state.active_turns
        claims: SessionTurns | None = app.state.turn_claims
        lease = settings.service_turn_claim_lease_seconds
        if session_id in active_turns:
            METRICS.increment("chemclaw_turns_conflict_total")
            raise HTTPException(
                status_code=409, detail="a turn is already running for this session"
            )
        active_turns.add(session_id)
        semaphore = app.state.turn_semaphore

        async def _turn_events() -> AsyncIterator[dict[str, str]]:
            # Release the permit and the session's turn slot when the stream ends — normal
            # completion, error, timeout, or client disconnect (the generator is closed, running
            # this finally) — so neither is ever leaked.
            heartbeat = (
                None
                if claims is None
                else asyncio.create_task(_hold_turn_claim(claims, session_id, lease))
            )
            try:
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
                        app.state.agent_pool.lease(live.profile) as turn_agent,
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
                            budget=app.state.budget,
                            dry_run=body.dry_run,
                            connectors=app.state.connector_factory(live.profile),
                            history=app.state.history,
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
                        )
                    )
                    yield {"event": timeout_event.type, "data": timeout_event.model_dump_json()}
            finally:
                if heartbeat is not None:
                    heartbeat.cancel()
                semaphore.release()
                active_turns.discard(session_id)
                if claims is not None:
                    await _release_turn_claim(claims, session_id)

        acquired = False
        claimed = False
        handed_off = False
        try:
            # Runaway-cost guard (budget #3): refuse before taking a permit if this session/user
            # has exhausted its turn or token budget — a clean 429, not a started-then-killed
            # turn.
            try:
                app.state.budget.check(session_id, principal.oid)
            except BudgetExceeded as exc:
                METRICS.increment("chemclaw_turns_refused_budget_total")
                raise HTTPException(status_code=429, detail=str(exc)) from exc
            # Claimed before the permit rather than after, so a turn that is already running
            # elsewhere is refused without first occupying one of this process's permits for the
            # duration of the admission wait. A failed checkout raises `ConnectionError` and is
            # shed as a 503 by `_database_unavailable` — the guard fails closed, and retryably.
            if claims is not None and not await claims.claim(session_id, _WORKER_ID, lease):
                METRICS.increment("chemclaw_turns_conflict_total")
                raise HTTPException(
                    status_code=409, detail="a turn is already running for this session"
                )
            claimed = claims is not None
            try:
                await asyncio.wait_for(
                    semaphore.acquire(), timeout=settings.service_turn_admission_timeout_seconds
                )
            except TimeoutError as exc:
                # Shedding is the admission control working as designed — and was completely
                # invisible from outside until this counter existed.
                METRICS.increment("chemclaw_turns_shed_total")
                raise HTTPException(
                    status_code=503, detail="server at capacity; retry shortly"
                ) from exc
            acquired = True
            METRICS.increment("chemclaw_turns_started_total")
            response = EventSourceResponse(_turn_events())
            handed_off = True
            return response
        finally:
            # try/finally, not `except Exception`: cancellation (a client gone mid-admission) is
            # a BaseException, and missing it here leaked the session's active-turns entry —
            # 409-bricking the session until restart. Until the streaming response is handed
            # off, this owns the cleanup; afterwards the generator's own finally does.
            if not handed_off:
                active_turns.discard(session_id)
                if acquired:
                    semaphore.release()
                if claimed and claims is not None:
                    await _release_turn_claim(claims, session_id)

    @app.get("/sessions/{session_id}/events")
    async def session_events(
        session_id: str,
        principal: Principal = Depends(require_principal),
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
        await _resolve_session(session_id, principal)
        streams: dict[str, int] = app.state.event_streams
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
                async for pushed in stream_new_events(session_id, kinds=("job_completed",)):
                    job_id = str(pushed.payload.get("job_id", ""))
                    # Flip the harness todo that was waiting on this job (F3-T3 follow-up), so
                    # the session's *next* turn sees it as done instead of open forever. The
                    # live session may already be gone from the LRU cache (`_owned_session`
                    # above only required it to exist when this stream *started*) — a miss here
                    # is a safe no-op, matching `complete_awaiting_job`'s own no-op-on-miss
                    # contract.
                    if settings.harness_enabled:
                        live_entry = app.state.live_sessions.get(session_id)
                        if live_entry is not None:
                            await complete_awaiting_job(
                                live_entry.session, job_id, reason=f"QM job {job_id} completed"
                            )
                    event = JobCompletedEvent(job_id=job_id, summary=pushed.payload)
                    yield {"event": event.type, "data": event.model_dump_json()}
            finally:
                _release_stream_slot()

        handed_off = False
        try:
            response = EventSourceResponse(_events())
            handed_off = True
            return response
        finally:
            # Mirrors the turn route: any BaseException before the response is handed off must
            # return the slot, or the user's stream budget leaks toward a permanent 429.
            if not handed_off:
                _release_stream_slot()

    async def _owned_approval(approval_id: str, principal: Principal) -> None:
        """Authorize the caller against a hold's owner, or 404 (no existence leak either way).

        Mirrors `_resolve_session`: an unknown hold and someone else's hold are indistinguishable
        from outside. The dev path (`entra_required` off) has no real actor, so an unowned hold
        stays answerable — matching how every other route degrades in dev.
        """
        try:
            owner = await approval_owner(approval_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="no such approval hold") from exc
        if owner and owner != principal.oid:
            raise HTTPException(status_code=404, detail="no such approval hold")

    @app.post("/sessions/{session_id}/attachments")
    async def upload_attachment(
        session_id: str,
        file: UploadFile,
        principal: Principal = Depends(require_principal),
    ) -> AttachmentSummary:
        """Attach a working file to a conversation (gap AGT-3).

        The only way data entered the system was the scheduled ELN sync, so a chemist could not
        hand over a CSV of runs or an SOP — the highest-frequency real request for a lab
        assistant.

        Session-scoped and in-memory by design: an attachment is working material for a
        conversation, not knowledge. Anything in it worth keeping goes through the PR-gate like
        every other machine-touched write; routing uploads into the graph would bypass the GxP
        line.

        Unsupported formats are refused with a message naming what *is* supported (422), never
        silently half-parsed — a PDF "read" by scraping whatever bytes look like text would
        produce confident nonsense a chemist could not tell from a real reading.
        """
        await _resolve_session(session_id, principal)
        raw = await file.read()
        try:
            attachment = parse_attachment(file.filename or "upload", raw, file.content_type)
        except AttachmentError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        ATTACHMENTS.add(session_id, attachment)
        return AttachmentSummary(
            name=attachment.name,
            content_type=attachment.content_type,
            rows=attachment.rows,
            excerpt=attachment.text[: settings.note_excerpt_chars],
        )

    @app.post("/events/knowledge-merged", status_code=202)
    async def knowledge_merged(
        principal: Principal = Depends(require_principal),
    ) -> dict[str, str]:
        """Tell the deployment a note merged, so freshness stops being bounded by a timer (SCH-6).

        The whole system was poll-on-a-timer: there was no inbound event path at all, so the
        worst-case staleness of a merged note was the slowest configured interval, everywhere. A
        git host's post-merge webhook (or an operator) calls this, and the derived note index is
        rebuilt now rather than at the next scheduled sweep — collapsing gap SCH-2's staleness
        window from an interval to seconds.

        Idempotent and cheap to over-call: the reindex is an upsert, and a duplicate delivery
        just rebuilds an already-current index. Authenticated like every other non-health route.
        """
        started = await request_note_reindex()
        return {"status": "accepted", "workflow_id": started}

    @app.get("/metrics")
    async def metrics() -> Response:
        """Prometheus exposition for this pod (gap DEP-4).

        Unauthenticated on purpose, like `/healthz` and `/readyz`: a scrape happens before and
        independently of user identity, and the NetworkPolicy is what keeps it inside the
        cluster. It exposes counts and capacity only — never a session id, a user, or any turn
        content.
        """
        return Response(content=METRICS.render(), media_type=CONTENT_TYPE)

    @app.get("/schedules")
    async def schedules(
        principal: Principal = Depends(require_principal),
    ) -> list[ScheduleHealth]:
        """Health of every periodic job: when it last ran, and whether it succeeded (gap SCH-4).

        Nothing reported this, so an ELN sync failing every run advanced no cursor and raised no
        alarm — it surfaced weeks later as "the agent doesn't know about recent experiments",
        the hardest class of problem to attribute.

        Read from Temporal's own schedule state rather than a second table: Temporal is already
        the authority on when a Schedule fired and how the run ended, and a mirrored table could
        only ever drift from it.
        """
        return await describe_schedules()

    @app.get("/approvals")
    async def list_approvals(
        principal: Principal = Depends(require_principal),
    ) -> list[PendingApproval]:
        """The caller's open approval holds — the review queue (gap RCH-3).

        Without this route the durable Yes/No hold (D-032) was a dead end: a hold could be
        started, but its id was only ever returned into a turn that then ended, and the thin UI
        rendered the request as an inert trace line. A hold that nobody can find or answer can
        only time out, which silently drops the knowledge it was holding.

        Scoped to the caller: a hold authorizes a knowledge write, so it is answerable only by
        the chemist whose turn raised it.
        """
        return await list_pending_approvals(owner=principal.oid)

    @app.get("/approvals/{approval_id}")
    async def get_approval(
        approval_id: str,
        principal: Principal = Depends(require_principal),
    ) -> ApprovalStatusOut:
        """One hold's current state (`pending`/`approved`/`rejected`/`expired`)."""
        await _owned_approval(approval_id, principal)
        try:
            return ApprovalStatusOut(
                approval_id=approval_id, status=await approval_status(approval_id)
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="no such approval hold") from exc

    @app.post("/approvals/{approval_id}/decision", status_code=204)
    async def decide(
        approval_id: str,
        body: ApprovalDecisionIn,
        principal: Principal = Depends(require_principal),
    ) -> Response:
        """Deliver the human Yes/No to a pending hold — the button click, finally wired.

        Deliberately an HTTP route and **not** an agent tool: the agent proposes, a human signs
        off (D-005). A tool would let the agent approve its own candidate and collapse the GxP
        line the whole PR-gate exists to draw.
        """
        await _owned_approval(approval_id, principal)
        try:
            await decide_approval(approval_id, body.approved)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="no such approval hold") from exc
        return Response(status_code=204)

    @app.get("/sessions/{session_id}/plan")
    async def get_plan(
        session_id: str,
        principal: Principal = Depends(require_principal),
    ) -> PlanStatusOut:
        """The plan awaiting a decision, with the hash a client must post back to approve it."""
        live = await _resolve_session(session_id, principal)
        plan = await todo_titles(live.session)
        plan_hash = await current_plan_hash(live.session)
        decision = await _plan_approvals().decision(session_id, plan_hash)
        return PlanStatusOut(
            session_id=session_id,
            plan_hash=plan_hash,
            plan=plan,
            mode=session_mode(live.session),
            approved=bool(decision and decision[0]),
            decided_by=decision[1] if decision else None,
        )

    @app.post("/sessions/{session_id}/plan/decision", status_code=204)
    async def decide_plan(
        session_id: str,
        body: PlanDecisionIn,
        principal: Principal = Depends(require_principal),
    ) -> Response:
        """Approve (or reject) a harness plan — the pre-execution GxP gate, finally enforced.

        Deliberately an HTTP route and **not** an agent tool, for the same reason
        `POST /approvals/{id}/decision` is not (D-005): MAF advertises a `mode_set` tool to the
        model by default, so until this existed the agent moved itself out of plan mode and the
        audit trail recorded that under the asking chemist's identity. `PlanApprovalModeProvider`
        retracts that tool; this is the only remaining path into execute mode.

        The posted `plan_hash` must match the plan the session is proposing *now*. A mismatch is a
        409, not a silent approval of the current plan: it means the plan changed between being
        shown and being approved, and the human agreed to something else.
        """
        live = await _resolve_session(session_id, principal)
        plan_hash = await current_plan_hash(live.session)
        if body.plan_hash != plan_hash:
            raise HTTPException(
                status_code=409,
                detail="the plan changed since it was shown; re-read it and decide again",
            )
        await _plan_approvals().record(session_id, plan_hash, principal.oid or "", body.approved)
        if body.approved:
            grant_execute(live.session)
        return Response(status_code=204)

    if _STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")

    return app


# CSP for the self-served chat UI (SEC-5): everything is same-origin except the one inline
# <style> block in index.html (so style-src needs 'unsafe-inline') and data: images; app.js is
# external (script-src 'self') and the SSE stream is same-origin (connect-src 'self'). base-uri
# and frame-ancestors are locked down to blunt injection and clickjacking.
_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'"
)

# The full header set, as the `(name, value)` pairs the ASGI response-start message wants. A
# tuple rather than four `setdefault` calls so adding a header is one line and the middleware
# stays a loop.
_SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("Content-Security-Policy", _CONTENT_SECURITY_POLICY),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Strict-Transport-Security", "max-age=63072000; includeSubDomains"),
)


async def _database_unavailable(request: Request, exc: Exception) -> Response:
    """Turn a failed Postgres checkout into a retryable 503 instead of an unhandled 500.

    `create_session` writes the session's owner row before returning an id, so it needs a
    connection; under load 16 of those writes raised `psycopg_pool.PoolTimeout` and, with no
    handler anywhere, became HTTP 500s. A 500 tells a client "this request is broken, do not
    retry" — the opposite of the truth. The pool was not even exhausted: it held 13 of a
    permitted 64 connections and opened none during the run, so the callers were waiting for a
    connection that was *available* and could not be handed to them, which is the same event-loop
    starvation that used to show up as a connect timeout.

    Answered with the admission path's wording on purpose. "Server at capacity; retry shortly" is
    what a shed turn already says, the client behaviour is identical (back off and retry), and a
    browser has no business learning which piece of infrastructure is behind it — while a
    misconfigured DSN still names itself loudly in the log line below.
    """
    METRICS.increment("chemclaw_db_unavailable_total")
    logger.warning("shedding %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=503, content={"detail": "server at capacity; retry shortly"})


def _refuse_unauthenticated_exposure() -> None:
    """Fail closed when the app would run unauthenticated (`entra_required` off) network-exposed.

    With `entra_required` False every request is the shared dev principal and all authorization
    gates are open (SEC-2) — intended for local dev only. Binding that mode to a non-loopback
    interface (the `service_host="0.0.0.0"` default) exposes it to the network, so the service
    refuses to boot rather than leaving the whole deployment's safety to one env var defaulting
    the insecure way (the earlier warn-and-boot was one missed log line from an open
    deployment).
    `service_allow_insecure=true` is the explicit, conscious opt-out — it boots with the loud
    warning instead. Loopback dev and Entra-enforced deployments are untouched.
    """
    if settings.entra_required or settings.service_host in _LOOPBACK_HOSTS:
        return
    if not settings.service_allow_insecure:
        raise RuntimeError(
            "SECURITY: entra_required is False but the service binds a non-loopback interface "
            f"({settings.service_host!r}) — every request would run as the shared dev principal "
            "with all authorization gates OPEN. Set CHEMCLAW_ENTRA_REQUIRED=true for any shared/"
            "exposed deployment, bind a loopback interface for local dev, or set "
            "CHEMCLAW_SERVICE_ALLOW_INSECURE=true to explicitly accept an unauthenticated, "
            "network-exposed service."
        )
    logger.warning(
        "SECURITY: entra_required is False but the service binds a non-loopback interface (%r) — "
        "every request runs as the shared dev principal with all authorization gates OPEN "
        "(service_allow_insecure=true). Set CHEMCLAW_ENTRA_REQUIRED=true for any shared/exposed "
        "deployment.",
        settings.service_host,
    )


class _SecurityHeaders:
    """Stamp the browser security headers onto every response — pure ASGI, never buffering (SEC-5).

    Pure ASGI rather than `BaseHTTPMiddleware`, which is what this used to be. That wrapper runs
    the downstream app as a *second task* and pipes its ASGI messages through a memory stream, so
    a request that ends without ever sending a response — a client that gives up while waiting
    for an admission permit, a pod draining mid-stream on a rolling deploy, anything that
    cancels the handler — reaches `call_next` as a closed stream and is re-raised as
    `RuntimeError("No response returned.")`: a 500 with a traceback where the honest outcome is
    a closed connection. A 50-user load run logged 44 of them, every one on the SSE turn route,
    and the same wrapper is why an `EventSourceResponse` cannot be run under more than one
    uvicorn worker safely.

    This wraps only `send`, mutating the `http.response.start` headers in place. The body is
    never re-tasked, never buffered, and a long-lived SSE stream is byte-for-byte what the route
    produced.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap `app`, the rest of the ASGI stack below this middleware."""
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass the call through, adding the headers to the response-start message.

        Non-HTTP scopes (lifespan, websocket) carry no response headers, so they pass straight
        through — a middleware that assumed `http` would break startup.
        """
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in _SECURITY_HEADERS:
                    # setdefault, so a route that deliberately sets its own policy still wins.
                    headers.setdefault(name, value)
            await send(message)

        await self._app(scope, receive, _send)


def _add_security_headers(app: FastAPI) -> None:
    """Add the browser security headers to every response, when `service_security_headers` is on.

    Off only when a deployment fronts its own header policy at the ingress/Route; on by default
    so the app is safe standalone. The headers are static, so one pure-ASGI middleware sets them
    on every response (including static files and errors) without touching the route handlers.
    """
    if settings.service_security_headers:
        app.add_middleware(_SecurityHeaders)


def _add_cors(app: FastAPI) -> None:
    """Apply the configured CORS allow-list (empty = no cross-origin access, the safe default)."""
    origins = [o.strip() for o in settings.service_cors_origins.split(",") if o.strip()]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )
