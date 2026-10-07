"""The connector-side runtime: wrap a FastMCP capability as the FastAPI app a connector serves.

Every connector is the same shape: `/healthz`, `/livez`, `/metrics` and the MCP streamable-HTTP
transport at `/mcp`, over a `FastMCP` holding the tools. Written once so a bundle's `app.py` is
three lines and the cross-cutting behaviours cannot be forgotten: running the MCP session manager
(mounting does not run a sub-app's lifespan, and without it the server hangs on the first request),
bearer auth, error sanitising, result publishing, and logging the `X-Chemclaw-*` caller identity.
That identity is logged and bound for attribution, never trusted: authorization happened in core.
"""

import asyncio
import functools
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from hmac import compare_digest
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.lowlevel.server import request_ctx
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from chemclaw.connectors.caller import bind_caller, reset_caller
from chemclaw.core import db
from chemclaw.core.asgi import BodySizeLimit
from chemclaw.core.call_identity import (
    HEADER_ACTOR,
    HEADER_CORRELATION,
    HEADER_DRY_RUN,
    HEADER_SESSION,
)
from chemclaw.core.config import settings
from chemclaw.core.errors import AtCapacityError
from chemclaw.core.metrics import CONTENT_TYPE, METRICS
from chemclaw.core.tracing import continue_trace

logger = logging.getLogger(__name__)


# Sentinel `_declared_bearer_env` returns when it cannot learn what this bundle requires. No
# variable has this name, so every request is refused: a connector that cannot read its manifest
# serves nothing.
_UNRESOLVED_AUTH = "CHEMCLAW_CONNECTOR_AUTH_UNRESOLVED"


def _declared_bearer_env(name: str) -> str | None:
    """The env var holding this bundle's bearer token, `None` for `mode: none`, or fail closed.

    Imported lazily because `connector_app` runs at import time in bundle modules. Fails closed (the
    sentinel) when discovery raises, and when a manifest ships beside this module
    (`_ships_a_manifest`) but discovery did not find it, meaning the process is looking at the wrong
    tree. An app with no shipped manifest is synthetic (tests build them) and stays open. A private
    bundle outside this package cannot be told apart from a synthetic app when the registry is
    misconfigured, so it must assert its own credential (`token_env`).
    """
    from chemclaw.connectors.manifest import BearerAuth, HttpEndpoint
    from chemclaw.connectors.registry import discovered

    try:
        found = discovered()
    except Exception:
        logger.exception(
            "connector_auth_unresolved: connector %s could not read its manifests, so it cannot "
            "tell whether it requires a bearer token; refusing every MCP request until it can",
            name,
        )
        return _UNRESOLVED_AUTH
    for _bundle, manifest in found.values():
        if manifest.name == name and isinstance(manifest.endpoint, HttpEndpoint):
            auth = manifest.endpoint.auth
            return auth.token_env if isinstance(auth, BearerAuth) else None
    if _ships_a_manifest(name):
        logger.error(
            "connector_auth_unresolved: connector %s ships a manifest that this process did not "
            "discover, so it cannot tell whether it requires a bearer token; check connectors_dir "
            "(currently %s). Refusing every MCP request until it resolves",
            name,
            settings.connectors_dir,
        )
        return _UNRESOLVED_AUTH
    return None


def _ships_a_manifest(name: str) -> bool:
    """Whether a `connector.yaml` for `name` ships inside this package.

    Resolved against `__file__`, not `settings.connectors_dirs`, because the configured roots are
    what a misconfiguration gets wrong. See `_declared_bearer_env`.
    """
    from chemclaw.connectors.registry import MANIFEST_FILENAME

    return (Path(__file__).parent / name / MANIFEST_FILENAME).is_file()


# How much of a caller-authored path may reach a log record: enough for every route served, short
# enough that a caller cannot spend the logging lock on a redaction scan. Separate from the front
# door's constant because a connector may not import `api`.
_MAX_LOGGED_PATH_CHARS = 128


def _clipped(value: str) -> str:
    """`value` bounded for a log record, marked when it was actually cut.

    The marker lets an operator tell a clipped path from a short one.
    """
    if len(value) <= _MAX_LOGGED_PATH_CHARS:
        return value
    return f"{value[:_MAX_LOGGED_PATH_CHARS]}…(+{len(value) - _MAX_LOGGED_PATH_CHARS})"


