"""The one MCP client-session primitive: connect, classify the failure, decode the answer.

Beside `core/db.py` (the connection pool), `core/http.py` (the HTTP client factory) and
`core/temporal_client.py` (the client-per-process) — the kernel owns each engine's single
primitive on everyone's behalf, and an outbound MCP session is that same kind of thing.

**Why this is here rather than duplicated.** `connectors/calc/remote.py` worked out, against a live
server, four things that are easy to get wrong and invisible when you do:

* the *connect* bound must be short even when the *read* bound is fifteen minutes, or a deleted pod
  stalls a durable activity for a quarter of an hour per attempt while the heartbeat reports it
  healthy;
* the MCP session's read bound must trip *before* httpx's, because `mcp.client.streamable_http`
  catches its own read timeout at debug level and never reconnects — so the answer is lost silently
  and the caller waits forever, where the session bound raises and names the timeout;
* a rejected credential arrives as an `httpx.HTTPStatusError` nested inside the `ExceptionGroup`
  that `streamablehttp_client`'s task group raises, so the tree has to be walked; and it must not be
  classified as an outage, because a 401 never comes back on its own and a durable job would spend
  its whole retry budget being told the same thing;
* `isError=True` covers three different answers — "the tool refused you", "the server fell over"
  and "the server is full" — and the first is the only one no retry can fix. The wire carries no
  error code and no structured content on that path, so each of the other two is told apart by a
  fixed string the serving side puts at the **head** of the message (`SERVER_INTERNAL_ERROR`,
  `SERVER_AT_CAPACITY`) — matched at that position by `server_marked`, because a domain refusal
  quotes the caller's own arguments back and an unanchored match is therefore forgeable from one;
* a call that hits the read bound gives up **locally only** — the SDK raises and sends the server
  nothing — so the server runs the tool to completion and throws the answer away
  (`cancel_on_timeout`).

The second client (the reaction labeller) made that a duplication rather than a one-off, and 150
lines of measured hazard-handling is the last thing to copy. What stays at each call site is the
part that genuinely differs: the error *classes* and their wording, because those are read by a
chemist and name a specific service.

**This module raises its own three exceptions and knows nothing about `ChemclawError`.** The
retryable/non-retryable split is the caller's contract with Temporal, and the caller is the only
one that knows which of its two error classes a given failure belongs in.
"""

import json
import logging
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.exceptions import McpError
from mcp.types import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    CancelledNotification,
    CancelledNotificationParams,
    ClientNotification,
    ClientRequest,
    EmptyResult,
    PingRequest,
)

from chemclaw.core.http import default_ssl_context

# An `httpx` request hook: one coroutine taking the outbound request, called on every hop of a
# redirect chain. Named here so both this module's two seams and their callers spell one type, and
# so the parameter reads as what it is rather than as an opaque callable.
RequestHook = Callable[[httpx.Request], Awaitable[None]]

# How long to wait for the TCP/TLS handshake, as distinct from how long a tool may take to answer.
# Deliberately not a config field: it is a property of "is this host there at all", the same for
# every server, and a deployment that needs a longer one has a network problem a setting would only
# hide. Short, because a dark server must degrade quickly.
CONNECT_TIMEOUT_SECONDS = 5.0

# How much looser the HTTP read timeout is than the MCP session's own bound. The two must not be
# equal, and their order is the fix rather than a tuning detail — see the module docstring's second
# bullet. The visible bound must always be the one that trips; the invisible one is a backstop for
# a connection that stops producing bytes entirely.
READ_TIMEOUT_GRACE_SECONDS = 5.0

# The JSON-RPC codes that blame the request rather than the server. FastMCP answers `-32602` for
# arguments that fail a tool's own schema before its body ever runs, which is the "atom index past
# the molecule" class: bad data, and no retry changes it.
REQUEST_FAULT_CODES = frozenset({PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS})

logger = logging.getLogger(__name__)

