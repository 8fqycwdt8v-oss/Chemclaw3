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

**`vector_dialect` is here for exactly the same reason, and it arrived late.** How a warehouse
spells a similarity search is as much a dialect fact as how it spells a parameter, and `sql.py` was
neutral about the second while hardcoding the first: `VECTOR_COSINE_SIMILARITY` and
`?::VECTOR(FLOAT, n)` are Snowflake's names, in a module whose docstring claims to contribute only
structure. The second driver is what exposed it. A dialect owns two things a vendor genuinely
differs on — what the similarity function is called and how a query vector is *bound*, which is the
sharper half: Snowflake binds a Python list against a `VECTOR` cast, while a warehouse with no array
parameter type has to take the vector as a scalar and parse it server-side. A driver that offers no
dialect cannot serve a `vector:` block at all, and says so rather than emitting SQL its server will
reject.
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
class VectorDialect(Protocol):
    """How one warehouse spells a similarity search. Owned by the driver, used by `sql.py`.

    Two methods, because a vendor differs on exactly two things here and `sql.py` contributes the
    rest of the statement unchanged.
    """

    def similarity(self, metric: str) -> tuple[str, str]:
        """The function that computes `metric`, and the direction it sorts.

        One call rather than two lookups because the pair moves together: a distance sorts ascending
        and a similarity descending, and a metric added with the wrong pairing would return the
        *least* similar rows while looking entirely correct.

        Raises:
            WarehouseQueryError: This warehouse has no function for that metric. Non-retryable,
                because a binding asking for one it does not have fails identically every time.
        """
        ...

    def query_vector(self, placeholder: str, vector: Sequence[float], dim: int) -> tuple[str, Any]:
        """The expression standing in for the query vector, and the single value bound into it.

        Returned as a pair rather than as a rendered literal because the vector is a *value* — the
        one thing `sql.py` never writes into a statement — and because the encoding differs: a
        warehouse with a native vector type binds the list, one without has to take a scalar it can
        parse. `dim` is the configured embedding width, which a typed cast needs and a parsed one
        does not.
        """
        ...


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

    @property
    def vector_dialect(self) -> "VectorDialect | None":
        """How this warehouse spells a similarity search, or `None` if it cannot do one.

        `None` is a real answer, not an omission: the ingest half is ordinary ANSI `SELECT` work
        that every warehouse here can serve, while an in-warehouse similarity search needs a
        function this driver has verified exists. A binding that declares a `vector:` block against
        a driver answering `None` is refused with a message naming the driver, which is a better
        failure than a server rejecting a function it has never heard of on the first query.
        """
        ...

    def cursor(self) -> AbstractAsyncContextManager[WarehouseCursor]:
        """A cursor for one statement, released on exit.

        The only method, and there is deliberately no `close`. The data-source seam builds a half
        and never disposes it — there is no lifecycle hook to call one from — so a connection lives
        for the process's life by design, and a `close()` nobody can reach would be an interface
        promise with no mechanism behind it. A driver that needs teardown does it in its own
        `__del__` or leaves it to the process exit its session timeout already assumes.
        """
        ...