def _app_relative_path(request: Request) -> str:
    """This request's path within this app, with any mount prefix removed.

    Not `request.url.path`: inside a mounted sub-app Starlette keeps the full path and records the
    prefix in `root_path`, so a probe allowlist on `/healthz` would stop matching under the dev
    composite's `/<name>` mounts and the readiness probe would get 401.
    """
    root = request.scope.get("root_path", "")
    path = request.url.path
    return path[len(root) :] if root and path.startswith(root) else path


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Verify the bearer token a `mode: bearer` manifest says this connector requires.

    Middleware, not a route dependency: `/mcp` is mounted, and a mount bypasses the enclosing app's
    dependencies. `/healthz`, `/livez` and `/metrics` stay open for the kubelet and Prometheus (the
    exposition carries counts only). Compared with `compare_digest`; a missing expected token
    refuses rather than matching the empty string.
    """

    def __init__(self, app: Any, *, connector: str, token_env: str | None = None) -> None:
        """Bind the connector name; the declared auth mode is resolved on first request.

        `token_env` is for a surface with no `connector.yaml` (core's read-only MCP face,
        `api/mcp_face.py`); supplied, the manifest lookup never runs, so such a surface cannot fall
        into the "synthetic app stays open" branch.
        """
        super().__init__(app)
        self._connector = connector
        self._token_env: str | None = token_env
        # A supplied name needs no resolution; a supplied empty name fails closed below.
        self._resolved = token_env is not None

    def _declared(self) -> str | None:
        """The env var this bundle's manifest names, resolved once, on first use.

        Lazy, so building an app does not warm the registry cache against whatever `connectors_dir`
        is set at that moment. The fail-closed sentinel is not cached, so a fixed manifest takes
        effect without a restart; only a resolved answer is kept.
        """
        if not self._resolved:
            self._token_env = _declared_bearer_env(self._connector)
            self._resolved = self._token_env != _UNRESOLVED_AUTH
        return self._token_env

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Refuse anything but the two probes and `/metrics` without the configured bearer token."""
        path = _app_relative_path(request)
        if path in ("/healthz", "/livez", "/metrics"):
            return await call_next(request)
        token_env = self._declared()
        if token_env is None:
            return await call_next(request)
        expected = os.environ.get(token_env, "")
        presented = request.headers.get("authorization", "")
        scheme, _, offered = presented.partition(" ")
        # Compared as bytes: `compare_digest` on a non-ASCII `str` raises `TypeError`, which would
        # let any caller turn this refusal into a 500.
        if (
            not expected
            or scheme.lower() != "bearer"
            or not compare_digest(
                offered.strip().encode("utf-8", "surrogateescape"),
                expected.encode("utf-8", "surrogateescape"),
            )
        ):
            # Clipped: this runs before any credential check, so the path is an unauthenticated
            # caller's string and an unbounded one would hold the logging lock through the redaction
            # scan.
            logger.warning(
                "connector %s refused an unauthenticated MCP request to %s",
                self._connector,
                _clipped(path),
            )
            return Response(status_code=401, content="unauthorized")
        return await call_next(request)


