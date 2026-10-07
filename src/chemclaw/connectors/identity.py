"""What travels with a connector call that is about the connector: its own credential.

Identity (who is asking) is `chemclaw.core.call_identity`, so non-connector MCP clients can use it
too; read it for the header contract and the redirect strip. The credential (who we are) is an
`httpx.Auth` on the connector's client, so it is also present on the MCP `initialize`, and it lives
here because which credential is declared in the bundle's `connector.yaml`.
"""

import os
from collections.abc import Generator

import httpx

from chemclaw.connectors.manifest import BearerAuth, ConnectorAuth, NoAuth


class MissingConnectorCredential(RuntimeError):
    """A connector declares a bearer credential whose environment variable is unset."""


class _EnvBearerAuth(httpx.Auth):
    """Send `Authorization: Bearer <$env>`, reading the variable per request.

    Read at request time so a rotated secret takes effect without a restart. A missing variable
    raises rather than sending an empty credential, which would surface as an opaque 401.
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
    # Not `assert_never`: `ConnectorAuth` is a plain union, and an unhandled variant must fail
    # loudly rather than silently send no credential.
    raise ValueError(f"connector {connector!r}: unsupported auth mode {type(auth).__name__}")
