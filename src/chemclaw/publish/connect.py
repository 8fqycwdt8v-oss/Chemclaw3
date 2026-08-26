"""Building the connection a sink's binding describes, from `chemclaw.core.connect`.

The mechanics live in `chemclaw.core.connect` — late-binding a `module:callable`, reading `*_env`
credentials at connect time, registering each name with the log-redaction inventory — because the
outbound direction attaches exactly the way the inbound one does.

**This module used to hold its own copy of them**, and its docstring said why: the warehouse seam's
`ConnectionBinding` was Snowflake-shaped, with an `account`, a `warehouse` and a `role` and no host
or port, so pointing a Postgres results store at it meant either abusing `account` as a hostname or
adding Postgres fields to a model describing Snowflake. That model is gone
(`D-2026-08-26-the-driver-s-signature-is-the-schema`) and with it the reason for two copies: both
seams now validate a connection block against *the driver's own signature*, which is what this
module argued for in the first place.

What stays here is `SinkConnectionError`. `chemclaw.durable.publish` marks non-retryable errors by
exact class name, so which exception a failed attachment raises is a retry contract — a shared error
class would quietly make the outbound direction retry something the inbound one gives up on.
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

    Public because `make sink-validate` resolves the driver *without* connecting, in order to bind
    the block against its signature — the offline half of "the driver's signature is the schema".
    """
    return _resolve_driver(reference, error=SinkConnectionError, what="connection driver")


def open_connection(connection: Mapping[str, Any]) -> Any:
    """Build whatever `connection.driver` names, from the rest of the block.

    Returns the driver's own object rather than a narrowed type: what a `SqlResultSink` needs is a
    `Warehouse`, and it checks that itself — this function's job is the resolution and the
    credentials, not the contract.
    """
    return _open_connection(connection, error=SinkConnectionError, what="result sink connection")
