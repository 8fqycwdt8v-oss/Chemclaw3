"""Building the database connection a binding describes, from `chemclaw.core.connect`.

The mechanics — late-binding a `module:callable`, reading `*_env` credentials at connect time and
registering each name with the log-redaction inventory — are not specific to the inbound direction,
and they are not written here. They live in `chemclaw.core.connect`, which the result-sink seam and
the vector-store registry attach through as well, because all three do the same thing: reach a
database this system does not own.

What is left here is the seam's own two contributions, and both matter:

**The error type.** `BindingError` is a `ChemclawError`, which `chemclaw.durable.publish` marks
non-retryable *by exact class name*. A binding naming a driver whose client is not installed fails
identically on every retry, so it must arrive under that name rather than under a shared one — the
reason `core.connect` takes the exception class as a parameter instead of raising its own.

**The `Warehouse` contract.** `open_connection` returns whatever the driver built; this function
declares it a `Warehouse`, which is this package's Protocol. That is a static claim rather than an
`isinstance` gate on purpose: a driver missing `cursor` fails on its first statement with an
`AttributeError` naming the method, and a `runtime_checkable` Protocol check would pass anything
with two attributes of the right names anyway.
"""

import json

from chemclaw.core.connect import open_connection
from chemclaw.ingest.eln.warehouse.binding import BindingError, ConnectionBinding
from chemclaw.ingest.eln.warehouse.driver import Warehouse

# One live connection per distinct `connection:` block, for the life of the process. Keyed on the
# block rather than on the source name because the block is what decides *which database* — two
# manifests naming one lakehouse with one credential are one connection, and `pistachio` and
# `eln-databricks` (different catalogues, deliberately different tokens) are two.
_OPEN: dict[str, Warehouse] = {}


def _key(block: dict[str, object]) -> str:
    """A stable string for one connection block. `default=str` because a value may be anything."""
    return json.dumps(block, sort_keys=True, default=str)


def open_warehouse(connection: ConnectionBinding) -> Warehouse:
    """The `Warehouse` this binding describes, opened once per process. Raises `BindingError`.

    **Reused rather than reopened, and that is what makes the missing `close()` defensible.**
    `Warehouse` has no teardown method on the written premise that "the data-source seam builds a
    half and never disposes it — so a connection lives for the process's life by design". The
    premise was about the *seam*, and the seam does not behave that way: `active_retrieve_sources()`
    is a list comprehension with no memoisation, run inside the `gather_evidence` tool body, so a
    fresh `WarehouseVectorRetriever` was built on every tool call — measured, 100 constructions for
    100 sweeps — and each one opened a SQL session nothing could close. Server-side that state
    outlives the object by an idle timeout measured in tens of minutes, so a busy chat pod exhausts
    the workspace's sessions within the hour; the symptom is a `WarehouseQueryError`, which is to
    say silently-missing evidence.

    Caching the *connection* rather than the retrieve half is what fixes it without inventing a
    second lifecycle. Constructing a half is free by design (`_connection` and `_index_store` are
    both lazy precisely so a chat pod's startup opens nothing), so what had to become
    process-lived is the thing the protocol already claims is: the connection.

    A driver whose session dies under it still recovers on its own — the protocol says so, and
    `DatabricksWarehouse._session_lost` is what implements it — so a handle held here is never a
    handle known to be dead.
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

    For tests, which prime a fake warehouse per test and would otherwise be served the one the
    previous test primed. Deliberately *not* `close_…`: there is nothing to close, which is the
    whole subject of `open_warehouse`'s docstring, and a name promising teardown that does not
    happen is the kind of claim `tests/test_degraded.py` exists to stop.
    """
    _OPEN.clear()
