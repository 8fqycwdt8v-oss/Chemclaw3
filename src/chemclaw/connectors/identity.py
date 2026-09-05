"""What travels with a connector call: the turn's identity as headers, and our own credential.

Two separate concerns, deliberately carried by two different mechanisms:

- **Identity** (who is asking) is read from the turn's ambient ContextVars — the same ones audit and
  authorization read — by an `httpx` request hook on the connector's own client, so it lands on
  every request the connection makes *to that connector's own origin*, and on no other
  (`turn_identity_hook`).
- **Our credential** (who *we* are) is an `httpx.Auth` on that same client, because it must also be
  present on the MCP `session.initialize()` that happens when the connection opens.

**Why a request hook and not a header-provider callback.** MCP clients typically offer a
per-call header callback that looks purpose-built for this, and it does not work over streamable
HTTP. Measured against a live server: the callback *is* invoked, with the right values, and the
server receives nothing. The reason is that such a callback passes its headers through a
`ContextVar` set inside `call_tool`, while the HTTP request is actually issued by the MCP
transport's `post_writer` task — created when the connection opened, so it never sees a variable
set afterwards. A hook on our own client runs *in* that task and reads the ambient identity there,
which is why this works where the callback does not. The sibling trap is auth: callback headers are
absent during `initialize()` entirely, so a credential passed that way 401s at connect — which is
why our credential is an `httpx.Auth` on the client instead.

**Why this is correct per turn.** The transport's tasks inherit the context of whoever opened
the connection, so the identity is only truthful if a connection belongs to exactly one turn —
which is precisely why a turn opens its own `HeldConnectorSession` rather than sharing one
process-wide, and why the graph itself is compiled per turn (D-2026-08-10). Sharing is not merely
inaccurate: two concurrent turns over one connector session misattribute each other's calls.

**The headers are advisory, never authorization.** Audit (`chemclaw.agent.audit`) and the per-tool
gate
(`chemclaw.agent.tool_authz`) run in core, before the call leaves this process; a connector may log
the
actor to correlate its own records, and it must never make an access decision on a header's word
— anything reachable from outside the trust boundary would be trivially spoofable. It is the same
shape as every other non-Entra transport here (architektur.md §7.2): the downstream runs under our
service identity while the requesting user's oid travels with the request and is logged, so the
audit trail can always answer "which real user drove this".

**Which is exactly why `X-Chemclaw-Roles` is gone**
(`D-2026-08-26-an-entitlement-set-is-not-provenance`). Being advisory is what made it pure cost:
the sentence above says a connector must never decide on it, and correlating records needs the
actor and the correlation id, not the caller's entitlements — so it had one writer and, measured
across both repositories, zero readers. Meanwhile it was the one header with no bound: under
`entra_group_claims_as_roles` it carries every AD group a user is in, to every connector including
servers this family does not host, and the users it grows longest for are the ones
`_principal_from_claims` already warns about. What is sent now is the minimum that makes the trail
joinable.
"""

import os
from collections.abc import Awaitable, Callable, Generator

import httpx

from chemclaw.agent.turn_flags import is_dry_run
from chemclaw.connectors.manifest import BearerAuth, ConnectorAuth, NoAuth
from chemclaw.core.identity_context import (
    get_current_actor,
    get_current_correlation_id,
)
from chemclaw.core.session_context import get_current_session_id
from chemclaw.core.tracing import trace_header_names, trace_headers

# The header contract, as constants so the connector-side reader and this writer cannot drift.
HEADER_ACTOR = "X-Chemclaw-Actor"
HEADER_SESSION = "X-Chemclaw-Session"
# The turn's correlation id, so a connector's own records join to core's audit trail on the same
# key core uses (REV-11). Without it the trail stopped at this process boundary: `agents.audit`
# stamps every in-core tool call with a correlation id, and the connector serving that call logged
# under an id of its own with nothing tying the two together — so "show me everything that happened
# in this turn" was answerable in core and unanswerable across the four runtimes the turn spans.
HEADER_CORRELATION = "X-Chemclaw-Correlation-Id"
HEADER_DRY_RUN = "X-Chemclaw-Dry-Run"

