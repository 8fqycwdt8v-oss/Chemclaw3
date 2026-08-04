"""A `Warehouse` that serves canned rows and records what it was asked — the offline test seam.

The whole point of `chemclaw.ingest.eln.warehouse.driver` being Protocols with no vendor import is
that this file can exist: every behaviour of the binding engine — the watermark predicate, the
child-table fan-out, unit conversion, vocabulary mapping, attribute bounding, similarity ordering —
is asserted here with no Snowflake tenant, no credentials and no client installed.

It records `executed` so a test can assert the *exact statement* the engine would send. That matters
more than it looks: a bug in the cursor predicate does not surface as an exception, it surfaces as
an ELN that silently stops re-ingesting amended runs.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any


class FakeCursor:
    """One statement, answered from whatever the fake was primed with."""

    def __init__(self, warehouse: "FakeWarehouse") -> None:
        """Answer from `warehouse`, recording what it is asked."""
        self._warehouse = warehouse
        self._rows: list[dict[str, Any]] = []

    async def execute(self, sql: str, params: Sequence[Any]) -> None:
        """Record the statement and pick the response matching the relation it names."""
        self._warehouse.executed.append((sql, list(params)))
        if self._warehouse.fail_with is not None:
            raise self._warehouse.fail_with
        self._rows = self._warehouse.respond(sql)

    async def fetchall(self) -> list[dict[str, Any]]:
        """The primed rows for the last statement."""
        return self._rows


class FakeWarehouse:
    """A `Warehouse` whose answers are keyed by the relation a statement names.

    Keyed by relation rather than by call order so a test reads as "this table holds these rows"
    rather than "the third query returns this" — the engine is free to reorder its child-table
    queries without invalidating every test that ever touched it.
    """

    def __init__(
        self, tables: dict[str, list[dict[str, Any]]] | None = None, placeholder: str = "?"
    ) -> None:
        """Prime the relations this warehouse can answer for."""
        self.tables: dict[str, list[dict[str, Any]]] = tables or {}
        self.executed: list[tuple[str, list[Any]]] = []
        self.closed = False
        self.fail_with: Exception | None = None
        self.connect_options: dict[str, Any] = {}
        self._placeholder = placeholder

    @property
    def placeholder(self) -> str:
        """The parameter marker the engine should emit for this connection."""
        return self._placeholder

    def respond(self, sql: str) -> list[dict[str, Any]]:
        """The rows of whichever primed relation this statement reads from."""
        for relation, rows in self.tables.items():
            if f" {relation} " in sql or sql.endswith(f" {relation}"):
                return [dict(row) for row in rows]
        return []

    @asynccontextmanager
    async def cursor(self) -> AsyncIterator[FakeCursor]:
        """A cursor for one statement."""
        yield FakeCursor(self)

    async def close(self) -> None:
        """Record that the engine released the connection."""
        self.closed = True


# The warehouse `open_fake` will hand out next. A module-level slot because `connection.driver` is
# a `module:callable` *string* resolved by name — there is no other channel through which a test can
# reach the object the engine is about to build.
NEXT: FakeWarehouse | None = None


def open_fake(**options: Any) -> FakeWarehouse:
    """A binding's `connection.driver`: hand back the warehouse this test primed.

    Records the connect options it was called with, so a test can assert that credentials were read
    from the environment variables the binding named — the one part of `connect` that has real
    behaviour and would otherwise need a live warehouse to observe.
    """
    if NEXT is None:
        raise AssertionError("call tests.warehouse_fake.prime() before building a half")
    NEXT.connect_options = dict(options)
    return NEXT


def prime(**tables: list[dict[str, Any]]) -> FakeWarehouse:
    """Prime the warehouse `open_fake` returns, and hand it back for assertions."""
    global NEXT
    NEXT = FakeWarehouse(dict(tables))
    return NEXT
