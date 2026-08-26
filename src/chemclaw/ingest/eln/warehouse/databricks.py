"""The Databricks SQL driver — one of the modules in this package that knows a vendor exists.

Alone in a file and imported by nothing: `connection.driver` in a binding names it,
`chemclaw.core.connect` resolves that string when a connection is first opened, and a repository
with no Databricks client installed runs the whole test suite against a fake.

**Its constructor signature is the connection block's schema** — `server_hostname`,
`access_token_env`, `warehouse_id`, `catalog`, `schema` — in Databricks' own words rather than in a
seam vocabulary translated per vendor. That is the rule
`D-2026-08-26-the-driver-s-signature-is-the-schema` settled: the model this driver used to be built
through named an `account`, a `warehouse` and a `role`, so this file had to redefine three of those
to mean something else and *refuse* two more that have no analogue here. Attaching the next
database — a Postgres, a DuckDB file, a ClickHouse, a vector database — is now a module beside this
one with its own keywords, and nothing shared to widen.

**What this driver serves, and what it does not.** The ingest half needs nothing special — the
statements `sql.py` builds for it are `SELECT *`, `COALESCE`, `>= ?`, `ORDER BY … ASC`, `LIMIT ?`
and `IN (…)`, which Databricks SQL runs unchanged. The similarity search is where dialects differ,
and this one differs in *two* places rather than one:

* the function is `vector_cosine_similarity`, lower case and taking `ARRAY<FLOAT>`; and
* there is no `VECTOR` type and no array *parameter*. Native parameters are scalars, so a 1536-float
  query vector cannot be bound as a list at all. It is bound as one JSON string and parsed
  server-side with `from_json(?, 'ARRAY<FLOAT>')` — which keeps the vector a bound *value* rather
  than statement text, the invariant `sql.py`'s own docstring is built on. `ARRAY<FLOAT>` and not
  `ARRAY<DOUBLE>`, because `vector_cosine_similarity` accepts only the first.

Only `cosine` is offered. Databricks' names for L2 and inner product are not verified here, and a
dialect that guesses one would emit a statement the server rejects on the first query rather than a
message naming the metric.

**No host literal.** The workspace hostname arrives from the binding's named environment variable
and goes straight to the client, which builds its own URL — `tests/test_no_egress.py` refuses an
external host in first-party code on purpose, because the address of a data source belongs in
configuration where attaching one is a reviewable decision.

**Sync client, async seam.** The vendor client blocks, so every call crosses `asyncio.to_thread`: a
retriever runs inside a `gather`, and a blocking driver call on the event loop stalls every other
leg of the fan-out for the length of a warehouse query.
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

    A `BindingError` because that is what it is: a binding named this driver on a deployment whose
    image does not carry the client. Not transient, so retrying only delays the message.
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

        `dim` is unused: `from_json` takes the width from the document, and there is no cast to
        declare it to. A width mismatch against the stored column is then the server's
        `VECTOR_DIMENSION_MISMATCH`, which names both widths — a better message than a truncation.
        """
        return f"from_json({placeholder}, 'ARRAY<FLOAT>')", json.dumps(list(vector))


