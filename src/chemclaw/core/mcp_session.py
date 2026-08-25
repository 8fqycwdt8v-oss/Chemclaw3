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
  is worth a retry — the sibling repo's `mcp_server_kit` distinguishes them by a fixed string.

The second client (the reaction labeller) made that a duplication rather than a one-off, and 150
lines of measured hazard-handling is the last thing to copy. What stays at each call site is the
part that genuinely differs: the error *classes* and their wording, because those are read by a
chemist and name a specific service.

**This module raises its own three exceptions and knows nothing about `ChemclawError`.** The
retryable/non-retryable split is the caller's contract with Temporal, and the caller is the only
one that knows which of its two error classes a given failure belongs in.
"""

import json
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.exceptions import McpError
from mcp.types import INVALID_PARAMS, INVALID_REQUEST, METHOD_NOT_FOUND, PARSE_ERROR

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