# What a fleet server says when a tool raised something that was *not* a deliberate domain message.
# `Chemclaw3-mcp`'s `mcp_server_kit.app._sanitize_tool_errors` re-raises a `ValueError` cause
# untouched — that is the worded refusal — and replaces everything else with this exact string,
# logging the real exception server-side.
#
# **Matching it is the difference between a retry and a dead job.** FastMCP turns *every* exception
# in a tool body into `isError=True`, so a subprocess timeout, a non-zero exit and a full scratch
# directory all arrive looking exactly like a domain refusal — which callers register non-retryable.
# A string is a weak contract and it is the only signal on the wire; if the server ever rewords it,
# this stops matching and the behaviour degrades to a misclassification rather than a new failure.
SERVER_INTERNAL_ERROR = "an internal error occurred"

# What `Chemclaw3-mcp`'s `servers/calc` says when it turned a call away because the pod was full
# rather than because the request was wrong (`engine/admission.AT_CAPACITY_MARKER`).
#
# **The wire has no other channel, and that is measured rather than assumed.** `mcp.server.lowlevel`
# builds a refused call's answer with `_make_error_result`, which is a `CallToolResult` carrying one
# text block, `isError=True`, and *no* `structuredContent` and no error code; FastMCP's `Tool.run`
# has already flattened every exception type into one `ToolError` before that. Driven against the
# running server on 2026-09-05, a saturation refusal arrives as `isError=True`,
# `structuredContent=None`, and text beginning `Error executing tool <name>: [calc-at-capacity] …`.
# So a fixed token in the message is the only thing that can carry this, exactly as
# `SERVER_INTERNAL_ERROR` above already does for "the server broke".
#
# **What it is worth is the difference between backpressure and a dead job.** Without it a full pod
# is indistinguishable from an unparameterised solvent: `McpRequestRefused` -> `CalcToolError` ->
# `_BAD_DATA_TYPES` -> non-retryable, so a durable calculation failed on its first attempt carrying
# the serving side's own advice to retry. Under load "full" is the normal state, which is what makes
# a third class necessary rather than tidy.
#
# The literal is transcribed rather than imported: the two repositories share no package, so
# nothing detects a reword automatically — each side pins the spelling it expects in a test of its
# own (`tests/test_calc_remote.py` here, the calc server's admission test there), which
# fails whoever changes one side, not whoever changes the other.
SERVER_AT_CAPACITY = "[calc-at-capacity]"

# The one wrapper the transport puts in front of a tool's own message: `Tool.run` raises
# `ToolError(f"Error executing tool {self.name}: {e}")` and `_make_error_result` puts `str(e)` on
# the wire unchanged, so a marker the server wrote at the head of its message arrives either bare
# or behind exactly this. Non-greedy to the first `": "`, which is the server's own separator — a
# served tool name carries no colon, and the *unserved* name path (`Unknown tool: …`, the one place
# a caller's string opens the message) does not match this at all.
_TOOL_ERROR_PREFIX = re.compile(r"^Error executing tool .*?: ")


def server_marked(message: str, marker: str) -> bool:
    """Whether the *server* opened this refusal with `marker`, rather than quoting it back.

    **`marker in message` is forgeable from a tool argument, and both markers were matched that
    way.** These servers word their domain refusals with the caller's own strings interpolated —
    `servers/calc`'s solvent check raises "…has no parameters for {name!r}…" and its xTB wrapper
    does the same with `method` — and `solvent` is a free-form argument on the tool surface. So
    `solvent="[calc-at-capacity]"` came back as a refusal *containing* the token, was classified
    `McpAtCapacity`, and turned a permanently bad input into ~28 minutes of backoff plus an
    increment of `chemclaw_calc_backend_at_capacity_total` — the series the shipped alert rule pages
    "scale the calculation tier" on. A caller could manufacture that page from an argument.
    Reproduced end to end before this function existed.

    Both sides' prose already claimed the position was what made this safe: `AT_CAPACITY_MARKER` is
    documented as placed "at the *head* of the message so it survives every wrapping the transport
    applies". Only the wrapping the *server* applies survives to the head; an echoed argument lands
    in the middle. This is that claim made true, and it is used for `SERVER_INTERNAL_ERROR` as well,
    which is the same forgery with a worse consequence — that one raises `McpServerFault(internal=
    True)`, which callers count on `chemclaw_degraded_total` and read as "the backend is dark".

    Args:
        message: The text of a `CallToolResult` carrying `isError=True`.
        marker: The fixed token the serving side writes at the head of that message.

    Returns:
        True when the message begins with `marker`, allowing for the transport's own prefix.
    """
    return _TOOL_ERROR_PREFIX.sub("", message.lstrip(), count=1).startswith(marker)


