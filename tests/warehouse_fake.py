"""A `Warehouse` that serves canned rows and records what it was asked — the offline test seam.

Lets the binding engine be tested with no tenant, credentials or vendor client. It records
`executed` so a test can assert the exact statement sent: a cursor-predicate bug does not raise,
it silently stops re-ingesting amended runs.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from chemclaw.ingest.eln.warehouse.driver import WarehouseQueryError


class FakeVectorDialect:
    """A `VectorDialect` serving all three metrics, so `sql.py` can be tested for being neutral.

    The names are deliberately not any vendor's: they prove the driver's function name and sort
    direction reach the statement. Real spellings are pinned in `tests/test_databricks_warehouse.py`
    and meet end to end in `test_warehouse_retriever.py`.
    """

    _METRICS: dict[str, tuple[str, str]] = {
        "cosine": ("FAKE_COSINE_SIMILARITY", "DESC"),
        "inner": ("FAKE_INNER_PRODUCT", "DESC"),
        "l2": ("FAKE_L2_DISTANCE", "ASC"),
    }

    def similarity(self, metric: str) -> tuple[str, str]:
        """The fake's function for `metric`, and the direction it sorts."""
        try:
            return self._METRICS[metric]
        except KeyError:
            raise WarehouseQueryError(f"no fake similarity function for {metric!r}") from None

    def query_vector(self, placeholder: str, vector: Sequence[float], dim: int) -> tuple[str, Any]:
        """Bind the list straight into the placeholder — the simplest of the real encodings."""
        return placeholder, list(vector)


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

    Keyed by relation rather than call order, so the engine may reorder its child-table queries.
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
        self._vector_dialect: Any = FakeVectorDialect()

    @property
    def placeholder(self) -> str:
        """The parameter marker the engine should emit for this connection."""
        return self._placeholder

    @property
    def vector_dialect(self) -> Any:
        """`FakeVectorDialect`, so what the engine contributes is asserted without a vendor's words.

        A test sets this to `None` for the no-dialect case, or to a real dialect for a vendor's
        spelling.
        """
        return self._vector_dialect

    def respond(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        """The rows of whichever primed relation this statement reads from.

        Ignores WHERE, ORDER BY and LIMIT; `WatermarkWarehouse` honours them where they are the
        subject.
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

    Without it, a cursor that never advances past a repeated page cannot be reproduced. It applies
    the clauses' semantics rather than parsing them (the text is pinned by
    `test_the_cursor_filters_on_the_later_of_created_and_modified`). `params` is `[since, limit]`.
    """

    def __init__(
        self,
        tables: dict[str, list[dict[str, Any]]],
        entry_relation: str,
        created_at: str,
        modified_at: str | None = None,
        key: str = "",
        retracted_at: str | None = None,
    ) -> None:
        """Serve `entry_relation` under its declared watermark columns; other tables as canned."""
        super().__init__(tables)
        self._entry_relation = entry_relation
        self._created_at = created_at
        self._modified_at = modified_at
        self._key = key
        self._retracted_at = retracted_at

    def _watermark(self, row: dict[str, Any]) -> Any:
        """The value the entry statement filters and orders on.

        `COALESCE(modified, created)`, and with a retraction column the `GREATEST` of that and the
        withdrawal — mirroring `sql.watermark_expression`.
        """
        window = (
            row[self._modified_at]
            if self._modified_at and row.get(self._modified_at) is not None
            else row[self._created_at]
        )
        if self._retracted_at and row.get(self._retracted_at) is not None and window is not None:
            return max(window, row[self._retracted_at])
        return window

    def _rank(self, row: dict[str, Any]) -> tuple[Any, str]:
        """The total order the statement asks for: the watermark, then the entry key.

        The key tiebreaker makes a page cut out of a watermark tie deterministic, so a cursor can
        resume it.
        """
        return self._watermark(row), str(row.get(self._key, ""))

    def respond(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        """Rows at or after the bound cursor, in `(watermark, key)` order, cut to the bound limit.

        `[since, limit]` starts a page at the cursor; `[block, block, after_key, limit]` continues
        inside a watermark block a page could not hold (e.g. a DATE watermark or a bulk reload).
        """
        rows = super().respond(sql, params)
        if f" {self._entry_relation} " not in sql:
            return rows
        if len(params) == 4:
            block, after_key, limit = params[0], str(params[2]), params[3]
            keep = [
                row
                for row in rows
                if self._watermark(row) > block
                or (self._watermark(row) == block and str(row.get(self._key, "")) > after_key)
            ]
        else:
            since, limit = params[0], params[1]
            keep = [row for row in rows if self._watermark(row) >= since]
        return sorted(keep, key=self._rank)[:limit]


# The warehouse `open_fake` will hand out next. A module-level slot because `connection.driver` is
# a `module:callable` *string* resolved by name — there is no other channel through which a test can
# reach the object the engine is about to build.
NEXT: FakeWarehouse | None = None


def open_fake(**options: Any) -> FakeWarehouse:
    """A binding's `connection.driver`: hand back the warehouse this test primed.

    Records the connect options, so a test can assert credentials were read from the named
    environment variables.
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

    The keyset counterpart of `WatermarkWarehouse`: applies the semantics of
    `WHERE cursor > ? ORDER BY cursor ASC LIMIT ?` without parsing it. `params` is `[after, limit]`
    on a resumed page and `[limit]` on the first.
    """

    def __init__(
        self, tables: dict[str, list[dict[str, Any]]], corpus_relation: str, cursor_column: str
    ) -> None:
        """Serve `corpus_relation` under its keyset column; other tables as canned."""
        super().__init__(tables)
        self._corpus_relation = corpus_relation
        self._cursor = cursor_column

    def _rank(self, row: dict[str, Any]) -> tuple[int, str]:
        """The order the warehouse walks: NULL first, then the cursor value as text.

        NULLs first, as Spark's ASC sort does, so a row with no cursor value lands on page one where
        it decides what the next page resumes after.
        """
        value = row.get(self._cursor)
        return (0, "") if value is None else (1, str(value))

    def respond(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        """The next page of the corpus relation, or a canned table for anything else."""
        if f" {self._corpus_relation} " not in sql:
            return super().respond(sql, params)
        rows = sorted(self.tables[self._corpus_relation], key=self._rank)
        if len(params) == 2:
            after, limit = str(params[0]), int(params[1])
            # `> ?` against a NULL column is NULL, never true — so a NULL row is *dropped* by any
            # resumed page, exactly as the warehouse drops it, and cannot be re-read by rewinding.
            rows = [
                r for r in rows if r.get(self._cursor) is not None and str(r[self._cursor]) > after
            ]
        else:
            limit = int(params[0])
        return [dict(row) for row in rows[:limit]]
