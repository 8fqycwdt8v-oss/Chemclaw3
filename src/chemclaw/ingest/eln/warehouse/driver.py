"""The narrow database seam the binding engine runs against — Protocols, and nothing else.

Imports no driver or third-party package, so SQL generation, mapping and search are tested against a
fake. The real client lives in `chemclaw.ingest.eln.warehouse.databricks`.

Not `chemclaw.core.db`: that is the application's own Postgres (one DSN, one pool); a warehouse
takes credentials from a manifest, connects per source and speaks another dialect.

Dialect facts live on the connection so `sql.py` stays neutral: `placeholder` (`?` vs `%s`) and
`vector_dialect` (the similarity function's name and how a query vector is bound — against a native
vector cast, or as a scalar parsed server-side). A driver with no dialect cannot serve a `vector:`
block and says so.
"""

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, runtime_checkable

from chemclaw.core.errors import ChemclawError


class WarehouseQueryError(ChemclawError):
    """A query the warehouse refused: a relation that does not exist, a column, a type.

    A `ChemclawError`, which `chemclaw.durable.publish` treats as non-retryable: a renamed column
    fails identically every time. An unreachable warehouse raises `ConnectionError` instead, as
    `chemclaw.core.db` does.
    """


@runtime_checkable
class VectorDialect(Protocol):
    """How one warehouse spells a similarity search. Owned by the driver, used by `sql.py`.

    Two methods for the two things vendors differ on; `sql.py` writes the rest.
    """

    def similarity(self, metric: str) -> tuple[str, str]:
        """The function that computes `metric`, and the direction it sorts.

        Returned together so a distance is never paired with a descending sort (or vice versa),
        which would silently return the least similar rows.

        Raises:
            WarehouseQueryError: This warehouse has no function for that metric; non-retryable.
        """
        ...

    def query_vector(self, placeholder: str, vector: Sequence[float], dim: int) -> tuple[str, Any]:
        """The expression standing in for the query vector, and the single value bound into it.

        A pair because the vector is a value `sql.py` never writes into a statement, and its
        encoding differs (native list vs parsed scalar). `dim` is the embedding width a typed cast
        needs.
        """
        ...


@runtime_checkable
class WarehouseCursor(Protocol):
    """One in-flight statement. Rows come back as column-keyed dicts, never tuples.

    The engine is column-name driven (a binding says `AMOUNT_G`), and a fake is then just a list of
    dicts.
    """

    async def execute(self, sql: str, params: Sequence[Any]) -> None:
        """Run `sql`, binding `params` positionally in the connection's placeholder style."""
        ...

    async def fetchall(self) -> list[dict[str, Any]]:
        """Every remaining row of the last `execute`, each keyed by column name."""
        ...


@runtime_checkable
class BatchingCursor(Protocol):
    """A cursor that can run one statement over many parameter sets in one go.

    A separate Protocol because sites bring their own drivers
    (D-2026-08-26-the-driver-s-signature-is-the-schema): requiring `executemany` on
    `WarehouseCursor` would break every two-method driver for an optimisation. Probed by
    `execute_many`. psycopg runs the sets in pipeline mode, one round trip for N statements.
    """

    async def executemany(self, sql: str, params_seq: Sequence[Sequence[Any]]) -> None:
        """Run `sql` once per entry in `params_seq`, binding each positionally.

        A bulk bind of one statement. What a failed set leaves behind is driver-specific (psycopg
        rolls back the whole set even under autocommit; a looping driver keeps earlier entries), so
        only idempotent upserts may use this.
        """
        ...


async def execute_many(
    cursor: WarehouseCursor, sql: str, params_seq: Sequence[Sequence[Any]]
) -> None:
    """Run `sql` over every parameter set — in one round trip where the driver can, else in N.

    The one place the optional capability is probed; a `runtime_checkable` `isinstance` is a
    `hasattr` check, which is the right test.
    """
    if isinstance(cursor, BatchingCursor):
        await cursor.executemany(sql, params_seq)
        return
    for params in params_seq:
        await cursor.execute(sql, params)


@runtime_checkable
class Warehouse(Protocol):
    """A connected warehouse the engine can query. One per data source, built from its binding."""

    @property
    def placeholder(self) -> str:
        """The parameter marker this connection binds with (`?` for Databricks, `%s` psycopg)."""
        ...

    @property
    def vector_dialect(self) -> "VectorDialect | None":
        """How this warehouse spells a similarity search, or `None` if it cannot do one.

        `None` is a real answer: a binding with a `vector:` block against such a driver is refused
        with a message naming the driver, rather than the server rejecting an unknown function.
        """
        ...

    def cursor(self) -> AbstractAsyncContextManager[WarehouseCursor]:
        """A cursor for one statement, released on exit.

        The only method; there is no `close` because nothing could call it: retrieve halves are
        rebuilt per call and discarded. `connect.open_warehouse` keeps one connection per
        `connection:` block for the process's life. A driver whose session can die drops it on a
        transient failure and reconnects on the next call (`DatabricksWarehouse._session_lost`); it
        must never keep serving a handle it knows is dead.
        """
        ...
