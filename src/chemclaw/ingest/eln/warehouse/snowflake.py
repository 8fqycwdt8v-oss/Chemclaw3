"""The Snowflake driver — the one module in this package that knows a vendor exists.

Alone in a file, and imported by nothing: `connection.driver` in a binding names it, and
`warehouse.connect` resolves that string when a connection is first opened. So a chat pod that only
retrieves never loads it, a repository with no Snowflake client installed runs the whole test suite
against a fake, and swapping in a second warehouse is a new module beside this one rather than a
branch inside the engine.

**The client is not a dependency of this repository**, and that is why it is imported inside
`_client()` rather than at module scope — the one place in this package that departs from the seam's
"import whatever you need at the top" rule, and it departs for a reason the rule does not cover.
That rule is about *which process* pays for an import; this is about a package that is not installed
in any of them. `tests/test_publish.py` imports every first-party module to enumerate the error
hierarchy, so a module-scope `import snowflake.connector` would make this repository's own test
suite depend on a client only a real deployment has. Deferring the import to first use keeps the
module importable everywhere, keeps it type-checked, and still fails with a directive message on the
one path that genuinely needs the client.

**No host literal.** The account identifier goes to the client, which builds its own URL. Writing
`f"{account}.snowflakecomputing.com"` here would put an external host into first-party code, which
`tests/test_no_egress.py` refuses on purpose — the address of a data source belongs in
configuration, where attaching one is a reviewable decision.

**Sync client, async seam.** The vendor client blocks, so every call crosses `asyncio.to_thread`.
The engine's seam is async because its other implementations need to be and because a retriever runs
inside a `gather` — a blocking driver call on the event loop would stall every other leg of the
fan-out for the duration of a warehouse query.
"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from cryptography.hazmat.primitives import serialization

from chemclaw.ingest.eln.warehouse.binding import BindingError
from chemclaw.ingest.eln.warehouse.driver import WarehouseQueryError


def _client() -> Any:
    """The Snowflake client module, or a directive error saying it is not installed.

    A `BindingError` because that is what it is: a binding named this driver on a deployment whose
    image does not carry the client. Not transient, so retrying it would only delay the message.
    """
    try:
        import snowflake.connector
    except ImportError as exc:
        raise BindingError(
            "this binding names the Snowflake driver, but `snowflake-connector-python` is not "
            "installed. It is not a dependency of this repository — a deployment that binds a "
            "Snowflake source installs it"
        ) from exc
    return snowflake.connector


def _private_key_der(pem: str) -> bytes:
    """Convert a PEM private key to the DER bytes the client wants.

    Key-pair auth is the only credential form Snowflake does not plan to retire, and it is the one
    an unattended worker should use. The client takes DER; a secret store hands out PEM. Converting
    here keeps that mismatch out of the deployment's business.
    """
    loaded = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    return loaded.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


class _SnowflakeCursor:
    """One statement against a Snowflake connection, returning column-keyed rows."""

    def __init__(self, cursor: Any, client: Any) -> None:
        """Wrap a client cursor, keeping the client module for its error types."""
        self._cursor = cursor
        self._client = client

    async def execute(self, sql: str, params: Sequence[Any]) -> None:
        """Run the statement, translating a client error into the engine's non-retryable type."""
        try:
            await asyncio.to_thread(self._cursor.execute, sql, tuple(params))
        except self._client.errors.ProgrammingError as exc:
            # A relation or column the binding names and the warehouse does not have. Identical on
            # every retry, so it is `WarehouseQueryError` (non-retryable) rather than a transient.
            raise WarehouseQueryError(f"warehouse rejected the query: {exc}") from exc
        except self._client.errors.OperationalError as exc:
            # Network, session or timeout. Transient by nature, so `ConnectionError` — which
            # Temporal retries — exactly as `chemclaw.core.db` splits the same two cases.
            raise ConnectionError(f"warehouse unreachable: {exc}") from exc

    async def fetchall(self) -> list[dict[str, Any]]:
        """Every row of the last statement, each keyed by column name."""
        rows = await asyncio.to_thread(self._cursor.fetchall)
        return [dict(row) for row in rows]


