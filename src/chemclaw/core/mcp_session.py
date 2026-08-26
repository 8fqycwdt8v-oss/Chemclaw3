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
* `isError=True` covers both "the tool refused you" and "the server fell over", and only the second
  is worth a retry — the sibling repo's `mcp_server_kit` distinguishes them by a fixed string;
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
from collections.abc import AsyncIterator, Callable
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


class McpConnectFailed(Exception):
    """The server could not be reached, so nothing ran. The caller decides what to call it."""


class McpCredentialRefused(Exception):
    """The server was reached and refused this client's credential; `status` is 401 or 403."""

    def __init__(self, status: int) -> None:
        """Record the refusing status so the caller can name it in an operator-facing message."""
        super().__init__(f"HTTP {status}")
        self.status = status


class McpRequestRefused(Exception):
    """The server answered and said no. Bad data: the identical call is refused identically."""


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
    answering". This function reaches into two upstream privates, so an SDK bump that renames either
    would have turned a lost *cancellation* into a total *outage*, with `tests/test_upstream_surface.py`
    going red beside it and every calc job failing regardless. Degrading to today's behaviour — no
    cancellation, the call abandoned locally — is the only acceptable failure mode for an
    enhancement to an otherwise working session. Found by the fake `ClientSession` in
    `tests/test_calc_remote.py`, which implements exactly what `open_session` uses and no more; the
    test fake was right and this function was not.

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


def short_connect_client(read_bound_seconds: float) -> Callable[..., httpx.AsyncClient]:
    """A `httpx_client_factory` whose *connect* bound is short however long the read bound is.

    `streamablehttp_client(timeout=…)` composes one `httpx.Timeout` for connect, write and pool
    alike, so a 900-second read bound also gave a black-holed endpoint 900 seconds to accept a TCP
    connection (measured: `connect 900.0, write 900.0, pool 900.0`).

    The client is built here rather than delegated to `mcp.shared._httpx_utils`, which is the SDK's
    own factory and a *private* module — `tests/test_third_party_layering.py` keeps its
    private-import allow-list deliberately empty. What that factory adds over a plain client is
    `follow_redirects=True`, so that is what is restated: an MCP endpoint behind an ingress that
    redirects `/mcp` to `/mcp/` is ordinary, and httpx does not follow by default.
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
    url: str, *, token_env: str, timeout_seconds: float
) -> AsyncIterator[ClientSession]:
    """Open one MCP session to `url` with the bearer from `token_env` attached.

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
            httpx_client_factory=short_connect_client(timeout_seconds),
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
        if SERVER_INTERNAL_ERROR in message:
            raise McpServerFault(tool, internal=True)
        raise McpRequestRefused(f"{tool} failed: {message}")
    text = text_of(result.content)
    try:
        return json.loads(text)
    except ValueError as exc:
        raise McpRequestRefused(f"{tool} returned no JSON: {text[:200]}") from exc


def text_of(content: Any) -> str:
    """The text of an MCP content list, joined — the shape every fleet tool answers in."""
    return "".join(getattr(block, "text", "") for block in content)