class McpConnectFailed(Exception):
    """The server could not be reached, so nothing ran. The caller decides what to call it."""


class McpCredentialRefused(Exception):
    """The server was reached and refused this client's credential; `status` is 401 or 403."""

    def __init__(self, status: int) -> None:
        """Record the refusing status so the caller can name it in an operator-facing message."""
        super().__init__(f"HTTP {status}")
        self.status = status


class McpRequestRefused(Exception):
    """The server answered and said no.

    Bad data unless a subclass says otherwise: the identical call is refused identically. The one
    subclass that says otherwise is `McpAtCapacity`, which is a refusal about the *server's* state
    rather than about the request — kept inside this hierarchy so that every existing handler keeps
    treating it as the refusal it is, and only a caller that has something better to do with
    backpressure has to know it exists.
    """


class McpAtCapacity(McpRequestRefused):
    """The server was reached, ran nothing, and refused because it is full.

    The third state the two classes around this one did not have. `McpRequestRefused` means the
    request was wrong and `McpServerFault` means the server broke; a busy pod is neither, and it is
    the only one of the three where waiting and asking again is the correct response.

    A subclass rather than a sibling, deliberately: `invoke` has exactly two callers today
    (`connectors/calc/remote.py` and `ingest/labels/labeller.py`) and only the first has any use for
    the distinction, so a sibling would have silently escaped the second's `except` clauses. Under
    this hierarchy a caller that does nothing keeps the behaviour it had.
    """


class McpServerFault(Exception):
    """The server failed while running the call. Transient: the identical call may yet work.

    `internal` separates the two shapes, because they read differently to whoever is looking at the
    message: `True` means the server answered "I broke" (`SERVER_INTERNAL_ERROR`), `False` means it
    stopped answering at all. Both are retryable and the distinction is not decorative — an
    operator chasing the first looks at the server's logs, and the second at the network.
    """

    def __init__(self, tool: str, *, internal: bool = False) -> None:
        """Record which tool was in flight and whether the server named the fault itself."""
        super().__init__(tool)
        self.tool = tool
        self.internal = internal


# The JSON-RPC code the SDK puts on the `McpError` it raises when a request outlives its read
# bound. It is `httpx.codes.REQUEST_TIMEOUT` (408), which is a *client-side* invention rather than
# anything a server sent — `mcp.shared.session.send_request` builds it around its own
# `anyio.fail_after`. That is what makes it usable as the signal here: a server answering a genuine
# JSON-RPC error never uses it. Pinned in `tests/test_upstream_surface.py`.
_READ_TIMEOUT_CODE = int(httpx.codes.REQUEST_TIMEOUT)


