"""Publishing to a SQL database running the shipped schema.

Reuses the inbound warehouse seam's dialect-neutral `Warehouse`/`WarehouseCursor` Protocol
(read-only-ness lives in that seam's `sql.py`, not the driver), connected through
`chemclaw.core.connect` with the `connection:` block as the driver's own signature. The
statements are Postgres (`ON CONFLICT`); another engine needs a `MERGE` emitter in
`dialect.py`, not configuration.
"""

import logging
from collections.abc import Sequence
from typing import Any

from chemclaw.core.config import settings
from chemclaw.core.metrics_bridge import degraded
from chemclaw.ingest.eln.warehouse.driver import (
    Warehouse,
    WarehouseQueryError,
    execute_many,
)
from chemclaw.publish.connect import SinkConnectionError, open_connection
from chemclaw.publish.dialect import (
    REQUIRED_COLUMNS,
    TABLE_ORDER,
    rows_for,
    upsert_statement,
)
from chemclaw.publish.driver import SinkRejectedError, SinkUnavailableError
from chemclaw.publish.record import ResultRecord

logger = logging.getLogger(__name__)


class SqlResultSink:
    """Writes published records into a SQL database running `schema/result-store/`.

    Writes down to the schema it finds: a site may not grant DDL to the runtime principal, so the
    sink probes `information_schema.columns` once per pass and omits optional columns the site
    lacks, rather than turning a schema lag into a total outage.
    """

    def __init__(
        self,
        *,
        name: str,
        tenant_id: str,
        connection: dict[str, Any],
        writer_version: str = "",
    ) -> None:
        """Hold the binding; connect lazily, on the first delivery.

        Args:
            name: The sink's manifest name, for log lines and errors.
            tenant_id: What this deployment calls itself on every publication row.
            connection: The `connection:` block: a `module:callable` driver plus whatever that
                driver's signature takes. Any key ending `_env` names an environment variable read
                at connect time.
            writer_version: The ChemClaw release stamped on each row, so a consumer can tell an
                absent measurement from an absent column. Defaults to this deployment's revision.
        """
        self._name = name
        self._tenant_id = tenant_id
        self._connection_binding = dict(connection)
        # The same Git SHA the audit trail stamps, so both records of "which ChemClaw3 did this"
        # agree.
        self._writer_version = writer_version or settings.deployment_revision
        # Qualifies the column probe the way the writes resolve; empty leaves it unqualified.
        self._schema = str(self._connection_binding.get("schema") or "")
        self._warehouse: Warehouse | None = None
        self._columns: dict[str, set[str]] | None = None
        # Optional columns already reported absent, per table, so a lag is reported once per pass.
        self._reported: dict[str, set[str]] = {}

    async def aclose(self) -> None:
        """Close the held connection and forget the probed schema.

        The drain builds a sink per run, so this prevents a connection leak per pass; the column
        cache
        goes with it so a newly applied migration is picked up next pass.
        """
        warehouse = self._warehouse
        self._warehouse = None
        self._columns = None
        self._reported = {}
        closer = getattr(warehouse, "aclose", None)
        if closer is not None:
            # Not every `Warehouse` holds something to close — the Protocol does not require it of
            # a driver, only of a *sink*. A site's own driver that opens nothing needs no method.
            await closer()

    def _connect(self) -> Warehouse:
        """The connection, opened once and held for this sink's life."""
        if self._warehouse is None:
            warehouse = open_connection(self._connection_binding)
            if not isinstance(warehouse, Warehouse):
                raise SinkConnectionError(
                    f"result sink {self._name!r}: "
                    f"{self._connection_binding.get('driver')!r} did not build a Warehouse "
                    "(it must expose `placeholder` and an async `cursor()`)"
                )
            self._warehouse = warehouse
        return self._warehouse

    async def _known_columns(self, warehouse: Warehouse) -> dict[str, set[str]]:
        """Which columns the site's schema actually has, probed once and cached for the sink's
        lifetime.
        """
        if self._columns is not None:
            return self._columns
        # Qualified by the schemas on the search path the writes resolve through, so a same-named
        # table
        # elsewhere (an archive, another tenant) cannot answer for the target. Split on commas
        # because
        # `schema:` may name several.
        schemas = [part.strip().lower() for part in self._schema.split(",") if part.strip()]
        predicate = (
            " AND LOWER(table_schema) IN ("
            + ", ".join([warehouse.placeholder] * len(schemas))
            + ")"
            if schemas
            else ""
        )
        parameters: list[str] = [*TABLE_ORDER, *schemas]
        async with warehouse.cursor() as cursor:
            await cursor.execute(
                # `LOWER(table_name)`: engines fold unquoted identifiers differently, and a probe
                # that matched
                # nothing would report every table missing.
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE LOWER(table_name) IN ("
                + ", ".join([warehouse.placeholder] * len(TABLE_ORDER))
                + ")"
                + predicate,
                parameters,
            )
            rows = await cursor.fetchall()
        found: dict[str, set[str]] = {}
        for row in rows:
            # Column names come back upper-cased on Snowflake and Oracle, lower on Postgres.
            table = str(row.get("table_name") or row.get("TABLE_NAME") or "").lower()
            column = str(row.get("column_name") or row.get("COLUMN_NAME") or "").lower()
            if table:
                found.setdefault(table, set()).add(column)
        missing = [table for table in TABLE_ORDER if table not in found]
        if missing:
            raise SinkRejectedError(
                f"result sink {self._name!r}: the target has no {', '.join(missing)}. "
                "Run `python -m chemclaw.cli.sink_schema` and apply the printed DDL."
            )
        # Columns that carry a measurement are required, like tables (see
        # `dialect.REQUIRED_COLUMNS`);
        # only the rest are omitted on a lag.
        for table, required in sorted(REQUIRED_COLUMNS.items()):
            absent = sorted(required - found[table])
            if absent:
                raise SinkRejectedError(
                    f"result sink {self._name!r}: {table} lacks {', '.join(absent)}, which "
                    "carry the measurement rather than describe it — a row written without them "
                    "would assert less than it claims. Run `python -m chemclaw.cli.sink_schema` "
                    "and apply the printed DDL."
                )
        await self._refuse_an_unseeded_registry(warehouse)
        self._columns = found
        return found

    async def _refuse_an_unseeded_registry(self, warehouse: Warehouse) -> None:
        """Refuse a store whose `property_definition` is empty, before any row is written.

        The DDL in `schema/result-store/` ships no registry rows (those come from `sink_schema
        --seed`),
        so an unseeded store accepts every spine row and refuses every fact row, leaving calculation
        rows with zero facts that read as "produced nothing". Writes are autocommit with no
        rollback, so
        the only clean place to refuse is ahead of the first statement.
        """
        async with warehouse.cursor() as cursor:
            await cursor.execute("SELECT count(*) AS n FROM property_definition", [])
            rows = await cursor.fetchall()
        count = int(next(iter(rows[0].values())) if rows else 0)
        if count == 0:
            raise SinkRejectedError(
                f"result sink {self._name!r}: property_definition is empty, so every fact row "
                "would be refused by its foreign key while the spine rows landed — leaving "
                "calculations with no measurements. Run "
                "`python -m chemclaw.cli.sink_schema --seed` and apply it."
            )

    def _report_dropped(self, table: str, dropped: set[str]) -> None:
        """Say once per table what this site's schema cannot hold, not once per row.

        Reported through `degraded()` so it is counted as well as logged, at WARNING because an
        optional column absent on an older store is the sanctioned case ("not recorded").
        """
        seen = self._reported.setdefault(table, set())
        if dropped <= seen:
            return
        seen |= dropped
        degraded(
            logger,
            "result_sink_schema_lag",
            "result sink %s: %s lacks %s; those values are not published (this site's schema is "
            "behind this release — run `python -m chemclaw.cli.sink_schema`)",
            self._name,
            table,
            ", ".join(sorted(dropped)),
            level=logging.WARNING,
            exc_info=False,
        )

    async def deliver(self, records: Sequence[ResultRecord]) -> None:
        """Write every record's rows, in dependency order, idempotently.

        No transaction spans the batch (the Protocol has no transaction control); every write is an
        upsert onto a content-addressed key, so a half-failed batch leaves a partial but correct
        state
        the retry completes. Rows are grouped into one statement per `(table, column set)` (see
        `_batches`) and sent with `execute_many`, so a pass costs a handful of round trips rather
        than
        one per row. psycopg makes each group atomic; `tests/test_publish_end_to_end.py` asserts the
        stored rows match row-at-a-time writes.
        """
        if not records:
            return
        try:
            warehouse = self._connect()
            columns_by_table = await self._known_columns(warehouse)
        except SinkRejectedError:
            # The site's schema is the problem and a retry cannot fix it; re-raised ahead of the
            # availability arm.
            raise
        except Exception as exc:
            # Any other connect-time failure is the destination not working: content failures arrive
            # as
            # `WarehouseQueryError` or `SinkRejectedError`, and a vendor error such as
            # `psycopg.OperationalError` must be retried, not treated as a poison record.
            raise SinkUnavailableError(f"result sink {self._name!r} is unreachable: {exc}") from exc

        projected = [
            (
                record.calc_ref,
                rows_for(record, tenant_id=self._tenant_id, writer_version=self._writer_version),
            )
            for record in records
        ]
        for (table, columns), rows in self._batches(projected, columns_by_table).items():
            statement = upsert_statement(table, columns, warehouse.placeholder)
            try:
                async with warehouse.cursor() as cursor:
                    await execute_many(cursor, statement, [values for values, _ in rows])
            except WarehouseQueryError as exc:
                # A batch shares a failure, so it is replayed row by row to name the offending table
                # and
                # `calc_ref`. Safe because every statement is an idempotent upsert.
                await self._row_at_a_time(warehouse, table, statement, rows, exc)
            except Exception as exc:
                # Same widening as the connect arm: a server that went away stays retryable.
                raise SinkUnavailableError(
                    f"result sink {self._name!r} became unreachable mid-batch: {exc}"
                ) from exc

    def _batches(
        self,
        projected: Sequence[tuple[str, dict[str, list[dict[str, Any]]]]],
        columns_by_table: dict[str, set[str]],
    ) -> dict[tuple[str, tuple[str, ...]], list[tuple[list[Any], str]]]:
        """Every row this batch will write, grouped into the statements that can carry them.

        Keyed on `(table, column set)`, which is what `upsert_statement` depends on; the column set
        is
        per row because the omission filter is. Table-major across the whole batch in `TABLE_ORDER`
        (dict insertion order, relied on deliberately), so every parent is written before every
        child.
        """
        batches: dict[tuple[str, tuple[str, ...]], list[tuple[list[Any], str]]] = {}
        for table in TABLE_ORDER:
            known = columns_by_table[table]
            for calc_ref, rows_by_table in projected:
                for row in rows_by_table.get(table) or []:
                    # Omit optional columns the site lacks (absent reads as "not recorded");
                    # required ones were
                    # refused at the probe.
                    usable = {key: value for key, value in row.items() if key in known}
                    dropped = set(row) - set(usable)
                    if dropped:
                        self._report_dropped(table, dropped)
                    group = batches.setdefault((table, tuple(usable)), [])
                    group.append((list(usable.values()), calc_ref))
        return batches

    async def _row_at_a_time(
        self,
        warehouse: Warehouse,
        table: str,
        statement: str,
        rows: Sequence[tuple[list[Any], str]],
        refusal: WarehouseQueryError,
    ) -> None:
        """Re-send one group singly, so the refusal names the row that caused it.

        Raises when a row is refused. Returns normally only when every row is accepted on replay, in
        which case the rows are written and the delivery stands; `degraded()` records the fallback.
        """
        degraded(
            logger,
            "result_sink_batch_replayed",
            "result sink %s: a batch of %d %s rows was refused; replaying it row at a time to "
            "name the row (%s)",
            self._name,
            len(rows),
            table,
            refusal,
            level=logging.WARNING,
            exc_info=False,
        )
        for values, calc_ref in rows:
            try:
                async with warehouse.cursor() as cursor:
                    await cursor.execute(statement, values)
            except WarehouseQueryError as exc:
                raise SinkRejectedError(
                    f"result sink {self._name!r} refused a {table} row for {calc_ref!r}: {exc}"
                ) from exc
            except Exception as exc:
                raise SinkUnavailableError(
                    f"result sink {self._name!r} became unreachable mid-batch: {exc}"
                ) from exc
