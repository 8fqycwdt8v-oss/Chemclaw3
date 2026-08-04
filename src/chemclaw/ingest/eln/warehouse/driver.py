"""The narrow database seam the binding engine runs against — Protocols, and nothing else.

This module imports no driver and no third-party package, on purpose. It is what lets the rest of
the engine — SQL generation, mapping, unit conversion, similarity search — be exercised in CI
against a fake, on a machine with no warehouse and no vendor client installed. The real client
lives alone in `chemclaw.ingest.eln.warehouse.snowflake`, imported only when a binding names it.

**Why a Protocol rather than reusing `chemclaw.core.db`.** That module is the *application's*
Postgres: one DSN from settings, one pool, one statement timeout. A warehouse is a different thing
attached differently — its credentials come from a manifest's environment names, its connection is
per-source rather than per-process, and its dialect is not Postgres. Sharing the helper would have
meant teaching it about both, which is how a connection helper becomes a configuration union.

**`placeholder` is on the connection because parameter style is a dialect fact.** Snowflake binds
`?`, psycopg binds `%s`, and the alternative to asking is a module-level `paramstyle` mutation in
the vendor client — a global that every other user of that client in the process inherits. Asking
keeps `sql.py` dialect-neutral and lets a test assert the exact string that would be sent.
"""

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, runtime_checkable

from chemclaw.core.errors import ChemclawError


class WarehouseQueryError(ChemclawError):
    """A query the warehouse refused: a relation that does not exist, a column, a type.

    A `ChemclawError` (so a `ValueError`), which `chemclaw.durable.publish` marks non-retryable by
    class name. That is the right stance here: a binding naming a column the site renamed will fail
    identically on every retry, and burning a Temporal retry budget on it only delays the operator
    seeing the message. An *unreachable* warehouse is the opposite case and raises `ConnectionError`
    instead — the same split `chemclaw.core.db` makes, for the same reason.
    """


@runtime_checkable
class WarehouseCursor(Protocol):
    """One in-flight statement. Rows come back as column-keyed dicts, never tuples.

    Dicts because the whole engine is column-name-driven: a binding says `AMOUNT_G`, and resolving
    that through a positional index would mean threading the `SELECT` list's order through every
    layer that touches a row. It also means a fake is a list of dicts.
    """

    async def execute(self, sql: str, params: Sequence[Any]) -> None:
        """Run `sql`, binding `params` positionally in the connection's placeholder style."""
        ...

    async def fetchall(self) -> list[dict[str, Any]]:
        """Every remaining row of the last `execute`, each keyed by column name."""
        ...


@runtime_checkable
class Warehouse(Protocol):
    """A connected warehouse the engine can query. One per data source, built from its binding."""

    @property
    def placeholder(self) -> str:
        """The parameter marker this connection binds with (`?` for Snowflake, `%s` for psycopg)."""
        ...

    def cursor(self) -> AbstractAsyncContextManager[WarehouseCursor]:
        """A cursor for one statement, released on exit."""
        ...

    async def close(self) -> None:
        """Release the connection. Idempotent — the engine may close a source more than once."""
        ...