class SnowflakeWarehouse:
    """A `Warehouse` backed by the Snowflake Python client.

    Built by `warehouse.connect.open_warehouse` from the binding's `connection:` block, so its
    keyword arguments are that block's non-secret fields plus whichever credentials the binding
    named environment variables for.
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
        self._options: dict[str, Any] = {
            key: value
            for key, value in (
                ("account", account),
                ("user", user),
                ("password", password),
                ("warehouse", warehouse),
                ("database", database),
                ("schema", schema),
                ("role", role),
            )
            if value
        }
        if private_key:
            self._options["private_key"] = _private_key_der(private_key)
        # Bound on the session rather than re-applied per cursor: a runaway scan on a shared
        # warehouse costs real money and the binding's timeout is the only thing that stops one, but
        # an `ALTER SESSION` before every statement would double the round trips to say it once.
        self._options["session_parameters"] = {
            "STATEMENT_TIMEOUT_IN_SECONDS": query_timeout_seconds
        }
        self._options["network_timeout"] = query_timeout_seconds
        self._options["login_timeout"] = query_timeout_seconds
        # Positional binding, set per connection rather than through the module-level `paramstyle`
        # global the client also offers — that global is process-wide and would silently change the
        # parameter style for any other user of this client in the same process.
        self._options["paramstyle"] = "qmark"
        self._connection: Any | None = None

    @property
    def placeholder(self) -> str:
        """Snowflake binds positionally under `paramstyle='qmark'`."""
        return "?"

    async def _connect(self) -> Any:
        """Open the connection once, classifying a failure as retryable or not.

        **Every client error used to become a `ConnectionError`**, which this package's own split
        says means "the warehouse is unreachable, retrying may work" (`driver.WarehouseQueryError`
        documents the other half). A wrong password, an unknown account identifier and a role the
        user does not hold are none of those: they fail identically on every attempt, so the sync
        burned its whole Temporal retry budget before an operator saw a message that then said
        "cannot connect" about a credential problem.

        The split follows the DB-API 2.0 hierarchy the client implements (PEP 249):
        `InterfaceError` is a fault in the client or how it was called, and `ProgrammingError` is a
        request the server understood and refused — which is where authentication and authorization
        failures land. Both are configuration, so both raise `WarehouseQueryError`, which
        `durable.publish` already marks non-retryable by class name. Everything else — the
        operational family, timeouts, transport — keeps `ConnectionError`.

        **What is not verified here, stated rather than implied**: the mapping from Snowflake's own
        error codes onto those classes. That needs a real tenant, and until one exists this is the
        documented contract rather than a measured one. It is strictly better than the previous
        behaviour either way — every one of these was retryable before, including a typo'd password.
        """
        if self._connection is None:
            client = _client()
            try:
                self._connection = await asyncio.to_thread(client.connect, **self._options)
            except (client.errors.InterfaceError, client.errors.ProgrammingError) as exc:
                raise WarehouseQueryError(
                    f"the warehouse refused this connection: {exc}. Check the account identifier, "
                    "the credentials and the role the binding names — retrying will not change it"
                ) from exc
            except client.errors.Error as exc:
                raise ConnectionError(f"cannot connect to the warehouse: {exc}") from exc
        return self._connection

    @asynccontextmanager
    async def cursor(self) -> AsyncIterator[_SnowflakeCursor]:
        """A dict cursor for one statement, closed on exit whatever happened inside."""
        connection = await self._connect()
        client = _client()
        raw = await asyncio.to_thread(connection.cursor, client.DictCursor)
        try:
            yield _SnowflakeCursor(raw, client)
        finally:
            await asyncio.to_thread(raw.close)
