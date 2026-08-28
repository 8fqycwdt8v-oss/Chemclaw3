"""The front door's cross-cutting HTTP armor: headers, body caps, CORS, and the fail-closed boots.

Everything here applies to *every* request or to the process as a whole — nothing is specific to a
route, which is the line that separates this module from `chemclaw/api/routes/` (R3.2). `create_app`
(`api/app.py`) is the only caller of the installers; the `_SecurityHeaders` middleware itself is
pure ASGI for the streaming-safety reason its docstring carries.
"""

import json
import logging
import re
import time
import uuid

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from chemclaw.connectors.identity import HEADER_CORRELATION
from chemclaw.core.asgi import BodySizeLimit
from chemclaw.core.config import settings
from chemclaw.core.http import LOOPBACK_HOSTS
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
    logger.warning("shedding %s %s: %s", request.method, request.url.path[:256], exc)
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
        "shedding %s %s: %s", request.method, request.url.path[:256], exc, exc_info=exc.__cause__
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
    if settings.entra_required or settings.service_host in LOOPBACK_HOSTS:
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


def _refuse_public_llm_exposure() -> None:
    """Fail closed when a network-exposed process sends its LLM traffic to the public vendor API.

    `llm_provider="anthropic"` with no `llm_base_url` builds a client pointed at the public
    Anthropic API (api.anthropic.com; agent/llm_provider._anthropic_model passes no base_url),
    so a real deployment on that default sends every prompt, tool result and completion —
    user free text and confidential chemistry — to a third-party SaaS instead of the internal
    gateway. The client fails closed on a missing *credential* but not on this *destination*, and
    D2's measurement confirmed the constructed client resolves to the public host.

    Gated on the same non-loopback-bind signal as `_refuse_unauthenticated_exposure`, and checked
    here at app boot rather than in the `Settings` validator, so it fires for an actual serving
    process without breaking the many enforced-posture `Settings(...)` constructions in tests and in
    `Chemclaw3_mock` that legitimately never dial an LLM. Loopback dev on a developer's own
    Anthropic key is untouched; `openai_compatible` + `llm_base_url` (the shipped chart) satisfies
    it, as does an `llm_base_url` naming an anthropic-compatible gateway.
    """
    if settings.service_host in LOOPBACK_HOSTS or settings.llm_base_url:
        return
    if settings.llm_provider == "anthropic":
        raise RuntimeError(
            "SECURITY: this process binds a non-loopback interface "
            f"({settings.service_host!r}) with llm_provider='anthropic' and no "
            "CHEMCLAW_LLM_BASE_URL "
            "— every prompt and completion (confidential chemistry) would go to the public "
            "Anthropic API rather than the internal gateway. Set "
            "CHEMCLAW_LLM_PROVIDER=openai_compatible with CHEMCLAW_LLM_BASE_URL pointing at the "
            "internal endpoint (what the shipped chart does), or set CHEMCLAW_LLM_BASE_URL to an "
            "anthropic-compatible gateway you host. The bare anthropic provider is the "
            "loopback/dev "
            "path only."
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


class _RequestObservability:
    """One record per HTTP request: the access log, the RED metrics, and the correlation id.

    **There was no first-party record of an HTTP request at all.** The only one was uvicorn's own
    access line — client address, method, *raw path*, status — with no latency, no route template,
    no actor, no session, no correlation id (the three rendered `-`, because nothing outside
    `run_turn` ever stamped them) and no byte count. There was no HTTP metric of any kind either,
    so "what is p95 on `/jobs`", "which route is returning 5xx" and "is this slow because of the
    model or the database" were all unanswerable from outside the process.

    **Pure ASGI, never `BaseHTTPMiddleware`**, for the reason `_SecurityHeaders` above gives in
    full: that wrapper runs the app as a second task through a memory stream and turns every
    cancelled SSE stream into a spurious 500 — 44 of them in one 50-user run, every one on the
    turn route. This wraps `send` and reads `scope`; the body is never re-tasked.

    **`route` is the FastAPI route *template*, never the raw path.** The raw path is
    attacker-controlled, so it is both a metric-cardinality bomb and a redaction cost: a 115 KB
    request line reaching the redaction filter through uvicorn's access log stalled a pod for 21 s
    with the logging lock held, *unauthenticated* (`core/logging.py`). `APIRoute.matches` writes
    the matched route into the scope, and Starlette merges that child scope into this one — so the
    template is readable here once the app has run, and an unmatched request (a 404 on a bogus
    path, a static file, a redirect) collapses onto one fixed `<unmatched>` series instead of one
    per URL anybody cares to invent.

    **Where it sits in the stack, and what that buys.** `create_app` installs this *first*, which
    under Starlette's `insert(0)` semantics makes it the innermost user middleware — inside the
    security headers, inside the body cap, and outside `ExceptionMiddleware`. Inside the security
    headers is what fixes the 500: Starlette's own `ServerErrorMiddleware` sits above every user
    middleware, so a default 500 was served with none of the browser security headers on it, and
    with no correlation id for a chemist to quote. Answering the 500 here means it carries both.
    Outside `ExceptionMiddleware` is what makes the status honest: the 401s, 404s, 422s and 429s
    the handlers produce are all seen here as ordinary responses.

    What that ordering deliberately leaves out is the 413 from `BodySizeLimit`, which runs above
    this and answers without ever calling down. It is counted by its own
    `chemclaw_requests_too_large_total` and logged where it is refused, so the fact survives — it
    just is not in this log line. A CORS preflight is outside it for the same reason.

    **The label set is inside the registry's cap, measured rather than assumed.** `core/metrics`
    refuses a counter past 64 label series (D-152). Across 158 front-door tests this counter grew
    **35** series and no route produced more than three status classes, so three per route is the
    worst case: 20 templates plus `<unmatched>` is 63 against 64 — safe, and one route away from
    not being. `tests/test_api_observability.py` asserts that arithmetic, so the route that would
    make it start dropping series fails a test instead of silently under-reporting in production.
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
                answered = True
                # `setdefault`, so a route that already carries an id of its own keeps it. Every
                # response gets one: 22 of the 23 routes used to give a client nothing to quote in
                # a bug report, and only the SSE turn path put an id on its error event.
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
                    _route_template(scope),
                    correlation,
                    extra={"correlation_id": correlation},
                )
                if answered:
                    # The response is already on the wire (an SSE stream that died mid-answer), so
                    # there is nothing truthful left to send — and **`status` is left alone**:
                    # the client was told 200 and booking a 500 here would put a status on the
                    # counter that nothing ever answered with. The log line above is the record.
                    raise
                status = 500
                await _answer_internal_error(send, correlation)
        finally:
            _record_request(
                scope, status, time.perf_counter() - started, response_bytes, correlation
            )
            _reset_request_identity(scope)
            reset_current_correlation_id(token)


# Marks a scope this middleware is serving, so the two binders below are no-ops in a stack that
# does not carry it (a route exercised directly, an app built without the middleware). A binder
# that stamped an ambient nobody resets would leak one request's identity into the next.
_SCOPE_BOUND = "chemclaw.observed"
# Where a bound identity's reset token is parked for `_RequestObservability`'s `finally`. The
# **middleware owns every reset**, because it is the one frame that runs on every exit path: a
# FastAPI dependency has no teardown unless it is a generator, and turning the authentication gate
# into one would change the shape of every route that depends on it.
_SCOPE_IDENTITY_TOKEN = "chemclaw.identity_token"
_SCOPE_SESSION_TOKEN = "chemclaw.session_token"
# The actor for the access-log line, stamped by the authentication gate once it knows one.
_SCOPE_ACTOR = "chemclaw.actor"

# The route label for a request that matched no route. A fixed literal, so the whole family stays
# bounded by the route table (a source constant) rather than by what a caller puts in a URL.
_UNMATCHED_ROUTE = "<unmatched>"

# What an inbound correlation id may look like to be adopted. Hex, dashes and underscores, bounded
# — the shape this system's own ids (a `uuid4().hex`) and an ingress-generated request id both
# take. Anything else is replaced rather than sanitised: the id is written into log lines, into
# `audit_events` and into a response header, and a value that reaches all three is not a field to
# be generous about. A *literal* rather than a setting, because it is a format, not a threshold —
# there is no deployment for which a different answer is right.
_CORRELATION_ID = re.compile(r"\A[A-Za-z0-9_-]{8,64}\Z")

# How many pydantic error objects a 422 body may carry. Pydantic materialises one per bad list
# element, so an unbounded render turns a linear body into a linear response: measured at 683,520
# errors and ~32 MB from a 2 MB body, on the pod's single uvicorn worker, reachable by any
# authenticated caller (`docs/archive/lessons-2026-08.md`). Its own webhook body is already bounded
# in `api/routes/proposals.py` for exactly this reason; this bounds every other route the same way,
# without changing the shape a client reads (`detail` is still a list of error objects).
_MAX_VALIDATION_ERRORS = 20


def bind_request_actor(request: Request, actor: str, roles: frozenset[str]) -> None:
    """Make the authenticated caller ambient for the rest of this request.

    Called from `require_principal`, which is the one funnel every authenticated route passes
    through — so this is the same "a gate a new route cannot forget" argument that put the rate
    budget there. What it buys is the ~30 WARNING sites already in this tree: they log under a
    `ContextFilter` that renders `actor=-` on every non-turn route today, because `run_turn` was
    the only thing in the process that ever stamped one.

    A no-op outside `_RequestObservability`, which owns the reset — see `_SCOPE_BOUND`.
    """
    if not request.scope.get(_SCOPE_BOUND):
        return
    request.scope[_SCOPE_ACTOR] = actor
    request.scope[_SCOPE_IDENTITY_TOKEN] = set_current_identity(actor, roles)


def bind_request_session(request: Request, session_id: str) -> None:
    """Make the resolved session ambient for the rest of this request.

    Called from the session-ownership gate (`api/deps.resolve_session`) rather than from the
    middleware, because the session id is a *routed* path parameter: the router runs below this
    middleware, so at request entry there is nothing to bind and the raw path is the wrong place
    to look for one. Same no-op rule and same reset owner as `bind_request_actor`.
    """
    if not request.scope.get(_SCOPE_BOUND):
        return
    request.scope[_SCOPE_SESSION_TOKEN] = set_current_session_id(session_id)


def _reset_request_identity(scope: Scope) -> None:
    """Undo whatever the two binders above stamped, in reverse order."""
    session_token = scope.pop(_SCOPE_SESSION_TOKEN, None)
    if session_token is not None:
        reset_current_session_id(session_token)
    identity_token = scope.pop(_SCOPE_IDENTITY_TOKEN, None)
    if identity_token is not None:
        reset_current_identity(identity_token)


def _request_correlation_id(headers: Headers) -> str:
    """Adopt the caller's correlation id when it is well formed, else mint one.

    Adopting is what makes a chemist's click traceable from the browser through the ingress into
    this pod and on into the MCP fleet: `X-Chemclaw-Correlation-Id` is the header this system
    already *sends* on every connector call (`connectors/identity.py`, one definition, imported
    here rather than respelled), and until now it was never *read* — an id the UI or the ingress
    generated was silently replaced by one nobody upstream had.
    """
    inbound = headers.get(HEADER_CORRELATION, "")
    return inbound if _CORRELATION_ID.match(inbound) else uuid.uuid4().hex


def _route_template(scope: Scope) -> str:
    """This request's route template, or `<unmatched>` — never the raw path (see the class)."""
    path = getattr(scope.get("route"), "path", None)
    return path if isinstance(path, str) and path else _UNMATCHED_ROUTE


async def _answer_internal_error(send: Send, correlation: str) -> None:
    """The 500 a client can act on: one sentence, plus the id to quote in a bug report.

    Worded as `runner.failure_event` words a failed turn, and for the same reason — the exception
    detail (a DSN, a driver error, a workflow id) stays in the log line above, and what crosses the
    wire is a classification plus the key the audit trail is joined on.
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

    Skipped entirely for a request that produced no response at all — a disconnect during
    admission, a pod draining mid-stream. `status=0` is not a status, and booking it as one would
    put a fabricated class on the counter every operator reads as "what did we answer".
    """
    if not status:
        return
    route = _route_template(scope)
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
        # The three context keys, passed explicitly so `ContextFilter`'s `setdefault` keeps them.
        # Explicit rather than left to the ambient stamp, because the filter lives on the *handler*
        # — so a process that has not run `configure_logging`, or a handler somebody added later,
        # would drop the one field this record exists to be joined on.
        correlation_id=correlation,
        actor=str(scope.get(_SCOPE_ACTOR, "")),
        session_id=str((scope.get("path_params") or {}).get("session_id", "")),
    )


