"""The ASGI front door (plan step F2-T1/F2-T2): a browser chat surface over the Chemclaw agent.

`create_app` builds a FastAPI app that lets a non-developer chemist open a page, start a session,
and converse with the agent — watching its plan, tool calls, and cited answer stream in. It owns one
agent instance for the process and a per-session `AgentSession` (in-memory for F2; F3 makes the
store durable and adds job→session push-back). The agent factory is injectable so tests drive the
whole app with a fake streaming agent and no live model or credentials.

Routes: `GET /healthz` (liveness), `GET /readyz` (readiness), `POST /sessions` (start a session),
`POST /sessions/{id}/messages` (send a turn, Server-Sent-Events stream of `service.events`), and the
static chat UI at `/`. Identity (Entra OIDC on every non-health route) is layered on in F4.
"""

import asyncio
import logging
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Protocol

from fastapi import Depends, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
from sse_starlette.sse import EventSourceResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import Response

from agents.chemclaw_agent import build_agent
from agents.harness_todo import complete_awaiting_job
from agents.session_events import stream_new_events
from chemclaw.config import settings
from service.auth import Principal, require_principal
from service.budget import BudgetExceeded, BudgetTracker
from service.events import ErrorEvent, JobCompletedEvent
from service.runner import run_turn

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"

# Loopback interfaces: binding here keeps the unauthenticated dev mode reachable only from the local
# host, so it is not a network-exposed footgun. Anything else (notably the "0.0.0.0" default) is.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class _LiveSessions:
    """A bounded, LRU cache of the front door's live in-process sessions with their owner (COR-3).

    The service keeps the live `AgentSession` object per session id; without a bound this map grows
    for the pod's whole lifetime (a memory leak). This caps it and evicts the least-recently-used
    entry when full — an evicted session's durable history still lives in the session store, only
    the live in-process handle is dropped, so the worst case under memory pressure is a client
    starting a new session. Session and owner are stored together so the two can never drift.
    """

    def __init__(self, capacity: int) -> None:
        """Create a registry holding at most `capacity` live sessions."""
        self._capacity = capacity
        self._entries: OrderedDict[str, tuple[Any, str | None]] = OrderedDict()

    def add(self, session_id: str, session: Any, owner: str | None) -> None:
        """Register a live session (most-recently-used), evicting the oldest past capacity."""
        self._entries[session_id] = (session, owner)
        self._entries.move_to_end(session_id)
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)

    def get(self, session_id: str) -> tuple[Any, str | None] | None:
        """Return the `(session, owner)` for `session_id` (marking it recently used), or None."""
        entry = self._entries.get(session_id)
        if entry is None:
            return None
        self._entries.move_to_end(session_id)
        return entry


class SessionOwners(Protocol):
    """The durable session-ownership registry the front door rehydrates from after a restart (F3).

    Kept as a Protocol so the concrete `agents.session_store.SessionOwnerStore` (which needs a
    database) is imported only on the durable path, and a test can inject an in-memory fake.
    """

    async def record(self, session_id: str, owner: str | None) -> None:
        """Record a session's owner at creation (idempotent)."""
        ...

    async def lookup(self, session_id: str) -> tuple[bool, str | None]:
        """Return `(found, owner)` for a session id — `(False, None)` when unknown."""
        ...


def _default_owner_store() -> SessionOwners | None:
    """The durable session-ownership store, but only when durable sessions are on (else None).

    Rehydration is meaningful only when there is durable history to resume, so it is gated on the
    same `session_store="postgres"` switch: under the in-memory store there is nothing to reattach
    to and a cache miss stays a 404 (today's behavior). Imported lazily so the dev/test path never
    pulls in psycopg for a store it will not use.
    """
    if settings.session_store != "postgres":
        return None
    from agents.session_store import SessionOwnerStore

    return SessionOwnerStore()


class MessageIn(BaseModel):
    """One turn's user message posted to the messages endpoint."""

    message: str

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


class SessionOut(BaseModel):
    """The identifier of a freshly created session."""

    session_id: str


