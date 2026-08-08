"""The front door's cross-cutting HTTP armor: headers, body caps, CORS, and the fail-closed boots.

Everything here applies to *every* request or to the process as a whole — nothing is specific to a
route, which is the line that separates this module from `chemclaw/api/routes/` (R3.2). `create_app`
(`api/app.py`) is the only caller of the installers; the `_SecurityHeaders` middleware itself is
pure ASGI for the streaming-safety reason its docstring carries.
"""

import logging

from fastapi import FastAPI
from starlette.datastructures import MutableHeaders
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from chemclaw.core.asgi import BodySizeLimit
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS

logger = logging.getLogger(__name__)

# Loopback interfaces: binding here keeps the unauthenticated dev mode reachable only from the
# local host, so it is not a network-exposed footgun. Anything else (notably the "0.0.0.0"
# default) is.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# What a client is told when the process cannot take the work right now. One literal because it is
# said in two shapes — an error *event* on an already-open turn stream (D-166) and a 503 body from
# `_database_unavailable` — and the client behaviour it asks for is the same either way: back off
# and retry. A browser has no business learning which piece of infrastructure was full.
_AT_CAPACITY = "server at capacity; retry shortly"

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

    Answered with the admission path's wording on purpose (`_AT_CAPACITY`): it is what a shed turn
    already says, the client behaviour is identical (back off and retry), and a browser has no
    business learning which piece of infrastructure is behind it — while a misconfigured DSN still
    names itself loudly in the log line below.
    """
    METRICS.increment("chemclaw_db_unavailable_total")
    logger.warning("shedding %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=503, content={"detail": _AT_CAPACITY})


async def _subsystem_unavailable(request: Request, exc: Exception) -> Response:
    """Turn an unreachable durable subsystem into a retryable 503 instead of an unhandled 500.

    `job_status` and `cancel_job` used to report *every* Temporal `RPCError` as "no such job", so a
    broker roll during a cancel told an operator their runaway DFT run did not exist. Narrowing that
    to NOT_FOUND was right and, without this handler, exchanged one wrong answer for another: with
    no handler registered the raise became a bare HTTP 500, whose contract is "this request is
    broken, do not retry" — the opposite of the truth, and a page for the on-call as an application
    bug.

    Unlike `_database_unavailable` this relays the exception's own message rather than the capacity
    wording, because `SubsystemUnavailableError` is written for a human by contract
    (`core/errors.py`): it names the subsystem, says the work never began, and carries no hostname,
    port or driver text — those live on `__cause__`, for the log below.

    **Counts its own requests, not the turn probe's.** This used to increment
    `chemclaw_durable_unreachable_total`, whose declaration is "turns whose durable-subsystem health
    probe failed (Temporal did not answer)" and whose alert says so — while the handler fires per
    *request* for the whole `SubsystemUnavailableError` family, `DocumentIndexError` (a pgvector
    failure) included. One series, two populations, two denominators, and an alert whose summary was
    true of only one of them. The sibling above is the pattern: one counter per shedding handler.
    """
    METRICS.increment("chemclaw_subsystem_unavailable_total")
    # `exc_info` because the sentence above is only true if something logs the `__cause__`. The
    # relayed message is deliberately free of hostname, port and driver text, so without the chain
    # the operator's copy of this event says no more than the client's — the half of the contract
    # that had no implementation.
    logger.warning(
        "shedding %s %s: %s", request.method, request.url.path, exc, exc_info=exc.__cause__
    )
    return JSONResponse(status_code=503, content={"detail": str(exc)})


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


def _add_body_size_limit(app: FastAPI) -> None:
    """Bound every request body when `service_max_request_bytes` is set (0 disables).

    `BodySizeLimit` itself lives in `chemclaw.core.asgi` — shared with `connectors.server`, whose
    `/mcp` needed the identical fix (Sec-5) — not here, so this is only the front door's wiring of
    it to its own setting.
    """
    if settings.service_max_request_bytes:
        app.add_middleware(BodySizeLimit, max_bytes=settings.service_max_request_bytes)


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
