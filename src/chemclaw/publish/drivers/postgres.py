"""A `Warehouse` over psycopg, so a Postgres results store needs no vendor client.

The same Protocol the inbound ELN drivers implement, kept in the publishing package because it
exists to write this system's results. Credentials come from the binding's named environment
variables, as on the inbound side.
"""

from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from typing import Any

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from chemclaw.core.config import PG_LOOPBACK_HOSTS, require_pg_tls, settings
from chemclaw.core.connect import check_identifier
from chemclaw.core.db import register_connection, unregister_connection
from chemclaw.ingest.eln.warehouse.driver import (
    VectorDialect,
    WarehouseCursor,
    WarehouseQueryError,
)
from chemclaw.publish.connect import SinkConnectionError


def _refuse_plaintext_connection(dsn: str, host: str) -> None:
    """Refuse a non-loopback sink connection that cannot require TLS, under the enforced posture.

    Under `entra_required` the connection (confidential results, the sink's password) must not
    cross a network in cleartext. A `dsn` is checked with the shared `require_pg_tls` guard; the
    discrete `host=/password=` form cannot express `sslmode`, so non-loopback use of it is refused
    with the remedy (a `dsn` with `sslmode=verify-full`). Loopback dev is exempt.
    """
    if not settings.entra_required:
        return
    if dsn:
        require_pg_tls(dsn, "postgres sink dsn")
        return
    if host and host.lower() not in PG_LOOPBACK_HOSTS:
        raise SinkConnectionError(
            f"entra_required=true with a non-loopback postgres sink host {host!r} given as "
            "discrete connection parameters: that form has no sslmode keyword, so libpq's default "
            "permits a silent plaintext fallback carrying the sink password. Provide a `dsn:` with "
            "sslmode=verify-full (and sslrootcert=<ca>) instead, or bind a loopback host for dev."
        )


def _adapted(params: Sequence[Any]) -> list[Any]:
    """`params` with every JSON document wrapped in `Jsonb`, the one way psycopg adapts one.

    Shared by `execute` and `executemany` so both adapt identically.
    """
    return [Jsonb(value) if isinstance(value, dict | list) else value for value in params]


@contextmanager
def _mapped_errors() -> Iterator[None]:
    """Re-raise a server programming error as `WarehouseQueryError`; let a connection loss through.

    `WarehouseQueryError` is non-retryable (an undefined column fails forever), so
    `OperationalError` passes through as itself and stays retryable.
    """
    try:
        yield
    except psycopg.OperationalError:
        raise
    except psycopg.Error as exc:
        raise WarehouseQueryError(f"{exc.__class__.__name__}: {exc}") from exc


class _PostgresCursor:
    """One in-flight statement, returning column-keyed dicts."""

    def __init__(self, cursor: psycopg.AsyncCursor[Any]) -> None:
        """Wrap an open psycopg cursor."""
        self._cursor = cursor

    async def execute(self, sql: str, params: Sequence[Any]) -> None:
        """Run `sql` with `params` bound positionally, adapting JSON values on the way.

        The JSON wrapping is a dialect fact, so it lives in the driver rather than the row builder
        (psycopg cannot adapt a bare `dict`; another driver may want a JSON string). Server
        programming errors become `WarehouseQueryError` (non-retryable, the fix is DDL); connection
        failures pass through as retryable.
        """
        with _mapped_errors():
            await self._cursor.execute(sql, _adapted(params))

    async def executemany(self, sql: str, params_seq: Sequence[Sequence[Any]]) -> None:
        """Run `sql` once per parameter set in psycopg's pipeline mode: one round trip, not N.

        The optional half of the cursor seam (`warehouse.driver.BatchingCursor`): optional because a
        `runtime_checkable` check tests member presence, and site-written drivers need not implement
        it. Adaptation and error mapping are `execute`'s. psycopg wraps the whole set in one
        implicit transaction even on an autocommit connection, so a failing set leaves none of its
        rows; the grain of "partial" is a statement, which `SqlResultSink` relies on when it replays
        a group.
        """
        with _mapped_errors():
            await self._cursor.executemany(sql, [_adapted(params) for params in params_seq])

    async def fetchall(self) -> list[dict[str, Any]]:
        """Every remaining row, keyed by column name."""
        rows = await self._cursor.fetchall()
        return [dict(row) for row in rows]


