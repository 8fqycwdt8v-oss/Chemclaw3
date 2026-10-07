"""The front door's cross-cutting HTTP armour: headers, body caps, CORS, the boot guard.

Everything here applies to every request or to the process, never to one route; routes live in
`chemclaw/api/routes/`. `create_app` (`api/app.py`) is the only caller of the installers. The
gateway guard lives in `chemclaw.core.llm_gateway` because every turn-taking process needs it; the
bind guard here is about this app alone.
"""

import ipaddress
import json
import logging
import re
import time
import uuid
from math import isfinite
from typing import Any

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from chemclaw.core.asgi import BodySizeLimit
from chemclaw.core.call_identity import HEADER_CORRELATION
from chemclaw.core.config import settings
from chemclaw.core.http import is_loopback_host
from chemclaw.core.identity_context import (
    reset_current_correlation_id,
    reset_current_identity,
    set_current_correlation_id,
    set_current_identity,
)
from chemclaw.core.logging import log_event
from chemclaw.core.metrics import METRICS
from chemclaw.core.session_context import reset_current_session_id, set_current_session_id

logger = logging.getLogger(__name__)


# What a client is told when the process cannot take the work right now — as an error event on an
# open turn stream or as a 503 body. Either way: back off and retry. Which infrastructure was full
# is not the browser's business.
AT_CAPACITY = "server at capacity; retry shortly"

# CSP for the self-served chat UI: same-origin except the inline `<style>` in index.html (hence
# `'unsafe-inline'` styles) and data: images; base-uri and frame-ancestors are locked down.
_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'"
)

# The header set, as the `(name, value)` pairs the ASGI response-start message wants.
_SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("Content-Security-Policy", _CONTENT_SECURITY_POLICY),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Strict-Transport-Security", "max-age=63072000; includeSubDomains"),
)


async def _database_unavailable(request: Request, exc: Exception) -> Response:
    """Turn a failed Postgres checkout into a retryable 503 instead of an unhandled 500.

    A pool timeout is transient; a 500 would tell the client not to retry. Answered with
    `AT_CAPACITY`, like a shed turn, while the log line names the cause.
    """
    METRICS.increment("chemclaw_db_unavailable_total")
    logger.warning("shedding %s %s: %s", request.method, request.url.path[:256], exc)
    return JSONResponse(status_code=503, content={"detail": AT_CAPACITY})


async def _subsystem_unavailable(request: Request, exc: Exception) -> Response:
    """Turn an unreachable durable subsystem into a retryable 503 instead of an unhandled 500.

    Relays the exception's own message, because `SubsystemUnavailableError` is written for a human
    by contract (`core/errors.py`): it names the subsystem and carries no hostname or driver text,
    which live on `__cause__`. Counted on its own per-request counter.
    """
    METRICS.increment("chemclaw_subsystem_unavailable_total")
    # `exc_info` so the operator's log carries the `__cause__` the client's message omits.
    logger.warning(
        "shedding %s %s: %s", request.method, request.url.path[:256], exc, exc_info=exc.__cause__
    )
    return JSONResponse(status_code=503, content={"detail": str(exc)})


#: Server addresses already reported by `arrived_over_the_network`, so the SECURITY line is said
#: once per socket. Bounded, since an unauthenticated caller chooses the address.
_REPORTED_EXPOSURES: set[str] = set()
_MAX_REPORTED_EXPOSURES = 8


def arrived_over_the_network(scope: Scope) -> bool:
    """Whether this request landed on an address reachable from outside the machine.

    The socket, not the setting: `_refuse_unauthenticated_exposure` reads `settings.service_host`,
    which uvicorn's `--host` can contradict. `scope["server"]` is the accepted connection's own
    address, so a local request on a `0.0.0.0` bind reads loopback. A non-IP `server` (the test
    client's name, an in-process call) is not judged; the boot guard covers configuration.

    Args:
        scope: The ASGI scope of the request being served.

    Returns:
        True only when the request demonstrably arrived on a routable address.
    """
    server = scope.get("server") or ()
    host = str(server[0]) if server and server[0] else ""
    try:
        ipaddress.ip_address(host.strip("[]").split("%", 1)[0])
    except ValueError:
        # A name, a unix socket path, or nothing at all: no address was observed.
        return False
    return not is_loopback_host(host)