class CallerLogMiddleware(BaseHTTPMiddleware):
    """Log the `X-Chemclaw-*` caller identity of every request, and bind it for the tools.

    Advisory only: recorded so a connector's logs and durable rows can be joined to core's audit
    trail by actor and session, never used for an access decision. `chemclaw.connectors.caller`
    holds the contextvars and the trust rule; this sets and resets them.
    """

    def __init__(self, app: ASGIApp, connector: str) -> None:
        """Bind the connector's name so one log line identifies which capability was called."""
        super().__init__(app)
        self._connector = connector

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Log the caller, bind it for the duration of the request, then serve it unchanged."""
        actor = request.headers.get(HEADER_ACTOR, "")
        session = request.headers.get(HEADER_SESSION, "")
        correlation = request.headers.get(HEADER_CORRELATION, "")
        # Bound *before* the line below, so the line carries the ids as record fields as well as in
        # its text — the one line that names the call is the one a search by correlation id finds.
        tokens = bind_caller(actor, session, correlation)
        try:
            logger.info(
                "connector %s request: path=%s actor=%s session=%s correlation=%s dry_run=%s",
                self._connector,
                request.url.path,
                actor or "-",
                session or "-",
                correlation or "-",
                request.headers.get(HEADER_DRY_RUN, "-"),
            )
            # Adopt the caller's trace so this connector's spans are children of the calling turn.
            # Safe to take from outside, unlike identity, because a trace grants nothing.
            with continue_trace(request.headers):
                return await call_next(request)
        finally:
            # Tool bodies run in the MCP session-manager task, not this one;
            # `_bind_caller_per_tool_call` gives them their caller. This binding serves the rest of
            # the request path.
            reset_caller(tokens)


def _bind_caller_per_tool_call(server: FastMCP) -> None:
    """Re-bind the caller from the request the tool call is serving, not the one that connected.

    A tool body runs in the session-manager task created at `initialize`, so without this it reads
    the handshake's identity for the whole MCP session. `request_ctx` carries the ASGI request per
    JSON-RPC message; with no request context (stdio, a direct test call) the middleware's binding
    stands.

    Wrapped around `_sanitize_tool_errors`'s patch of the same method. Idempotent, marked on the
    manager whose method is replaced, so building several apps over one `FastMCP` does not stack
    wrappers.
    """
    manager = server._tool_manager
    if getattr(manager, "_chemclaw_binds_caller", False):
        return
    wrapped_call_tool = manager.call_tool

    async def _call_tool(
        tool_name: str,
        arguments: dict[str, Any],
        context: Any = None,
        convert_result: bool = False,
    ) -> Any:
        request = getattr(request_ctx.get(None), "request", None)
        headers = getattr(request, "headers", None)
        if headers is None:
            return await wrapped_call_tool(
                tool_name, arguments, context=context, convert_result=convert_result
            )
        tokens = bind_caller(
            headers.get(HEADER_ACTOR, ""),
            headers.get(HEADER_SESSION, ""),
            headers.get(HEADER_CORRELATION, ""),
        )
        try:
            # The caller's trace, adopted in the tool's own task for the same reason as its
            # identity, so a span opened in a tool body is parented to the calling turn.
            with continue_trace(headers):
                return await wrapped_call_tool(
                    tool_name, arguments, context=context, convert_result=convert_result
                )
        finally:
            reset_caller(tokens)

    manager.call_tool = _call_tool  # type: ignore[method-assign,assignment]
    manager._chemclaw_binds_caller = True  # type: ignore[attr-defined]


def _sanitize_tool_errors(server: FastMCP, *, name: str) -> None:
    """Replace an unexpected tool exception's text with a generic notice before it reaches a caller.

    `Tool.run` folds `str(exc)` into the error result, which can leak a DSN or internal path.
    `ValueError` (including `ChemclawError` and pydantic's `ValidationError`) is this codebase's
    caller-safe family and passes through; anything else is replaced and logged for the operator.
    FastMCP has no tool-call middleware, so this patches the tool manager's `call_tool`, once for
    every connector. Idempotent like `_bind_caller_per_tool_call`.
    """
    manager = server._tool_manager
    if getattr(manager, "_chemclaw_sanitizes_errors", False):
        return
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
            if isinstance(exc.__cause__, AtCapacityError):
                # A full backend is not a fault: keep its marker first (read by
                # `core/mcp_session.at_capacity`) and its chemist-facing sentence, so callers can
                # tell "ask again" from "broken".
                busy = exc.__cause__
                raise ToolError(f"Error executing tool {tool_name}: {busy.marker} {busy}") from busy
            if isinstance(exc.__cause__, ValueError):
                raise  # a deliberately-worded domain message (or a validation error) — safe as-is
            logger.exception(
                "connector %s: tool %r raised an unexpected exception", name, tool_name
            )
            raise ToolError(
                f"Error executing tool {tool_name}: an internal error occurred"
            ) from exc.__cause__

    manager.call_tool = _call_tool  # type: ignore[method-assign,assignment]
    manager._chemclaw_sanitizes_errors = True  # type: ignore[attr-defined]


def _publish_tool_results(server: FastMCP, *, name: str) -> None:
    """Offer every tool's own result to the external results store, for every bundle at once.

    The publish hook for tool composites, which have no cache row and are not jobs, so the cache and
    job hooks never see them. Installed here so tool authors have nothing to remember;
    `chemclaw.publish.hooks` decides what is published. Wraps `Tool.fn` rather than `call_tool`
    because the hook routes on the result model, which `call_tool` has already converted to content
    blocks. Returns the result unchanged; a failed publish is invisible to the caller. Idempotent.
    """
    for tool in server._tool_manager.list_tools():
        if not tool.is_async or getattr(tool.fn, "_chemclaw_publishes", False):
            # Sync tools are left alone: `Tool.is_async` was fixed at registration, so an async
            # wrapper over a sync function would never be awaited.
            continue
        tool.fn = _publishing(tool.fn, connector=name, tool_name=tool.name)


def _publishing(
    fn: Callable[..., Awaitable[Any]], *, connector: str, tool_name: str
) -> Callable[..., Awaitable[Any]]:
    """One tool's body, with its result offered to the results store after it returns."""

    @functools.wraps(fn)
    async def _run(**kwargs: Any) -> Any:
        result = await fn(**kwargs)
        # Imported lazily so a deployment with no sink never loads the projection machinery or
        # RDKit.
        from chemclaw.publish.hooks import publish_tool_result

        await publish_tool_result(
            connector=connector, tool=tool_name, arguments=kwargs, result=result
        )
        return result

    _run._chemclaw_publishes = True  # type: ignore[attr-defined]
    return _run


# The addresses FastMCP itself admits by default: its DNS-rebinding guard, on for any server whose
# configured host is loopback — which `FastMCP(name)` always is, however uvicorn binds it.
_LOOPBACK_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*")


def _transport_security(name: str) -> TransportSecuritySettings:
    """The Host/Origin allow-list for this connector's `/mcp`: loopback, plus its own address.

    `FastMCP(name)` enables DNS-rebinding protection for loopback only, which answers 421 to every
    caller dialling a Service name while `/healthz` stays green. The guard stays on, admitting
    loopback and `connector_urls[name]`; with no URL configured it is FastMCP's default.
    """
    hosts = list(_LOOPBACK_HOSTS)
    origins = [f"http://{host}" for host in _LOOPBACK_HOSTS]
    address = urlsplit(settings.connector_urls.get(name, ""))
    if address.netloc:
        hosts.append(address.netloc)
        origins.append(f"{address.scheme}://{address.netloc}")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins
    )


