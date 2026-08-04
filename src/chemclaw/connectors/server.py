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

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
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
from chemclaw.core.asgi import BodySizeLimit
from chemclaw.core.config import settings
from chemclaw.core.metrics import CONTENT_TYPE, METRICS
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


def _sanitize_tool_errors(server: FastMCP, *, name: str) -> None:
    """Replace an unexpected tool exception's text with a generic notice before it reaches a caller.

    Measured, not assumed (a probe against this exact `mcp` version, over the real streamable-HTTP
    transport): `Tool.run` already turns any exception a tool raises into a JSON-RPC tool-error
    result rather than an HTTP fault, but it folds the exception's `str()` in verbatim —
    `f"Error executing tool {name}: {e}"` — so an unhandled `psycopg.OperationalError` or a stray
    path reaches the model with a DSN or an internal identifier attached. This is not about
    *whether* a caller sees an error, only about *what it is allowed to say*.

    `ValueError` is the one exception family this codebase already treats as "a deliberately-worded,
    caller-safe message" (`chemclaw.core.errors.ChemclawError` and `ConnectorError` both derive from
    it, and so does pydantic's own `ValidationError`) — every connector tool that raises to explain
    a bad SMILES or a bad argument already raises one of these, so only this family is let through
    unchanged. Anything else is a bug or an infrastructure fault, not a message written for the
    model to read, and is replaced here — with the real exception logged so an operator can still
    find it.

    There is no supported hook for this in `FastMCP` (no tool-call middleware in this version), so
    the interception point is the tool manager's own `call_tool` — the one place every tool call
    passes through before `Tool.run` composes the leaking message. Patched once here, the one
    shared choke point every connector's app is built through, rather than once per bundle.
    """
    manager = server._tool_manager  # noqa: SLF001 - the only interception point this mcp version offers
    original_call_tool = manager.call_tool

    async def _call_tool(
        tool_name: str,
        arguments: dict[str, Any],
        context: Any = None,
        convert_result: bool = False,
    ) -> Any:
        try:
            return await original_call_tool(
                tool_name, arguments, context=context, convert_result=convert_result
            )
        except ToolError as exc:
            if isinstance(exc.__cause__, ValueError):
                raise  # a deliberately-worded domain message (or a validation error) — safe as-is
            logger.exception(
                "connector %s: tool %r raised an unexpected exception", name, tool_name
            )
            raise ToolError(
                f"Error executing tool {tool_name}: an internal error occurred"
            ) from exc.__cause__

    manager.call_tool = _call_tool  # type: ignore[method-assign,assignment]


def connector_app(
    server: FastMCP, *, name: str, on_start: Callable[[], Coroutine[Any, Any, None]] | None = None
) -> FastAPI:
    """Build the FastAPI app that serves one connector's MCP capability.

    Args:
        server: The `FastMCP` instance holding the capability's tools. Its tools are served as-is;
            which of them the agent may call is decided by the manifest's `tools` allow-list in
            core, not here, so the same server can also expose index/write tools for the ingestion
            path.
        name: The connector's name (must match its bundle folder and manifest `name`), used in the
            health payload and the request log.
        on_start: Optional coroutine started once at startup — the hook a bundle uses to report
            the state of what it serves (`molfp`/`rxnfp` log how many fingerprints their index
            actually holds, so an operator learns of an unbuilt index before a chemist does).
            Diagnostics only, and treated as such: it is *started*, not awaited (see the lifespan),
            and a bundle's hook owns swallowing its own failures — a connector that refuses to
            start because it could not describe itself is strictly worse than one that starts.

    Returns:
        A FastAPI app exposing `GET /healthz`, `GET /metrics`, and the MCP endpoint at `/mcp`.
    """
    _sanitize_tool_errors(server, name=name)
    mcp_app = server.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Run the MCP session manager, and pool Postgres, for the app's lifetime.

        The session manager is the sub-app's own lifespan, which mounting does not run.
        `db.pooling` is here for the same reason it is in the front door's lifespan: a
        connector that touches a store (`calc` reads and writes the calculation cache) otherwise
        opens a connection per tool call, and the handshake lands on the same event loop that
        serves every other request on this process.

        `on_start` is launched inside the pool (so a bundle's report borrows a pooled connection
        rather than paying its own handshake) but deliberately **not awaited**: it touches the
        database, and an unreachable one would hold readiness for the whole pool timeout — a
        diagnostic that can delay a connector becoming ready is worse than the blindness it cures.
        Measured, not assumed: awaiting it kept the connector composite from starting inside the
        transport test's window with no Postgres running. The task is kept referenced so it is not
        garbage-collected mid-flight, and cancelled if shutdown beats it.
        """
        async with db.pooling(), server.session_manager.run():
            report: asyncio.Task[None] | None = (
                asyncio.create_task(on_start()) if on_start is not None else None
            )
            try:
                yield
            finally:
                if report is not None:
                    report.cancel()

    app = FastAPI(title=f"chemclaw-connector-{name}", lifespan=lifespan)
    app.add_middleware(CallerLogMiddleware, connector=name)
    # Added *after* `CallerLogMiddleware`: Starlette wraps in add-order with the most recently
    # added outermost, so this one now sits outside it and refuses an oversized body before any
    # handler — including the logging middleware's own `dispatch` — ever reads it (Sec-5: `/mcp`
    # had no cap at all, unlike the front door's `_add_body_size_limit`).
    if settings.connector_max_request_bytes:
        app.add_middleware(BodySizeLimit, max_bytes=settings.connector_max_request_bytes)

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