def cancel_on_timeout(session: ClientSession) -> None:
    """Make a request that outlives its read bound tell the server to stop, not just give up here.

    **The read bound bounds our wait and nothing else, and that was the whole of the defect.**
    `mcp.shared.session.send_request` waits inside `anyio.fail_after` and, on expiry, raises
    `McpError` and sends the server nothing at all — no `notifications/cancelled` — while the
    session stays open for the rest of the turn (`connectors.transport`). Measured against a running
    server: a 30 s tool behind a 2 s bound ran to completion with nobody holding the answer, and was
    interrupted only when the session was finally torn down.

    That is affordable for a dictionary lookup and not for what this fleet actually hosts.
    `Chemclaw3-mcp`'s `calc` server is documented as "a call here can be minutes or hours,
    deliberately", and `science/calc/store.cached_compute` retries a miss — so an abandoned CREST
    search kept a pod's CPU while the next attempt started a second identical one beside it.

    The server half already exists: `mcp.shared.session` cancels the in-flight request when it
    receives the notification. Only the client never sent one.

    **The ping is not decoration, and it is the part that took measuring.** Sending the notification
    alone is *not* enough over streamable HTTP: the POST is issued and answered 202, and the server
    session does not observe it until further traffic moves on that session — reproducibly, the
    cancelled tool ran on for the full ten seconds the probe waited and the server-side handler
    logged nothing. Following it with a `ping` on the same session makes delivery deterministic
    (measured across repeated runs, both ways). One round trip on a path that has just spent its
    whole timeout is a cost worth paying, and it doubles as evidence the session is still usable.

    **Two upstream shapes are read here, both pinned in `tests/test_upstream_surface.py`.** The id
    to cancel is `session._request_id` — the counter `send_request` claims and increments — read
    immediately before delegating, which is safe under concurrency because a coroutine runs
    synchronously to its first `await` and `send_request`'s first is the stream write, so no other
    task can interleave between this read and that increment. The second is `_READ_TIMEOUT_CODE`.

    Best-effort by construction, in **both** halves. A transport that is already gone cannot carry
    a cancellation, and failing the call for that would replace a wasted computation with a lost
    error; and a session that does not expose what this needs is left alone rather than refused.

    That second half is the one worth stating, because getting it wrong is much worse than the
    defect this fixes. `open_session` calls this *before* it marks the connection established, so
    anything raised here is classified as `McpConnectFailed` — "the calculation service is not
    answering". This function reaches into two upstream privates, so an SDK bump renaming either
    would have turned a lost *cancellation* into a total *outage*, with
    `tests/test_upstream_surface.py` going red beside it and every calc job failing regardless.
    Degrading to today's behaviour — no cancellation, the call abandoned locally — is the only
    acceptable failure mode for an enhancement to an otherwise working session. Found by the fake
    `ClientSession` in `tests/test_calc_remote.py`, which implements exactly what `open_session`
    uses and no more; the test fake was right and this function was not.

    Args:
        session: A live `ClientSession`, wrapped in place. Called once per session, right after
            `initialize()`. A session not exposing `send_request`/`_request_id` is returned
            unwrapped.
    """
    send_request = getattr(session, "send_request", None)
    if send_request is None or not hasattr(session, "_request_id"):
        logger.warning(
            "this MCP client session exposes no %s, so a call that outlives its read bound will be "
            "abandoned without telling the server to stop; see core/mcp_session.cancel_on_timeout",
            "send_request" if send_request is None else "_request_id",
        )
        return

    async def send_request_cancelling(*args: Any, **kwargs: Any) -> Any:
        """Delegate, and on a read-bound timeout ask the server to abandon that request."""
        request_id = session._request_id
        try:
            return await send_request(*args, **kwargs)
        except McpError as exc:
            if exc.error.code != _READ_TIMEOUT_CODE:
                raise
            await _ask_server_to_cancel(session, request_id, send_request)
            raise

    session.send_request = send_request_cancelling  # type: ignore[method-assign]


async def _ask_server_to_cancel(session: ClientSession, request_id: int, send_request: Any) -> None:
    """Send `notifications/cancelled` for `request_id` and flush it, swallowing whatever that costs.

    Separate from `cancel_on_timeout` so the "never let this failure replace the caller's" rule is
    one small function with one `except`, rather than a nested `try` inside the wrapper where a
    later edit could let it escape.

    `send_request` is the **unwrapped** bound method, and passing it rather than calling
    `session.send_ping()` is what stops this recursing: the ping is subject to the same read bound,
    so a session that has stopped answering entirely would time out here too, re-enter the wrapper,
    and ping again — forever. The ping is a flush, not a request anyone is waiting on, so it must
    not be able to schedule more work of its own.
    """
    try:
        await session.send_notification(
            ClientNotification(
                CancelledNotification(
                    method="notifications/cancelled",
                    params=CancelledNotificationParams(
                        requestId=request_id,
                        reason="the caller's request timeout expired",
                    ),
                )
            )
        )
        # See `cancel_on_timeout`: without this the notification sits undelivered until the session
        # sees other traffic, which the last tool call of a turn never does.
        await send_request(ClientRequest(PingRequest(method="ping")), EmptyResult)
    # Broad on purpose: the caller's `McpError` is the error that matters, and no failure to
    # deliver a courtesy cancellation may replace it.
    except Exception:
        logger.warning(
            "could not tell the connector to cancel request %s; it may run to completion with "
            "nobody waiting for the answer",
            request_id,
            exc_info=True,
        )