class PostgresWarehouse:
    """A `Warehouse` backed by psycopg. Built by `warehouse.connect.open_warehouse`.

    Its keyword arguments are the binding's `connection:` block, named as an operator writes them.
    One connection, opened lazily and held for the driver's life; `aclose` releases it.
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
        connect_timeout_seconds: int = 10,
    ) -> None:
        """Hold the connection parameters; connect on the first cursor.

        A `dsn` wins when given. `schema` becomes a `search_path` option, keeping the SQL generator
        free of site identifiers. `connect_timeout_seconds` bounds the handshake, which
        `statement_timeout` does not: a blackholed endpoint would otherwise hang the drain.
        """
        if not 1 <= query_timeout_seconds <= 3600:
            # `statement_timeout=0` means no timeout, so an out-of-range value would disable the
            # bound. The driver checks its own keyword because its signature is the binding's
            # schema.
            raise SinkConnectionError(
                "`query_timeout_seconds` must be between 1 and 3600; "
                f"got {query_timeout_seconds}, and 0 means no statement timeout at all"
            )
        if not 1 <= connect_timeout_seconds <= 3600:
            # libpq reads `connect_timeout=0` as no timeout, so the same check applies.
            raise SinkConnectionError(
                "`connect_timeout_seconds` must be between 1 and 3600; "
                f"got {connect_timeout_seconds}, and 0 means no connect timeout at all"
            )
        self._connect_timeout = connect_timeout_seconds
        options = [f"-c statement_timeout={int(query_timeout_seconds * 1000)}"]
        if schema:
            # Checked because it reaches libpq's `options`, which split on whitespace: a schema
            # carrying a space could smuggle a second `-c` setting (such as `statement_timeout=0`).
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
        _refuse_plaintext_connection(dsn, host)

    @property
    def placeholder(self) -> str:
        """The psycopg parameter marker."""
        return "%s"

    @property
    def vector_dialect(self) -> VectorDialect | None:
        """None: this driver writes results and never searches them.

        Present because a `runtime_checkable` Protocol check requires every member; `None` is the
        Protocol's own "does not do similarity".
        """
        return None

    async def _connection(self) -> psycopg.AsyncConnection[Any]:
        """The live connection, opened on first use and reopened if it was closed.

        Registered with `core/db` while held, since a bare connection occupies a backend like a pool
        slot; it counts against `chemclaw_pg_pool_max_size` only when its endpoint is
        `postgres_dsn`'s.
        """
        if self._conn is None or self._conn.closed:
            # Not passed when the site's `dsn` already sets one: a keyword would silently override
            # it. `dict[str, Any]` because `connect()` is overloaded and mypy matches `**kwargs`
            # against each overload's positional parameters.
            timeout: dict[str, Any] = (
                {}
                if self._dsn and "connect_timeout" in conninfo_to_dict(self._dsn)
                else {"connect_timeout": self._connect_timeout}
            )
            if self._dsn:
                self._conn = await psycopg.AsyncConnection.connect(
                    self._dsn,
                    options=self._options,
                    row_factory=dict_row,
                    autocommit=True,
                    **timeout,
                )
            else:
                self._conn = await psycopg.AsyncConnection.connect(
                    options=self._options,
                    row_factory=dict_row,
                    autocommit=True,
                    **self._parts,
                    **timeout,
                )
            # Passed explicitly: psycopg keeps no conninfo attribute, and a parts-built driver has
            # none until `make_conninfo` builds it.
            register_connection(self._conn, self._conninfo())
        return self._conn

    def _conninfo(self) -> str:
        """The connection string this driver dials, in libpq's own keyword form.

        One spelling for `core/db`'s endpoint comparison whichever way the driver was configured,
        built through `make_conninfo` so parts are quoted as libpq quotes them.
        """
        return self._dsn or make_conninfo(**{k: str(v) for k, v in self._parts.items()})

    async def aclose(self) -> None:
        """Release the held connection. Safe to call twice, and on one never opened.

        The drain builds a new driver every pass (so a rotated credential applies next run), so a
        connection not closed here would leak once per pass until `max_connections` is exhausted.
        """
        if self._conn is not None and not self._conn.closed:
            unregister_connection(self._conn)
            await self._conn.close()
        self._conn = None

    @asynccontextmanager
    async def cursor(self) -> AsyncIterator[WarehouseCursor]:
        """A cursor for one statement, released on exit.

        `autocommit` is correct here: every statement is an upsert onto a content-addressed key, so
        a half-failed batch leaves a partial but correct state the outbox retry completes.
        """
        conn = await self._connection()
        async with conn.cursor() as cursor:
            yield _PostgresCursor(cursor)
