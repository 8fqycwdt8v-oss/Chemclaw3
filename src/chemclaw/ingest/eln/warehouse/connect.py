"""Building the database connection a binding describes, from `chemclaw.core.connect`.

The mechanics (late-binding a `module:callable`, reading `*_env` credentials at connect time,
registering them for log redaction) live in `chemclaw.core.connect`, shared with sinks and vector
stores. This module adds two things:

- **The error type.** `BindingError` is a `ChemclawError`, which `chemclaw.durable.publish` treats
  as non-retryable by exact class name; a missing driver fails identically on every retry.
- **The `Warehouse` contract.** A static claim rather than an `isinstance` gate: a driver missing
  `cursor` fails on its first statement naming the method, and a structural check would prove
  little.
"""

import json

from chemclaw.core.connect import open_connection
from chemclaw.ingest.eln.warehouse.binding import BindingError, ConnectionBinding
from chemclaw.ingest.eln.warehouse.driver import Warehouse

# One live connection per distinct `connection:` block for the life of the process, keyed on the
# block because it decides which database: two manifests with the same block share one connection.
_OPEN: dict[str, Warehouse] = {}


def _key(block: dict[str, object]) -> str:
    """A stable string for one connection block. `default=str` because a value may be anything."""
    return json.dumps(block, sort_keys=True, default=str)


def open_warehouse(connection: ConnectionBinding) -> Warehouse:
    """The `Warehouse` this binding describes, opened once per process. Raises `BindingError`.

    Reused because the seam rebuilds retrieve halves on every tool call; a connection per
    construction would leak server-side SQL sessions until the workspace ran out. Halves stay cheap
    to construct (their connection is lazy) and the connection is process-lived, which is why
    `Warehouse` has no `close()`. A driver whose session dies recovers on its own
    (`DatabricksWarehouse._session_lost`).
    """
    block = {"driver": connection.driver, **connection.options}
    key = _key(block)
    cached = _OPEN.get(key)
    if cached is not None:
        return cached
    warehouse: Warehouse = open_connection(block, error=BindingError, what="warehouse connection")
    _OPEN[key] = warehouse
    return warehouse


def forget_open_warehouses() -> None:
    """Drop every remembered connection, so the next call opens a fresh one.

    For tests that prime a fake warehouse each. Not named `close_…`: nothing is closed.
    """
    _OPEN.clear()