def note_network_exposure(host: str) -> None:
    """Say once, loudly, that an unauthenticated request arrived from the network.

    Once per address: it is a deployment fault, and a per-request warning would be log volume an
    unauthenticated caller controls.
    """
    if len(_REPORTED_EXPOSURES) >= _MAX_REPORTED_EXPOSURES or host in _REPORTED_EXPOSURES:
        return
    _REPORTED_EXPOSURES.add(host)
    logger.warning(
        "SECURITY: refusing a request that arrived on %r while entra_required is False — every "
        "request served here would run as the shared dev principal with all authorization gates "
        "OPEN. Set CHEMCLAW_ENTRA_REQUIRED=true for any shared/exposed deployment, bind a loopback "
        "interface for local dev (both CHEMCLAW_SERVICE_HOST and the server's own --host), or set "
        "CHEMCLAW_SERVICE_ALLOW_INSECURE=true to explicitly accept an unauthenticated, "
        "network-exposed service.",
        host,
    )


def _refuse_unauthenticated_exposure() -> None:
    """Fail closed when the app would run unauthenticated (`entra_required` off) network-exposed.

    Without `entra_required` every request is the shared dev principal and every authorization gate
    is open, so binding a non-loopback interface refuses to boot. `service_allow_insecure=true` is
    the explicit opt-out (boots with a loud warning). Loopback is decided by
    `core.http.is_loopback_host`.
    """
    if settings.entra_required or is_loopback_host(settings.service_host):
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
    """Stamp the browser security headers onto every response — pure ASGI, never buffering.

    Not `BaseHTTPMiddleware`, which runs the app as a second task and turns a request cancelled
    before responding (a client giving up, a draining pod) into a spurious 500. This wraps only
    `send`, so an SSE stream passes byte for byte.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap `app`, the rest of the ASGI stack below this middleware."""
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass the call through, adding the headers to the response-start message.

        Non-HTTP scopes (lifespan, websocket) pass straight through.
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


class _RequestObservability:
    """One record per HTTP request: the access log, the RED metrics, and the correlation id.

    Pure ASGI, for `_SecurityHeaders`' reason. `route` is the FastAPI route template, never the raw
    path, which is attacker-controlled — a cardinality bomb and a redaction cost. Starlette merges
    the matched route into this scope, so the template is readable after the app runs; unmatched
    requests share one `<unmatched>` series.

    Installed innermost: inside the security headers and body cap, so a 500 answered here carries
    the headers and a correlation id; outside `ExceptionMiddleware`, so handler 4xx responses are
    recorded. The body cap's 413 and CORS preflights are answered above it and are not in this log.
    `tests/test_api_observability.py` checks routes × status classes stays within the registry's
    per-counter series cap.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap `app`, the rest of the ASGI stack below this middleware."""
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Serve one request under a correlation id, then record what happened to it."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        correlation = _request_correlation_id(Headers(scope=scope))
        token = set_current_correlation_id(correlation)
        scope[_SCOPE_BOUND] = True
        started = time.perf_counter()
        status = 0
        response_bytes = 0
        answered = False

        async def _send(message: Message) -> None:
            nonlocal status, response_bytes, answered
            if message["type"] == "http.response.start":
                status = int(message["status"])
                # Default `headers` before anything reads it: it is optional in ASGI and
                # `MutableHeaders` raises without it, which would leave the connection hanging.
                message.setdefault("headers", [])
                answered = True
                # `setdefault`, so a route's own id is kept. Every response carries one for bug
                # reports.
                MutableHeaders(scope=message).setdefault(HEADER_CORRELATION, correlation)
            elif message["type"] == "http.response.body":
                response_bytes += len(message.get("body", b""))
            await send(message)

        try:
            try:
                await self._app(scope, receive, _send)
            except Exception:
                # Not `BaseException`: a cancelled request — a client that hung up, a pod draining
                # — is an ended connection, not a server error, and must stay one.
                logger.exception(
                    "unhandled error serving %s %s (correlation %s)",
                    scope.get("method", ""),
                    route_template(scope),
                    correlation,
                    extra={"correlation_id": correlation},
                )
                if answered:
                    # Already on the wire (an SSE stream that died mid-answer): nothing truthful
                    # left to send, and `status` stays as the client was told. The log line is the
                    # record.
                    raise
                status = 500
                await _answer_internal_error(send, correlation)
        finally:
            _record_request(
                scope, status, time.perf_counter() - started, response_bytes, correlation
            )
            _reset_request_identity(scope)
            reset_current_correlation_id(token)