def create_app(
    agent_factory: Callable[[], Any] = build_agent,
    owner_store: SessionOwners | None = None,
) -> FastAPI:
    """Build the front-door FastAPI app.

    Args:
        agent_factory: Builds the process's agent. Defaults to `build_agent` (the config-selected
            provider); tests pass a factory returning a fake streaming agent so the whole HTTP
            surface is exercised without a live model.
        owner_store: The durable session-ownership registry used to reattach a client to its session
            after a pod restart. Defaults to the config-gated store (present only under
            `session_store="postgres"`); tests inject an in-memory fake to exercise rehydration
            without a database.

    Returns:
        A configured `FastAPI` application.
    """
    _refuse_unauthenticated_exposure()
    app = FastAPI(title="Chemclaw", docs_url=None, redoc_url=None)
    _add_security_headers(app)
    _add_cors(app)
    # One agent per process, built lazily on first use so importing the app needs no credentials;
    # per-session threads keep conversations apart. F3 replaces the in-memory session map with a
    # durable store and wires job→session push-back.
    app.state.agent = None
    app.state.agent_factory = agent_factory
    # Bounded LRU of live sessions, each carrying its owner Entra oid so a session can only be
    # posted to / streamed by its creator (defense-in-depth beyond the unguessable uuid4 id). The
    # bound keeps the map from growing for the pod's lifetime (COR-3).
    app.state.live_sessions = _LiveSessions(settings.service_max_live_sessions)
    # Durable session-ownership registry (F3): the record a restarted front door rehydrates from so
    # a returning client reattaches to its session instead of being forced onto a new one. None with
    # the in-memory session store (nothing durable to reattach to — a cache miss stays a 404).
    app.state.session_owners = owner_store if owner_store is not None else _default_owner_store()
    # Admission control on concurrent turns (AG-15): a bounded permit set caps how many turns hit
    # the shared LLM endpoint at once. A permit is held for a turn's whole streamed run; a turn that
    # cannot get one within the admission timeout is shed with 503. Built here so it binds to the
    # app's event loop on first await.
    app.state.turn_semaphore = asyncio.Semaphore(settings.service_max_concurrent_turns)
    # Per-session turn serialization: session ids with a turn currently in flight. Two concurrent
    # turns on one session would drive `agent.run` against the same AgentSession state at once,
    # interleaving two turns' messages in one thread — so a second turn is rejected with 409 while
    # one runs, matching the admission semaphore's shed-don't-queue semantics (a queued turn would
    # silently pin a second permit and still interleave from the user's point of view; a 409 tells
    # the client — a double-submit or a second tab — to wait for the running turn). Check-and-add is
    # atomic on the event loop (no await between them), so the gate has no race window.
    app.state.active_turns = set()
    # Per-user count of open push-back event streams. The turn semaphore only guards POSTed turns;
    # each event stream polls the database for its whole lifetime, so without a cap one user's
    # scripted (or abandoned-tab) streams could pile up unbounded DB load. Entries are removed when
    # a user's last stream closes, so the map stays small.
    app.state.event_streams = {}
    # Runaway-cost guard (service.budget): meters each turn's token usage and counts turns per
    # session and per user, refusing a turn (429) that would exceed a configured cap. In-process and
    # off unless `budget_enabled`; the missing ceiling above the per-turn loop cap.
    app.state.budget = BudgetTracker()

    async def _resolve_session(session_id: str, principal: Principal) -> Any:
        """Return the caller's session — from the live cache, or rehydrated from durable ownership.

        A live-cache hit is authorized against its stored owner. On a miss, if durable rehydration
        is on (`session_store="postgres"`), the durable owner is looked up: a session the caller
        owns is rebuilt as a live handle over its persisted history, so a pod restart no longer
        forces the client onto a new session (orphaning its history and unconsumed push-back). An
        unknown session — or one owned by someone else — is a 404 with no existence leak either way.
        """
        entry = app.state.live_sessions.get(session_id)
        if entry is not None:
            session, owner = entry
            if owner is not None and owner != principal.oid:
                raise HTTPException(status_code=404, detail="unknown session")
            return session
        return await _rehydrate_session(session_id, principal)

    async def _rehydrate_session(session_id: str, principal: Principal) -> Any:
        """Rebuild a live session from its durable owner record, or 404 if it cannot reattach."""
        owners: SessionOwners | None = app.state.session_owners
        if owners is None:
            raise HTTPException(status_code=404, detail="unknown session")
        found, owner = await owners.lookup(session_id)
        if not found or (owner is not None and owner != principal.oid):
            raise HTTPException(status_code=404, detail="unknown session")
        # Re-check the cache after the awaited lookup: two racing requests would otherwise each
        # mint a live handle over the same durable thread, and the loser's handle would keep
        # writing outside the cache. The first rehydrator's handle wins; both callers share it.
        entry = app.state.live_sessions.get(session_id)
        if entry is not None:
            return entry[0]
        # The durable history provider reloads the thread on the session's first use, so rebuilding
        # the handle is enough to resume the conversation; register it so later turns hit the cache.
        session = _agent().create_session(session_id=session_id)
        app.state.live_sessions.add(session_id, session, owner)
        return session

    def _agent() -> Any:
        if app.state.agent is None:
            app.state.agent = app.state.agent_factory()
        return app.state.agent

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness: the process is up."""
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        """Readiness: the agent can be built (config/provider resolves)."""
        _agent()
        return {"status": "ready"}

    @app.post("/sessions")
    async def create_session(
        principal: Principal = Depends(require_principal),
    ) -> SessionOut:
        """Start a new conversation session and return its id (requires an authenticated user)."""
        session_id = uuid.uuid4().hex
        # Persist ownership first (durable path only), so the session reattaches after a restart
        # even if the pod dies before the first turn writes any history.
        if app.state.session_owners is not None:
            await app.state.session_owners.record(session_id, principal.oid)
        app.state.live_sessions.add(
            session_id, _agent().create_session(session_id=session_id), principal.oid
        )
        return SessionOut(session_id=session_id)

    @app.post("/sessions/{session_id}/messages")
    async def post_message(
        session_id: str,
        body: MessageIn,
        principal: Principal = Depends(require_principal),
    ) -> EventSourceResponse:
        """Run one turn for the session and stream its events as SSE.

        Admission-controlled (AG-15): the turn takes one of the process's turn permits for its
        whole streamed run, and is shed with 503 if none frees within the admission timeout — so a
        burst of concurrent turns cannot pile onto the shared internal LLM endpoint. One turn at a
        time per session: a second concurrent POST to the same session is a 409 (a double-submit
        cannot interleave two turns into one conversation thread). The permit hold is wall-clock
        bounded (`service_turn_timeout_seconds`): a hung model stream or a slow-reading client
        cannot pin a permit forever — on expiry the client gets one error event and the permit is
        released.
        """
        session = await _resolve_session(session_id, principal)
        active_turns: set[str] = app.state.active_turns
        if session_id in active_turns:
            raise HTTPException(
                status_code=409, detail="a turn is already running for this session"
            )
        active_turns.add(session_id)
        semaphore = app.state.turn_semaphore

        async def _turn_events() -> AsyncIterator[dict[str, str]]:
            # Release the permit and the session's turn slot when the stream ends — normal
            # completion, error, timeout, or client disconnect (the generator is closed, running
            # this finally) — so neither is ever leaked.
            try:
                try:
                    # The deadline covers the whole streamed run *including* client consumption:
                    # the generator is suspended inside this scope at each `yield`, so a stalled
                    # model stream and a slow-reading client are both bounded (AG-15's missing
                    # wall-clock half). A stall inside `run_turn` surfaces here as TimeoutError and
                    # becomes one user-safe error event; a stall in the transport tears the stream
                    # down, and the `finally` still frees the permit either way.
                    async with asyncio.timeout(settings.service_turn_timeout_seconds):
                        async for event in run_turn(
                            _agent(),
                            session,
                            body.message,
                            actor=principal.oid,
                            roles=principal.roles,
                            budget=app.state.budget,
                        ):
                            yield {"event": event.type, "data": event.model_dump_json()}
                except TimeoutError:
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
                semaphore.release()
                active_turns.discard(session_id)

        acquired = False
        handed_off = False
        try:
            # Runaway-cost guard (budget #3): refuse before taking a permit if this session/user
            # has exhausted its turn or token budget — a clean 429, not a started-then-killed turn.
            try:
                app.state.budget.check(session_id, principal.oid)
            except BudgetExceeded as exc:
                raise HTTPException(status_code=429, detail=str(exc)) from exc
            try:
                await asyncio.wait_for(
                    semaphore.acquire(), timeout=settings.service_turn_admission_timeout_seconds
                )
            except TimeoutError as exc:
                raise HTTPException(
                    status_code=503, detail="server at capacity; retry shortly"
                ) from exc
            acquired = True
            response = EventSourceResponse(_turn_events())
            handed_off = True
            return response
        finally:
            # try/finally, not `except Exception`: cancellation (a client gone mid-admission)
            # is a BaseException, and missing it here leaked the session's active-turns entry —
            # 409-bricking the session until restart. Until the streaming response is handed
            # off, this owns the cleanup; afterwards the generator's own finally does.
            if not handed_off:
                active_turns.discard(session_id)
                if acquired:
                    semaphore.release()

    @app.get("/sessions/{session_id}/events")
    async def session_events(
        session_id: str,
        principal: Principal = Depends(require_principal),
    ) -> EventSourceResponse:
        """Stream async job push-back for the session (F3-T3): a finished job wakes the chat.

        Bounded per user (`service_max_event_streams_per_user`): each stream polls the database
        for its whole lifetime, so unbounded streams are a connection-exhaustion vector (429 past
        the cap). The claim is scoped to `job_completed` in the SQL itself — the claim is
        destructive (at-most-once), so filtering after it would silently destroy events of any
        other kind meant for another consumer.
        """
        await _resolve_session(session_id, principal)
        streams: dict[str, int] = app.state.event_streams
        if streams.get(principal.oid, 0) >= settings.service_max_event_streams_per_user:
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
                    # Flip the harness todo that was waiting on this job (F3-T3 follow-up), so the
                    # session's *next* turn sees it as done instead of open forever. The live
                    # session may already be gone from the LRU cache (`_owned_session` above only
                    # required it to exist when this stream *started*) — a miss here is a safe
                    # no-op, matching `complete_awaiting_job`'s own no-op-on-miss contract.
                    if settings.harness_enabled:
                        live_entry = app.state.live_sessions.get(session_id)
                        if live_entry is not None:
                            await complete_awaiting_job(
                                live_entry[0], job_id, reason=f"QM job {job_id} completed"
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
            # Mirrors the turn route: any BaseException before the response is handed off
            # must return the slot, or the user's stream budget leaks toward a permanent 429.
            if not handed_off:
                _release_stream_slot()

    if _STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")

    return app


# CSP for the self-served chat UI (SEC-5): everything is same-origin except the one inline <style>
# block in index.html (so style-src needs 'unsafe-inline') and data: images; app.js is external
# (script-src 'self') and the SSE stream is same-origin (connect-src 'self'). base-uri and
# frame-ancestors are locked down to blunt injection and clickjacking.
_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'"
)


def _refuse_unauthenticated_exposure() -> None:
    """Fail closed when the app would run unauthenticated (`entra_required` off) network-exposed.

    With `entra_required` False every request is the shared dev principal and all authorization
    gates are open (SEC-2) — intended for local dev only. Binding that mode to a non-loopback
    interface (the `service_host="0.0.0.0"` default) exposes it to the network, so the service
    refuses to boot rather than leaving the whole deployment's safety to one env var defaulting
    the insecure way (the earlier warn-and-boot was one missed log line from an open deployment).
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


def _add_security_headers(app: FastAPI) -> None:
    """Add the browser security headers to every response, when `service_security_headers` is on.

    Off only when a deployment fronts its own header policy at the ingress/Route; on by default so
    the app is safe standalone. The headers are static, so a lightweight middleware sets them on
    every response (including static files and errors) without touching the route handlers.
    """
    if not settings.service_security_headers:
        return

    async def _set_headers(request: Request, call_next: Callable[[Request], Any]) -> Response:
        response: Response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", _CONTENT_SECURITY_POLICY)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
        )
        return response

    app.add_middleware(BaseHTTPMiddleware, dispatch=_set_headers)


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
