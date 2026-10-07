"""The Databricks SQL driver — one of the modules in this package that knows a vendor exists.

Imported by nothing directly: a binding's `connection.driver` names it and `chemclaw.core.connect`
resolves it at first connect, so the suite runs without the client. The constructor signature is the
connection block's schema, in Databricks' own terms
(D-2026-08-26-the-driver-s-signature-is-the-schema).

The ingest statements `sql.py` builds run unchanged. The similarity search differs in two places:
the function is `vector_cosine_similarity` over `ARRAY<FLOAT>`, and there is no array parameter, so
the query vector is bound as one JSON string and parsed with `from_json(?, 'ARRAY<FLOAT>')`, keeping
it a bound value rather than statement text. Only `cosine` is offered.

No host literal: the hostname comes from the binding's environment variable. The client is
synchronous, so every call crosses `asyncio.to_thread` to keep a retriever from stalling the
fan-out.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any

from chemclaw.ingest.eln.warehouse.binding import BindingError
from chemclaw.ingest.eln.warehouse.driver import VectorDialect, WarehouseQueryError

logger = logging.getLogger(__name__)

# The SQL warehouse path a bare warehouse id expands to. A binding may give either — the id, which
# is what the Databricks UI shows, or the full path, which is what the client wants.
_WAREHOUSE_PATH = "/sql/1.0/warehouses/{warehouse}"


def _client() -> Any:
    """The Databricks SQL client module, or a directive error saying it is not installed.

    A `BindingError`: a binding named this driver on an image without the client, which retrying
    will not fix.
    """
    try:
        from databricks import sql as databricks_sql
    except ImportError as exc:
        raise BindingError(
            "this binding names the Databricks driver, but `databricks-sql-connector` is not "
            "installed. It is not a dependency of this repository — a deployment that binds a "
            "Databricks source installs it"
        ) from exc
    return databricks_sql


class DatabricksVectorDialect:
    """Databricks' spelling of a similarity search: one function, and a JSON-parsed query vector."""

    def similarity(self, metric: str) -> tuple[str, str]:
        """`vector_cosine_similarity`, sorted descending. Only cosine is served."""
        if metric != "cosine":
            raise WarehouseQueryError(
                f"the Databricks driver serves only metric 'cosine', not {metric!r}: this "
                "repository has not verified a Databricks function for the others, and guessing "
                "one would fail on the server rather than here"
            )
        return "vector_cosine_similarity", "DESC"

    def query_vector(self, placeholder: str, vector: Sequence[float], dim: int) -> tuple[str, Any]:
        """Bind the vector as one JSON scalar and let the server parse it into `ARRAY<FLOAT>`.

        `dim` is unused: `from_json` takes the width from the document, and a mismatch surfaces as
        the server's `VECTOR_DIMENSION_MISMATCH`, naming both widths.
        """
        return f"from_json({placeholder}, 'ARRAY<FLOAT>')", json.dumps(list(vector))


class _DatabricksCursor:
    """One statement against a Databricks SQL warehouse, returning column-keyed rows."""

    def __init__(self, cursor: Any, client: Any, on_session_lost: Callable[[], None]) -> None:
        """Wrap a client cursor, keeping the client module for its error types.

        `on_session_lost` lets the transient-error arm evict the dead session in the same decision
        that classifies the error; keeping a dead handle cached would make an expired session a
        permanent outage.
        """
        self._cursor = cursor
        self._client = client
        self._on_session_lost = on_session_lost

    async def execute(self, sql: str, params: Sequence[Any]) -> None:
        """Run the statement, translating a client error into the engine's own two types."""
        try:
            # A list rather than a tuple: the connector reads a sequence as positional `?` markers,
            # and a dict as named ones. `sql.py` builds positional statements.
            await asyncio.to_thread(self._cursor.execute, sql, list(params))
        except self._client.OperationalError as exc:
            # Network, session or timeout: transient, so `ConnectionError` (which Temporal retries),
            # as `chemclaw.core.db` does. The session is dropped too, so the retry reconnects.
            self._on_session_lost()
            raise ConnectionError(f"warehouse unreachable: {exc}") from exc
        except self._client.Error as exc:
            # A relation or column the binding names and the warehouse lacks: identical on every
            # retry, so non-retryable. The driver's text, which quotes the statement and the site's
            # schema, goes to the log only, never into the exception a chemist or the model sees.
            logger.exception("warehouse rejected a statement")
            raise WarehouseQueryError(
                "the warehouse rejected the query; the statement and the warehouse's own message "
                "are in this pod's log"
            ) from exc

    async def fetchall(self) -> list[dict[str, Any]]:
        """Every row of the last statement, each keyed by column name.

        `Row.asDict()`, since `dict(row)` over the connector's tuple-like `Row` does not key by
        column.
        """
        rows = await asyncio.to_thread(self._cursor.fetchall)
        return [row.asDict() for row in rows]


