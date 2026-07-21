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

import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse
from starlette.middleware.cors import CORSMiddleware

from agents.chemclaw_agent import build_agent
from agents.session_events import stream_new_events
from chemclaw.config import settings
from service.auth import Principal, require_principal
from service.events import JobCompletedEvent
from service.runner import run_turn

_STATIC_DIR = Path(__file__).parent / "static"


class MessageIn(BaseModel):
    """One turn's user message posted to the messages endpoint."""

    message: str


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
    app = FastAPI(title="Chemclaw", docs_url=None, redoc_url=None)
    _add_cors(app)
    # One agent per process, built lazily on first use so importing the app needs no credentials;
    # per-session threads keep conversations apart. F3 replaces the in-memory session map with a
    # durable store and wires job→session push-back.
    app.state.agent = None
    app.state.agent_factory = agent_factory
    app.state.sessions = {}

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
        app.state.sessions[session_id] = _agent().create_session(session_id=session_id)
        return SessionOut(session_id=session_id)

    @app.post("/sessions/{session_id}/messages")
    async def post_message(
        session_id: str,
        body: MessageIn,
        principal: Principal = Depends(require_principal),
    ) -> EventSourceResponse:
        """Run one turn for the session and stream its events as SSE."""
        session = app.state.sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown session")

        async def _events() -> AsyncIterator[dict[str, str]]:
            async for event in run_turn(_agent(), session, body.message):
                yield {"event": event.type, "data": event.model_dump_json()}

        return EventSourceResponse(_events())

    @app.get("/sessions/{session_id}/events")
    async def session_events(
        session_id: str,
        principal: Principal = Depends(require_principal),
    ) -> EventSourceResponse:
        """Stream async job push-back for the session (F3-T3): a finished job wakes the chat."""
        if session_id not in app.state.sessions:
            raise HTTPException(status_code=404, detail="unknown session")

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
