"""The Databricks SQL driver — the second module in this package that knows a vendor exists.

Alone in a file and imported by nothing, exactly as `snowflake.py` is: `connection.driver` in a
binding names it, `warehouse.connect` resolves that string when a connection is first opened, and a
repository with no Databricks client installed runs the whole test suite against a fake.

**What this driver serves, and what it does not.** The ingest half needs nothing special — the
statements `sql.py` builds for it are `SELECT *`, `COALESCE`, `>= ?`, `ORDER BY … ASC`, `LIMIT ?`
and `IN (…)`, which Databricks SQL runs unchanged. The similarity search is where the dialects
differ, and this one differs in *two* places rather than one:

* the function is `vector_cosine_similarity`, not Snowflake's `VECTOR_COSINE_SIMILARITY`; and
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

**Sync client, async seam.** The vendor client blocks, so every call crosses `asyncio.to_thread`,
for the reason `snowflake.py` gives: a retriever runs inside a `gather`, and a blocking driver call
on the event loop stalls every other leg of the fan-out for the length of a warehouse query.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Sequence
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

    def __init__(self, cursor: Any, client: Any) -> None:
        """Wrap a client cursor, keeping the client module for its error types."""
        self._cursor = cursor
        self._client = client

    async def execute(self, sql: str, params: Sequence[Any]) -> None:
        """Run the statement, translating a client error into the engine's own two types."""
        try:
            # A list rather than a tuple: the connector reads a sequence as positional `?` markers,
            # and a dict as named ones. `sql.py` builds positional statements.
            await asyncio.to_thread(self._cursor.execute, sql, list(params))
        except self._client.OperationalError as exc:
            # Network, session or timeout. Transient by nature, so `ConnectionError` — which
            # Temporal retries — exactly as `chemclaw.core.db` splits the same two cases.
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
        over one raises rather than keying by column — the one line that differs from the Snowflake
        cursor, and the kind of difference that would otherwise surface as an empty result.
        """
        rows = await asyncio.to_thread(self._cursor.fetchall)
        return [row.asDict() for row in rows]


class DatabricksWarehouse:
    """A `Warehouse` backed by the Databricks SQL connector.

    Built by `warehouse.connect.open_warehouse` from the binding's `connection:` block, so its
    keyword arguments are that block's fields. Three of them mean something different here than
    they do for Snowflake, and the binding's comments should say so:

    | binding field | Databricks |
    | --- | --- |
    | `account_env` | the workspace hostname (`adb-….azuredatabricks.net`) |
    | `password_env` | a personal access token |
    | `warehouse` | the SQL warehouse id, or its full `/sql/1.0/warehouses/…` path |
    | `database` | the Unity Catalog *catalog* |
    | `schema` | the schema within it |

    The alternative was a driver-specific pass-through on `ConnectionBinding`, which would add a
    field to the shared model that exactly one driver reads. These five already mean "which tenant,
    which credential, which compute, which namespace"; the vendor's word for each is what differs.
    """

    def __init__(
        self,
        *,
        account: str = "",
        user: str = "",
        password: str = "",
        private_key: str = "",
        warehouse: str = "",
        database: str = "",
        schema: str = "",
        role: str = "",
        query_timeout_seconds: int = 60,
    ) -> None:
        """Record what to connect with. The connection itself is opened lazily, on first use."""
        if not account:
            raise BindingError(
                "the Databricks driver needs `account_env` naming the variable that holds the "
                "workspace hostname"
            )
        if not password:
            raise BindingError(
                "the Databricks driver authenticates with a personal access token; name the "
                "variable holding it in `password_env`"
            )
        if not warehouse:
            raise BindingError(
                "the Databricks driver needs `warehouse` set to the SQL warehouse id (or its full "
                "/sql/1.0/warehouses/... path) — there is no default compute to fall back on"
            )
        # Refused rather than dropped. Both are meaningful on Snowflake and have no analogue here,
        # so silently ignoring one would leave a deployment believing a credential or an access
        # restriction was in force when it was not.
        for field, value in (("private_key_env", private_key), ("role", role)):
            if value:
                raise BindingError(
                    f"the Databricks driver has no use for `{field}`: it authenticates with a "
                    "personal access token, and access is governed by Unity Catalog grants on the "
                    "token's principal rather than by a role named in the connection"
                )
        self._options: dict[str, Any] = {
            "server_hostname": account,
            "access_token": password,
            "http_path": (
                warehouse
                if warehouse.startswith("/")
                else _WAREHOUSE_PATH.format(warehouse=warehouse)
            ),
        }
        if user:
            # Not a credential here — the token carries the identity — but the connector forwards it
            # to the server as the session's user agent entry, which is what an operator greps for
            # in the query history when asking who ran a statement.
            self._options["_user_agent_entry"] = user
        if database:
            self._options["catalog"] = database
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

    @asynccontextmanager
    async def cursor(self) -> AsyncIterator[_DatabricksCursor]:
        """A cursor for one statement, closed on exit whatever happened inside."""
        connection = await self._connect()
        client = _client()
        raw = await asyncio.to_thread(connection.cursor)
        try:
            yield _DatabricksCursor(raw, client)
        finally:
            await asyncio.to_thread(raw.close)
