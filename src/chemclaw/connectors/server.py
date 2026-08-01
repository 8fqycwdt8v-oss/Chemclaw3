"""The connector-side runtime: wrap a FastMCP capability as the FastAPI app a connector serves.

Every connector we own is the same shape — a FastAPI application exposing `/healthz` for the startup
probe, `/metrics` for the scrape, and `/mcp` for the MCP streamable-HTTP transport, over a `FastMCP`
instance holding the capability's tools. That shape is written once here so a new connector's
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

from chemclaw.api.metrics import CONTENT_TYPE, METRICS
from chemclaw.connectors.caller import bind_caller, reset_caller
from chemclaw.connectors.identity import (
    HEADER_ACTOR,
    HEADER_CORRELATION,
    HEADER_DRY_RUN,
    HEADER_SESSION,
)
from chemclaw.core import db
from chemclaw.core.tracing import continue_trace

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
            # Adopt the caller's trace, so this connector's spans are children of the turn that
            # called it rather than the root of an unrelated one. Note the asymmetry with the two
            # lines above: the `X-Chemclaw-*` identity headers are advisory and must never reach an
            # access decision, while trace context is safe to take from outside precisely because
            # it grants nothing — the worst a forged `traceparent` achieves is attaching spans to
            # someone else's trace.
            with continue_trace(request.headers):
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
        A FastAPI app exposing `GET /healthz`, `GET /metrics`, and the MCP endpoint at `/mcp`.
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
        """Liveness/readiness for the core startup probe (`chemclaw.connectors.health`).

        One route for both, and honestly so rather than by omission: uvicorn accepts connections
        only after the lifespan above has completed, so this route answering *is* the evidence
        that the MCP session manager is running and the Postgres pool is open. A separate
        `/readyz` here could only assert the same fact a second time.
        """
        return {"status": "ok", "connector": name}

    @app.get("/metrics")
    async def metrics() -> Response:
        """Prometheus exposition for this connector process.

        A connector records through `chemclaw.core.metrics_bridge` like everything else, and the
        registry that bridge finds is per-process — so these counters are this pod's, and until
        this route existed nothing could read them. Unauthenticated for the same reason the front
        door's copy is: a scrape happens independently of user identity, the NetworkPolicy keeps
        the port inside the cluster, and the exposition carries counts only.
        """
        return Response(content=METRICS.render(), media_type=CONTENT_TYPE)

    # Mounted last: Starlette matches routes in definition order, so the routes above win and
    # everything else — notably `/mcp` — falls through to the MCP transport.
    app.mount("/", mcp_app)
    return app
