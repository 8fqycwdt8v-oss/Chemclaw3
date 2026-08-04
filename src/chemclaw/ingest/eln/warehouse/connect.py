"""Building the warehouse connection a binding describes, and reading its credentials safely.

Shared by both halves because both connect the same way and neither should own the other's copy of
it. Two things happen here and nowhere else:

**Late-binding the driver.** `connection.driver` is a `module:callable`, resolved the moment a
connection is first needed rather than at import. That is the same mechanism the data-source seam
uses for its own halves and it buys the same property here: a process that never queries the
warehouse never imports the vendor client, and a repository with no client installed still passes
its whole test suite against a fake driver named by a test's own binding.

**Reading credentials from the environment, at connect time.** The binding names variables; it never
carries values. Reading them late means a rotated secret is picked up by the next connection rather
than the next deploy, and a missing one fails with a message naming the variable instead of an
authentication error from a vendor client.
"""

import importlib
import logging
import os
from typing import Any

from chemclaw.core.logging import register_secret_env
from chemclaw.ingest.eln.warehouse.binding import BindingError, ConnectionBinding
from chemclaw.ingest.eln.warehouse.driver import Warehouse

logger = logging.getLogger(__name__)

# The binding fields naming a credential, and the connect keyword each one supplies.
_CREDENTIALS = {
    "account_env": "account",
    "user_env": "user",
    "password_env": "password",
    "private_key_env": "private_key",
}


def _resolve_driver(reference: str) -> Any:
    """Import `module:callable` and return it, or fail naming both halves of the reference."""
    module_name, _, attribute = reference.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise BindingError(
            f"cannot import {module_name!r} for warehouse driver {reference!r}: {exc}. "
            "A driver's client package is installed only where that warehouse is actually reached."
        ) from exc
    driver = getattr(module, attribute, None)
    if driver is None:
        raise BindingError(f"{module_name!r} has no attribute {attribute!r} (from {reference!r})")
    if not callable(driver):
        raise BindingError(f"{reference!r} is not callable")
    return driver


def connect_options(connection: ConnectionBinding) -> dict[str, Any]:
    """The keyword arguments a driver is built with: addresses from the binding, secrets from env.

    Every credential variable is registered with the log-redaction inventory *before* it is read, so
    a driver that echoes its own configuration into a traceback cannot put a private key into a log.
    An empty variable is treated as absent — an unset secret and one set to the empty string are the
    same failure, and letting the second through would reach the warehouse as an anonymous login.
    """
    options: dict[str, Any] = {}
    for field, keyword in _CREDENTIALS.items():
        variable: str = getattr(connection, field)
        if not variable:
            continue
        register_secret_env(variable)
        value = os.environ.get(variable, "")
        if not value:
            raise BindingError(
                f"the warehouse binding names {variable!r} for its {keyword}, "
                "but that environment variable is unset or empty"
            )
        options[keyword] = value

    for field in ("warehouse", "database", "role"):
        if value := getattr(connection, field):
            options[field] = value
    if connection.db_schema:
        options["schema"] = connection.db_schema
    options["query_timeout_seconds"] = connection.query_timeout_seconds
    return options


def open_warehouse(connection: ConnectionBinding) -> Warehouse:
    """Build the `Warehouse` this binding describes. Raises `BindingError` if it cannot."""
    driver = _resolve_driver(connection.driver)
    options = connect_options(connection)
    logger.info(
        "opening warehouse via %s (database=%s schema=%s)",
        connection.driver,
        connection.database or "-",
        connection.db_schema or "-",
    )
    warehouse: Warehouse = driver(**options)
    return warehouse