# Marks a scope this middleware is serving, so the binders below are no-ops elsewhere: an ambient
# nobody resets would leak into the next request.
_SCOPE_BOUND = "chemclaw.observed"
# Where a bound identity's reset token waits for `_RequestObservability`'s `finally`. The middleware
# owns every reset because it runs on every exit path.
_SCOPE_IDENTITY_TOKEN = "chemclaw.identity_token"
_SCOPE_SESSION_TOKEN = "chemclaw.session_token"
# The session for the access-log line, stamped by the ownership gate once it has *resolved* one —
# never read off `path_params`. See `_record_request` for what that distinction is worth.
_SCOPE_SESSION = "chemclaw.session_id"
# The actor for the access-log line, stamped by the authentication gate once it knows one.
_SCOPE_ACTOR = "chemclaw.actor"

# The route label for a request that matched no route. A fixed literal, so the whole family stays
# bounded by the route table (a source constant) rather than by what a caller puts in a URL.
_UNMATCHED_ROUTE = "<unmatched>"

# The shape an inbound correlation id must have to be adopted: hex, dashes, underscores, bounded
# (covers a `uuid4().hex` and typical ingress ids). Anything else is replaced, since the id reaches
# logs, `audit_events` and a response header. A format, not a threshold, so not a setting.
_CORRELATION_ID = re.compile(r"\A[A-Za-z0-9_-]{8,64}\Z")

# How many pydantic error objects a 422 body may carry: pydantic emits one per bad list element, so
# an unbounded render amplifies a large body. This bounds the count; `_render_errors` bounds the
# bytes each echoes back.
_MAX_VALIDATION_ERRORS = 20

# How long a caller-controlled string may be where it is echoed into a log record or a 422 body.
# Covers every id this system mints; `SecretRedactingFilter`'s cost is linear in record length under
# the logging lock, so this bounds what a caller can spend.
_MAX_LOGGED_CHARS = 128


def clip_for_log(value: str) -> str:
    """`value` bounded for a log record or an error body, marked when it was actually cut.

    The marker matters: a clipped id must not be mistaken for a short one. The bound is the module
    constant, not a parameter, so there is one number.
    """
    return (
        value
        if len(value) <= _MAX_LOGGED_CHARS
        else f"{value[:_MAX_LOGGED_CHARS]}…(+{len(value) - _MAX_LOGGED_CHARS})"
    )


# How deep `_json_safe` walks before naming what it stopped at. Equal to the clip, because `input`
# is already clipped to at most half that depth; this floors the other keys (`ctx` and future ones).
_MAX_ERROR_DEPTH = _MAX_LOGGED_CHARS


def _json_safe(value: Any, depth: int = 0) -> Any:
    """`value` with every non-finite float replaced by its name, bounded in depth.

    `json.dumps` refuses NaN, which would turn a 422 into a 500 inside the handler. The name, not
    `null`, so the client sees which value to fix.
    """
    if isinstance(value, float):
        # `isfinite` rather than `!= value`, so ±inf is caught alongside NaN — both are refused
        # by `json.dumps`, and `Infinity` is a `json.loads` literal exactly as `NaN` is.
        return value if isfinite(value) else repr(value)
    if depth >= _MAX_ERROR_DEPTH:
        # Named rather than truncated silently, for `clip_for_log`'s reason: a reader cannot tell a
        # dropped value from an absent one.
        return f"…(nested past {_MAX_ERROR_DEPTH})"
    if isinstance(value, dict):
        return {key: _json_safe(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth + 1) for item in value]
    return value


