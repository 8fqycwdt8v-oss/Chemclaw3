"""The Databricks SQL driver and its vector dialect, against a fake client module.

The driver is the only module in `chemclaw.ingest.eln.warehouse` that knows this vendor exists, and
what is worth pinning here is the handful of places it differs from the Snowflake one in a way that
would otherwise surface as an empty result rather than an error:

* rows come back as tuple-like `Row` objects, so `dict(row)` keys by *position*, not by column;
* the query vector cannot be bound as a list — there is no array parameter type — so it goes as one
  JSON scalar parsed server-side; and
* three `connection:` fields mean something different here than they do for Snowflake, and two
  others mean nothing at all and are refused rather than dropped.
"""

import asyncio
import functools
import json
from typing import Any

import pytest

from chemclaw.ingest.eln.warehouse.binding import BindingError
from chemclaw.ingest.eln.warehouse.databricks import (
    DatabricksVectorDialect,
    DatabricksWarehouse,
)
from chemclaw.ingest.eln.warehouse.driver import VectorDialect, Warehouse, WarehouseQueryError


def _sync(test: Any) -> Any:
    """Run an `async def` test on its own loop; this repository has no async pytest plugin."""

    @functools.wraps(test)
    def runner(*args: Any, **kwargs: Any) -> None:
        asyncio.run(test(*args, **kwargs))

    return runner


class _Row:
    """A stand-in for the connector's tuple-like `Row`: iterable, but keyed only via `asDict`."""

    def __init__(self, mapping: dict[str, Any]) -> None:
        self._mapping = mapping

    def __iter__(self) -> Any:
        return iter(self._mapping.values())

    def asDict(self) -> dict[str, Any]:  # the vendor's spelling
        return dict(self._mapping)


class _FakeCursor:
    def __init__(self, rows: list[_Row], recorder: list[tuple[str, list[Any]]]) -> None:
        self._rows = rows
        self._recorder = recorder
        self.closed = False

    def execute(self, sql: str, params: list[Any]) -> None:
        self._recorder.append((sql, params))

    def fetchall(self) -> list[_Row]:
        return self._rows

    def close(self) -> None:
        self.closed = True


class _FakeConnection:
    def __init__(self, rows: list[_Row], recorder: list[tuple[str, list[Any]]]) -> None:
        self._rows = rows
        self._recorder = recorder
        self.cursors: list[_FakeCursor] = []

    def cursor(self) -> _FakeCursor:
        made = _FakeCursor(self._rows, self._recorder)
        self.cursors.append(made)
        return made


class _FakeClientModule:
    """The slice of `databricks.sql` the driver touches, including its DB-API error classes."""

    class Error(Exception):
        pass

    class OperationalError(Error):
        pass

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = [_Row(row) for row in (rows or [])]
        self.executed: list[tuple[str, list[Any]]] = []
        self.connect_options: dict[str, Any] = {}
        self.raise_on_execute: Exception | None = None

    def connect(self, **options: Any) -> _FakeConnection:
        self.connect_options = options
        connection = _FakeConnection(self.rows, self.executed)
        if self.raise_on_execute is not None:
            error = self.raise_on_execute

            def _boom(sql: str, params: list[Any]) -> None:
                raise error

            original = connection.cursor

            def _cursor() -> _FakeCursor:
                made = original()
                made.execute = _boom  # type: ignore[method-assign]
                return made

            connection.cursor = _cursor  # type: ignore[method-assign]
        return connection


def _bind(monkeypatch: pytest.MonkeyPatch, client: _FakeClientModule) -> None:
    """Point the driver's late import at the fake, as a deployment points it at the SDK."""
    from chemclaw.ingest.eln.warehouse import databricks as module

    monkeypatch.setattr(module, "_client", lambda: client)


def _warehouse(**overrides: Any) -> DatabricksWarehouse:
    options: dict[str, Any] = {
        "account": "adb-1234.11.azuredatabricks.net",
        "password": "dapi-token",
        "warehouse": "abc123",
        "database": "eln_prod",
        "schema": "reactions",
        "query_timeout_seconds": 45,
    }
    options.update(overrides)
    return DatabricksWarehouse(**options)


# --- the connection binding maps onto Databricks' own vocabulary --------------------------------


def test_the_driver_satisfies_the_warehouse_protocol() -> None:
    """It is a `Warehouse`, dialect and all."""
    assert isinstance(_warehouse(), Warehouse)
    assert isinstance(DatabricksVectorDialect(), VectorDialect)


def test_binding_fields_map_onto_the_clients_own_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """`account`/`password`/`warehouse`/`database` are this seam's words for vendor concepts."""
    client = _FakeClientModule()
    _bind(monkeypatch, client)
    asyncio.run(_warehouse()._connect())
    assert client.connect_options["server_hostname"] == "adb-1234.11.azuredatabricks.net"
    assert client.connect_options["access_token"] == "dapi-token"
    assert client.connect_options["http_path"] == "/sql/1.0/warehouses/abc123"
    assert client.connect_options["catalog"] == "eln_prod"
    assert client.connect_options["schema"] == "reactions"
    assert client.connect_options["session_configuration"] == {"statement_timeout": "45"}


def test_a_full_http_path_is_taken_as_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """The UI shows an id; an admin often has the path. Both are legitimate, so both work."""
    client = _FakeClientModule()
    _bind(monkeypatch, client)
    asyncio.run(_warehouse(warehouse="/sql/1.0/warehouses/deadbeef")._connect())
    assert client.connect_options["http_path"] == "/sql/1.0/warehouses/deadbeef"


