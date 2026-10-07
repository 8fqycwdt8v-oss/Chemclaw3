"""The one MCP client-session primitive: connect, classify the failure, decode the answer.

The kernel owns each engine's single client primitive (`core/db.py`, `core/http.py`,
`core/temporal_client.py`); an outbound MCP session is the same kind of thing. It encodes hazards
that are invisible when wrong:

* the connect bound is short even when the read bound is long, so a dead pod fails fast;
* the MCP session's read bound trips before httpx's, because the SDK swallows its own HTTP read
  timeout and never reconnects;
* a rejected credential arrives nested inside an `ExceptionGroup` and must not be classified as an
  outage, since a 401 never recovers on retry;
* `isError=True` covers "refused", "server broke", "server full" and "time budget"; the latter are
  told apart by markers the server writes at the head of the message (`server_marked`), since an
  unanchored match is forgeable from echoed arguments;
* a read timeout gives up locally only, so `cancel_on_timeout` tells the server to stop.

Error classes and their wording stay at each call site, since they are read by a chemist and name a
specific service. This module raises its own exceptions and knows nothing about `ChemclawError`; the
retryable split is the caller's contract with Temporal.
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

# An `httpx` request hook: a coroutine taking the outbound request, called on every redirect hop.
RequestHook = Callable[[httpx.Request], Awaitable[None]]

# TCP/TLS handshake bound, as distinct from how long a tool may take. Not configurable: it answers
# "is this host there at all", and a deployment needing longer has a network problem.
CONNECT_TIMEOUT_SECONDS = 5.0

# How much looser the HTTP read timeout is than the MCP session's own bound. The session bound must
# trip first so the timeout is raised visibly; the HTTP bound is a backstop for a dead connection.
READ_TIMEOUT_GRACE_SECONDS = 5.0

# JSON-RPC codes that blame the request rather than the server (for example `-32602` for arguments
# failing the tool's schema): bad data, and no retry changes it.
REQUEST_FAULT_CODES = frozenset({PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS})

logger = logging.getLogger(__name__)

# What a fleet server says when a tool raised something other than a deliberate domain message
# (`mcp_server_kit.app._sanitize_tool_errors` keeps `ValueError` text and replaces the rest with
# this). FastMCP turns every exception into `isError=True`, so this string is the only wire signal
# separating a retryable server fault from a non-retryable refusal; a reword degrades to
# misclassification.
SERVER_INTERNAL_ERROR = "an internal error occurred"

# What `servers/calc` says when it turned a call away because the pod was full
# (`engine/admission.AT_CAPACITY_MARKER`).
#
# A refused call reaches the wire as one text block with `isError=True`, no structured content and
# no error code, so a fixed token at the head of the message is the only possible channel. Without
# it a full pod would read as bad input and fail a durable calculation non-retryably. Transcribed
# rather than imported (the repositories share no package); each side pins it in a test.
SERVER_AT_CAPACITY = "[calc-at-capacity]"

# What `servers/calc` says when its inline wall clock stopped a calculation
# (`engine/budget.TIME_BUDGET_MARKER`), on the same channel as the capacity marker. It is about the
# pod's load, not the input, so callers must not report it as a property of the molecule. It stays a
# non-retryable refusal (a retry burns the same budget); the marker buys the name. Pinned on each
# side.
SERVER_TIME_BUDGET = "[calc-time-budget]"

# The wrapper the transport puts in front of a tool's own message (`Error executing tool <name>: `).
# Non-greedy to the first `": "`; a served tool name has no colon, and the unserved-name path
# (`Unknown tool: …`) does not match.
_TOOL_ERROR_PREFIX = re.compile(r"^Error executing tool .*?: ")


# The family-wide full-pod refusal format, `[<server>-at-capacity]` (`mcp_server_kit.limits` and
# `core/errors.AtCapacityError`). Matched as a format rather than a list of names because servers
# are addressed by configuration.
_AT_CAPACITY = re.compile(r"\[[a-z0-9][a-z0-9_-]*-at-capacity\]")


def at_capacity(message: str) -> bool:
    """Whether the *server* refused this call because it was full, in the fleet's one format.

    Head-of-message only, as in `server_marked`, so an echoed argument cannot manufacture a retry.

    Args:
        message: The text of a `CallToolResult` carrying `isError=True`.

    Returns:
        True when the message opens with `[<server>-at-capacity]`, allowing for the transport's
        own prefix.
    """
    return _AT_CAPACITY.match(_TOOL_ERROR_PREFIX.sub("", message.lstrip(), count=1)) is not None


def server_marked(message: str, marker: str) -> bool:
    """Whether the *server* opened this refusal with `marker`, rather than quoting it back.

    Servers interpolate caller arguments into domain refusals, so `marker in message` is forgeable:
    a free-form argument could turn a permanent refusal into backoff retries and page on-call. Only
    the server's own placement survives to the head of the message.

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

    Bad data unless a subclass says otherwise. Subclasses (`McpAtCapacity`, `McpTimeBudget`) stay
    inside this hierarchy so existing handlers keep treating them as refusals.
    """


class McpAtCapacity(McpRequestRefused):
    """The server was reached, ran nothing, and refused because it is full.

    The one state where waiting and asking again is correct. A subclass rather than a sibling so a
    caller that does nothing with the distinction keeps its refusal handling.
    """


class McpTimeBudget(McpRequestRefused):
    """The server ran the call and its own wall clock stopped it before an answer.

    A refusal subclass for the same reason as `McpAtCapacity`, but not backpressure: the work was
    done and spent, so nothing makes it retryable.
    """


class McpServerFault(Exception):
    """The server failed while running the call. Transient: the identical call may yet work.

    `internal=True` means the server answered "I broke" (look at its logs); `False` means it stopped
    answering (look at the network). Both are retryable.
    """

    def __init__(self, tool: str, *, internal: bool = False) -> None:
        """Record which tool was in flight and whether the server named the fault itself."""
        super().__init__(tool)
        self.tool = tool
        self.internal = internal


# The code the SDK puts on the `McpError` it raises when a request outlives its read bound: 408, a
# client-side invention no real server error uses. Pinned in `tests/test_upstream_surface.py`.
_READ_TIMEOUT_CODE = int(httpx.codes.REQUEST_TIMEOUT)


def cancel_on_timeout(session: ClientSession) -> None:
    """Make a request that outlives its read bound tell the server to stop, not just give up here.

    On read timeout the SDK raises locally and sends nothing, so the server runs the tool to
    completion (minutes or hours for `calc`) while a retry starts a duplicate. This wraps
    `send_request` to send `notifications/cancelled` for the timed-out request, followed by a
    `ping`:
    over streamable HTTP the server does not observe the notification until more traffic moves on
    the session.

    Reads two upstream privates (`session._request_id`, read just before delegating, which is safe
    because no other task can interleave before `send_request`'s first await; and the 408 code),
    both pinned in `tests/test_upstream_surface.py`. Best-effort in both halves: a failed
    cancellation never replaces the caller's error, and a session lacking these attributes is left
    unwrapped, because `open_session` calls this before marking the connection established and a
    raise here would read as an outage.

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

    `send_request` is the unwrapped bound method, so the flushing ping cannot itself time out,
    re-enter the wrapper and ping again forever.
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

    Unset is not an error: the server decides, and a server that enforces a credential answers 401,
    surfacing as `McpCredentialRefused`.
    """
    return os.environ.get(variable) or None


def short_connect_client(
    read_bound_seconds: float, request_hook: RequestHook | None = None
) -> Callable[..., httpx.AsyncClient]:
    """A `httpx_client_factory` whose *connect* bound is short however long the read bound is.

    `streamablehttp_client(timeout=…)` uses one `httpx.Timeout` for connect, write and pool, so a
    long read bound would also give a black-holed endpoint that long to connect. Built here rather
    than via the SDK's private factory; `follow_redirects=True` is restated from it, since an
    ingress redirecting `/mcp` to `/mcp/` is ordinary. Because redirects are followed, the request
    hook's origin guard is the only layer stripping identity headers on a foreign origin.

    Args:
        read_bound_seconds: The read budget to fall back to when the SDK passes no timeout.
        request_hook: An `httpx` request hook stamping every outbound request — in practice
            `connectors.identity.turn_identity_hook`, which attaches actor, session, correlation
            id and `traceparent` and strips them on a foreign origin. A parameter because `core`
            may not import a sibling package, and the same hook as the connector registry's so
            the origin-strip control exists once.
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

    A rejection is not an outage: a 401 never recovers on its own. It arrives as an
    `httpx.HTTPStatusError` nested in `streamablehttp_client`'s `ExceptionGroup`, so the tree is
    walked. Only 401 and 403 count; other statuses go to the caller's outage path.
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

    `request_hook` (normally `connectors.identity.turn_identity_hook(url)`) stamps trace and
    identity headers so the remote call joins the turn's trace and logs. The credential is a
    connection header because MCP's per-call header callback does not apply to `initialize()`. One
    session per call, since transport tasks inherit the opener's context and a shared session would
    misattribute concurrent callers.

    Raises `McpCredentialRefused` or `McpConnectFailed` when the connection cannot be established.
    Exceptions raised inside the caller's `async with` body pass through untouched (the `connected`
    flag), so an unrelated failure is never relabelled as a service outage.
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

    `McpRequestRefused` carries the server's message; `McpServerFault` means nobody answered or the
    server broke; `McpAtCapacity` means it is full (worth retrying); `McpTimeBudget` means its wall
    clock stopped the work. Only here is a call known to be in flight, so only here can a call
    failure be classified. `None` means the tool succeeded and returned no content; a caller needing
    an object refuses it in its own words.
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
        if at_capacity(message):
            raise McpAtCapacity(f"{tool} was refused: {message}")
        if server_marked(message, SERVER_TIME_BUDGET):
            raise McpTimeBudget(f"{tool} was stopped: {message}")
        raise McpRequestRefused(f"{tool} failed: {message}")
    text = text_of(result.content)
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except ValueError as exc:
        raise McpRequestRefused(f"{tool} returned no JSON: {text[:200]}") from exc


def text_of(content: Any) -> str:
    """The text of an MCP content list, joined — the shape every fleet tool answers in."""
    return "".join(getattr(block, "text", "") for block in content)