async def _validation_failed(request: Request, exc: Exception) -> Response:
    """A 422 that leaves a trace — measured, a validation failure emitted **zero** log records.

    So a client looping on a malformed body was indistinguishable from silence: no log line, no
    metric, and a response the caller alone ever saw. The counter is what makes it alertable and
    the WARNING is what says which route and how badly.

    The errors are counted rather than rendered into the log, and the body is capped at
    `_MAX_VALIDATION_ERRORS` — see that constant for the measurement. A handler that logged
    `exc.errors()` would rebuild the amplification inside the log stack instead of on the wire,
    which is the worse of the two places for it.
    """
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    route = _route_template(request.scope)
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
        # The *locations* of the first few, which name the offending fields without echoing their
        # values back into the log.
        first_locations=[".".join(str(part) for part in e.get("loc", ())) for e in errors[:5]],
    )
    return JSONResponse(
        status_code=422, content=jsonable_encoder({"detail": errors[:_MAX_VALIDATION_ERRORS]})
    )


def _add_request_observability(app: FastAPI) -> None:
    """Install the access log, the RED metrics and the correlation id — see `_RequestObservability`.

    Unconditional: there is no deployment for which "serve requests and keep no record of them" is
    the right posture, and the one knob that would express it (turn the access log off) is
    uvicorn's, in `deploy/entrypoint.sh`, where it is now redundant with this.
    """
    app.add_middleware(_RequestObservability)
    app.add_exception_handler(RequestValidationError, _validation_failed)


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
