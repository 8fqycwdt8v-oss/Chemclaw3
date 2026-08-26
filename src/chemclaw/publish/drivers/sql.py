"""Publishing to a SQL database running the shipped schema.

**Reuses the inbound warehouse seam's driver Protocol rather than defining a second one.**
`ingest/eln/warehouse/driver.py`'s `Warehouse`/`WarehouseCursor` are already dialect-neutral —
`execute(sql, params)` does not care whether the statement reads or writes, and `placeholder` is on
the connection because parameter style is a dialect fact. The read-only-ness of that seam lives in
its `sql.py`, not in the driver. So a driver written for the inbound seam is already shaped to
write, and this one connects through the same Protocol.

**The statements it sends are Postgres, though** (`dialect.py`) — the upserts are `ON CONFLICT`,
which several warehouses spell `MERGE` instead. The seam is portable; the emitter is not yet.
Reaching another engine is a `MERGE` emitter beside the current one, not a configuration change.

**The `connection:` block is the driver's own signature**, here and on the inbound side alike
(`D-2026-08-26-the-driver-s-signature-is-the-schema`). This module used to argue for the opposite —
that the inbound seam's `ConnectionBinding` could not be reused because it enumerated one vendor's
connection fields and had no host or port. It enumerates nothing now, and both seams resolve their
driver through `chemclaw.core.connect`, which keeps the discipline that mattered: variables are
*named*, read at connect time, and registered for log redaction first.
"""

import logging
from collections.abc import Sequence
from typing import Any

from chemclaw.core.config import settings
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
                absent measurement from an absent column. Defaults to this deployment's own
                revision; a manifest sets it only to override that.
        """
        self._name = name
        self._tenant_id = tenant_id
        self._connection_binding = dict(connection)
        # **Defaulted to the deployment's revision rather than left empty.** Nothing in this tree
        # computed a writer version and the shipped manifest declares none, so the column the DDL
        # justifies with "without these, 'why is `in_domain` null for everything before March' is
        # unanswerable" held `''` on every row a real deployment writes — recorded, and blank.
        # `deployment_revision` is the same Git SHA the audit trail already stamps for exactly this
        # question, so the two records of "which ChemClaw3 did this" agree by construction.
        self._writer_version = writer_version or settings.deployment_revision
        # The schema the driver was given, so the column probe below can be qualified the way the
        # writes are. Empty for a driver that spells its namespace differently, which leaves the
        # probe unqualified — the behaviour every target had until now.
        self._schema = str(self._connection_binding.get("schema") or "")
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
        # **Qualified by the same schema the writes resolve through.** The statements this class
        # builds name no schema and are resolved by the connection's `search_path`, so a probe that
        # asked by table *name* alone was answering about a different table the moment the target
        # held a same-named relation anywhere else the role can see — an archive, a staging copy, a
        # second tenant. The union then keeps a column the site's own table does not have and every
        # row of that table is refused; the mirror case is a DDL applied off the search path, where
        # the "the target has no ..." guard passes while every write fails.
        #
        # Split on commas, because `schema:` becomes a `search_path` and a search path may name
        # several — matching the whole string would find nothing there and report every table
        # missing, which is the failure this probe's `LOWER()` already exists to avoid.
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
                # `LOWER(table_name)`, because `information_schema` is not case-agnostic: Postgres
                # stores unquoted identifiers folded down and Snowflake and Oracle fold them up, so
                # binding this module's lowercase literals against the raw column matched nothing at
                # all on two of the three engines — and a probe that finds no tables reports every
                # table missing, which reads exactly like a site that never ran the DDL.
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
