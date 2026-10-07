"""What travels with an outbound call: the turn's identity, as headers, for one origin.

Reads only the turn's ambient ContextVars (actor, session, correlation id, dry-run flag, W3C trace
context), so any MCP client can use it, connector or not
(`D-2026-09-14-identity-stamping-is-cores-not-a-connectors`). Turning a bundle's declared auth
into an `httpx.Auth` stays in `chemclaw.connectors.identity`.

A request hook on our own client, not an MCP per-call header callback: over streamable HTTP the
request is issued by the transport's writer task, which never sees a ContextVar set in
`call_tool`, while a hook runs in that task. Because transport tasks inherit the context of
whoever opened the connection, a connection must belong to exactly one turn.

The headers are advisory: authorization happens in core before the call leaves, and a server may
log the actor to correlate records but must never make an access decision on a header. Only the
minimum that makes the audit trail joinable is sent; the caller's roles are not.
"""

from collections.abc import Awaitable, Callable

import httpx

from chemclaw.core.identity_context import (
    get_current_actor,
    get_current_correlation_id,
)
from chemclaw.core.session_context import get_current_session_id
from chemclaw.core.tracing import trace_header_names, trace_headers
from chemclaw.core.turn_flags import is_dry_run

# The header contract, as constants so the connector-side reader and this writer cannot drift.
HEADER_ACTOR = "X-Chemclaw-Actor"
HEADER_SESSION = "X-Chemclaw-Session"
# The turn's correlation id, so a connector's records join to core's audit trail on the same key.
HEADER_CORRELATION = "X-Chemclaw-Correlation-Id"
HEADER_DRY_RUN = "X-Chemclaw-Dry-Run"

# The four `X-Chemclaw-*` headers this module mints, as constants so readers cannot drift. Not the
# list the origin guard strips; see `_strippable_headers`.
STAMPED_HEADERS = (
    HEADER_ACTOR,
    HEADER_SESSION,
    HEADER_CORRELATION,
    HEADER_DRY_RUN,
)

# Default ports, so a host with and without its explicit default port compare as one origin (as in
# httpx's own `_same_origin`).


_DEFAULT_PORTS = {"http": 80, "https": 443}


def turn_headers() -> dict[str, str]:
    """The current turn's identity as connector headers, read from the ambient ContextVars.

    Absent context yields an absent header, not an empty one, so a log cannot claim an anonymous
    user.
    The dry-run flag is always sent. Nothing from the tool call itself is included: arguments are
    model-authored and must not enter the transport envelope.

    Returns:
        The headers to attach to this connector request.
    """
    headers = {HEADER_DRY_RUN: "true" if is_dry_run() else "false"}
    actor = get_current_actor()
    if actor is not None:
        headers[HEADER_ACTOR] = actor
    session_id = get_current_session_id()
    if session_id:
        headers[HEADER_SESSION] = session_id
    correlation_id = get_current_correlation_id()
    if correlation_id:
        # Absent rather than empty for the same reason the actor is: off the request path there is
        # genuinely no turn, and an empty id in a connector's log would read as one that exists.
        headers[HEADER_CORRELATION] = correlation_id
    # W3C trace context beside the correlation id: the id joins log lines by grep, `traceparent`
    # joins
    # spans live. Empty when tracing is off (the default).
    headers.update(trace_headers())
    return headers


def _strippable_headers() -> frozenset[str]:
    """Every header name `turn_headers()` can produce, for the guard that removes them again.

    Derived rather than listed, so the trace headers (`traceparent`, `tracestate`, `baggage`) are
    stripped along with the `X-Chemclaw-*` ones. The trace half comes from `trace_header_names()`
    because a span may have ended before the redirect hop. Not cached: it depends on tracing
    configuration and runs only on the redirect path.
    """
    names = (*STAMPED_HEADERS, *turn_headers(), *trace_header_names())
    return frozenset(name.lower() for name in names)


def _origin(url: httpx.URL) -> tuple[str, str, int]:
    """The (scheme, host, port) an identity header may travel to, default port filled in."""
    return (url.scheme, url.host, url.port or _DEFAULT_PORTS.get(url.scheme, 0))


def turn_identity_hook(endpoint_url: str) -> Callable[[httpx.Request], Awaitable[None]]:
    """Build the `httpx` request hook that stamps the turn's identity for one connector endpoint.

    Registered on the connector's own client so it runs in the task that issues the request (see the
    module docstring). Bound to the endpoint's origin: httpx runs the hook on every redirect hop and
    copies the previous request's headers (dropping only `Authorization`), so on a foreign origin
    the
    hook removes every header `turn_headers()` produced. `registry.connector_http_client` refuses
    redirects anyway, but `core.mcp_session.short_connect_client` (the calc backend's client)
    follows
    them, so for it this strip is the only layer.

    Args:
        endpoint_url: The connector's effective endpoint URL, the one origin its identity headers
            may reach.

    Returns:
        The request hook to install on that connector's client.
    """
    allowed = _origin(httpx.URL(endpoint_url))

    async def stamp(request: httpx.Request) -> None:
        """Stamp the turn's identity, or remove it if this request left the connector's origin."""
        if _origin(request.url) != allowed:
            for header in _strippable_headers():
                request.headers.pop(header, None)
            return
        request.headers.update(turn_headers())

    return stamp
