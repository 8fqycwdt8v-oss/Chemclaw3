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
        self._rows = self._warehouse.respond(sql, list(params))

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
        self.fail_with: Exception | None = None
        self.connect_options: dict[str, Any] = {}
        self._placeholder = placeholder

    @property
    def placeholder(self) -> str:
        """The parameter marker the engine should emit for this connection."""
        return self._placeholder

    def respond(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        """The rows of whichever primed relation this statement reads from.

        Ignores the statement's WHERE, ORDER BY and LIMIT: most tests here assert *what statement
        the engine emitted*, and answering them from a canned table keeps the row fixtures readable.
        `WatermarkWarehouse` is the counterpart for the tests where those clauses are the subject.
        """
        for relation, rows in self.tables.items():
            if f" {relation} " in sql or sql.endswith(f" {relation}"):
                return [dict(row) for row in rows]
        return []

    @asynccontextmanager
    async def cursor(self) -> AsyncIterator[FakeCursor]:
        """A cursor for one statement."""
        yield FakeCursor(self)


class WatermarkWarehouse(FakeWarehouse):
    """A `FakeWarehouse` whose entry relation honours the statement's WHERE, ORDER BY and LIMIT.

    Needed because the plain fake answers every statement with the whole primed table, so the one
    failure mode that the paging contract exists to prevent — a cursor that does not advance past
    the page the warehouse keeps returning — cannot be reproduced against it. A sync that has
    wedged permanently and a sync with nothing to do look identical from the outside, and every
    existing test here saw the second one.

    It applies the *semantics* of those clauses rather than parsing them: the exact clause text is
    already pinned by `test_the_cursor_filters_on_the_later_of_created_and_modified`, so restating
    it as a parser here would only be a second place for the two to disagree. `params` is
    `[since, limit]` — the engine binds both, which is itself asserted next door.
    """

    def __init__(
        self,
        tables: dict[str, list[dict[str, Any]]],
        entry_relation: str,
        created_at: str,
        modified_at: str | None = None,
    ) -> None:
        """Serve `entry_relation` under its declared watermark columns; other tables as canned."""
        super().__init__(tables)
        self._entry_relation = entry_relation
        self._created_at = created_at
        self._modified_at = modified_at

    def _watermark(self, row: dict[str, Any]) -> Any:
        """The value the entry statement filters and orders on: COALESCE(modified, created)."""
        if self._modified_at and row.get(self._modified_at) is not None:
            return row[self._modified_at]
        return row[self._created_at]

    def respond(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        """Rows at or after the bound cursor, oldest watermark first, cut to the bound limit."""
        rows = super().respond(sql, params)
        if f" {self._entry_relation} " not in sql:
            return rows
        since, limit = params[0], params[1]
        keep = sorted((row for row in rows if self._watermark(row) >= since), key=self._watermark)
        return keep[:limit]


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


def prime_warehouse(warehouse: FakeWarehouse) -> FakeWarehouse:
    """Prime an already-built warehouse (a `WatermarkWarehouse`), and hand it back."""
    global NEXT
    NEXT = warehouse
    return warehouse


class KeysetWarehouse(FakeWarehouse):
    """A `FakeWarehouse` whose corpus relation honours the statement's keyset WHERE and LIMIT.

    The counterpart of `WatermarkWarehouse` for the other paging contract. The plain fake answers
    every statement with the whole primed table, which makes the one failure a keyset drain exists
    to prevent — a cursor that does not advance past the page the warehouse keeps returning —
    impossible to reproduce: a wedged drain and a finished one look identical from outside.

    Applies the *semantics* of `WHERE cursor > ? ORDER BY cursor ASC LIMIT ?` rather than parsing
    the clause, for the reason its sibling gives: the exact text is pinned by the statement test, so
    a parser here would only be a second place for the two to disagree. `params` is `[after, limit]`
    on a resumed page and `[limit]` on the first one — which is itself the contract
    `corpus_statement` documents.
    """

    def __init__(
        self, tables: dict[str, list[dict[str, Any]]], corpus_relation: str, cursor_column: str
    ) -> None:
        """Serve `corpus_relation` under its keyset column; other tables as canned."""
        super().__init__(tables)
        self._corpus_relation = corpus_relation
        self._cursor = cursor_column

    def respond(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        """The next page of the corpus relation, or a canned table for anything else."""
        if f" {self._corpus_relation} " not in sql:
            return super().respond(sql, params)
        rows = sorted(self.tables[self._corpus_relation], key=lambda r: str(r[self._cursor]))
        if len(params) == 2:
            after, limit = str(params[0]), int(params[1])
            rows = [r for r in rows if str(r[self._cursor]) > after]
        else:
            limit = int(params[0])
        return [dict(row) for row in rows[:limit]]
