"""What travels with a connector call that is *about the connector*: its own credential.

One concern, since `D-2026-09-14-identity-stamping-is-cores-not-a-connectors` moved the other one.

- **Identity** (who is asking) is `chemclaw.core.call_identity`. It reads the turn's ambient
  ContextVars and nothing about connectors, and it had to leave so that a *non-connector* MCP
  client could reach it — `chemclaw.ingest.labels.labeller` could not, because
  `ingest -> connectors` is not an edge `tests/test_layering.py` permits, so its leg to the
  labelling server ran for hours inside a durable activity carrying `Authorization` and nothing
  else.
- **Our credential** (who *we* are) is an `httpx.Auth` on the connector's client, because it must
  also be present on the MCP `session.initialize()` that happens when the connection opens — and
  because *which* credential is a fact declared in a bundle's `connector.yaml`, which is what makes
  it this module's and not core's. It is the `connectors.manifest` import below that draws the
  line, and it is the only first-party import here outside `core`.

Read `chemclaw.core.call_identity` for the header contract, the redirect strip, and why a request
hook rather than a per-call header callback.
"""

import os
from collections.abc import Generator

import httpx

from chemclaw.connectors.manifest import BearerAuth, ConnectorAuth, NoAuth


class MissingConnectorCredential(RuntimeError):
    """A connector declares a bearer credential whose environment variable is unset."""


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
