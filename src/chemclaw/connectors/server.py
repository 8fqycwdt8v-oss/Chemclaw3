"""The connector-side runtime: wrap a FastMCP capability as the FastAPI app a connector serves.

Every connector we own is the same shape — a FastAPI application exposing `/healthz` for the startup
probe and `/mcp` for the MCP streamable-HTTP transport, over a `FastMCP` instance holding the
capability's tools. That shape is written once here so a new connector's
`connectors/<name>/server/app.py` is three lines, and so the two cross-cutting behaviors it needs
cannot be forgotten per connector:

- **Running the MCP session manager.** `FastMCP.streamable_http_app()` returns a Starlette app whose
  *own* lifespan starts the session manager; mounting that app inside FastAPI does not run a
  sub-app's lifespan, so the parent must drive it explicitly. Getting this wrong produces a server
  that accepts connections and then hangs on the first request — the failure this helper exists to
  make impossible.
- **Logging the calling identity.** The `X-Chemclaw-*` headers arrive on every request
  (`chemclaw.connectors.identity`); logging the actor and session here is what lets a connector's
  own records
  be reconciled with the core audit trail, which is the whole point of sending them. It is logged,
  never trusted: authorization happened in core before the call was made, and a header on a request
  is not evidence of anything a connector should act on.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from chemclaw.connectors.caller import bind_caller, reset_caller
from chemclaw.connectors.identity import (
    HEADER_ACTOR,
    HEADER_CORRELATION,
    HEADER_DRY_RUN,
    HEADER_SESSION,
)
from chemclaw.core import db

logger = logging.getLogger(__name__)


class CallerLogMiddleware(BaseHTTPMiddleware):
    """Log the `X-Chemclaw-*` caller identity of every request, and bind it for the tools.

    Advisory only, in both roles. The headers say who core says is asking; they are recorded so a
    connector's own records and logs can be joined to the core audit trail by actor and session,
    and they are never an input to an access decision — a connector that gated on one would be
    trusting an unauthenticated string.

    Binding is what makes the second half of that sentence reachable. Logging alone let a connector
    correlate its *log lines*; a connector that writes a durable row — a persisted BO suggestion —
    had no way to stamp it with the conversation that asked for it, so the row could not be traced
    back to a chemist or a turn. `chemclaw.connectors.caller` holds the contextvars and the trust
    rule; this is where they are set and, importantly, reset.
    """

    def __init__(self, app: ASGIApp, connector: str) -> None:
        """Bind the connector's name so one log line identifies which capability was called."""
        super().__init__(app)
        self._connector = connector

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Log the caller, bind it for the duration of the request, then serve it unchanged."""
        actor = request.headers.get(HEADER_ACTOR, "")
        session = request.headers.get(HEADER_SESSION, "")
        logger.info(
            "connector %s request: path=%s actor=%s session=%s dry_run=%s",
            self._connector,
            request.url.path,
            actor or "-",
            session or "-",
            request.headers.get(HEADER_DRY_RUN, "-"),
        )
        tokens = bind_caller(actor, session, request.headers.get(HEADER_CORRELATION, ""))
        try:
            return await call_next(request)
        finally:
            # Defensive rather than load-bearing, and worth being honest about: each request runs
            # in its own task context, so a `ContextVar` set here is already invisible to the next
            # one. The reset costs nothing and holds if that ever stops being true — but no test
            # can fail without it, so it is not claimed as a guarantee.
            reset_caller(tokens)


def connector_app(server: FastMCP, *, name: str) -> FastAPI:
    """Build the FastAPI app that serves one connector's MCP capability.

    Args:
        server: The `FastMCP` instance holding the capability's tools. Its tools are served as-is;
            which of them the agent may call is decided by the manifest's `tools` allow-list in
            core, not here, so the same server can also expose index/write tools for the ingestion
            path.
        name: The connector's name (must match its bundle folder and manifest `name`), used in the
            health payload and the request log.

    Returns:
        A FastAPI app exposing `GET /healthz` and the MCP endpoint at `/mcp`.
    """
    mcp_app = server.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Run the MCP session manager, and pool Postgres, for the app's lifetime.

        The session manager is the sub-app's own lifespan, which mounting does not run.
        `db.pooling` is here for the same reason it is in the front door's lifespan: a
        connector that touches a store (`calc` reads and writes the calculation cache) otherwise
        opens a connection per tool call, and the handshake lands on the same event loop that
        serves every other request on this process.
        """
        async with db.pooling(), server.session_manager.run():
            yield

    app = FastAPI(title=f"chemclaw-connector-{name}", lifespan=lifespan)
    app.add_middleware(CallerLogMiddleware, connector=name)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness/readiness for the core startup probe (`chemclaw.connectors.health`)."""
        return {"status": "ok", "connector": name}

    # Mounted last: Starlette matches routes in definition order, so `/healthz` above wins and
    # everything else — notably `/mcp` — falls through to the MCP transport.
    app.mount("/", mcp_app)
    return app
