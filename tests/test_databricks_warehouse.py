"""The Databricks SQL driver and its vector dialect, against a fake client module.

The driver is the module in `chemclaw.ingest.eln.warehouse` that knows this vendor exists, and what
is worth pinning here is what only it can be wrong about — the places where a mistake surfaces as an
empty result rather than as an error:

* rows come back as tuple-like `Row` objects, so `dict(row)` keys by *position*, not by column;
* the query vector cannot be bound as a list — there is no array parameter type — so it goes as one
  JSON scalar parsed server-side; and
* its constructor signature *is* the `connection:` block's schema
  (`D-2026-08-26-the-driver-s-signature-is-the-schema`), so what a binding may say is exactly what
  these parameters are called, and a compute target that is neither given nor derivable is refused
  here rather than by the server.
"""

import asyncio
import functools
import json
import logging
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
    def __init__(self, client: "_FakeClientModule") -> None:
        self._client = client
        self.closed = False

    def execute(self, sql: str, params: list[Any]) -> None:
        # Consulted at call time rather than patched in at connect time: a test that kills the
        # session *between* two statements is the whole subject of the eviction tests below, and a
        # failure baked into the connection object could not express it.
        if self._client.raise_on_execute is not None:
            raise self._client.raise_on_execute
        self._client.executed.append((sql, params))

    def fetchall(self) -> list[_Row]:
        return self._client.rows

    def close(self) -> None:
        self.closed = True


class _FakeConnection:
    def __init__(self, client: "_FakeClientModule") -> None:
        self._client = client
        self.cursors: list[_FakeCursor] = []

    def cursor(self) -> _FakeCursor:
        if self._client.raise_on_cursor is not None:
            raise self._client.raise_on_cursor
        made = _FakeCursor(self._client)
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
        # A SQL warehouse session is not permanent — it expires, and the warehouse itself can be
        # stopped or scaled to zero — so what a test needs to express is "this handle is dead now".
        self.raise_on_cursor: Exception | None = None
        self.connects = 0

    def connect(self, **options: Any) -> _FakeConnection:
        self.connect_options = options
        self.connects += 1
        return _FakeConnection(self)


def _bind(monkeypatch: pytest.MonkeyPatch, client: _FakeClientModule) -> None:
    """Point the driver's late import at the fake, as a deployment points it at the SDK."""
    from chemclaw.ingest.eln.warehouse import databricks as module

    monkeypatch.setattr(module, "_client", lambda: client)


def _warehouse(**overrides: Any) -> DatabricksWarehouse:
    options: dict[str, Any] = {
        "server_hostname": "adb-1234.11.azuredatabricks.net",
        "access_token": "dapi-token",
        "warehouse_id": "abc123",
        "catalog": "eln_prod",
        "schema": "reactions",
        "query_timeout_seconds": 45,
    }
    options.update(overrides)
    return DatabricksWarehouse(**options)


# --- the connection block is this driver's own signature ----------------------------------------


def test_the_driver_satisfies_the_warehouse_protocol() -> None:
    """It is a `Warehouse`, dialect and all."""
    assert isinstance(_warehouse(), Warehouse)
    assert isinstance(DatabricksVectorDialect(), VectorDialect)


def test_binding_fields_reach_the_client_under_its_own_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A binding writes Databricks' words, and they arrive as Databricks' words.

    Nothing translates in between, which is the whole of the generality claim: the next database is
    a driver with *its* vocabulary, not a field added to a model shared with this one.
    """
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
    asyncio.run(_warehouse(warehouse_id="", http_path="/sql/1.0/warehouses/deadbeef")._connect())
    assert client.connect_options["http_path"] == "/sql/1.0/warehouses/deadbeef"


@pytest.mark.parametrize("missing", ["server_hostname", "access_token"])
def test_the_fields_with_no_default_are_refused_when_absent(missing: str) -> None:
    """Refused where the binding is read, not by an authentication error from a vendor client."""
    with pytest.raises(BindingError, match=missing):
        _warehouse(**{missing: ""})


@pytest.mark.parametrize("compute", [{"warehouse_id": ""}, {"http_path": "/sql/1.0/warehouses/x"}])
def test_exactly_one_compute_target_is_required(compute: dict[str, str]) -> None:
    """Neither is a warehouse that does not exist; both is a question the reader cannot answer.

    There is no default compute to fall back on, so an absent one has to fail here rather than as a
    connection error minutes into a sync. Naming *both* is refused for the opposite reason: it would
    resolve silently to whichever the driver happened to prefer, and a binding whose meaning depends
    on that is a binding nobody can review.
    """
    with pytest.raises(BindingError, match="exactly one"):
        _warehouse(**compute)


@pytest.mark.parametrize("seconds", [0, -1, 3601])
def test_a_timeout_outside_the_bound_is_refused(seconds: int) -> None:
    """`0` is the worst value the field can take, so it cannot be the one that slips through.

    The bound used to live on the shared connection model, typed `int` with `ge=1, le=3600`. That
    model no longer knows any driver's vocabulary, so the range moved to the driver whose keyword it
    is — and it has to exist somewhere, because a SQL warehouse reads `statement_timeout=0` as *no*
    timeout: the one setting standing between a runaway scan and a shared warehouse's bill, turned
    off by the value a reader would assume means "none allowed".
    """
    with pytest.raises(BindingError, match="between 1 and 3600"):
        _warehouse(query_timeout_seconds=seconds)


def test_a_path_written_into_warehouse_id_is_refused_rather_than_interpolated() -> None:
    """One field used to take either form; two fields do not get to be lenient about it.

    A binding whose author pasted the full path into `warehouse_id` would otherwise build
    `/sql/1.0/warehouses//sql/1.0/warehouses/<id>` and fail at connect time with a message about the
    workspace — a defect in the binding, reported as a defect somewhere else.
    """
    with pytest.raises(BindingError, match="http_path"):
        _warehouse(warehouse_id="/sql/1.0/warehouses/abc123")


def test_a_key_this_driver_does_not_take_is_a_typeerror_naming_it() -> None:
    """The offline check for "the driver's signature is the schema", at the driver's own door.

    A binding copied from another vendor's — `role:`, `private_key_env:`, `account_env:` — used to
    be refused by a hand-written list inside this driver, because the shared connection model
    accepted those keys from anyone. There is no such model now, so the refusal is Python's, it
    names the offending keyword, and it cannot fall out of step with the signature.
    `make datasource-validate` runs exactly this bind offline, before anything connects.
    """
    with pytest.raises(TypeError, match="role"):
        DatabricksWarehouse(  # type: ignore[call-arg]
            server_hostname="h", access_token="t", warehouse_id="w", role="CHEMCLAW_READER"
        )


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
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The site's table and column names must not reach a chemist's transcript or the model.

    A `WarehouseQueryError` raised inside a durable job is marked non-retryable by class name
    (`durable/publish.py`), so its *message* reaches the session — and a driver's own text quotes
    the failing statement. That is a schema disclosure through an error path, and it reads as
    ordinary diagnostics right up until someone asks where the transcript went.

    What replaces it is not "less information" but information an operator can act on: a pointer to
    this pod's log, where the whole thing is. The last two assertions are what make that true rather
    than claimed — nothing is lost, only moved.
    """
    client = _FakeClientModule()
    secret = "[TABLE_OR_VIEW_NOT_FOUND] eln_prod.reactions.V_SECRET"
    client.raise_on_execute = client.Error(secret)
    _bind(monkeypatch, client)

    @_sync
    async def run() -> None:
        async with _warehouse().cursor() as cursor:
            with caplog.at_level(logging.ERROR):
                with pytest.raises(WarehouseQueryError) as caught:
                    await cursor.execute("SELECT * FROM V_SECRET", [])
            assert "V_SECRET" not in str(caught.value)
            assert "log" in str(caught.value)
            assert isinstance(caught.value.__cause__, client.Error)
            assert any(
                secret in record.getMessage() + str(record.exc_info) for record in caplog.records
            ), "the detail has to survive somewhere, or this is redaction by deletion"

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