# The four `X-Chemclaw-*` headers this module mints, named as constants so a connector-side reader
# and this writer cannot drift. **This is not the list the origin guard strips** — that list is
# `turn_headers()`'s own keys, because `turn_headers` also emits the W3C trace context and a
# hand-maintained second list is how four of six got stripped and two did not. See
# `_strippable_headers`.
STAMPED_HEADERS = (
    HEADER_ACTOR,
    HEADER_SESSION,
    HEADER_CORRELATION,
    HEADER_DRY_RUN,
)

# The port an origin means when the URL does not spell one out, so a plain `http` host and the
# same host written with an explicit `:80` compare equal — the same normalization httpx's own
# `_same_origin` does before it strips `Authorization`.
_DEFAULT_PORTS = {"http": 80, "https": 443}


class MissingConnectorCredential(RuntimeError):
    """A connector declares a bearer credential whose environment variable is unset."""


def turn_headers() -> dict[str, str]:
    """The current turn's identity as connector headers, read from the ambient ContextVars.

    Absent context yields an absent header rather than an empty one: off the request path (the
    CLI, a worker, a test) there genuinely is no actor, and sending `X-Chemclaw-Actor: ""` would
    let a connector's log claim an anonymous user made the call. The dry-run flag is always
    sent, because "not a dry run" is a real state a connector may want to see.

    Nothing from the tool call itself appears here. The headers say *who* is calling, never
    *what* they asked for: the arguments are model-authored, and folding one into a header would
    put model-controlled text into the transport envelope, where a connector's request log and
    any intermediary would read it as our own metadata.

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
    # W3C trace context, alongside — not instead of — the correlation id above. The custom header
    # was the readiness review's tell that the standard one was missing, and the two answer
    # different questions: a correlation id joins *log lines* after the fact, by grep, and survives
    # where no collector is configured; `traceparent` joins *spans*, live, so a connector's work
    # appears inside the turn that asked for it instead of as an orphan trace nobody looks for.
    # Empty when tracing is off, which is the default, so this adds a boolean read per request.
    headers.update(trace_headers())
    return headers


class _EnvBearerAuth(httpx.Auth):
    """Send `Authorization: Bearer <$env>`, reading the variable per request.

    Reading at request time (not at construction) is what makes a rotated secret take effect
    without a restart: the front door holds one connector tool for the process's whole lifetime,
    so a token captured at import would be pinned to whatever was mounted at startup. A missing
    variable raises rather than sending an empty credential — a 401 from a silently
    unauthenticated call is much harder to diagnose than a named configuration error.
    """

    def __init__(self, token_env: str, connector: str) -> None:
        """Bind the variable name to read and the connector to name in an error."""
        self._token_env = token_env
        self._connector = connector

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        """Attach the bearer credential to `request`, or raise naming the unset variable."""
        token = os.environ.get(self._token_env)
        if not token:
            raise MissingConnectorCredential(
                f"connector {self._connector!r} needs a bearer token in ${self._token_env}, "
                "which is unset or empty"
            )
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


def _strippable_headers() -> frozenset[str]:
    """Every header name `turn_headers()` can produce, for the guard that removes them again.

    **Derived, not restated.** The guard used to walk `STAMPED_HEADERS`, a hand-written tuple of
    the four `X-Chemclaw-*` names — while `turn_headers()` ends with `headers.update(
    trace_headers())`, which adds `traceparent`, `tracestate` and `baggage` when tracing is on. So
    a cross-origin redirect had four of six removed and the trace context copied through to the
    attacker's origin, carrying this deployment's trace and span ids. A second list that has to be
    remembered is the defect; asking the producer what it produces cannot drift from it.

    The trace half comes from `trace_header_names()` rather than from `turn_headers()` itself,
    because the stamp and the strip happen at different moments: `trace_headers()` answers with an
    empty dict once the span has ended, and a redirect hop whose span closed in between would then
    carry a `traceparent` nothing removed. The propagator knows its own field names whether or not
    a span is live.

    Called per stripped request rather than cached, since both halves depend on what this
    deployment has tracing configured to do. It runs only on the redirect path, which is the path
    that must not be fast.
    """
    names = (*STAMPED_HEADERS, *turn_headers(), *trace_header_names())
    return frozenset(name.lower() for name in names)


def _origin(url: httpx.URL) -> tuple[str, str, int]:
    """The (scheme, host, port) an identity header may travel to, default port filled in."""
    return (url.scheme, url.host, url.port or _DEFAULT_PORTS.get(url.scheme, 0))


def turn_identity_hook(endpoint_url: str) -> Callable[[httpx.Request], Awaitable[None]]:
    """Build the `httpx` request hook that stamps the turn's identity for one connector endpoint.

    Registered on the connector's own client (`chemclaw.connectors.registry`), so it runs inside the
    task that issues the request — the one place that can see the turn's ambient context (see the
    module docstring for why a per-call header callback cannot).

    **Bound to the endpoint's origin, and it strips rather than merely skips.** A request hook runs
    on *every* hop of a redirect chain (httpx `_send_handling_redirects`), and httpx builds the
    redirected request from the previous request's headers — dropping only `Authorization`, and
    only cross-origin. So a connector answering `302` toward an origin an attacker controls would
    otherwise have
    harvested the caller's Entra object id, session id and correlation id — plus the W3C trace
    context — on every turn, from a header set that carries identity and nothing else strips.
    (Not "the full role set": `X-Chemclaw-Roles` was deleted by
    `D-2026-08-26-an-entitlement-set-is-not-provenance`, as the module docstring twelve lines above
    this one already says. The guard is unchanged; only this sentence's account of the stakes was
    a snapshot of a header set that no longer exists.) Declining to *re-add* them on a foreign
    origin is not enough, because the copied originals arrive anyway; the hook therefore removes
    them.

    **And it removes everything `turn_headers()` produced, not a list of four.** That function ends
    with `headers.update(trace_headers())`, so `traceparent`, `tracestate` and `baggage` ride along
    with the identity — and the guard walked `STAMPED_HEADERS`, which names only the
    `X-Chemclaw-*` half. See `_strippable_headers`.

    **On the second layer, which exists for some callers and not for the most privileged one.**
    `registry.connector_http_client` sets `follow_redirects=False`, so a bundle's own client never
    reaches this branch. `core.mcp_session.short_connect_client` — the client the calc backend
    uses, which is the hottest and most privileged connection in the system — sets
    `follow_redirects=True`, and for it this hook is the *only* layer. That is why the strip has to
    be complete rather than merely present.

    Args:
        endpoint_url: The connector's effective endpoint URL — the one origin its identity headers
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


def auth_for(auth: ConnectorAuth, connector: str) -> httpx.Auth | None:
    """The `httpx.Auth` for a connector's declared auth mode, or `None` when it needs no credential.

    One dispatch site for the auth union, so adding a mode is one variant in
    `chemclaw.connectors.manifest.ConnectorAuth` plus one branch here.

    Args:
        auth: The connector's declared auth mode.
        connector: The connector's name, for the error message when a credential is missing.

    Returns:
        An `httpx.Auth` to attach to the connector's HTTP client, or `None` for `mode: none`.
    """
    if isinstance(auth, BearerAuth):
        return _EnvBearerAuth(auth.token_env, connector)
    if isinstance(auth, NoAuth):
        return None
    # Deliberately not `assert_never`: `ConnectorAuth` is a plain union, and a variant added
    # without a branch here must fail loudly at build time rather than silently sending no
    # credential.
    raise ValueError(f"connector {connector!r}: unsupported auth mode {type(auth).__name__}")
