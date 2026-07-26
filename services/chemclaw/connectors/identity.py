"""What travels with a connector call: the turn's identity as headers, and our own credential.

Two separate concerns, deliberately carried by two different mechanisms, because MAF splits them:

- **Identity** (who is asking) changes per tool call and is read from the turn's ambient
  ContextVars — the same ones audit and authorization read. MAF's `header_provider` hook is invoked
  once per `call_tool` and injects the result into that call's HTTP requests
  (`agent_framework/_mcp.py:3085-3110`), so nothing has to be rebuilt per turn and two concurrent
  turns can never see each other's identity.
- **Our credential** (who *we* are) must be on *every* request, including the MCP
  `session.initialize()` that happens when the tool's context is entered. `header_provider` is
  explicitly not called then — MAF's own `security.py:3425-3431` documents that using it for auth
  produces 401s at connect that surface as opaque cancel-scope errors — so the credential goes on
  the `httpx.AsyncClient` as an `httpx.Auth`, which applies to the whole connection.

**The headers are advisory, never authorization.** Audit (`agents.audit`) and the per-tool gate
(`agents.tool_authz`) run in core, before the call leaves this process; a connector may log the
actor to correlate its own records, and it must never make an access decision on a header's word —
anything reachable from outside the trust boundary would be trivially spoofable. This mirrors the
ADR'd HPC identity bridge (`agents/identity/hpc_bridge.py`, architektur.md §7.2): the downstream
runs under our service identity while the requesting user's oid travels with the request and is
logged, so the audit trail can always answer "which real user drove this".
"""

import os
from collections.abc import Generator
from typing import Any

import httpx

from agents.dialogue_tools import is_dry_run
from agents.identity_context import get_current_actor, get_current_roles
from agents.session_context import get_current_session_id
from connectors.manifest import BearerAuth, ConnectorAuth, NoAuth

# The header contract, as constants so the connector-side reader and this writer cannot drift.
HEADER_ACTOR = "X-Chemclaw-Actor"
HEADER_ROLES = "X-Chemclaw-Roles"
HEADER_SESSION = "X-Chemclaw-Session"
HEADER_DRY_RUN = "X-Chemclaw-Dry-Run"


class MissingConnectorCredential(RuntimeError):
    """A connector declares a bearer credential whose environment variable is unset."""


def turn_headers(_kwargs: dict[str, Any] | None = None) -> dict[str, str]:
    """The current turn's identity as connector headers (MAF's `header_provider` signature).

    Absent context yields an absent header rather than an empty one: off the request path (the CLI,
    a worker, a test) there genuinely is no actor, and sending `X-Chemclaw-Actor: ""` would let a
    connector's log claim an anonymous user made the call. The dry-run flag is always sent, because
    "not a dry run" is a real state a connector may want to see.

    Args:
        _kwargs: The tool call's arguments, which MAF passes to a header provider. Unused: the
            headers describe *who* is calling, never *what* they asked for — folding an argument
            into a header would put model-controlled data into the transport envelope.

    Returns:
        The headers to attach to this call's HTTP requests.
    """
    headers = {HEADER_DRY_RUN: "true" if is_dry_run() else "false"}
    actor = get_current_actor()
    if actor is not None:
        headers[HEADER_ACTOR] = actor
    roles = get_current_roles()
    if roles:
        # Space-delimited and sorted: the OAuth `scope` convention, and stable so two calls by the
        # same user produce byte-identical headers (a log or a cache keyed on them stays useful).
        headers[HEADER_ROLES] = " ".join(sorted(roles))
    session_id = get_current_session_id()
    if session_id:
        headers[HEADER_SESSION] = session_id
    return headers


class _EnvBearerAuth(httpx.Auth):
    """Send `Authorization: Bearer <$env>`, reading the variable per request.

    Reading at request time (not at construction) is what makes a rotated secret take effect without
    a restart: the front door holds one connector tool for the process's whole lifetime, so a token
    captured at import would be pinned to whatever was mounted at startup. A missing variable raises
    rather than sending an empty credential — a 401 from a silently unauthenticated call is much
    harder to diagnose than a named configuration error.
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


def auth_for(auth: ConnectorAuth, connector: str) -> httpx.Auth | None:
    """The `httpx.Auth` for a connector's declared auth mode, or `None` when it needs no credential.

    One dispatch site for the auth union, so adding a mode is one variant in
    `connectors.manifest.ConnectorAuth` plus one branch here.

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
    # Deliberately not `assert_never`: `ConnectorAuth` is a plain union, and a variant added without
    # a branch here must fail loudly at build time rather than silently sending no credential.
    raise ValueError(f"connector {connector!r}: unsupported auth mode {type(auth).__name__}")