def connector_app(
    server: FastMCP,
    *,
    name: str,
    on_start: Callable[[], Coroutine[Any, Any, None]] | None = None,
    token_env: str | None = None,
) -> FastAPI:
    """Build the FastAPI app that serves one connector's MCP capability.

    Args:
        server: The `FastMCP` instance holding the capability's tools. Every tool served is
        reachable by anything that can reach the pod, so `connector-validate` refuses one the
            manifest does not declare.
        name: The connector's name (must match its bundle folder and manifest `name`), used in the
            health payload and the request log.
        token_env: The environment variable holding this surface's bearer token, for a surface with
            no `connector.yaml` (core's read-only MCP face). Omitted, the bundle's manifest decides.
        on_start: Optional diagnostic coroutine started (not awaited) once at startup, e.g. to log
            the index size; it must swallow its own failures.

    Returns:
        A FastAPI app exposing `GET /healthz`, `GET /livez`, `GET /metrics`, and the MCP endpoint
        at `/mcp`.
    """
    _sanitize_tool_errors(server, name=name)
    # Outermost, so the identity a tool stamps on a durable row is bound before anything else runs.
    _bind_caller_per_tool_call(server)
    # Innermost of the three: it runs inside the tool body's own frame, where the result is still
    # the model it was declared as. See `_publish_tool_results`.
    _publish_tool_results(server, name=name)
    server.settings.transport_security = _transport_security(name)
    mcp_app = server.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Run the MCP session manager, and pool Postgres, for the app's lifetime.

        The session manager is the sub-app's own lifespan, which mounting does not run. The pool
        avoids a connection per tool call for connectors that touch a store. `on_start` runs inside
        the pool but is not awaited, so an unreachable database cannot delay readiness; the task is
        kept referenced and cancelled on shutdown.
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
    # Always installed; resolves the manifest's auth on first request and passes through for
    # `mode: none`, so no bundle can forget to wire it.
    app.add_middleware(BearerAuthMiddleware, connector=name, token_env=token_env)
    # Added after `CallerLogMiddleware`, so it is outermost and refuses an oversized body before any
    # handler reads it.
    if settings.connector_max_request_bytes:
        app.add_middleware(BodySizeLimit, max_bytes=settings.connector_max_request_bytes)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Readiness, for the kubelet and for core's startup sweep (`chemclaw.connectors.health`).

        uvicorn accepts connections only after the lifespan completes, so answering proves the
        session manager and pool are up.
        """
        return {"status": "ok", "connector": name}

    @app.get("/livez")
    async def livez() -> dict[str, str]:
        """Liveness, and nothing else: answering proves the process still serves HTTP.

        Separate from `/healthz` because a liveness failure kills the container, so it must consult
        nothing a restart cannot fix; a check later added to `/healthz` must not become a restart
        trigger.
        """
        return {"status": "alive", "connector": name}

    @app.get("/metrics")
    async def metrics() -> Response:
        """Prometheus exposition for this connector process.

        Exposes this pod's `chemclaw.core.metrics_bridge` registry. Unauthenticated like the front
        door's: a scrape has no user identity, the NetworkPolicy keeps it in-cluster, and it carries
        counts only.
        """
        return Response(content=METRICS.render(), media_type=CONTENT_TYPE)

    # Mounted last: Starlette matches routes in definition order, so the routes above win and
    # everything else — notably `/mcp` — falls through to the MCP transport.
    app.mount("/", mcp_app)
    return app