class DatabricksWarehouse:
    """A `Warehouse` backed by the Databricks SQL connector.

    Built by `chemclaw.core.connect.open_connection` from the binding's `connection:` block, whose
    keys are the parameters below (`*_env` names an environment variable holding a secret). The
    words are Databricks' own.
    """

    def __init__(
        self,
        *,
        server_hostname: str = "",
        access_token: str = "",
        warehouse_id: str = "",
        http_path: str = "",
        catalog: str = "",
        schema: str = "",
        user_agent_entry: str = "",
        query_timeout_seconds: int = 60,
    ) -> None:
        """Record what to connect with. The connection itself is opened lazily, on first use."""
        if not server_hostname:
            raise BindingError(
                "the Databricks driver needs `server_hostname_env` naming the variable that holds "
                "the workspace hostname (adb-....azuredatabricks.net)"
            )
        if not access_token:
            raise BindingError(
                "the Databricks driver authenticates with a personal access token; name the "
                "variable holding it in `access_token_env`"
            )
        if bool(warehouse_id) == bool(http_path):
            raise BindingError(
                "the Databricks driver needs exactly one of `warehouse_id` (the id the SQL "
                "warehouse page shows) or `http_path` (its full /sql/1.0/warehouses/... path); "
                "there is no default compute to fall back on, and naming both leaves which one is "
                "in force to the reader"
            )
        if warehouse_id.startswith("/"):
            # Strict about the form: interpolating a path into the template would build a doubled
            # path that fails at connect time with a misleading workspace error.
            raise BindingError(
                f"`warehouse_id` is the bare id the SQL warehouse page shows, not a path; "
                f"{warehouse_id!r} looks like an `http_path:` — name it in that field instead"
            )
        if not 1 <= query_timeout_seconds <= 3600:
            # The only bound on a runaway scan of a shared warehouse, and `0` means "no timeout" to
            # Spark, so it is refused. Checked here because it is this driver's keyword.
            raise BindingError(
                "`query_timeout_seconds` must be between 1 and 3600; "
                f"got {query_timeout_seconds}, and 0 means no timeout at all to a SQL warehouse"
            )
        self._options: dict[str, Any] = {
            "server_hostname": server_hostname,
            "access_token": access_token,
            "http_path": http_path or _WAREHOUSE_PATH.format(warehouse=warehouse_id),
        }
        if user_agent_entry:
            # Not a credential: forwarded as the session's user agent entry, which operators search
            # for in the query history.
            self._options["_user_agent_entry"] = user_agent_entry
        if catalog:
            self._options["catalog"] = catalog
        if schema:
            self._options["schema"] = schema
        # Bound on the session once rather than a `SET` before every statement.
        self._options["session_configuration"] = {"statement_timeout": str(query_timeout_seconds)}
        self._connection: Any | None = None
        # Concurrent callers share one cached instance (`open_warehouse`); without this lock two
        # overlapping `_connect()` calls would each open a session and orphan one until its
        # server-side idle timeout.
        self._connect_lock = asyncio.Lock()

    @property
    def placeholder(self) -> str:
        """Databricks native parameters bind positionally with `?`."""
        return "?"

    @property
    def vector_dialect(self) -> VectorDialect:
        """Cosine only — see `DatabricksVectorDialect`."""
        return DatabricksVectorDialect()

    async def _connect(self) -> Any:
        """Open the connection once, or raise `ConnectionError` so the caller can retry.

        Locked end to end, so a second coroutine awaits the same attempt rather than opening a
        second session.
        """
        async with self._connect_lock:
            if self._connection is None:
                client = _client()
                try:
                    self._connection = await asyncio.to_thread(client.connect, **self._options)
                except client.Error as exc:
                    raise ConnectionError(f"cannot connect to the warehouse: {exc}") from exc
            return self._connection

    def _session_lost(self) -> None:
        """Forget the open session, so the next call opens a new one.

        A Databricks SQL session expires, and the warehouse may stop or scale to zero; without this
        every later statement would fail against the same dead handle for the pod's life. The
        trigger is a transient failure this driver already recognises, and the cost is one
        reconnect; `ConnectionError` tells Temporal to retry.
        """
        self._connection = None

    @asynccontextmanager
    async def cursor(self) -> AsyncIterator[_DatabricksCursor]:
        """A cursor for one statement, closed on exit whatever happened inside.

        Opening the cursor is inside the error translation too: on an expired session
        `connection.cursor()` itself raises the client's `Error`, which must become
        `ConnectionError` or `WarehouseQueryError` like any other failure.
        """
        connection = await self._connect()
        client = _client()
        try:
            raw = await asyncio.to_thread(connection.cursor)
        except client.Error as exc:
            self._session_lost()
            raise ConnectionError(f"warehouse session is gone: {exc}") from exc
        try:
            yield _DatabricksCursor(raw, client, self._session_lost)
        finally:
            await asyncio.to_thread(raw.close)