# --- a dead session is dropped, not kept ---------------------------------------------------------


def test_a_dead_session_is_dropped_so_the_next_call_reconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The connection is opened once and memoized; nothing ever cleared it.

    A Databricks SQL session expires, and the warehouse behind it auto-stops overnight. After that
    every statement failed against the same dead handle — *for the life of the process*, because
    there is no reset path anywhere and the seam has no lifecycle hook to call one from. In the
    retriever that leg then returned `[]` to every question with the pod reporting healthy; in the
    sync it failed every attempt and every retry. Dropping the handle costs one reconnect and turns
    a permanent outage into the transient failure it actually is — which is what `ConnectionError`
    already promises the caller, and what Temporal's retry already knows how to ride out.
    """
    client = _FakeClientModule()
    _bind(monkeypatch, client)
    warehouse = _warehouse()

    @_sync
    async def run() -> None:
        async with warehouse.cursor() as cursor:
            await cursor.execute("SELECT 1", [])
        assert client.connects == 1

        # The session dies. `connection.cursor()` raises from *outside* `execute`'s translation,
        # so this used to escape as the vendor's own error class — neither retryable nor reportable.
        client.raise_on_cursor = client.Error("Invalid SessionHandle")
        with pytest.raises(ConnectionError):
            async with warehouse.cursor():
                pass

        client.raise_on_cursor = None
        async with warehouse.cursor() as cursor:
            await cursor.execute("SELECT 1", [])
        assert client.connects == 2, "the dead handle was kept and every later call reused it"

    run()


def test_a_transient_statement_failure_also_drops_the_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`OperationalError` is "the session is gone", so keeping the session makes the retry futile.

    The translation to `ConnectionError` was already right and already retried by Temporal; what
    made the retry useless is that every attempt reached the same dead handle. The error and the
    eviction are one decision, so they are made in one place.
    """
    client = _FakeClientModule()
    client.raise_on_execute = client.OperationalError("socket closed")
    _bind(monkeypatch, client)
    warehouse = _warehouse()

    @_sync
    async def run() -> None:
        async with warehouse.cursor() as cursor:
            with pytest.raises(ConnectionError):
                await cursor.execute("SELECT 1", [])
        client.raise_on_execute = None
        async with warehouse.cursor() as cursor:
            await cursor.execute("SELECT 1", [])
        assert client.connects == 2

    run()


def test_a_rejected_statement_keeps_the_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """A relation the binding names and the warehouse does not have says nothing about the session.

    The eviction is scoped to the transient arm on purpose: reconnecting on a `WarehouseQueryError`
    would pay a handshake per bad statement and would hide nothing, since the next statement fails
    identically. `_WAREHOUSES` and the adapter's own handle keep the same object either way — what
    is dropped is the session under it, never the configured warehouse.
    """
    client = _FakeClientModule()
    client.raise_on_execute = client.Error("[TABLE_OR_VIEW_NOT_FOUND]")
    _bind(monkeypatch, client)
    warehouse = _warehouse()

    @_sync
    async def run() -> None:
        async with warehouse.cursor() as cursor:
            with pytest.raises(WarehouseQueryError):
                await cursor.execute("SELECT * FROM V_NOPE", [])
        client.raise_on_execute = None
        async with warehouse.cursor() as cursor:
            await cursor.execute("SELECT 1", [])
        assert client.connects == 1, "a rejected statement paid a reconnect it did not need"

    run()
