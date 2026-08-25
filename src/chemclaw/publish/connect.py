"""Building the connection a sink's binding describes, and reading its credentials safely.

Two things happen here and nowhere else, both borrowed from `ingest/eln/warehouse/connect.py`
because they earned their place there:

**Late-binding the driver.** `connection.driver` is a `module:callable` resolved the first time a
connection is needed rather than at import, so a process that never publishes never imports the
client — and a repository with no vendor package installed still runs its whole suite against a
fake named by a test's own binding.

**Reading credentials from the environment, at connect time.** The binding names variables; it
never carries values. Reading late means a rotated secret is picked up by the next connection
rather than the next deploy, and a missing one fails naming the variable instead of surfacing as an
authentication error from inside a client.

**Why this is not `warehouse.connect.open_warehouse` reused verbatim.** That function validates
against `ConnectionBinding`, which is Snowflake-shaped: it has an `account`, a `warehouse` and a
`role`, and **no host or port**. Pointing a Postgres results store at it means either abusing
`account` as a hostname or adding Postgres fields to a model that exists to describe Snowflake —
both of which make one model mean two things. So the connection block here is validated by *the
driver's own signature*, which is the same rule the data-source seam applies to its `config:`
block: the callable is the schema, and `make sink-validate` binds the block against it.
"""

import importlib
import logging
import os
from typing import Any

from chemclaw.core.errors import ChemclawError
from chemclaw.core.logging import register_secret_env

logger = logging.getLogger(__name__)

# The suffix that marks a key as naming an environment variable rather than carrying a value.
# Generic rather than a fixed list of credential names, because each driver has its own: psycopg
# wants a password, Snowflake a private key, and a future one something else. What they share is
# that a secret is *named* here, never written.
_ENV_SUFFIX = "_env"


class SinkConnectionError(ChemclawError):
    """A sink's connection could not be built from its binding."""


def _resolve(reference: str) -> Any:
    """Import `module:callable` and return it, or fail naming both halves of the reference."""
    module_name, _, attribute = reference.partition(":")
    if not module_name or not attribute:
        raise SinkConnectionError(
            f"connection driver {reference!r} is not 'module:callable' "
            "(e.g. 'chemclaw.publish.drivers.postgres:PostgresWarehouse')"
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise SinkConnectionError(
            f"cannot import {module_name!r} for connection driver {reference!r}: {exc}. "
            "A driver's client package is installed only where that store is actually written to."
        ) from exc
    driver = getattr(module, attribute, None)
    if driver is None:
        raise SinkConnectionError(f"{module_name!r} has no attribute {attribute!r}")
    if not callable(driver):
        raise SinkConnectionError(f"{reference!r} is not callable")
    return driver


def connect_options(connection: dict[str, Any]) -> dict[str, Any]:
    """The keyword arguments a driver is built with: addresses from the binding, secrets from env.

    Every `*_env` key becomes its stem, read from the environment. Each variable is registered with
    the log-redaction inventory *before* it is read, so a driver that echoes its own configuration
    into a traceback cannot put a credential in a log.

    An empty variable is treated as absent: an unset secret and one set to the empty string are the
    same failure, and letting the second through would reach the store as an anonymous login.
    """
    options: dict[str, Any] = {}
    for key, value in connection.items():
        if key == "driver":
            continue
        if not key.endswith(_ENV_SUFFIX):
            options[key] = value
            continue
        variable = str(value)
        if not variable:
            continue
        register_secret_env(variable)
        resolved = os.environ.get(variable, "")
        if not resolved:
            raise SinkConnectionError(
                f"the sink binding names {variable!r} for its "
                f"{key[: -len(_ENV_SUFFIX)]!r}, but that environment variable is unset or empty"
            )
        options[key[: -len(_ENV_SUFFIX)]] = resolved
    return options


def open_connection(connection: dict[str, Any]) -> Any:
    """Build whatever `connection.driver` names, from the rest of the block.

    Returns the driver's own object rather than a narrowed type: what a `SqlResultSink` needs is a
    `Warehouse`, and it checks that itself — this function's job is the resolution and the
    credentials, not the contract.
    """
    reference = str(connection.get("driver") or "")
    if not reference:
        raise SinkConnectionError("a sink's `connection:` block must name a `driver:`")
    driver = _resolve(reference)
    options = connect_options(connection)
    logger.info(
        "opening result sink connection via %s (database=%s schema=%s)",
        reference,
        options.get("database", "-"),
        options.get("schema", "-"),
    )
    return driver(**options)