def bearer_from_env(variable: str) -> str | None:
    """The bearer held in `variable`, or `None` when it is unset or empty.

    Unset is not an error here, and the reason is that the server decides: a development server
    started without a credential accepts an unauthenticated call, and refusing to send one would
    make this module reject a request the server would have served. A server that *does* enforce
    one answers 401, which surfaces as `McpCredentialRefused`.
    """
    return os.environ.get(variable) or None


def short_connect_client(
    read_bound_seconds: float, request_hook: RequestHook | None = None
) -> Callable[..., httpx.AsyncClient]:
    """A `httpx_client_factory` whose *connect* bound is short however long the read bound is.

    `streamablehttp_client(timeout=…)` composes one `httpx.Timeout` for connect, write and pool
    alike, so a 900-second read bound also gave a black-holed endpoint 900 seconds to accept a TCP
    connection (measured: `connect 900.0, write 900.0, pool 900.0`).

    The client is built here rather than delegated to `mcp.shared._httpx_utils`, which is the SDK's
    own factory and a *private* module — `tests/test_third_party_layering.py` keeps its
    private-import allow-list deliberately empty. What that factory adds over a plain client is
    `follow_redirects=True`, so that is what is restated: an MCP endpoint behind an ingress that
    redirects `/mcp` to `/mcp/` is ordinary, and httpx does not follow by default.

    **That flag is why the request hook's origin guard has to be complete here.** The connector
    registry's own client sets `follow_redirects=False` and treats the hook as a second layer
    (`connectors/registry.py`); this one follows, so for the calc backend — the connection that
    carries the most privileged and most frequent traffic in the system — the hook is the *only*
    layer. Turning the flag off here would match the registry and is the obvious next narrowing;
    it is not taken in the same change as the guard's own fix because it is a live-integration
    change (an ingress or a `CHEMCLAW_CALC_SERVER_URL` written with a trailing slash starts failing
    instead of redirecting), and it needs a run against a real calc server to settle rather than an
    argument.

    Args:
        read_bound_seconds: The read budget to fall back to when the SDK passes no timeout.
        request_hook: An `httpx` request hook to stamp every outbound request — in practice
            `connectors.identity.turn_identity_hook`, which attaches the turn's actor, session,
            correlation id and W3C `traceparent`, and strips *everything it attached* again on a
            foreign origin. It is a *parameter* rather than an import because `chemclaw.core` may
            not depend on a sibling package (`tests/test_layering.py`), and it is deliberately the
            same hook the connector registry installs rather than a second one: the origin-strip
            guard it carries is a security control, and two copies is how one of them stops
            covering everything the other stamps.
    """

    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        bound = timeout if timeout is not None else httpx.Timeout(read_bound_seconds)
        return httpx.AsyncClient(
            headers=headers,
            auth=auth,
            follow_redirects=True,
            # One process-wide trust store; see `core.http.default_ssl_context` for what building
            # one per client cost the event loop (156.1 ms per turn across the connector fleet).
            verify=default_ssl_context(),
            # the calc backend is a loopback/in-cluster Service; ignore ambient proxies
            trust_env=False,
            event_hooks={"request": [request_hook]} if request_hook is not None else {},
            timeout=httpx.Timeout(
                bound.read,
                connect=CONNECT_TIMEOUT_SECONDS,
                write=bound.write,
                pool=bound.pool,
            ),
        )

    return factory


def auth_rejection(exc: BaseException) -> int | None:
    """The HTTP status if this connect failure was the server *refusing the credential*.

    A rejection is not an outage, and the difference is the retry: an unreachable host comes back,
    a 401 never does on its own. Measured against a live server: a bad bearer surfaces as
    `httpx.HTTPStatusError` (401) nested inside the `ExceptionGroup` that `streamablehttp_client`'s
    task group raises, so neither `except httpx.HTTPStatusError` nor a single `__cause__` hop finds
    it — the tree has to be walked.

    Only 401 and 403 count. Every other status is left to the caller's outage path, because a 500
    or a 502 genuinely is the server failing and genuinely may pass on retry.
    """
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, httpx.HTTPStatusError) and current.response.status_code in (
            401,
            403,
        ):
            return current.response.status_code
        stack.extend(getattr(current, "exceptions", ()))
        for nested in (current.__cause__, current.__context__):
            if nested is not None:
                stack.append(nested)
    return None