class _DatabricksCursor:
    """One statement against a Databricks SQL warehouse, returning column-keyed rows."""

    def __init__(self, cursor: Any, client: Any, on_session_lost: Callable[[], None]) -> None:
        """Wrap a client cursor, keeping the client module for its error types.

        `on_session_lost` is the warehouse's own eviction: the transient arm below decides that the
        session is gone, and deciding that while leaving the handle cached is what turned an
        overnight warehouse auto-stop into a permanent outage. The two are one decision, so they
        are made in one place.
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
            # Network, session or timeout. Transient by nature, so `ConnectionError` — which
            # Temporal retries — exactly as `chemclaw.core.db` splits the same two cases. And the
            # handle goes with it: a retry against the same dead session is not a retry.
            self._on_session_lost()
            raise ConnectionError(f"warehouse unreachable: {exc}") from exc
        except self._client.Error as exc:
            # A relation or column the binding names and the warehouse does not have. Identical on
            # every retry, so non-retryable.
            #
            # **The driver's text stays in the log and out of the exception.** A
            # `WarehouseQueryError` raised inside a durable job reaches the session, and the
            # client's message quotes the failing statement — so the site's table names, its column
            # names and the shape of the query the binding built would land in a chemist's
            # transcript and in the model's context. The full text is one `logger.exception` away.
            logger.exception("warehouse rejected a statement")
            raise WarehouseQueryError(
                "the warehouse rejected the query; the statement and the warehouse's own message "
                "are in this pod's log"
            ) from exc

    async def fetchall(self) -> list[dict[str, Any]]:
        """Every row of the last statement, each keyed by column name.

        `Row.asDict()` rather than `dict(row)`: the connector returns a tuple-like `Row`, so `dict`
        over one raises rather than keying by column — the kind of difference that would otherwise
        surface as an empty result rather than as an error.
        """
        rows = await asyncio.to_thread(self._cursor.fetchall)
        return [row.asDict() for row in rows]


class DatabricksWarehouse:
    """A `Warehouse` backed by the Databricks SQL connector.

    Built by `chemclaw.core.connect.open_connection` from the binding's `connection:` block, whose
    keys are the parameters below — `*_env` for the ones that name an environment variable holding a
    secret, the rest written directly. Nothing translates between a seam vocabulary and this one,
    which is the point: the words here are the words the Databricks documentation uses.
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
            # One field used to take either form and branch on this prefix. Two fields do not get
            # to be lenient about it: interpolating a path into the template would build
            # `/sql/1.0/warehouses//sql/1.0/warehouses/<id>`, which fails at connect time with a
            # message about the *workspace* rather than about the binding.
            raise BindingError(
                f"`warehouse_id` is the bare id the SQL warehouse page shows, not a path; "
                f"{warehouse_id!r} looks like an `http_path:` — name it in that field instead"
            )
        if not 1 <= query_timeout_seconds <= 3600:
            # The one thing standing between a runaway scan and a shared warehouse's bill, so `0`
            # is the worst possible value: Spark reads it as *no* timeout, the exact opposite of
            # what the field is for. Bounded here rather than in the binding model because it is
            # this driver's keyword — the model no longer knows any driver's vocabulary — and the
            # session parameter it becomes is this vendor's too.
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
            # Not a credential — the token carries the identity — but the connector forwards it to
            # the server as the session's user agent entry, which is what an operator greps for in
            # the query history when asking who ran a statement.
            self._options["_user_agent_entry"] = user_agent_entry
        if catalog:
            self._options["catalog"] = catalog
        if schema:
            self._options["schema"] = schema
        # Bound on the session rather than re-applied per cursor: a runaway scan on a shared
        # warehouse costs real money and the binding's timeout is the only thing that stops one, but
        # a `SET` before every statement would double the round trips to say it once.
        self._options["session_configuration"] = {"statement_timeout": str(query_timeout_seconds)}
        self._connection: Any | None = None

    @property
    def placeholder(self) -> str:
        """Databricks native parameters bind positionally with `?`."""
        return "?"

    @property
    def vector_dialect(self) -> VectorDialect:
        """Cosine only — see `DatabricksVectorDialect`."""
        return DatabricksVectorDialect()

    async def _connect(self) -> Any:
        """Open the connection once, or raise `ConnectionError` so the caller can retry."""
        if self._connection is None:
            client = _client()
            try:
                self._connection = await asyncio.to_thread(client.connect, **self._options)
            except client.Error as exc:
                raise ConnectionError(f"cannot connect to the warehouse: {exc}") from exc
        return self._connection

    def _session_lost(self) -> None:
        """Forget the open session, so the next call opens a new one.

        **The one thing this driver was missing.** `_connect` memoizes and nothing ever cleared it,
        while a Databricks SQL session is emphatically not permanent: it expires, and the warehouse
        behind it can be stopped or scaled to zero overnight. Every statement afterwards failed
        against the same dead handle for the life of the pod — in the retriever, where an
        `except Exception` backstop turned it into an empty result, so the ELN simply stopped
        answering while the pod read healthy; in a sync activity, on every attempt and every
        Temporal retry. That backstop is gone and the failure reaches `fanout._sweep`, which is
        what makes a dead session visible rather than merely survivable — but the reconnect below
        is still what stops it recurring.

        The seam has no lifecycle hook to close a connection from (`driver.Warehouse` says why it
        has no `close`), and it does not need one: what has to be dropped is the *session*, not the
        configured warehouse, and the trigger is a failure this driver already recognises. One
        reconnect on the next call is the whole cost, and `ConnectionError` already tells Temporal
        to come back.
        """
        self._connection = None

    @asynccontextmanager
    async def cursor(self) -> AsyncIterator[_DatabricksCursor]:
        """A cursor for one statement, closed on exit whatever happened inside.

        Opening the cursor is inside the translation too, and it was the gap that made the rest of
        it moot: `connection.cursor()` on an expired session raises the client's own `Error` from
        *outside* `_DatabricksCursor.execute`, so it was neither the retryable `ConnectionError`
        nor the reportable `WarehouseQueryError` — it escaped as a vendor class no caller in this
        tree knows, and the retriever's backstop turned it into an empty result.
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