def _render_errors(errors: list[Any]) -> list[Any]:
    """Pydantic's error objects with the caller's own bytes bounded — the 422's whole payload.

    FastAPI's `RequestValidationError.errors()` takes no `include_input=`, so clipping is manual.
    `input` and the tail of `loc` (caller-chosen for `extra_forbidden` and dict keys) are clipped;
    `url` is dropped; `type` and `msg` are kept.
    """
    rendered: list[Any] = []
    for error in errors:
        if not isinstance(error, dict):
            # Not every producer of a `RequestValidationError` is pydantic; anything that is not a
            # mapping is passed through as it came rather than being guessed at.
            rendered.append(error)
            continue
        trimmed = {key: value for key, value in error.items() if key != "url"}
        if "input" in trimmed and len(str(trimmed["input"])) > _MAX_LOGGED_CHARS:
            # Stringified and clipped only when too big, so ordinary `input` keeps its original JSON
            # shape.
            trimmed["input"] = clip_for_log(str(trimmed["input"]))
        if "loc" in trimmed:
            trimmed["loc"] = [
                clip_for_log(part) if isinstance(part, str) else part for part in trimmed["loc"]
            ]
        rendered.append(_json_safe(trimmed))
    return rendered


def bind_request_actor(request: Request, actor: str, roles: frozenset[str]) -> None:
    """Make the authenticated caller ambient for the rest of this request.

    Called from `require_principal`, which every authenticated route passes, so every log line on
    any route names its actor. A no-op outside `_RequestObservability`, which owns the reset.
    """
    if not request.scope.get(_SCOPE_BOUND):
        return
    request.scope[_SCOPE_ACTOR] = actor
    request.scope[_SCOPE_IDENTITY_TOKEN] = set_current_identity(actor, roles)


def bind_request_session(request: Request, session_id: str) -> None:
    """Make the resolved session ambient for the rest of this request.

    Called from `api/deps.resolve_session`, because the session id is a routed path parameter
    unknown at request entry. Same no-op rule and reset owner as `bind_request_actor`.
    """
    if not request.scope.get(_SCOPE_BOUND):
        return
    # Stamped here because only here has the id resolved to a session the caller may reach.
    request.scope[_SCOPE_SESSION] = clip_for_log(session_id)
    request.scope[_SCOPE_SESSION_TOKEN] = set_current_session_id(session_id)


def _reset_request_identity(scope: Scope) -> None:
    """Undo whatever the two binders above stamped, in reverse order."""
    scope.pop(_SCOPE_SESSION, None)
    session_token = scope.pop(_SCOPE_SESSION_TOKEN, None)
    if session_token is not None:
        reset_current_session_id(session_token)
    identity_token = scope.pop(_SCOPE_IDENTITY_TOKEN, None)
    if identity_token is not None:
        reset_current_identity(identity_token)


def _request_correlation_id(headers: Headers) -> str:
    """Adopt the caller's correlation id when it is well formed, else mint one.

    Lets a request be traced from the browser and ingress through this pod into the MCP fleet, using
    the same `X-Chemclaw-Correlation-Id` header this system sends on connector calls.
    """
    inbound = headers.get(HEADER_CORRELATION, "")
    return inbound if _CORRELATION_ID.match(inbound) else uuid.uuid4().hex


def route_template(scope: Scope) -> str:
    """This request's route template, or `<unmatched>` — never the raw path (see the class).

    Public because the authentication gate logs it on unauthenticated requests.
    """
    path = getattr(scope.get("route"), "path", None)
    return path if isinstance(path, str) and path else _UNMATCHED_ROUTE


