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

    A published record is confidential chemistry and the connection carries the sink's password, so
    under `entra_required` it must not cross a non-loopback network in cleartext — the same rule
    `require_pg_tls` enforces for the system's own database. A `dsn` states its own `sslmode` and is
    checked with that shared guard. The discrete `host=/password=` form has no `sslmode` keyword to
    set, so a non-loopback discrete binding cannot express TLS at all and is refused with the
    remedy: give a `dsn` with `sslmode=verify-full`. Loopback dev is exempt.
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

    Shared by `execute` and `executemany` so an adaptation added for one reaches the other: the
    batched drain falling back to row-by-row replay because only the single form knew a type would
    be a silent five-second pass rather than an error.
    """
    return [Jsonb(value) if isinstance(value, dict | list) else value for value in params]


@contextmanager
def _mapped_errors() -> Iterator[None]:
    """Re-raise a server programming error as `WarehouseQueryError`; let a connection loss through.

    `durable/publish.py` marks `WarehouseQueryError` non-retryable by class name, which is right for
    an undefined column (it fails identically forever) and wrong for a server that went away — so
    `OperationalError` passes through as itself, retryable.
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
        with _mapped_errors():
            await self._cursor.execute(sql, _adapted(params))

    async def executemany(self, sql: str, params_seq: Sequence[Sequence[Any]]) -> None:
        """Run `sql` once per parameter set in psycopg's pipeline mode — one round trip, not N.

        The optional half of the cursor seam (`warehouse.driver.BatchingCursor`), and the reason a
        publication drain is nine statements per pass rather than fifteen hundred. Measured against
        a live server over a full `result_publish_batch_size` pass of 100 records: **1,500 round
        trips and 5.6 s row-at-a-time against 9 and 0.41 s here**, with identical stored rows.

        **Optional on purpose, and this method is why the seam needed a second Protocol rather than
        a wider one.** `D-2026-08-26-the-driver-s-signature-is-the-schema` lets a site bring its own
        driver, and `runtime_checkable` `isinstance` tests member *presence* — so requiring this on
        `WarehouseCursor` would have made every site-written driver fail the check `_connect`
        already does, for what is only an optimisation. A driver without it takes the loop.

        The adaptation and the error mapping are `execute`'s — one `_adapted` and one
        `_mapped_errors` — for its reasons.

        **One property moved, and it is narrower than the seam's docstrings have promised.**
        psycopg wraps the whole parameter set in a single implicit transaction *even here, where the
        connection is autocommit* — driven, a four-row set failing on its third leaves **none** of
        the four, and the connection stays usable. So "a batch that fails halfway leaves a partial
        but correct state" still holds in kind, but the grain of "partial" is now a statement rather
        than a row. `SqlResultSink` relies on exactly that when it replays a refused group singly to
        recover which row the server objected to.
        """
        with _mapped_errors():
            await self._cursor.executemany(sql, [_adapted(params) for params in params_seq])

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
        connect_timeout_seconds: int = 10,
    ) -> None:
        """Hold the connection parameters; connect on the first cursor.

        A `dsn` wins when given, because a site with an existing connection string should not have
        to decompose it. `schema` becomes a `search_path` option rather than a qualified table name
        in every statement, which is what keeps the SQL generator free of site-specific identifiers.

        `connect_timeout_seconds` bounds the **handshake**, which `query_timeout_seconds` does not:
        `statement_timeout` starts counting once there is a session to run a statement in. Measured
        against a socket that accepts and never speaks — a stale DNS record, a firewall DROP, a
        load balancer with no backends — a sink with `query_timeout_seconds=2` was **still blocked
        after 20 s**. That is the concrete generator for a drain that hangs: it burns an attempt on
        every claimed row without marking any of them, and holds the pass against every other sink.
        Ten seconds, because a results warehouse that has not completed a TCP+TLS+auth handshake in
        ten is down, and the drain runs again in `result_publish_schedule_minutes`.
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
        if not 1 <= connect_timeout_seconds <= 3600:
            # Checked on the same terms as the statement bound above, and for the sharper reason:
            # libpq reads `connect_timeout=0` as *no* timeout, so an out-of-range value here
            # restores exactly the unbounded handshake this argument exists to end.
            raise SinkConnectionError(
                "`connect_timeout_seconds` must be between 1 and 3600; "
                f"got {connect_timeout_seconds}, and 0 means no connect timeout at all"
            )
        self._connect_timeout = connect_timeout_seconds
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
        _refuse_plaintext_connection(dsn, host)

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
        """The live connection, opened on first use and reopened if it was closed.

        **Registered with `core/db` while it is held**, because a bare connection occupies a backend
        exactly as a pool slot does and the process's own reading could only see pools
        (`D-2026-09-13-a-connection-counted-where-the-budget-applies`). It counts against
        `chemclaw_pg_pool_max_size` only when its endpoint is `postgres_dsn`'s: a sink pointed at
        a warehouse of its own is on a ceiling this deployment does not declare, and charging it to
        the primary's would be the under-count's mirror image.
        """
        if self._conn is None or self._conn.closed:
            # **Not passed when the site's own connection string already sets one.** A keyword wins
            # over a conninfo key in psycopg, so passing it unconditionally would silently overrule
            # a `connect_timeout` a DBA had deliberately written into the `dsn` — the one form of
            # this binding where the site can already express the bound. Absent there, this is the
            # only thing standing between a blackholed warehouse and an unbounded drain.
            # `dict[str, Any]`, not `dict[str, int]`: `connect()` is an overloaded signature and
            # mypy matches `**kwargs` against each overload's positional parameters, so a narrowed
            # value type is reported against `AdaptContext`, `str` and the cursor factory in turn.
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
            # The conninfo is passed rather than read back off the connection: psycopg keeps no
            # attribute carrying it, and a driver built from `connection:` parts has no single
            # string at all until `make_conninfo` builds one from them.
            register_connection(self._conn, self._conninfo())
        return self._conn

    def _conninfo(self) -> str:
        """The connection string this driver dials, in libpq's own keyword form.

        One spelling for the endpoint comparison `core/db` makes, whichever of the two ways this
        driver was configured: a site's own `dsn`, or the `connection:` block's keyword arguments.
        Built through `make_conninfo` rather than concatenated, so a part carrying a space or an
        equals sign is quoted the way libpq quotes it.
        """
        return self._dsn or make_conninfo(**{k: str(v) for k, v in self._parts.items()})

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
            unregister_connection(self._conn)
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
