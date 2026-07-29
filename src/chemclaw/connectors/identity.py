"""What travels with a connector call: the turn's identity as headers, and our own credential.

Two separate concerns, deliberately carried by two different mechanisms, because MAF splits them:

- **Identity** (who is asking) is read from the turn's ambient ContextVars — the same ones audit and
  authorization read — by an `httpx` request hook on the connector's own client, so it lands on
  every request the connection makes.
- **Our credential** (who *we* are) is an `httpx.Auth` on that same client, because it must also be
  present on the MCP `session.initialize()` that happens when the connection opens.

**Why a request hook and not MAF's `header_provider`.** MAF offers a `header_provider` callback
that looks purpose-built for this, and it does not work over streamable HTTP. Measured against a
live server: the provider *is* invoked, with the right values, and the server receives nothing.
The reason is that MAF passes the headers through a `ContextVar` set inside `call_tool`
(`agent_framework/_mcp.py:3104-3110`) while the HTTP request is actually issued by the MCP
transport's `post_writer` task — created when the connection opened, so it never sees a variable
set afterwards. A hook on our own client runs *in* that task and reads the ambient identity
there, which is why this works where the provider does not. (MAF's own `security.py:3425-3431`
documents the sibling trap for auth: provider headers are absent during `initialize()`, so a
credential passed that way 401s at connect.)

**Why this is correct per turn.** The transport's tasks inherit the context of whoever opened
the connection, so the identity is only truthful if a connection belongs to exactly one turn —
which is precisely why `chemclaw.agent.chemclaw_agent.connector_tools` builds fresh tools per turn
rather than sharing one set process-wide. Sharing them is not merely inaccurate: two concurrent
turns over one connector tool object deadlock.

**The headers are advisory, never authorization.** Audit (`chemclaw.agent.audit`) and the per-tool
gate
(`chemclaw.agent.tool_authz`) run in core, before the call leaves this process; a connector may log
the
actor to correlate its own records, and it must never make an access decision on a header's word
— anything reachable from outside the trust boundary would be trivially spoofable. This mirrors
the ADR'd HPC identity bridge (`agents/identity/hpc_bridge.py`, architektur.md §7.2): the
downstream runs under our service identity while the requesting user's oid travels with the
request and is logged, so the audit trail can always answer "which real user drove this".
"""

import os
from collections.abc import Generator

import httpx

from chemclaw.agent.dialogue_tools import is_dry_run
from chemclaw.agent.identity_context import get_current_actor, get_current_roles
from chemclaw.agent.session_context import get_current_session_id
from chemclaw.connectors.manifest import BearerAuth, ConnectorAuth, NoAuth

# The header contract, as constants so the connector-side reader and this writer cannot drift.
HEADER_ACTOR = "X-Chemclaw-Actor"
HEADER_ROLES = "X-Chemclaw-Roles"
HEADER_SESSION = "X-Chemclaw-Session"
HEADER_DRY_RUN = "X-Chemclaw-Dry-Run"


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
    roles = get_current_roles()
    if roles:
        # Space-delimited and sorted: the OAuth `scope` convention, and stable so two calls by
        # the same user produce byte-identical headers (a log or a cache keyed on them stays
        # useful).
        headers[HEADER_ROLES] = " ".join(sorted(roles))
    session_id = get_current_session_id()
    if session_id:
        headers[HEADER_SESSION] = session_id
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


async def stamp_turn_identity(request: httpx.Request) -> None:
    """`httpx` request hook: attach the turn's identity headers to one connector request.

    Registered on the connector's own client (`chemclaw.connectors.registry`), so it runs inside
    the task
    that issues the request — the one place that can see the turn's ambient context (see the
    module docstring for why MAF's `header_provider` cannot).
    """
    for header, value in turn_headers().items():
        request.headers[header] = value


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
