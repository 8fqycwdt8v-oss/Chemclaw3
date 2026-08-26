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

from chemclaw.core.connect import open_connection
from chemclaw.ingest.eln.warehouse.binding import BindingError, ConnectionBinding
from chemclaw.ingest.eln.warehouse.driver import Warehouse


def open_warehouse(connection: ConnectionBinding) -> Warehouse:
    """Build the `Warehouse` this binding describes. Raises `BindingError` if it cannot."""
    block = {"driver": connection.driver, **connection.options}
    warehouse: Warehouse = open_connection(block, error=BindingError, what="warehouse connection")
    return warehouse