@asynccontextmanager
async def open_session(
    url: str, *, token_env: str, timeout_seconds: float, request_hook: RequestHook | None = None
) -> AsyncIterator[ClientSession]:
    """Open one MCP session to `url` with the bearer from `token_env` attached.

    **`request_hook` is how this connection stops being anonymous.** The header dict built below
    carried `Authorization` and nothing else, so the calculation backend — which is every
    calculation this system runs, on the `cached_compute` miss path, minute-scale work with every
    minute inside a remote call — received no `traceparent`, no `X-Chemclaw-Correlation-Id`, no
    actor and no session. A trace stopped dead at that boundary and a log line on either side had
    nothing to join on. Passing `connectors.identity.turn_identity_hook(url)` closes both at once,
    because that hook already produces exactly this set; see `short_connect_client`.

    The credential is a *connection* header rather than a per-call one, for the reason
    `connectors.identity` records: MCP's per-call header callback is not applied to the
    `initialize()` that opens the connection, so a credential passed that way 401s at connect.

    A session per call, not one per process: the MCP transport's tasks inherit the context of
    whoever opened the connection, so a shared session misattributes concurrent callers to each
    other.

    Raises `McpCredentialRefused` or `McpConnectFailed` when the connection cannot be established.
    Anything raised inside the caller's `async with` body passes through **untouched** — that is
    what the `connected` flag is for, and it is not a nicety: relabelling those was a live defect,
    where a Postgres outage inside the body was reported to the chemist as "the calculation service
    is not answering" and, worse, was reclassified from bad data into a retryable outage.
    """
    token = bearer_from_env(token_env)
    headers = {"Authorization": f"Bearer {token}"} if token else None
    connected = False
    try:
        async with streamablehttp_client(
            url,
            headers=headers,
            timeout=timedelta(seconds=timeout_seconds),
            sse_read_timeout=timedelta(seconds=timeout_seconds + READ_TIMEOUT_GRACE_SECONDS),
            httpx_client_factory=short_connect_client(timeout_seconds, request_hook),
        ) as (read, write, _):
            async with ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=timeout_seconds)
            ) as session:
                await session.initialize()
                cancel_on_timeout(session)
                connected = True
                yield session
    except Exception as exc:
        if connected:
            raise
        rejected = auth_rejection(exc)
        if rejected is not None:
            raise McpCredentialRefused(rejected) from exc
        raise McpConnectFailed(url) from exc


async def invoke(session: ClientSession, tool: str, arguments: dict[str, Any]) -> Any:
    """Call one tool and return its decoded JSON payload, or raise the failure to classify.

    `McpRequestRefused` carries the server's own message, because that message is the whole content
    of the refusal. `McpServerFault` means nobody answered, or the server answered that it broke.
    `McpAtCapacity` — a subclass of the first — means the server answered that it is full, which is
    the one refusal that is worth asking again about.

    This is the only place that can classify a failure of the *call*, because it is the only place
    that knows a call was in flight — `open_session`'s guard deliberately stops at the connection.
    """
    try:
        result = await session.call_tool(tool, arguments)
    except McpError as exc:
        if exc.error.code in REQUEST_FAULT_CODES:
            raise McpRequestRefused(f"{tool} was refused: {exc.error.message}") from exc
        raise McpServerFault(tool) from exc
    except Exception as exc:
        raise McpServerFault(tool) from exc
    if result.isError:
        message = text_of(result.content)
        # `server_marked` rather than `marker in message`: a domain refusal quotes the caller's own
        # arguments back, so an unanchored match let a tool argument mint either classification.
        if server_marked(message, SERVER_INTERNAL_ERROR):
            raise McpServerFault(tool, internal=True)
        if server_marked(message, SERVER_AT_CAPACITY):
            raise McpAtCapacity(f"{tool} was refused: {message}")
        raise McpRequestRefused(f"{tool} failed: {message}")
    text = text_of(result.content)
    try:
        return json.loads(text)
    except ValueError as exc:
        raise McpRequestRefused(f"{tool} returned no JSON: {text[:200]}") from exc


def text_of(content: Any) -> str:
    """The text of an MCP content list, joined — the shape every fleet tool answers in."""
    return "".join(getattr(block, "text", "") for block in content)
