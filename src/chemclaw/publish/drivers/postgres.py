"""A `Warehouse` over psycopg, so a Postgres results store needs no vendor client.

The most likely target a site actually runs, and until this existed the SQL sink could only reach a
warehouse whose vendor client a deployment had installed. This is the same Protocol over `psycopg`,
which this repository already depends on.

**It lives here rather than beside the inbound drivers, and the reason is direction.** Those exist
to *read* a site's ELN; this one exists to *write* this system's own results. They implement one
Protocol because a connection is a connection — that is the reuse the Protocol was for — but a
reader looking for "how does publishing reach Postgres" should find it in the publishing package.

Credentials come from the binding's named environment variables, exactly as an inbound driver's do,
so both directions are configured the same way and a deployment moving between them changes a
manifest rather than a mechanism.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from chemclaw.core.connect import check_identifier
from chemclaw.ingest.eln.warehouse.driver import (
    VectorDialect,
    WarehouseCursor,
    WarehouseQueryError,
)
from chemclaw.publish.connect import SinkConnectionError


class _PostgresCursor:
    """One in-flight statement, returning column-keyed dicts."""

    def __init__(self, cursor: psycopg.AsyncCursor[Any]) -> None:
        """Wrap an open psycopg cursor."""
        self._cursor = cursor

    async def execute(self, sql: str, params: Sequence[Any]) -> None:
        """Run `sql` with `params` bound positionally, adapting JSON values on the way.

        **The JSON wrapping lives here rather than in the row builder**, for the same reason
        `placeholder` is a property of the connection: how a document is bound is a dialect fact.
        psycopg rejects a bare `dict` — it adapts one only through its `Jsonb` wrapper, and a
        mapping reaching it unwrapped fails with "cannot adapt type 'dict'" rather than being
        silently stringified. A warehouse driver may want a JSON *string* for the same column, so a
        row builder that wrapped for one would break the other.

        A programming error from the server — an undefined column, a type mismatch — is re-raised
        as `WarehouseQueryError`, which `durable/publish.py` marks non-retryable by class name: a
        statement naming a column the site has not created fails identically forever, and the fix
        is DDL rather than a wait. A *connection* failure passes through as itself, because that
        one genuinely is worth retrying.
        """
        adapted = [Jsonb(value) if isinstance(value, dict | list) else value for value in params]
        try:
            await self._cursor.execute(sql, adapted)
        except psycopg.OperationalError:
            # The server went away. Retryable, so it must not be flattened into a query error.
            raise
        except psycopg.Error as exc:
            raise WarehouseQueryError(f"{exc.__class__.__name__}: {exc}") from exc

    async def fetchall(self) -> list[dict[str, Any]]:
        """Every remaining row, keyed by column name."""
        rows = await self._cursor.fetchall()
        return [dict(row) for row in rows]


class PostgresWarehouse:
    """A `Warehouse` backed by psycopg. Built by `warehouse.connect.open_warehouse`.

    Its keyword arguments are the binding's `connection:` block, which is why they are named for
    that block's fields rather than for psycopg's: `database` and `schema` and `password` are what
    an operator writes, and this translates.

    **One connection, opened lazily and held.** The data-source seam builds a half and never
    disposes it — there is no lifecycle hook to close one from — so a connection lives for the
    sink's life by design, which the Protocol's own docstring records.
    """

    def __init__(
        self,
        *,
        host: str = "",
        port: int = 5432,
        user: str = "",
        password: str = "",
        database: str = "",
        schema: str = "",
        dsn: str = "",
        query_timeout_seconds: int = 60,
    ) -> None:
        """Hold the connection parameters; connect on the first cursor.

        A `dsn` wins when given, because a site with an existing connection string should not have
        to decompose it. `schema` becomes a `search_path` option rather than a qualified table name
        in every statement, which is what keeps the SQL generator free of site-specific identifiers.
        """
        if not 1 <= query_timeout_seconds <= 3600:
            # `statement_timeout=0` is Postgres' spelling of *no* timeout, so an out-of-range value
            # here disables the one bound on a runaway publish rather than tightening it. Checked in
            # the driver because this is the driver's own keyword: a sink's `connection:` block is
            # its constructor signature (`D-2026-08-26-the-driver-s-signature-is-the-schema`), and
            # no shared model is left to hold a range for it.
            raise SinkConnectionError(
                "`query_timeout_seconds` must be between 1 and 3600; "
                f"got {query_timeout_seconds}, and 0 means no statement timeout at all"
            )
        options = [f"-c statement_timeout={int(query_timeout_seconds * 1000)}"]
        if schema:
            # **Checked, because this one reaches a process argument rather than a statement.**
            # libpq splits `options` on whitespace and the *last* `-c` wins, so a schema carrying a
            # space is not a search path — it is a second setting, and the value it most usefully
            # sets is the `statement_timeout=0` the bound three lines up exists to refuse. The
            # checker is `core.connect`'s, the same one every binding identifier goes through, so
            # "what a connection block may contribute" has one spelling rather than two.
            check_identifier(schema, "connection schema", error=SinkConnectionError)
            options.append(f"-c search_path={schema}")
        self._options = " ".join(options)
        self._dsn = dsn
        self._parts: dict[str, Any] = {
            key: value
            for key, value in (
                ("host", host),
                ("port", port),
                ("user", user),
                ("password", password),
                ("dbname", database),
            )
            if value
        }
        self._conn: psycopg.AsyncConnection[Any] | None = None

    @property
    def placeholder(self) -> str:
        """The psycopg parameter marker."""
        return "%s"

    @property
    def vector_dialect(self) -> VectorDialect | None:
        """None: this driver writes results and never searches them.

        Present because `Warehouse` is a `@runtime_checkable` Protocol and `SqlResultSink._connect`
        checks against it — and a runtime Protocol check tests for the *presence of every member*,
        so omitting this one made the sink reject the only driver this repository ships for it with
        "did not build a Warehouse". Measured: `isinstance(PostgresWarehouse(...), Warehouse)` was
        False, and every delivery failed at the connect.

        `None` is the honest answer rather than a stub: `vector_dialect` exists so the *inbound*
        seam's `sql.py` can spell a similarity search, and nothing in the publish path searches
        anything. The Protocol's own reader treats None as "this driver does not do similarity".
        """
        return None

    async def _connection(self) -> psycopg.AsyncConnection[Any]:
        """The live connection, opened on first use and reopened if it was closed."""
        if self._conn is None or self._conn.closed:
            if self._dsn:
                self._conn = await psycopg.AsyncConnection.connect(
                    self._dsn, options=self._options, row_factory=dict_row, autocommit=True
                )
            else:
                self._conn = await psycopg.AsyncConnection.connect(
                    options=self._options, row_factory=dict_row, autocommit=True, **self._parts
                )
        return self._conn

    async def aclose(self) -> None:
        """Release the held connection. Safe to call twice, and on one never opened.

        The connection is opened on first use and kept for the driver's life, which is right for a
        batch of upserts and wrong for a process that builds a new driver every pass — and the drain
        does exactly that, deliberately, so a rotated credential takes effect on the next run rather
        than the next restart. Without this the two decisions multiplied: one leaked Postgres
        connection per drain, every `result_publish_schedule_minutes` (default 15), which reaches a
        stock `max_connections` of 100 inside a day and then fails the *whole* worker rather than
        the publish.
        """
        if self._conn is not None and not self._conn.closed:
            await self._conn.close()
        self._conn = None

    @asynccontextmanager
    async def cursor(self) -> AsyncIterator[WarehouseCursor]:
        """A cursor for one statement, released on exit.

        `autocommit` on the connection, so each upsert commits on its own. That is correct for this
        writer rather than a shortcut: every statement it issues is an upsert onto a
        content-addressed key, so a batch that fails halfway leaves a partial but *correct* state
        that the outbox's retry completes — which is the property that makes at-least-once delivery
        safe here.
        """
        conn = await self._connection()
        async with conn.cursor() as cursor:
            yield _PostgresCursor(cursor)
