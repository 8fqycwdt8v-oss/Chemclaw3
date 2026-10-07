"""Building the connection a sink's binding describes, from `chemclaw.core.connect`.

The mechanics (late-binding a `module:callable`, reading `*_env` credentials, registering them
for log redaction) are shared with the inbound seam; both validate a connection block against
the driver's own signature. What stays here is `SinkConnectionError`: `chemclaw.durable.publish`
marks non-retryable errors by class name, so this seam's error class is a retry contract.
"""

from collections.abc import Mapping
from typing import Any

from chemclaw.core.connect import open_connection as _open_connection
from chemclaw.core.connect import resolve_driver as _resolve_driver
from chemclaw.core.errors import ChemclawError


class SinkConnectionError(ChemclawError):
    """A sink's connection could not be built from its binding."""


def resolve_driver(reference: str) -> Any:
    """Import the `module:callable` a sink's `connection:` block names, under this seam's error.

    Public because `make sink-validate` resolves the driver without connecting, to bind the block
    against its signature offline.
    """
    return _resolve_driver(reference, error=SinkConnectionError, what="connection driver")


def open_connection(connection: Mapping[str, Any]) -> Any:
    """Build whatever `connection.driver` names, from the rest of the block.

    Returns the driver's own object; the sink checks the contract itself.
    """
    return _open_connection(connection, error=SinkConnectionError, what="result sink connection")