@pytest.mark.parametrize("missing", ["account", "password", "warehouse"])
def test_the_three_fields_with_no_default_are_refused_when_absent(missing: str) -> None:
    """Refused where the binding is read, not by an authentication error from a vendor client."""
    with pytest.raises(BindingError, match=missing.replace("account", "account_env")):
        _warehouse(**{missing: ""})


@pytest.mark.parametrize(
    ("field", "value"), [("private_key", "-----BEGIN"), ("role", "CHEMCLAW_READER")]
)
def test_a_field_with_no_analogue_is_refused_rather_than_dropped(field: str, value: str) -> None:
    """Silently ignoring `role` would leave a deployment believing an access restriction applies.

    Both fields are meaningful on Snowflake, so a binding copied from `eln-snowflake` will carry
    them. Dropping them would be the quiet failure; refusing them is the message.
    """
    with pytest.raises(BindingError, match="no use for"):
        _warehouse(**{field: value})


# --- rows: the difference that would otherwise be an empty result -------------------------------


def test_rows_are_keyed_by_column_name_not_by_position(monkeypatch: pytest.MonkeyPatch) -> None:
    """`dict(Row)` keys by position; the whole engine is column-name-driven, so `asDict` it is."""
    client = _FakeClientModule(rows=[{"REACTION_ID": "r-1", "YIELD_PCT": 82.0}])
    _bind(monkeypatch, client)

    @_sync
    async def run() -> None:
        warehouse = _warehouse()
        async with warehouse.cursor() as cursor:
            await cursor.execute("SELECT * FROM V_REACTION WHERE X >= ?", ["2026-01-01"])
            assert await cursor.fetchall() == [{"REACTION_ID": "r-1", "YIELD_PCT": 82.0}]

    run()


def test_parameters_are_bound_positionally_as_a_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sequence is read as `?` markers; a dict would be read as named ones."""
    client = _FakeClientModule()
    _bind(monkeypatch, client)

    @_sync
    async def run() -> None:
        async with _warehouse().cursor() as cursor:
            await cursor.execute("SELECT 1 WHERE a = ?", ("x",))

    run()
    assert client.executed == [("SELECT 1 WHERE a = ?", ["x"])]


def test_the_cursor_is_closed_even_when_the_body_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A leaked cursor holds a server-side operation open on a warehouse somebody pays for."""
    client = _FakeClientModule()
    _bind(monkeypatch, client)
    opened: list[_FakeCursor] = []

    @_sync
    async def run() -> None:
        warehouse = _warehouse()
        with pytest.raises(RuntimeError):
            async with warehouse.cursor() as cursor:
                opened.append(cursor._cursor)
                raise RuntimeError("boom")

    run()
    assert opened[0].closed


def test_the_placeholder_is_the_positional_marker() -> None:
    """Native parameters bind with `?`, which is what `sql.py` writes into every statement."""
    assert _warehouse().placeholder == "?"


# --- error translation: the retryable / non-retryable split -------------------------------------


def test_an_operational_error_is_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dropped connection says nothing about the query, so Temporal should ride it out."""
    client = _FakeClientModule()
    client.raise_on_execute = client.OperationalError("socket closed")
    _bind(monkeypatch, client)

    @_sync
    async def run() -> None:
        async with _warehouse().cursor() as cursor:
            with pytest.raises(ConnectionError):
                await cursor.execute("SELECT 1", [])

    run()


def test_a_rejected_statement_is_not_retryable_and_quotes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The site's table and column names must not reach a chemist's transcript or the model."""
    client = _FakeClientModule()
    client.raise_on_execute = client.Error("[TABLE_OR_VIEW_NOT_FOUND] eln_prod.reactions.V_SECRET")
    _bind(monkeypatch, client)

    @_sync
    async def run() -> None:
        async with _warehouse().cursor() as cursor:
            with pytest.raises(WarehouseQueryError) as caught:
                await cursor.execute("SELECT * FROM V_SECRET", [])
            assert "V_SECRET" not in str(caught.value)
            assert "log" in str(caught.value)

    run()


# --- the dialect --------------------------------------------------------------------------------


def test_the_query_vector_is_bound_as_one_json_scalar() -> None:
    """There is no array parameter type, so a 1536-float list cannot be bound as a list.

    It still has to be a *bound value* rather than statement text — `sql.py`'s whole invariant — so
    it goes as one JSON string that the server parses. `ARRAY<FLOAT>` and not `ARRAY<DOUBLE>`,
    because `vector_cosine_similarity` accepts only the first.
    """
    expression, bound = DatabricksVectorDialect().query_vector("?", [0.1, 0.2, 0.3], 3)
    assert expression == "from_json(?, 'ARRAY<FLOAT>')"
    assert json.loads(bound) == [0.1, 0.2, 0.3]


def test_cosine_is_the_function_and_it_sorts_descending() -> None:
    """A similarity sorts descending; the pair moves together so it is returned together."""
    assert DatabricksVectorDialect().similarity("cosine") == ("vector_cosine_similarity", "DESC")


@pytest.mark.parametrize("metric", ["l2", "inner"])
def test_an_unverified_metric_is_refused_here_rather_than_by_the_server(metric: str) -> None:
    """Guessing a function name would fail on the first query instead of naming the metric."""
    with pytest.raises(WarehouseQueryError, match="cosine"):
        DatabricksVectorDialect().similarity(metric)
