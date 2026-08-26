"""Publishing to a SQL database running the shipped schema.

**Reuses the inbound warehouse seam's driver Protocol rather than defining a second one.**
`ingest/eln/warehouse/driver.py`'s `Warehouse`/`WarehouseCursor` are already dialect-neutral —
`execute(sql, params)` does not care whether the statement reads or writes, and `placeholder` is on
the connection because parameter style is a dialect fact. The read-only-ness of that seam lives in
its `sql.py`, not in the driver. So a driver written for the inbound seam is already shaped to
write, and this one connects through the same Protocol.

**The statements it sends are Postgres, though** (`dialect.py`) — the upserts are `ON CONFLICT`,
which Snowflake and Oracle do not accept. The seam is portable; the emitter is not yet. Reaching
another engine is a `MERGE` emitter beside the current one, not a configuration change.

**What is deliberately *not* reused is that seam's `ConnectionBinding`.** It is Snowflake-shaped —
an account, a warehouse, a role, and **no host or port** — so pointing a Postgres store at it would
mean either abusing `account` as a hostname or teaching one model to describe two products. The
connection block here is validated by the driver's own signature instead, which is the same rule
the data-source seam applies to its `config:`. `publish/connect.py` keeps the credential discipline
that mattered: variables are *named*, read at connect time, and registered for log redaction first.
"""

import logging
from collections.abc import Sequence
from typing import Any

from chemclaw.ingest.eln.warehouse.driver import Warehouse, WarehouseQueryError
from chemclaw.publish.connect import SinkConnectionError, open_connection
from chemclaw.publish.dialect import TABLE_ORDER, rows_for, upsert_statement
from chemclaw.publish.driver import SinkRejectedError, SinkUnavailableError
from chemclaw.publish.record import ResultRecord

logger = logging.getLogger(__name__)


class SqlResultSink:
    """Writes published records into a SQL database running `schema/result-store/`.

    **Writes down to the schema it finds.** A site may not grant DDL to the runtime principal — this
    repository already splits its own migration DSN from its runtime DSN for that reason — so a
    deployment can be running an older schema than this image expects. Rather than failing every
    row, the sink probes `information_schema.columns` once and omits columns the site lacks,
    logging which. The alternative, writing the full statement and letting each row fail, turns a
    schema *lag* into a total publish outage, which is the worse failure.
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
            connection: The `connection:` block — a `module:callable` driver plus whatever that
                driver's signature takes. Any key ending `_env` names an environment variable to
                read at connect time; the value itself is never written in a manifest.
            writer_version: The ChemClaw release stamped on each row, so a consumer can tell an
                absent measurement from an absent column.
        """
        self._name = name
        self._tenant_id = tenant_id
        self._connection_binding = dict(connection)
        self._writer_version = writer_version
        self._warehouse: Warehouse | None = None
        self._columns: dict[str, set[str]] | None = None

    async def aclose(self) -> None:
        """Close the held connection and forget the probed schema.

        The drain builds a sink per run, so without this each pass leaked one connection — see
        `PostgresWarehouse.aclose`. The cached column set goes with it because the two are scoped
        together: the probe is cached for the sink's lifetime precisely so a site that applies a
        migration is picked up on the next pass rather than the next restart.
        """
        warehouse = self._warehouse
        self._warehouse = None
        self._columns = None
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
        """Which columns the site's schema actually has, probed once and cached.

        Cached for the sink's lifetime rather than forever: the drain builds a sink per run, so a
        DBA who adds a column sees it picked up on the next pass without a restart.
        """
        if self._columns is not None:
            return self._columns
        async with warehouse.cursor() as cursor:
            await cursor.execute(
                # `LOWER(table_name)`, because `information_schema` is not case-agnostic: Postgres
                # stores unquoted identifiers folded down and Snowflake and Oracle fold them up, so
                # binding this module's lowercase literals against the raw column matched nothing at
                # all on two of the three engines — and a probe that finds no tables reports every
                # table missing, which reads exactly like a site that never ran the DDL.
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE LOWER(table_name) IN ("
                + ", ".join([warehouse.placeholder] * len(TABLE_ORDER))
                + ")",
                list(TABLE_ORDER),
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
        self._columns = found
        return found

    async def deliver(self, records: Sequence[ResultRecord]) -> None:
        """Write every record's rows, in dependency order, idempotently.

        One transaction per batch is *not* attempted: the warehouse Protocol exposes a cursor and no
        transaction control, and every write here is an upsert onto a content-addressed key — so a
        batch that fails halfway leaves a partial but *correct* state that the retry completes. That
        is the property that makes the outbox's at-least-once delivery safe.
        """
        if not records:
            return
        try:
            warehouse = self._connect()
            columns_by_table = await self._known_columns(warehouse)
        except (ConnectionError, OSError) as exc:
            raise SinkUnavailableError(f"result sink {self._name!r} is unreachable: {exc}") from exc

        for record in records:
            rows_by_table = rows_for(
                record, tenant_id=self._tenant_id, writer_version=self._writer_version
            )
            for table in TABLE_ORDER:
                rows = rows_by_table.get(table) or []
                if not rows:
                    continue
                known = columns_by_table[table]
                for row in rows:
                    # Omit what the site does not have, rather than failing the row. A column added
                    # by a later release is absent here, and absent reads correctly as "not
                    # recorded" — which is what the additive-migration rule guarantees.
                    usable = {key: value for key, value in row.items() if key in known}
                    dropped = set(row) - set(usable)
                    if dropped:
                        logger.warning(
                            "result sink %s: %s lacks %s; those values are not published",
                            self._name,
                            table,
                            ", ".join(sorted(dropped)),
                        )
                    statement = upsert_statement(table, tuple(usable), warehouse.placeholder)
                    try:
                        async with warehouse.cursor() as cursor:
                            await cursor.execute(statement, list(usable.values()))
                    except WarehouseQueryError as exc:
                        raise SinkRejectedError(
                            f"result sink {self._name!r} refused a {table} row for "
                            f"{record.calc_ref!r}: {exc}"
                        ) from exc
                    except (ConnectionError, OSError) as exc:
                        raise SinkUnavailableError(
                            f"result sink {self._name!r} became unreachable mid-batch: {exc}"
                        ) from exc