async def _answer_internal_error(send: Send, correlation: str) -> None:
    """The 500 a client can act on: one sentence, plus the id to quote in a bug report.

    Exception detail stays in the log; the wire carries a classification and the correlation id.
    """
    body = json.dumps(
        {
            "detail": "The request could not be completed due to an internal error.",
            "correlation_id": correlation,
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 500,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (HEADER_CORRELATION.lower().encode(), correlation.encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _record_request(
    scope: Scope, status: int, elapsed: float, response_bytes: int, correlation: str
) -> None:
    """One INFO record and the two RED series for one served request.

    Skipped when no response was sent at all (a disconnect, a draining pod): `status=0` is not a
    status.
    """
    if not status:
        return
    route = route_template(scope)
    labels = {"route": route, "status_class": f"{status // 100}xx"}
    METRICS.increment("chemclaw_http_requests_total", labels=labels)
    METRICS.observe("chemclaw_http_request_duration_seconds", elapsed, labels={"route": route})
    log_event(
        logger,
        "http.request",
        "%s %s %d in %.1fms",
        scope.get("method", ""),
        route,
        status,
        elapsed * 1000.0,
        route=route,
        method=str(scope.get("method", "")),
        status=status,
        duration_ms=round(elapsed * 1000.0, 1),
        response_bytes=response_bytes,
        # Passed explicitly so the record keeps them even on a handler without `ContextFilter`.
        correlation_id=correlation,
        actor=str(scope.get(_SCOPE_ACTOR, "")),
        # The session the ownership gate resolved, never `path_params`, which holds the
        # unauthenticated caller's raw string before any dependency runs. `bind_request_session`
        # stamps it clipped.
        session_id=str(scope.get(_SCOPE_SESSION, "")),
    )


async def _validation_failed(request: Request, exc: Exception) -> Response:
    """A 422 that leaves a trace: a counter and a WARNING naming the route.

    Errors are counted, not logged in full, and the body is bounded by `_MAX_VALIDATION_ERRORS` and
    `_render_errors`, so the amplification is not rebuilt in the log stack.
    """
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    route = route_template(request.scope)
    METRICS.increment("chemclaw_request_validation_failures_total", labels={"route": route})
    log_event(
        logger,
        "http.validation_failed",
        "%d validation error(s) on %s %s",
        len(errors),
        request.method,
        route,
        level=logging.WARNING,
        route=route,
        method=request.method,
        error_count=len(errors),
        # Locations of the first few errors, clipped: the tail of `loc` can be a caller-chosen
        # string. `isinstance`, as in `_render_errors`, because not every producer of a
        # `RequestValidationError` is pydantic.
        first_locations=[
            clip_for_log(".".join(str(part) for part in e.get("loc", ())))
            for e in errors[:5]
            if isinstance(e, dict)
        ],
    )
    return JSONResponse(
        status_code=422,
        content=jsonable_encoder({"detail": _render_errors(list(errors[:_MAX_VALIDATION_ERRORS]))}),
    )


def _add_request_observability(app: FastAPI) -> None:
    """Install the access log, the RED metrics and the correlation id — see `_RequestObservability`.

    Unconditional: no deployment should serve requests without a record of them.
    """
    app.add_middleware(_RequestObservability)
    app.add_exception_handler(RequestValidationError, _validation_failed)


def _add_body_size_limit(app: FastAPI) -> None:
    """Bound every request body when `service_max_request_bytes` is set (0 disables).

    `BodySizeLimit` lives in `chemclaw.core.asgi`, shared with `connectors.server`.
    """
    if settings.service_max_request_bytes:
        app.add_middleware(BodySizeLimit, max_bytes=settings.service_max_request_bytes)


def _add_security_headers(app: FastAPI) -> None:
    """Add the browser security headers to every response, when `service_security_headers` is on.

    Off only when an ingress applies its own policy. Covers static files, errors and the body cap's
    413 (installed inside this). A CORS preflight is answered by the outermost `CORSMiddleware` and
    carries none, which is harmless: nothing is rendered from it.
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
