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
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
from sse_starlette.sse import EventSourceResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import Response

from agents.chemclaw_agent import build_agent
from agents.session_events import stream_new_events
from chemclaw.config import settings
from service.auth import Principal, require_principal
from service.events import JobCompletedEvent
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


def create_app(agent_factory: Callable[[], Any] = build_agent) -> FastAPI:
    """Build the front-door FastAPI app.

    Args:
        agent_factory: Builds the process's agent. Defaults to `build_agent` (the config-selected
            provider); tests pass a factory returning a fake streaming agent so the whole HTTP
            surface is exercised without a live model.

    Returns:
        A configured `FastAPI` application.
    """
    _warn_if_unauthenticated_and_exposed()
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
    # Admission control on concurrent turns (AG-15): a bounded permit set caps how many turns hit
    # the shared LLM endpoint at once. A permit is held for a turn's whole streamed run; a turn that
    # cannot get one within the admission timeout is shed with 503. Built here so it binds to the
    # app's event loop on first await.
    app.state.turn_semaphore = asyncio.Semaphore(settings.service_max_concurrent_turns)

    def _owned_session(session_id: str, principal: Principal) -> Any:
        """Return the session iff it exists and the caller owns it, else 404 (no existence leak)."""
        entry = app.state.live_sessions.get(session_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="unknown session")
        session, owner = entry
        if owner is not None and owner != principal.oid:
            raise HTTPException(status_code=404, detail="unknown session")
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
        burst of concurrent turns cannot pile onto the shared internal LLM endpoint.
        """
        session = _owned_session(session_id, principal)
        semaphore = app.state.turn_semaphore
        try:
            await asyncio.wait_for(
                semaphore.acquire(), timeout=settings.service_turn_admission_timeout_seconds
            )
        except TimeoutError as exc:
            raise HTTPException(
                status_code=503, detail="server at capacity; retry shortly"
            ) from exc

        async def _events() -> AsyncIterator[dict[str, str]]:
            # Release the permit when the stream ends — normal completion, error, or client
            # disconnect (the generator is closed, running this finally) — so it is never leaked.
            try:
                async for event in run_turn(
                    _agent(), session, body.message, actor=principal.oid, roles=principal.roles
                ):
                    yield {"event": event.type, "data": event.model_dump_json()}
            finally:
                semaphore.release()

        return EventSourceResponse(_events())

    @app.get("/sessions/{session_id}/events")
    async def session_events(
        session_id: str,
        principal: Principal = Depends(require_principal),
    ) -> EventSourceResponse:
        """Stream async job push-back for the session (F3-T3): a finished job wakes the chat."""
        _owned_session(session_id, principal)

        async def _events() -> AsyncIterator[dict[str, str]]:
            async for pushed in stream_new_events(session_id):
                if pushed.kind == "job_completed":
                    event = JobCompletedEvent(
                        job_id=str(pushed.payload.get("job_id", "")), summary=pushed.payload
                    )
                    yield {"event": event.type, "data": event.model_dump_json()}

        return EventSourceResponse(_events())

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


def _warn_if_unauthenticated_and_exposed() -> None:
    """Warn loudly when the app runs unauthenticated (`entra_required` off) on a non-loopback bind.

    With `entra_required` False every request is the shared dev principal and all authorization
    gates are open (SEC-2) — intended for local dev only. Binding that mode to a non-loopback
    interface (the `service_host="0.0.0.0"` default) exposes it to the network, so surface it at
    startup rather than leaving the whole deployment's safety to one env var defaulting the
    insecure way. Per the sign-off this warns and still boots; a deployment sets
    `CHEMCLAW_ENTRA_REQUIRED=true`.
    """
    if settings.entra_required or settings.service_host in _LOOPBACK_HOSTS:
        return
    logger.warning(
        "SECURITY: entra_required is False but the service binds a non-loopback interface (%r) — "
        "every request runs as the shared dev principal with all authorization gates OPEN. Set "
        "CHEMCLAW_ENTRA_REQUIRED=true for any shared/exposed deployment.",
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
