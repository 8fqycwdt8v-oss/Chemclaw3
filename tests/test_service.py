"""The front-door HTTP surface runs a turn end-to-end with a fake agent.

Exercises the real FastAPI app (health, readiness, sessions, the SSE stream, the static page) with
an injected fake streaming agent, so no live model, MCP subprocess or credentials are needed.
"""

import asyncio
import json
import re
import socket
import threading
import time
from collections.abc import AsyncIterator, Callable, MutableMapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import psycopg
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chemclaw.agent.session import TurnSession
from chemclaw.api.app import LiveSession, _LiveSessions, create_app
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from tests.fakes import asgi_client
from tests.fakes_turn import Piece, ScriptedTurn
from tests.pg import (
    TEST_SCHEMA,
    create_test_schema,
    drop_test_schema,
    migrated_db_or_skip,
)

# A minimal ASGI HTTP scope, for the one test that drives the app below `TestClient` (which
# cannot express "the handler was cancelled and nothing was ever sent").
_ASGI_GET_SCOPE: dict[str, Any] = {
    "type": "http",
    "asgi": {"version": "3.0", "spec_version": "2.1"},
    "http_version": "1.1",
    "method": "GET",
    "scheme": "http",
    "path": "/drained",
    "raw_path": b"/drained",
    "query_string": b"",
    "root_path": "",
    "headers": [(b"host", b"testserver")],
    "client": ("127.0.0.1", 1234),
    "server": ("testserver", 80),
}


class _SpyMcpTool:
    """An async-context-manager stand-in for a connector tool that records connect/teardown.

    `is_connected` is what `open_reachable` reads to report which connectors came up, and MAF reads
    it too — a spy that omitted it would look permanently unreachable.
    """

    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0
        self.is_connected = False
        self.name = "spy"

    async def __aenter__(self) -> "_SpyMcpTool":
        self.entered += 1
        self.is_connected = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.exited += 1


class _FakeAgent(ScriptedTurn):
    """Fake agent: yields two tokens per turn. Connectors are the front door's business, not its."""

    def create_session(self, *, session_id: str) -> TurnSession:
        """The one non-streaming method the front door calls on an agent."""
        return TurnSession(session_id=session_id)

    async def stream(self, message: str) -> AsyncIterator[Piece]:
        yield "hi "
        yield "there"


def _no_connectors(_profile: str | None = None) -> list[Any]:
    """The default connector factory for these tests: none.

    Most tests here are not about connectors, and defaulting to the real set would have every one of
    them dial a connector server that is not running.
    """
    return []


def _app(agent: ScriptedTurn | None = None, **kwargs: Any) -> FastAPI:
    """The app under test, wired to one fake through the seam a turn is driven by.

    `graph_factory` is how the fake gets in (see `tests.fakes_turn.ScriptedTurn`). Connectors
    default to none; a test that wants one passes a spec.
    """
    fake = agent if agent is not None else _FakeAgent()
    kwargs.setdefault("connector_factory", _no_connectors)
    return create_app(graph_factory=fake.graph_factory, **kwargs)


def _client(
    agent: _FakeAgent,
    connector_factory: Callable[[str | None], list[Any]] = _no_connectors,
) -> TestClient:
    """The app under test as a `TestClient`, with no connectors by default."""
    return TestClient(_app(agent, connector_factory=connector_factory))


def test_every_name_the_front_door_re_exports_has_a_reader() -> None:
    """Every name the front door re-exports in `__all__` has a reader.

    `__all__` is a test seam that silences F401, so an unread name could survive unnoticed. A reader
    is a route reading it through this module, `create_app` calling it, a test patching it by dotted
    path, or a test importing it from here.
    """
    import chemclaw.api.app as app_module

    root = Path(__file__).resolve().parents[1]
    api = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (root / "src" / "chemclaw" / "api").rglob("*.py")
        if path.name != "app.py"
    )
    own = (root / "src" / "chemclaw" / "api" / "app.py").read_text(encoding="utf-8")
    suite = "\n".join(path.read_text(encoding="utf-8") for path in root.rglob("tests/*.py"))
    imported: set[str] = set()
    for match in re.finditer(r"from chemclaw\.api\.app import ([^\n(]+|\([^)]*\))", suite):
        imported |= {name.strip("() \n") for name in match.group(1).split(",")}

    unread = [
        name
        for name in app_module.__all__
        if name != "create_app"
        and f"front_door.{name}" not in api
        and not re.search(r"(?<![.\w])" + re.escape(name) + r"\(", own)
        and f"chemclaw.api.app.{name}" not in suite
        and name not in imported
    ]
    assert unread == [], (
        f"{unread} is re-exported by chemclaw.api.app and read by nothing — no route, no call "
        "here, no patch and no test import. Being in `__all__` is what keeps the import lint-clean."
    )


def test_healthz_is_ok() -> None:
    """Liveness needs no agent and returns 200."""
    with _client(_FakeAgent()) as client:
        assert client.get("/healthz").json() == {"status": "ok"}


def _unreachable_database(*_args: Any, **_kwargs: Any) -> Any:
    """A `db.connection` replacement that fails the way an unreachable server does."""
    raise ConnectionError("Postgres unreachable at host=db: connection refused")


def test_readyz_reports_unready_when_the_store_it_needs_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under `session_store="postgres"`, `/readyz` reports unready when Postgres is unreachable."""
    monkeypatch.setattr(settings, "session_store", "postgres")
    monkeypatch.setattr(settings, "service_readiness_cache_seconds", 0.0)
    monkeypatch.setattr("chemclaw.api.routes.ops.db.connection", _unreachable_database)
    with _client(_FakeAgent()) as client:
        res = client.get("/readyz")
    assert res.status_code == 503
    assert res.json()["status"] == "database unreachable"


def test_a_database_outage_drains_the_pod_without_restarting_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database outage drains the pod without failing liveness.

    Restarting every pod for a shared outage destroys capacity and does not help reach the database.
    """
    monkeypatch.setattr(settings, "session_store", "postgres")
    monkeypatch.setattr(settings, "service_readiness_cache_seconds", 0.0)
    monkeypatch.setattr("chemclaw.api.routes.ops.db.connection", _unreachable_database)
    with _client(_FakeAgent()) as client:
        assert client.get("/readyz").status_code == 503
        assert client.get("/healthz").status_code == 200


def test_a_raising_connector_sweep_still_answers_a_readiness_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising connector sweep still answers the readiness probe.

    Connector health is reported, never gating, so a raise in the sweep must not become a 500.
    """
    monkeypatch.setattr(settings, "session_store", "memory")
    monkeypatch.setattr(settings, "service_readiness_cache_seconds", 0.0)

    async def _raises() -> Any:
        raise RuntimeError("the connector registry could not be read")

    monkeypatch.setattr("chemclaw.api.app.probe_connectors", _raises)
    with _client(_FakeAgent()) as client:
        res = client.get("/readyz")
        again = client.get("/readyz")

    assert res.status_code == 200, f"an unmeasurable connector fleet failed the pod: {res.text}"
    assert res.json()["status"] == "ready"
    assert again.status_code == 200, "the failure was cached into a permanently unready pod"


def test_readyz_does_not_probe_a_database_a_memory_deployment_does_not_have(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`session_store="memory"` has no store to answer for, so there is nothing to probe.

    The probe would otherwise report every dev run and every CLI-shaped deployment unready for
    lacking a database none of them use.
    """
    monkeypatch.setattr(settings, "session_store", "memory")
    monkeypatch.setattr(settings, "service_readiness_cache_seconds", 0.0)

    def _must_not_be_called(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a memory-store deployment probed the database")

    monkeypatch.setattr("chemclaw.api.routes.ops.db.connection", _must_not_be_called)
    with _client(_FakeAgent()) as client:
        res = client.get("/readyz")
    assert res.status_code == 200
    assert res.json()["status"] == "ready"


def test_readyz_refuses_a_pod_whose_image_is_ahead_of_the_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/readyz` refuses a pod whose image is ahead of the schema.

    `--no-hooks`, `kubectl set image` or a sync past a failed hook can skip the migration Job.
    Driven against the real `schema_migrations` with one unshippable migration filename.
    """
    asyncio.run(migrated_db_or_skip())
    monkeypatch.setattr(settings, "session_store", "postgres")
    monkeypatch.setattr(settings, "service_readiness_cache_seconds", 0.0)
    monkeypatch.setattr(
        "chemclaw.api.routes.ops.newest_shipped_migration",
        lambda: "999_a_migration_this_database_has_never_seen.sql",
    )
    with _client(_FakeAgent()) as client:
        res = client.get("/readyz")
    assert res.status_code == 503
    assert res.json()["status"] == "schema behind image", (
        "the pod was drained for the wrong reason — an operator running `curl` gets this line "
        "and nothing else"
    )


def test_readyz_stays_ready_when_the_schema_is_ahead_of_the_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/readyz` stays ready when the schema is ahead of the image, as after a rollback.

    That direction is reported by `core/migrate.py`'s `migrate.database_ahead` warning instead.
    """
    asyncio.run(migrated_db_or_skip())
    monkeypatch.setattr(settings, "session_store", "postgres")
    monkeypatch.setattr(settings, "service_readiness_cache_seconds", 0.0)
    # The image ships through the *first* tracked migration; the database holds every later one.
    oldest = sorted(
        path.name
        for path in (Path(settings.sql_migrations_dir).glob("*.sql"))
        if path.name != "000_schema_migrations.sql"
    )[0]
    monkeypatch.setattr("chemclaw.api.routes.ops.newest_shipped_migration", lambda: oldest)
    with _client(_FakeAgent()) as client:
        res = client.get("/readyz")
    assert res.status_code == 200, f"a rolled-back pod refused to serve: {res.text}"
    assert res.json()["status"] == "ready"


def test_a_database_with_no_migration_ledger_takes_the_pod_out_of_the_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database with no `schema_migrations` takes the pod out of the Route.

    The first migration creates the ledger, so its absence means nothing has been applied. Only an
    unreadable ledger (a privilege denial, tested below) stays ready.
    """
    schema = f"{TEST_SCHEMA}_no_ledger"
    base = settings.postgres_dsn.split("?")[0]
    asyncio.run(migrated_db_or_skip())
    asyncio.run(create_test_schema(base, schema))
    try:
        # `search_path` to the empty schema *only*: with `public` behind it the ledger every other
        # test migrated would resolve, and this test would silently assert nothing.
        separator = "&" if "?" in base else "?"
        monkeypatch.setattr(
            settings,
            "session_store_dsn",
            f"{base}{separator}options={quote(f'-c search_path={schema}')}",
        )
        monkeypatch.setattr(settings, "session_store", "postgres")
        monkeypatch.setattr(settings, "service_readiness_cache_seconds", 0.0)
        with _client(_FakeAgent()) as client:
            res = client.get("/readyz")
        assert res.status_code == 503, (
            f"a pod with no schema at all reported itself ready: {res.text}"
        )
        # The same status the behind-schema case answers, deliberately: it is the same fact at its
        # limit and the remedy is identical, and the log line is where the two are distinguished.
        assert res.json()["status"] == "schema behind image", res.text
    finally:
        asyncio.run(drop_test_schema(base, schema))


def test_a_ledger_this_role_may_not_select_does_not_take_the_pod_out_of_the_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ledger this role may not select does not take the pod out of the Route.

    A split session store may use a role that cannot read the ledger; refusing would be an outage
    over another server's grants. Driven through a real `InsufficientPrivilege`: the role has
    `USAGE` on the schema and no `SELECT` on the ledger, set via `-c role=` in the DSN and dropped
    in the `finally`.
    """
    asyncio.run(migrated_db_or_skip())
    schema = f"{TEST_SCHEMA}_norights"
    role = f"{schema}_role"
    base = settings.postgres_dsn.split("?")[0]

    async def _setup() -> None:
        async with await psycopg.AsyncConnection.connect(base, autocommit=True) as conn:
            await conn.execute(f'CREATE SCHEMA "{schema}"')
            await conn.execute(f'CREATE TABLE "{schema}".schema_migrations (filename text)')
            await conn.execute(f'CREATE ROLE "{role}" NOLOGIN')
            # USAGE and no SELECT: the table resolves, reading it does not.
            await conn.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"')

    async def _teardown() -> None:
        async with await psycopg.AsyncConnection.connect(base, autocommit=True) as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await conn.execute(f'REVOKE ALL ON SCHEMA public FROM "{role}"')
            await conn.execute(f'DROP ROLE IF EXISTS "{role}"')

    asyncio.run(_setup())
    try:
        separator = "&" if "?" in base else "?"
        options = quote(f"-c search_path={schema} -c role={role}")
        monkeypatch.setattr(settings, "session_store_dsn", f"{base}{separator}options={options}")
        monkeypatch.setattr(settings, "session_store", "postgres")
        monkeypatch.setattr(settings, "service_readiness_cache_seconds", 0.0)
        with _client(_FakeAgent()) as client:
            res = client.get("/readyz")
        assert res.status_code == 200, (
            "a ledger this role may not select drained the pod, which is the fleet-wide outage "
            f"the trade exists to avoid: {res.text}"
        )
        assert res.json()["status"] == "ready", res.text
    finally:
        asyncio.run(_teardown())


def test_readyz_reuses_its_database_verdict_inside_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unauthenticated route probed every ten seconds must not be a database fan-out on demand.

    The same cache the connector sweep uses, for the same reason: any caller can hit this route at
    will, so an uncached probe is one round trip per request against the store.
    """
    monkeypatch.setattr(settings, "session_store", "postgres")
    monkeypatch.setattr(settings, "service_readiness_cache_seconds", 60.0)
    probes = 0

    @asynccontextmanager
    async def _counting_connection(*_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
        nonlocal probes
        probes += 1

        class _Cursor:
            async def fetchone(self) -> tuple[bool]:
                return (True,)

        class _Conn:
            # Two statements now, and the double has to answer both: `SELECT 1` for reachability
            # and the ledger `EXISTS` for whether the schema carries this image. A double that
            # only accepted the first would make this test pass by not exercising the probe.
            async def execute(
                self, _sql: str, _params: tuple[object, ...] | None = None
            ) -> _Cursor:
                return _Cursor()

        yield _Conn()

    monkeypatch.setattr("chemclaw.api.routes.ops.db.connection", _counting_connection)
    with _client(_FakeAgent()) as client:
        for _ in range(5):
            assert client.get("/readyz").status_code == 200
    assert probes == 1


def test_readyz_bounds_the_whole_database_leg_not_just_the_statement_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`service_readiness_db_timeout_seconds` bounds acquiring a connection, not only using one.

    `statement_timeout` applies only after a connection exists, so the whole database leg needs an
    outer bound; the chart derives the probe timeout from this setting. The stand-in hangs well past
    the readiness budget, so a regression is a slow still-passing wait rather than a real hang.
    """
    budget = 0.2
    hang_seconds = 2.0
    monkeypatch.setattr(settings, "session_store", "postgres")
    monkeypatch.setattr(settings, "service_readiness_cache_seconds", 0.0)
    monkeypatch.setattr(settings, "service_readiness_db_timeout_seconds", budget)
    # Widened well past `hang_seconds` so this test cannot pass by accident of those settings'
    # defaults being small — an unbounded `_probe_database` would hang for `hang_seconds`, not for
    # either of these.
    monkeypatch.setattr(settings, "pg_connect_timeout_seconds", 30)
    monkeypatch.setattr(settings, "pg_pool_timeout_seconds", 30.0)

    class _StubConn:
        """A connection that would answer fine, if the probe ever got to ask it anything."""

        async def execute(self, _sql: str) -> None:
            return None

    @asynccontextmanager
    async def _hanging_connection(*_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
        # A connect or checkout that never arrives within the readiness budget. It yields a working
        # connection when it wakes, so an unbounded probe reports `200 ready` late rather than
        # failing for an unrelated reason.
        await asyncio.sleep(hang_seconds)
        yield _StubConn()

    monkeypatch.setattr("chemclaw.api.routes.ops.db.connection", _hanging_connection)
    with _client(_FakeAgent()) as client:
        started = time.monotonic()
        res = client.get("/readyz")
        elapsed = time.monotonic() - started

    assert res.status_code == 503
    assert res.json()["status"] == "database unreachable"
    assert elapsed < hang_seconds, (
        f"/readyz took {elapsed:.3f}s against a {budget}s database budget: acquiring a connection "
        "is unbounded and the probe waited out pg_connect_timeout_seconds/pg_pool_timeout_seconds "
        "instead"
    )


async def test_concurrent_readiness_probes_cost_one_connector_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fifty simultaneous `/readyz` probes cost one connector sweep, not fifty.

    `/readyz` is unauthenticated, so a caller could otherwise amplify one connection into many
    outbound ones.
    """
    monkeypatch.setattr(settings, "session_store", "memory")
    monkeypatch.setattr(settings, "service_readiness_cache_seconds", 60.0)
    sweeps = 0

    async def _counting_probe() -> list[Any]:
        nonlocal sweeps
        sweeps += 1
        # A suspension point, because the real sweep is an HTTP fan-out: without one there is no
        # window for a second caller to miss the cache, and the test would pass against the defect.
        await asyncio.sleep(0.05)
        return []

    monkeypatch.setattr("chemclaw.api.app.probe_connectors", _counting_probe)

    app = _app()
    async with asgi_client(app) as client:
        responses = await asyncio.gather(*(client.get("/readyz") for _ in range(50)))
    assert {res.status_code for res in responses} == {200}

    assert sweeps == 1, f"50 concurrent probes triggered {sweeps} connector sweeps"


async def test_concurrent_readiness_probes_cost_one_database_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent readiness probes cost one database checkout from the shared pool.

    Otherwise they starve authenticated requests waiting on the same pool.
    """
    monkeypatch.setattr(settings, "session_store", "postgres")
    monkeypatch.setattr(settings, "service_readiness_cache_seconds", 60.0)
    monkeypatch.setattr("chemclaw.api.app.probe_connectors", _no_probe)
    checkouts = 0

    @asynccontextmanager
    async def _counting_connection(*_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
        nonlocal checkouts
        checkouts += 1
        await asyncio.sleep(0.05)

        class _Cursor:
            async def fetchone(self) -> tuple[bool]:
                return (True,)

        class _Conn:
            # Two statements now, and the double has to answer both: `SELECT 1` for reachability
            # and the ledger `EXISTS` for whether the schema carries this image. A double that
            # only accepted the first would make this test pass by not exercising the probe.
            async def execute(
                self, _sql: str, _params: tuple[object, ...] | None = None
            ) -> _Cursor:
                return _Cursor()

        yield _Conn()

    monkeypatch.setattr("chemclaw.api.routes.ops.db.connection", _counting_connection)

    app = _app()
    async with asgi_client(app) as client:
        responses = await asyncio.gather(*(client.get("/readyz") for _ in range(50)))
    assert {res.status_code for res in responses} == {200}

    assert checkouts == 1, f"50 concurrent probes requested {checkouts} pooled connections"


async def _no_probe() -> list[Any]:
    """A connector sweep that answers instantly, for the tests that are about the other probe."""
    return []


def test_static_chat_page_is_served() -> None:
    """The browser chat surface is served at the root, with security headers, and still loads."""
    with _client(_FakeAgent()) as client:
        res = client.get("/")
        assert res.status_code == 200
        assert "Chemclaw" in res.text  # SEC-5: the CSP does not break the inline-styled UI
        # SEC-5: the browser security headers are present on the response.
        assert res.headers["X-Content-Type-Options"] == "nosniff"
        assert res.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in res.headers["Content-Security-Policy"]
        assert "Strict-Transport-Security" in res.headers


def test_security_headers_reach_a_streaming_sse_response() -> None:
    """The streamed SSE response carries the same security headers as a static page.

    Its headers are sent before its body exists, which distinguishes middleware implementations.
    """
    agent = _FakeAgent()
    with _client(agent) as client:
        session_id = client.post("/sessions").json()["session_id"]
        with client.stream(
            "POST", f"/sessions/{session_id}/messages", json={"message": "hello"}
        ) as res:
            assert res.status_code == 200
            assert res.headers["X-Frame-Options"] == "DENY"
            assert "frame-ancestors 'none'" in res.headers["Content-Security-Policy"]


async def test_a_cancelled_request_closes_the_connection_instead_of_500ing() -> None:
    """A handler cancelled before it responds closes the connection instead of returning a 500.

    Driven at the raw ASGI level: cancellation must propagate out of the app rather than be
    converted into a response, as `BaseHTTPMiddleware` would do.
    """
    app = _app()

    @app.get("/drained")
    async def drained() -> dict[str, str]:
        """Stand in for a handler the server cancels mid-request."""
        raise asyncio.CancelledError

    # The static UI is mounted at "/" and would otherwise swallow the path, since Starlette
    # matches routes in registration order.
    app.router.routes.insert(0, app.router.routes.pop())

    sent: list[MutableMapping[str, Any]] = []

    async def _send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    async def _receive() -> MutableMapping[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    with pytest.raises(asyncio.CancelledError):
        await app(_ASGI_GET_SCOPE, _receive, _send)

    assert sent == [], f"a cancelled handler still emitted a response: {sent}"


def test_a_launched_job_reaches_the_browser_as_an_sse_event() -> None:
    """A job announced by a tool is serialized into the turn's SSE stream, before the answer.

    The end-to-end half of D-042: without it the chemist saw nothing between their message and
    the answer, with the first sign of the job arriving only as the completion push-back.
    """
    from chemclaw.core.turn_signals import record_job_started

    class _JobAgent(_FakeAgent):
        """A fake whose turn announces a durable job before it says anything."""

        async def stream(self, message: str) -> AsyncIterator[Piece]:
            record_job_started("qm-sse", "report")
            yield "submitted"

    with _client(_JobAgent()) as client:
        session_id = client.post("/sessions").json()["session_id"]
        events = []
        with client.stream("POST", f"/sessions/{session_id}/messages", json={"message": "go"}) as r:
            for line in r.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[len("data:") :].strip()))

    # Order is chronological: the fake announces the job before its text, and the turn-signal sink
    # drains at the top of each update. `capability_degraded` is dropped first, since no Temporal
    # broker runs here and every turn truthfully announces that.
    streamed = [e for e in events if e["type"] != "capability_degraded"]
    assert [e["type"] for e in streamed] == ["job_started", "token", "answer"]
    assert streamed[0]["job_id"] == "qm-sse"


def _stream_events(  # type: ignore[no-untyped-def]
    client, session_id: str, message: str = "hi"
) -> list[dict[str, Any]]:
    """POST a turn and collect its SSE payloads, draining the stream so the generator finishes."""
    events: list[dict[str, Any]] = []
    with client.stream(
        "POST", f"/sessions/{session_id}/messages", json={"message": message}
    ) as res:
        assert res.status_code == 200
        for line in res.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:") :].strip()))
    return events


def test_a_waiting_turn_says_so_and_is_shed_on_the_stream(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """At capacity the turn reports `queued`, then ends with an error event — not an HTTP 503.

    The stream opens before the admission wait, so a busy front door is distinguishable from a dead
    one.
    """
    import asyncio

    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "service_turn_admission_timeout_seconds", 0.05)
    app = _app()
    # Zero permits → the turn can only wait and then be shed (deterministic, no concurrency).
    app.state.turn_semaphore = asyncio.Semaphore(0)
    with TestClient(app) as client:
        session_id = client.post("/sessions").json()["session_id"]
        events = _stream_events(client, session_id)

    assert [e["type"] for e in events] == ["queued", "error"]
    assert events[-1]["message"] == "server at capacity; retry shortly"
    # And the shed turn left nothing behind: the session takes another turn immediately.
    assert session_id not in app.state.active_turns


def test_an_uncontended_turn_emits_no_queued_event() -> None:
    """`queued` is a report of an actual wait, so the common case must not carry one.

    An event on every turn would be noise a surface has to render and then immediately un-render,
    and it would tell an operator the front door is contended when it is idle.
    """
    with _client(_FakeAgent()) as client:
        session_id = client.post("/sessions").json()["session_id"]
        events = _stream_events(client, session_id)

    assert "queued" not in [e["type"] for e in events]
    assert events[-1]["type"] == "answer"


def test_a_queued_turn_runs_once_a_permit_frees(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A queued turn runs once a permit frees."""
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "service_turn_admission_timeout_seconds", 30.0)
    queued_before = METRICS.value("chemclaw_turns_queued_total")

    async def _run() -> None:
        app = _app()
        semaphore = asyncio.Semaphore(0)  # nothing free yet
        app.state.turn_semaphore = semaphore

        async def _free_a_permit_once_the_turn_waits() -> None:
            # Driven off the counter rather than a sleep, so the release lands *after* the turn
            # has parked — the moment this test is about. `httpx.ASGITransport` buffers the whole
            # response, so reacting to the `queued` line on the wire is not available here.
            async with asyncio.timeout(5):
                while METRICS.value("chemclaw_turns_queued_total") == queued_before:
                    await asyncio.sleep(0.01)
            semaphore.release()

        async with asgi_client(app) as client:
            session_id = (await client.post("/sessions")).json()["session_id"]
            releaser = asyncio.create_task(_free_a_permit_once_the_turn_waits())
            res = await client.post(f"/sessions/{session_id}/messages", json={"message": "hi"})
            await releaser
            assert res.status_code == 200  # the stream opened *before* a permit existed
            events = [
                json.loads(line[len("data:") :].strip())
                for line in res.text.splitlines()
                if line.startswith("data:")
            ]
        types = [e["type"] for e in events]
        assert types[0] == "queued"  # the wait is reported, and reported first
        assert types[-1] == "answer"  # ...and the turn then runs to a real answer
        assert "error" not in types
        # The permit taken after the wait is handed back, not leaked.
        assert semaphore._value == 1

    asyncio.run(_run())


def test_permit_is_released_after_each_turn(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A finished turn returns its permit, so more turns than permits still all succeed.

    With one permit, three sequential turns pass only if each releases.
    """
    import asyncio

    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "service_turn_admission_timeout_seconds", 1.0)
    app = _app()
    app.state.turn_semaphore = asyncio.Semaphore(1)
    with TestClient(app) as client:
        session_id = client.post("/sessions").json()["session_id"]
        for _ in range(3):
            with client.stream(
                "POST", f"/sessions/{session_id}/messages", json={"message": "hi"}
            ) as res:
                assert res.status_code == 200
                for _line in res.iter_lines():  # drain the stream so the generator's finally runs
                    pass
    assert app.state.turn_semaphore._value == 1  # the permit is back, not leaked


def test_message_to_unknown_session_is_404() -> None:
    """Posting to a session that was never created is a clean 404, not a 500."""
    with _client(_FakeAgent()) as client:
        res = client.post("/sessions/nope/messages", json={"message": "hi"})
        assert res.status_code == 404


def test_oversized_message_is_rejected(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A message past the configured cap is a clean 422, not an unbounded read (SEC-4)."""
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "service_max_message_chars", 10)
    with _client(_FakeAgent()) as client:
        session_id = client.post("/sessions").json()["session_id"]
        res = client.post(f"/sessions/{session_id}/messages", json={"message": "x" * 11})
        assert res.status_code == 422


def test_a_session_is_owner_scoped() -> None:
    """A user cannot post into or stream a session another user created (review finding)."""
    from chemclaw.api.auth import Principal, require_principal

    app = _app()
    alice = Principal(oid="alice", upn="alice@corp", roles=frozenset())
    bob = Principal(oid="bob", upn="bob@corp", roles=frozenset())
    client = TestClient(app)

    app.dependency_overrides[require_principal] = lambda: alice
    session_id = client.post("/sessions").json()["session_id"]

    app.dependency_overrides[require_principal] = lambda: bob
    assert client.post(f"/sessions/{session_id}/messages", json={"message": "x"}).status_code == 404
    assert client.get(f"/sessions/{session_id}/events").status_code == 404  # not even existence

    app.dependency_overrides[require_principal] = lambda: alice
    ok = client.post(f"/sessions/{session_id}/messages", json={"message": "x"})
    assert ok.status_code == 200  # the owner still gets in


def test_null_owner_session_is_unreachable_once_entra_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A NULL-owner session is unreachable once Entra is required.

    Rows written in dev mode survive a flip to enforcement and must not become everyone's.
    """
    from chemclaw.api.auth import Principal, require_principal
    from chemclaw.core.config import settings

    app = _app()
    stranger = Principal(oid="stranger", upn="stranger@corp", roles=frozenset())
    app.dependency_overrides[require_principal] = lambda: stranger
    client = TestClient(app)

    session_id = "null-owner-session"
    app.state.live_sessions.add(
        session_id, _FakeAgent().create_session(session_id=session_id), None, None
    )

    # Dev-mode default (unchanged): an owner-less session degrades open, as documented.
    res = client.post(f"/sessions/{session_id}/messages", json={"message": "hi"})
    assert res.status_code == 200

    monkeypatch.setattr(settings, "entra_required", True)
    res = client.post(f"/sessions/{session_id}/messages", json={"message": "hi"})
    assert res.status_code == 404
    assert client.get(f"/sessions/{session_id}/events").status_code == 404


def test_job_pushback_streams_completed_events(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The events endpoint streams a finished job's push-back to the session (F3-T3)."""
    import chemclaw.api.app as app_module
    from chemclaw.agent.session_events import SessionEvent

    async def _fake_stream(session_id: str, **_: object) -> object:
        yield SessionEvent(
            session_id=session_id,
            kind="job_completed",
            payload={"job_id": "qm-1", "converged": True},
        )

    monkeypatch.setattr(app_module, "stream_new_events", _fake_stream)

    with _client(_FakeAgent()) as client:
        session_id = client.post("/sessions").json()["session_id"]
        events = []
        with client.stream("GET", f"/sessions/{session_id}/events") as res:
            assert res.status_code == 200
            for line in res.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[len("data:") :].strip()))

    assert events == [
        {
            "type": "job_completed",
            "job_id": "qm-1",
            "summary": {"job_id": "qm-1", "converged": True},
        }
    ]


def test_pushback_streams_a_question_waiting_on_a_person(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Push-back streams an `awaiting-answer` row instead of letting it age out unclaimed.

    The claim is kind-scoped, so including this kind steals from no other consumer.
    """
    import chemclaw.api.app as app_module
    from chemclaw.agent.session_events import SessionEvent
    from chemclaw.durable.awaiting import AWAITING_KIND

    claimed: list[tuple[str, ...]] = []

    async def _fake_stream(session_id: str, **kwargs: tuple[str, ...]) -> object:
        claimed.append(kwargs.get("kinds") or ())
        yield SessionEvent(
            session_id=session_id,
            kind=AWAITING_KIND,
            payload={
                "request_id": "await-9f2c",
                "kind": "measurement",
                "subject": "Isolated yield for arm B3",
                "asked_of": "process-chemist",
                "due_at": "2026-09-06T00:00:00Z",
                "reminders": 0,
                "state": "waiting",
            },
        )

    monkeypatch.setattr(app_module, "stream_new_events", _fake_stream)

    with _client(_FakeAgent()) as client:
        session_id = client.post("/sessions").json()["session_id"]
        events = []
        with client.stream("GET", f"/sessions/{session_id}/events") as res:
            assert res.status_code == 200
            for line in res.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[len("data:") :].strip()))

    assert events == [
        {
            "type": "awaiting_answer",
            "request_id": "await-9f2c",
            "state": "waiting",
            "subject": "Isolated yield for arm B3",
            "kind": "measurement",
            "asked_of": "process-chemist",
            "due_at": "2026-09-06T00:00:00Z",
            "reminders": 0,
        }
    ]
    # The kind really is claimed, rather than the payload merely being mapped: a route that mapped
    # it without asking for it would pass the assertion above against this fake and deliver nothing
    # against a database.
    assert AWAITING_KIND in claimed[0]
    # And the two kinds that were already claimed still are — widening must not narrow.
    assert {"job_completed", "job_failed"} <= set(claimed[0])


def test_pushback_streams_an_expired_question(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The expiry push, which lacks `kind`, `asked_of` and `due_at`, is not lost to validation.

    The row is claimed before this route reads it, so there is no second delivery.
    """
    import chemclaw.api.app as app_module
    from chemclaw.agent.session_events import SessionEvent
    from chemclaw.durable.awaiting import AWAITING_KIND

    async def _fake_stream(session_id: str, **_: object) -> object:
        yield SessionEvent(
            session_id=session_id,
            kind=AWAITING_KIND,
            payload={
                "request_id": "await-9f2c",
                "subject": "Isolated yield for arm B3",
                "state": "expired",
                "reminders": 3,
            },
        )

    monkeypatch.setattr(app_module, "stream_new_events", _fake_stream)

    with _client(_FakeAgent()) as client:
        session_id = client.post("/sessions").json()["session_id"]
        events = []
        with client.stream("GET", f"/sessions/{session_id}/events") as res:
            for line in res.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[len("data:") :].strip()))

    assert events == [
        {
            "type": "awaiting_answer",
            "request_id": "await-9f2c",
            "state": "expired",
            "subject": "Isolated yield for arm B3",
            "kind": "",
            "asked_of": "",
            "due_at": "",
            "reminders": 3,
        }
    ]


def test_pushback_survives_an_awaiting_payload_from_another_build(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A payload this build did not write costs one blank field, never the notification.

    The row is already claimed, and a raise would kill the generator and every queued event.
    `reminders="many"` is used because lax pydantic coerces `"2"`, which would not exercise the
    mapper's guard.
    """
    import chemclaw.api.app as app_module
    from chemclaw.agent.session_events import SessionEvent
    from chemclaw.durable.awaiting import AWAITING_KIND

    async def _fake_stream(session_id: str, **_: object) -> object:
        yield SessionEvent(
            session_id=session_id,
            kind=AWAITING_KIND,
            # `reminders` as a value no `int()` accepts, and no `state` at all.
            payload={"request_id": "await-9f2c", "reminders": "many"},
        )

    monkeypatch.setattr(app_module, "stream_new_events", _fake_stream)

    with _client(_FakeAgent()) as client:
        session_id = client.post("/sessions").json()["session_id"]
        events = []
        with client.stream("GET", f"/sessions/{session_id}/events") as res:
            for line in res.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[len("data:") :].strip()))

    assert len(events) == 1
    assert events[0]["request_id"] == "await-9f2c"
    assert events[0]["reminders"] == 0
    assert events[0]["state"] == "waiting"


def test_pushback_collapses_a_replayed_backlog_of_reminders(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A backlog of daily reminders reaches the browser as one open notice and one expiry.

    Unconsumed rows are not pruned, so the first connect claims the whole history. The stream
    reports each request's state, and the expiry, the transition that matters, is always sent last.
    """
    import chemclaw.api.app as app_module
    from chemclaw.agent.session_events import SessionEvent
    from chemclaw.durable.awaiting import AWAITING_KIND

    async def _fake_stream(session_id: str, **_: object) -> object:
        for reminder in range(15):
            yield SessionEvent(
                session_id=session_id,
                kind=AWAITING_KIND,
                payload={
                    "request_id": "await-9f2c",
                    "kind": "measurement",
                    "subject": "Isolated yield for arm B3",
                    "asked_of": "process-chemist",
                    "due_at": "2026-09-06T00:00:00Z",
                    "reminders": reminder,
                    "state": "waiting",
                },
            )
        yield SessionEvent(
            session_id=session_id,
            kind=AWAITING_KIND,
            payload={
                "request_id": "await-9f2c",
                "subject": "Isolated yield for arm B3",
                "state": "expired",
                "reminders": 15,
            },
        )

    monkeypatch.setattr(app_module, "stream_new_events", _fake_stream)

    with _client(_FakeAgent()) as client:
        session_id = client.post("/sessions").json()["session_id"]
        events = []
        with client.stream("GET", f"/sessions/{session_id}/events") as res:
            for line in res.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[len("data:") :].strip()))

    assert [e["state"] for e in events] == ["waiting", "expired"]
    # A different request is a different subject and is never collapsed into another's.
    assert {e["request_id"] for e in events} == {"await-9f2c"}


def test_pushback_reports_the_newest_state_of_a_collapsed_backlog(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The surviving frame of a collapsed run is the newest one, not the oldest.

    Drives the real tailer with only its claim faked, reading `collapse` from `kwargs`, so a route
    that stopped passing it fails.
    """
    import chemclaw.api.app as app_module
    from chemclaw.agent import session_events as session_events_module
    from chemclaw.agent.session_events import SessionEvent
    from chemclaw.durable.awaiting import AWAITING_KIND

    backlog = [
        SessionEvent(
            session_id="s",
            kind=AWAITING_KIND,
            payload={
                "request_id": "await-9f2c",
                "kind": "measurement",
                "subject": "Isolated yield for arm B3",
                "asked_of": "process-chemist",
                "due_at": "2026-09-06T00:00:00Z",
                "reminders": reminder,
                "state": "waiting",
            },
        )
        for reminder in range(15)
    ] + [
        SessionEvent(
            session_id="s",
            kind=AWAITING_KIND,
            payload={
                "request_id": "await-9f2c",
                "subject": "Isolated yield for arm B3",
                "state": "expired",
                "reminders": 14,
            },
        )
    ]

    async def _claim(_session_id: str) -> list[SessionEvent]:
        return backlog

    handed: list[object] = []

    async def _one_poll(session_id: str, **kwargs: object) -> object:
        """One claim through the production tailer, bounded so the SSE stream ends."""
        handed.append(kwargs.get("collapse"))
        async for event in session_events_module.stream_new_events(
            session_id,
            max_polls=1,
            claim=_claim,
            collapse=kwargs.get("collapse"),  # type: ignore[arg-type]
        ):
            yield event

    monkeypatch.setattr(app_module, "stream_new_events", _one_poll)

    with _client(_FakeAgent()) as client:
        session_id = client.post("/sessions").json()["session_id"]
        events = []
        with client.stream("GET", f"/sessions/{session_id}/events") as res:
            for line in res.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[len("data:") :].strip()))

    assert handed and handed[0] is not None, (
        "the route no longer hands the tailer a batch reduction, so the collapse it does perform "
        "can only ever keep the oldest frame of a run"
    )
    assert [e["state"] for e in events] == ["waiting", "expired"], events
    assert [e["reminders"] for e in events] == [14, 14], (
        "the collapse kept the oldest frame of the run, so the client was told the question had "
        f"been chased {events[0]['reminders']} times when it had been chased 14"
    )


def test_pushback_does_not_collapse_two_different_requests(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The collapse is per request, so two open questions are two notices."""
    import chemclaw.api.app as app_module
    from chemclaw.agent.session_events import SessionEvent
    from chemclaw.durable.awaiting import AWAITING_KIND

    async def _fake_stream(session_id: str, **_: object) -> object:
        for request_id in ("await-aaaa", "await-bbbb", "await-aaaa"):
            yield SessionEvent(
                session_id=session_id,
                kind=AWAITING_KIND,
                payload={"request_id": request_id, "state": "waiting"},
            )

    monkeypatch.setattr(app_module, "stream_new_events", _fake_stream)

    with _client(_FakeAgent()) as client:
        session_id = client.post("/sessions").json()["session_id"]
        events = []
        with client.stream("GET", f"/sessions/{session_id}/events") as res:
            for line in res.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[len("data:") :].strip()))

    assert [e["request_id"] for e in events] == ["await-aaaa", "await-bbbb"]


def test_pushback_for_unknown_session_is_404() -> None:
    """Subscribing to push-back for a session that never existed is a clean 404."""
    with _client(_FakeAgent()) as client:
        assert client.get("/sessions/nope/events").status_code == 404


class _FakeOwnerStore:
    """In-memory stand-in for the durable session-ownership registry (no database)."""

    def __init__(self) -> None:
        self.owners: dict[str, str | None] = {}
        # Stored beside the owner, as the real table stores it, so a rehydration test can see the
        # profile survive an eviction rather than a fake supplying what the column would (REV-14).
        self.profiles: dict[str, str | None] = {}
        self.created: dict[str, datetime] = {}
        # The two facts a conversation list is built from. `titles` is written on a session's first
        # turn; a session with no `updated` entry is one nobody has spoken in and is not listed.
        self.titles: dict[str, str] = {}
        self.updated: dict[str, datetime] = {}

    async def record(self, session_id: str, owner: str | None, profile: str | None = None) -> None:
        if session_id not in self.owners:
            self.owners[session_id] = owner
            self.profiles[session_id] = profile
            # Distinct, increasing timestamps so "newest first" is actually observable.
            self.created[session_id] = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(
                minutes=len(self.created)
            )

    async def lookup(self, session_id: str) -> tuple[bool, str | None, str | None]:
        if session_id in self.owners:
            return (True, self.owners[session_id], self.profiles[session_id])
        return (False, None, None)

    async def set_title_if_absent(self, session_id: str, title: str) -> None:
        self.titles.setdefault(session_id, title)
        # Stands in for the message row the real turn would have written: a turn happened, so this
        # session now has a last activity and starts being listed.
        self.updated[session_id] = datetime(2026, 6, 1, tzinfo=UTC) + timedelta(
            minutes=len(self.updated)
        )

    async def list_for_owner(
        self, owner: str | None
    ) -> list[tuple[str, datetime, datetime, str | None, str | None]]:
        rows = [
            (
                sid,
                self.created[sid],
                self.updated[sid],
                self.titles.get(sid),
                self.profiles[sid],
            )
            for sid, own in self.owners.items()
            if own == owner and sid in self.updated
        ]
        return sorted(rows, key=lambda row: row[2], reverse=True)


class _SharedTurnClaims:
    """The `session_turns` row, in memory — one instance stands in for the shared database.

    Two apps built over one of these are the faithful model of two uvicorn workers or two pods:
    separate processes, separate `active_turns` sets, one durable claim between them.
    """

    def __init__(self) -> None:
        self.holders: dict[str, str] = {}

    async def claim(
        self, session_id: str, holder: str, lease_seconds: float, *, actor: str | None = None
    ) -> bool:
        if session_id in self.holders:
            return False
        self.holders[session_id] = holder
        return True

    async def refresh(self, session_id: str, holder: str, lease_seconds: float) -> bool:
        # Nothing elapses inside a test, so a refresh has nothing to do — but it still reports
        # whether the claim is ours, which is what the heartbeat now acts on.
        return self.holders.get(session_id) == holder

    async def release(self, session_id: str, holder: str) -> None:
        if self.holders.get(session_id) == holder:
            del self.holders[session_id]


async def test_a_turn_running_on_another_worker_is_waited_for_not_run_beside() -> None:
    """A turn already claimed by another process is waited for here, not admitted a second time.

    The front door runs several replicas, so the durable claim row is the only trace of the
    sibling's turn; seeding it stands in for the other worker. The message waits in the session's
    line and runs only once that claim is released, without disturbing it.
    """
    claims = _SharedTurnClaims()
    app = _app(owner_store=_FakeOwnerStore(), turn_claims=claims)
    async with asgi_client(app) as client:
        session_id = (await client.post("/sessions")).json()["session_id"]
        claims.holders[session_id] = "another-worker"  # a turn is in flight over there
        waiting = asyncio.create_task(
            client.post(f"/sessions/{session_id}/messages", json={"message": "second"})
        )
        await asyncio.sleep(0.3)
        assert not waiting.done(), "a second turn ran beside the other worker's"
        assert claims.holders == {session_id: "another-worker"}  # waiting did not steal the slot
        del claims.holders[session_id]  # the other worker's turn ends
        answered = await asyncio.wait_for(waiting, timeout=10)

    assert answered.status_code == 200
    assert '"type":"queued"' in answered.text and '"type":"answer"' in answered.text
    assert claims.holders == {}, "the turn that waited did not give its claim back"


def test_a_finished_turn_hands_its_cross_process_claim_back() -> None:
    """The slot is taken for a turn's streamed run and given back when it ends, not leaked.

    A claim that outlived its turn would 409 the session for a whole lease every time — the
    durable version of the bug that once bricked a session's turns until the pod restarted.
    """
    claims = _SharedTurnClaims()
    app = _app(owner_store=_FakeOwnerStore(), turn_claims=claims)
    with TestClient(app) as client:
        session_id = client.post("/sessions").json()["session_id"]
        for _ in range(2):  # a second turn proves the first genuinely released
            with client.stream(
                "POST", f"/sessions/{session_id}/messages", json={"message": "hello"}
            ) as res:
                assert res.status_code == 200
                for _line in res.iter_lines():
                    pass
            assert claims.holders == {}


class _UnreachableOwnerStore(_FakeOwnerStore):
    """An ownership registry whose every call fails the way a starved pool checkout does.

    `chemclaw.core.db.connection` maps `PoolTimeout` and an unreachable server to `ConnectionError`.
    """

    async def record(self, session_id: str, owner: str | None, profile: str | None = None) -> None:
        raise ConnectionError("Postgres unreachable at host=db: couldn't get a connection")


def test_a_failed_postgres_checkout_sheds_with_503_and_is_counted() -> None:
    """Creating a session when no connection can be got is a counted, retryable 503, never a 500."""
    before = METRICS.value("chemclaw_db_unavailable_total")
    app = _app(owner_store=_UnreachableOwnerStore())
    with TestClient(app) as client:
        res = client.post("/sessions")
    assert res.status_code == 503
    assert res.json()["detail"] == "server at capacity; retry shortly"
    assert METRICS.value("chemclaw_db_unavailable_total") == before + 1


def _turn(client: TestClient, session_id: str, message: str) -> None:
    """Run one turn to completion, so the session has an activity and a name."""
    with client.stream("POST", f"/sessions/{session_id}/messages", json={"message": message}) as r:
        assert r.status_code == 200
        for _ in r.iter_lines():
            pass


def test_session_list_is_owner_scoped_and_most_recently_used_first() -> None:
    """`GET /sessions` returns the caller's own sessions, most recently used first — nobody else's.

    A session id is a capability, so listing another owner's would hand it out. Ordered by last
    activity, so `first` returns to the top when used again.
    """
    from chemclaw.api.auth import Principal, require_principal

    alice = Principal(oid="alice", upn="a@corp", roles=frozenset())
    bob = Principal(oid="bob", upn="b@corp", roles=frozenset())
    app = _app(owner_store=_FakeOwnerStore())
    client = TestClient(app)

    app.dependency_overrides[require_principal] = lambda: alice
    first = client.post("/sessions").json()["session_id"]
    second = client.post("/sessions").json()["session_id"]
    _turn(client, first, "What is the pKa of acetic acid?")
    _turn(client, second, "Which ligand for the Suzuki?")
    app.dependency_overrides[require_principal] = lambda: bob
    bobs = client.post("/sessions").json()["session_id"]
    _turn(client, bobs, "Bob's question.")

    app.dependency_overrides[require_principal] = lambda: alice
    listed = [row["session_id"] for row in client.get("/sessions").json()]
    assert listed == [second, first]
    assert bobs not in listed

    # Returning to the older conversation moves it to the top. Under the previous ordering — the
    # row's creation date — it would have stayed second forever, which is the whole complaint.
    _turn(client, first, "And in DMSO?")
    assert [row["session_id"] for row in client.get("/sessions").json()] == [first, second]

    app.dependency_overrides[require_principal] = lambda: bob
    assert [row["session_id"] for row in client.get("/sessions").json()] == [bobs]


def test_session_list_names_each_conversation_after_its_opening_question() -> None:
    """A conversation list needs names, and the service is the only thing that can supply them.

    Without this the response was ids and dates, so every client had to invent the same
    placeholder and every restored conversation looked identical until it was opened.
    """
    from chemclaw.api.auth import Principal, require_principal

    app = _app(owner_store=_FakeOwnerStore())
    app.dependency_overrides[require_principal] = lambda: Principal(
        oid="alice", upn="a@corp", roles=frozenset()
    )
    client = TestClient(app)
    session_id = client.post("/sessions").json()["session_id"]

    _turn(client, session_id, "  What is   the pKa\nof acetic acid? ")
    # Collapsed, not summarised, and not re-derived from the stored serialization.
    assert client.get("/sessions").json()[0]["title"] == "What is the pKa of acetic acid?"

    # A conversation is named by how it started, so a later turn must not rename it — otherwise the
    # sidebar entry a chemist navigates by changes under them on every message.
    _turn(client, session_id, "And in DMSO?")
    assert client.get("/sessions").json()[0]["title"] == "What is the pKa of acetic acid?"


def test_session_list_omits_a_session_nobody_ever_spoke_in() -> None:
    """A created-but-unused session is not listed as a conversation.

    The UI mints a session on the first keystroke, so abandoned drafts leave ownership rows.
    """
    from chemclaw.api.auth import Principal, require_principal

    app = _app(owner_store=_FakeOwnerStore())
    app.dependency_overrides[require_principal] = lambda: Principal(
        oid="alice", upn="a@corp", roles=frozenset()
    )
    client = TestClient(app)
    used = client.post("/sessions").json()["session_id"]
    warmed = client.post("/sessions").json()["session_id"]
    _turn(client, used, "A real question.")

    listed = [row["session_id"] for row in client.get("/sessions").json()]
    assert listed == [used]
    assert warmed not in listed


def test_a_registry_that_cannot_resume_advertises_no_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registry that cannot resume advertises no `X-Next-Cursor`.

    Only a `SessionOwnerStore` honours `after=`. The page ceiling is lowered via
    `service_max_listed_sessions` to make a page full.
    """
    from chemclaw.api.auth import Principal, require_principal
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "service_max_listed_sessions", 1)
    app = _app(owner_store=_FakeOwnerStore())
    app.dependency_overrides[require_principal] = lambda: Principal(
        oid="alice", upn="a@corp", roles=frozenset()
    )
    client = TestClient(app)
    session_id = client.post("/sessions").json()["session_id"]
    _turn(client, session_id, "A real question.")

    page = client.get("/sessions")

    assert page.status_code == 200
    assert [row["session_id"] for row in page.json()] == [session_id], "the page is full"
    assert "X-Next-Cursor" not in page.headers, (
        "this registry refuses `after=` with a 422, so a cursor it hands out is an instruction to "
        "make a request it will reject"
    )
    # And the refusal it would have run into, so the two halves are asserted together rather than
    # one of them being taken on trust.
    assert client.get("/sessions", params={"after": "anything"}).status_code == 422


def test_session_list_is_empty_without_a_durable_registry() -> None:
    """Under the in-memory store there is no durable registry, so the list is honestly empty.

    Reporting the process's live LRU instead would answer a question about the deployment with an
    eviction-dependent guess that a pod restart silently changes.
    """
    from chemclaw.api.auth import Principal, require_principal

    app = _app()  # owner_store None under the memory store
    app.dependency_overrides[require_principal] = lambda: Principal(
        oid="alice", upn="a@corp", roles=frozenset()
    )
    client = TestClient(app)
    client.post("/sessions")
    assert app.state.session_owners is None
    assert client.get("/sessions").json() == []


def test_transcript_reads_back_the_stored_thread() -> None:
    """`GET /sessions/{id}/messages` returns the session's stored thread, so a reload restores it.

    Seeded through `app.state.history`, the provider a real turn stores through, so the test covers
    the route's ownership gate, ordering and flattening.
    """
    from langchain_core.messages import AIMessage, HumanMessage

    from chemclaw.api.auth import Principal, require_principal

    app = _app()
    app.dependency_overrides[require_principal] = lambda: Principal(
        oid="alice", upn="a@corp", roles=frozenset()
    )
    client = TestClient(app)
    session_id = client.post("/sessions").json()["session_id"]
    assert client.get(f"/sessions/{session_id}/messages").json() == []  # nothing said yet

    session = app.state.live_sessions.get(session_id).session
    asyncio.run(
        app.state.history.save_messages(
            session_id,
            [HumanMessage(content="hello"), AIMessage(content="hi there")],
            state=session.state,
        )
    )

    transcript = client.get(f"/sessions/{session_id}/messages").json()
    assert [row["role"] for row in transcript] == ["user", "assistant"]
    assert transcript[0]["text"] == "hello"
    assert transcript[1]["text"] == "hi there"
    # Seeded off the request path, so no turn stored it: unknown, not a turn named "".
    assert [row["correlation_id"] for row in transcript] == [None, None]


def test_a_turn_writes_itself_into_the_transcript() -> None:
    """A turn that ran is readable afterwards — the half the seeded test cannot see.

    The graph keeps its thread in the checkpointer, so the runner must write `session_messages`
    itself. This test seeds nothing: it posts a message, lets the turn run, and reads the route.
    """
    from chemclaw.api.auth import Principal, require_principal

    app = _app()
    app.dependency_overrides[require_principal] = lambda: Principal(
        oid="alice", upn="a@corp", roles=frozenset()
    )
    client = TestClient(app)
    session_id = client.post("/sessions").json()["session_id"]

    with client.stream(
        "POST", f"/sessions/{session_id}/messages", json={"message": "what is the pKa?"}
    ) as res:
        assert res.status_code == 200
        for _line in res.iter_lines():
            pass

    transcript = client.get(f"/sessions/{session_id}/messages").json()
    assert [row["role"] for row in transcript] == ["user", "assistant"], transcript
    assert transcript[0]["text"] == "what is the pKa?"
    # `_FakeAgent` streams "hi " then "there"; the transcript stores the assembled answer, not the
    # fragments, because that is what a chemist reading back is owed.
    assert transcript[1]["text"] == "hi there"


def test_a_turns_transcript_rows_carry_its_correlation_id() -> None:
    """A detached client finds its turn's answer by the id it sent, not by the answer's text.

    Driven through a real turn on the in-memory store, which must answer the field as the durable
    one does; `tests/test_api_sessions.py` holds the Postgres half.
    """
    from chemclaw.api.auth import Principal, require_principal

    app = _app()
    app.dependency_overrides[require_principal] = lambda: Principal(
        oid="alice", upn="a@corp", roles=frozenset()
    )
    client = TestClient(app)
    session_id = client.post("/sessions").json()["session_id"]

    with client.stream(
        "POST",
        f"/sessions/{session_id}/messages",
        json={"message": "what is the pKa?"},
        headers={"X-Chemclaw-Correlation-Id": "ui-turn-0001"},
    ) as res:
        assert res.status_code == 200
        for _line in res.iter_lines():
            pass

    transcript = client.get(f"/sessions/{session_id}/messages").json()
    assert [(row["role"], row["correlation_id"]) for row in transcript] == [
        ("user", "ui-turn-0001"),
        ("assistant", "ui-turn-0001"),
    ], transcript


def test_transcript_of_an_unknown_session_is_404() -> None:
    """An id nobody owns is a 404, same as every other session-scoped route."""
    client = _client(_FakeAgent())
    assert client.get("/sessions/nope/messages").status_code == 404


def test_session_rehydrates_after_a_restart() -> None:
    """A returning client reattaches to its session after the live cache is wiped (F3).

    Simulates the pod restart the front door previously could not survive: ownership persists, so a
    cache miss looks the owner up and rebuilds the live handle instead of forcing a new session.
    """
    from chemclaw.api.auth import Principal, require_principal
    from chemclaw.core.config import settings

    owners = _FakeOwnerStore()
    app = _app(owner_store=owners)
    app.dependency_overrides[require_principal] = lambda: Principal(
        oid="alice", upn="alice@corp", roles=frozenset()
    )
    client = TestClient(app)

    session_id = client.post("/sessions").json()["session_id"]
    assert session_id in owners.owners  # ownership persisted at creation

    # Restart: the in-process live-session cache is gone; the durable owner record survives.
    app.state.live_sessions = _LiveSessions(settings.service_max_live_sessions)
    assert app.state.live_sessions.get(session_id) is None

    res = client.post(f"/sessions/{session_id}/messages", json={"message": "hi"})
    assert res.status_code == 200  # reattached, not a 404
    assert app.state.live_sessions.get(session_id) is not None  # re-registered in the cache


def test_rehydration_is_owner_scoped() -> None:
    """After a restart, a different user still cannot reattach to someone else's session (F3)."""
    from chemclaw.api.auth import Principal, require_principal
    from chemclaw.core.config import settings

    owners = _FakeOwnerStore()
    app = _app(owner_store=owners)
    client = TestClient(app)

    app.dependency_overrides[require_principal] = lambda: Principal(
        oid="alice", upn="a@corp", roles=frozenset()
    )
    session_id = client.post("/sessions").json()["session_id"]
    app.state.live_sessions = _LiveSessions(settings.service_max_live_sessions)  # restart

    app.dependency_overrides[require_principal] = lambda: Principal(
        oid="bob", upn="b@corp", roles=frozenset()
    )
    res = client.post(f"/sessions/{session_id}/messages", json={"message": "x"})
    assert res.status_code == 404  # not the owner → no reattach, no existence leak


def test_null_owner_rehydration_is_blocked_once_entra_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The durable-rehydration path applies the same NULL-owner rule as the live-cache path (Sec-3).

    A NULL owner recorded in the durable table (dev-mode write, surviving into enforcement) must
    404 on reattach once `entra_required` is on, exactly as the live-cache miss above does.
    """
    from chemclaw.api.auth import Principal, require_principal
    from chemclaw.core.config import settings

    owners = _FakeOwnerStore()
    session_id = "null-owner-durable"
    owners.owners[session_id] = None
    owners.profiles[session_id] = None
    owners.created[session_id] = datetime(2026, 1, 1, tzinfo=UTC)

    app = _app(owner_store=owners)
    stranger = Principal(oid="stranger", upn="stranger@corp", roles=frozenset())
    app.dependency_overrides[require_principal] = lambda: stranger
    client = TestClient(app)

    # Dev-mode default (unchanged): rehydration reattaches an owner-less durable session.
    res = client.post(f"/sessions/{session_id}/messages", json={"message": "hi"})
    assert res.status_code == 200

    app.state.live_sessions = _LiveSessions(settings.service_max_live_sessions)  # force rehydration
    monkeypatch.setattr(settings, "entra_required", True)
    res = client.post(f"/sessions/{session_id}/messages", json={"message": "hi"})
    assert res.status_code == 404


def test_no_rehydration_without_durable_store() -> None:
    """With no durable owner store (the in-memory session store), a cache miss stays a 404.

    The default path is unchanged: rehydration is gated on `session_store="postgres"`.
    """
    from chemclaw.core.config import settings

    app = _app()  # owner_store None under the memory store
    assert app.state.session_owners is None
    with TestClient(app) as client:
        session_id = client.post("/sessions").json()["session_id"]
        app.state.live_sessions = _LiveSessions(settings.service_max_live_sessions)  # restart
        res = client.post(f"/sessions/{session_id}/messages", json={"message": "x"})
        assert res.status_code == 404


def test_turn_is_refused_over_budget(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Once a session's turn budget is spent, the next turn is refused with 429 (budget #3)."""
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "budget_enabled", True)
    monkeypatch.setattr(settings, "budget_max_turns_per_session", 1)
    with _client(_FakeAgent()) as client:
        session_id = client.post("/sessions").json()["session_id"]
        # First turn runs to completion (draining the stream books it against the budget).
        with client.stream(
            "POST", f"/sessions/{session_id}/messages", json={"message": "hi"}
        ) as res:
            assert res.status_code == 200
            for _line in res.iter_lines():
                pass
        # Second turn exceeds the one-turn cap → refused before any streaming starts.
        res = client.post(f"/sessions/{session_id}/messages", json={"message": "again"})
        assert res.status_code == 429


def test_a_concurrent_burst_cannot_overrun_the_budget_by_more_than_the_permits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent burst cannot overrun the budget by more than the permit count.

    `check` is repeated after admission, so with one permit and a one-turn cap, exactly one of ten
    concurrent posts answers and the rest end with a `budget_exhausted` event on their stream.
    """
    from chemclaw.api.auth import Principal, require_principal
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "budget_enabled", True)
    monkeypatch.setattr(settings, "budget_max_turns_per_user", 1)
    monkeypatch.setattr(settings, "budget_max_turns_per_session", 0)
    monkeypatch.setattr(settings, "service_max_concurrent_turns", 1)
    monkeypatch.setattr(settings, "service_turn_admission_timeout_seconds", 10.0)

    async def _drive() -> None:
        app = _app()
        app.dependency_overrides[require_principal] = lambda: Principal(oid="u1", upn="u1@corp")
        async with asgi_client(app, timeout=30) as client:
            sessions = [(await client.post("/sessions")).json()["session_id"] for _ in range(10)]

            async def _turn(session_id: str) -> list[dict[str, Any]]:
                res = await client.post(f"/sessions/{session_id}/messages", json={"message": "hi"})
                if res.status_code == 429:
                    return [{"type": "429"}]
                return [
                    json.loads(line[len("data:") :].strip())
                    for line in res.text.splitlines()
                    if line.startswith("data:")
                ]

            streams = await asyncio.gather(*(_turn(s) for s in sessions))

        kinds = [[event["type"] for event in stream] for stream in streams]
        answered = [k for k in kinds if "answer" in k]
        assert len(answered) == 1, f"{len(answered)} turns ran against a one-turn budget: {kinds}"
        # Every other turn was told why, on its own stream or by status — never silently dropped.
        for stream in streams:
            if any(event["type"] == "answer" for event in stream):
                continue
            last = stream[-1]
            assert last["type"] in ("error", "429"), stream
            if last["type"] == "error":
                assert last["code"] == "budget_exhausted", last
        booked = app.state.budget._users.get("u1")
        assert booked is not None and booked.turns == 1, booked

    asyncio.run(_drive())


def test_budget_disabled_allows_many_turns(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """With budgets off (the default), turn count is never capped (unchanged behavior)."""
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "budget_enabled", False)
    monkeypatch.setattr(settings, "budget_max_turns_per_session", 1)
    with _client(_FakeAgent()) as client:
        session_id = client.post("/sessions").json()["session_id"]
        for _ in range(3):
            with client.stream(
                "POST", f"/sessions/{session_id}/messages", json={"message": "hi"}
            ) as res:
                assert res.status_code == 200
                for _line in res.iter_lines():
                    pass


def test_live_sessions_evicts_least_recently_used() -> None:
    """The bounded registry drops the LRU entry past capacity, keeping recent ones (COR-3)."""
    reg = _LiveSessions(capacity=2)
    reg.add("a", "sess-a", "owner-a")
    reg.add("b", "sess-b", "owner-b")
    # Touch "a" so "b" becomes the least-recently-used before the third insert.
    assert reg.get("a") == LiveSession(session="sess-a", owner="owner-a")
    reg.add("c", "sess-c", "owner-c")
    assert reg.get("b") is None  # evicted (LRU)
    assert reg.get("a") == LiveSession(session="sess-a", owner="owner-a")  # kept (recently used)
    assert reg.get("c") == LiveSession(session="sess-c", owner="owner-c")  # kept (newest)


def test_live_sessions_never_exceeds_capacity() -> None:
    """Adding far more sessions than the cap keeps the map bounded (no unbounded growth)."""
    reg = _LiveSessions(capacity=3)
    for i in range(100):
        reg.add(f"s{i}", f"sess-{i}", "owner")
    # Only the last 3 survive; the map never grew past the cap.
    assert reg.get("s99") is not None
    assert reg.get("s0") is None
    assert sum(reg.get(f"s{i}") is not None for i in range(100)) == 3


def _gated_agent(gate: asyncio.Event, started: asyncio.Event, blocked_message: str) -> _FakeAgent:
    """A fake whose turn for `blocked_message` parks on `gate` (concurrency tests).

    These tests use httpx's ASGI transport on a real loop, since the sync TestClient cannot hold
    one turn open while issuing another.
    """

    class _GatedAgent(_FakeAgent):
        """A fake whose turn parks until the test releases it."""

        async def stream(self, message: str) -> AsyncIterator[Piece]:
            if message == blocked_message:
                started.set()
                await gate.wait()
            yield "done"

    return _GatedAgent()


async def test_concurrent_turn_on_same_session_waits_in_line() -> None:
    """While one turn runs, a second POST to the same session waits for it instead of running.

    Two concurrent turns would interleave messages in one thread. A third message from the same
    sender while the second waits is refused: one place per sender per session.
    """
    gate = asyncio.Event()
    started = asyncio.Event()
    app = _app(_gated_agent(gate, started, "first"))
    async with asgi_client(app) as client:
        session_id = (await client.post("/sessions")).json()["session_id"]
        first = asyncio.create_task(
            client.post(f"/sessions/{session_id}/messages", json={"message": "first"})
        )
        await asyncio.wait_for(started.wait(), timeout=5)  # the first turn is mid-run
        second = asyncio.create_task(
            client.post(f"/sessions/{session_id}/messages", json={"message": "second"})
        )
        async with asyncio.timeout(5):
            while not (await client.get(f"/sessions/{session_id}/queue")).json()["waiting"]:
                await asyncio.sleep(0.01)
        assert not second.done(), "the second message ran beside the first"
        third = await client.post(f"/sessions/{session_id}/messages", json={"message": "third"})
        assert third.status_code == 409
        assert third.json()["detail"]["code"] == "already_waiting"
        assert "already have a message waiting" in third.json()["detail"]["message"]
        gate.set()
        assert (await first).status_code == 200
        waited = await second
        assert waited.status_code == 200
        assert '"type":"queued"' in waited.text and '"type":"answer"' in waited.text
        # The line is empty and the slot free again — the next message runs at once.
        ok = await client.post(f"/sessions/{session_id}/messages", json={"message": "fourth"})
        assert ok.status_code == 200 and '"type":"queued"' not in ok.text


async def test_concurrent_turns_on_different_sessions_are_admitted() -> None:
    """The per-session gate is per session: a turn on another session is not blocked."""
    gate = asyncio.Event()
    started = asyncio.Event()
    app = _app(_gated_agent(gate, started, "blocked"))
    async with asgi_client(app) as client:
        first = (await client.post("/sessions")).json()["session_id"]
        second = (await client.post("/sessions")).json()["session_id"]
        blocked = asyncio.create_task(
            client.post(f"/sessions/{first}/messages", json={"message": "blocked"})
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        other = await client.post(f"/sessions/{second}/messages", json={"message": "b"})
        assert other.status_code == 200  # a different session's turn runs concurrently
        gate.set()
        assert (await blocked).status_code == 200


def test_stalled_turn_times_out_and_frees_the_permit(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A turn past the wall-clock bound ends with one error event and releases its permit.

    Without this, a hung model stream would hold one of the few admission permits forever; a
    handful of stalls would collapse the front door (every turn shed 503) until restart.
    """
    from chemclaw.core.config import settings

    class _HungAgent(_FakeAgent):
        """A fake standing in for a hung LLM endpoint: one token, then nothing."""

        async def stream(self, message: str) -> AsyncIterator[Piece]:
            yield "partial"
            await asyncio.sleep(60)  # a hung LLM endpoint: never yields again
            yield "never"

    monkeypatch.setattr(settings, "service_turn_timeout_seconds", 0.2)
    app = _app(_HungAgent())
    with TestClient(app) as client:
        session_id = client.post("/sessions").json()["session_id"]
        events = []
        with client.stream(
            "POST", f"/sessions/{session_id}/messages", json={"message": "hi"}
        ) as res:
            assert res.status_code == 200
            for line in res.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[len("data:") :].strip()))
    assert events[-1]["type"] == "error"
    assert "time limit" in events[-1]["message"]
    # The permit and the session's turn slot are both released — capacity is not pinned.
    assert app.state.turn_semaphore._value == settings.service_max_concurrent_turns
    assert session_id not in app.state.active_turns


def test_a_client_cancelled_mid_admission_leaves_a_turn_that_finishes_itself(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A disconnect during the admission wait detaches the client; the turn queues, runs, frees.

    The turn belongs to the request, not the socket, so it keeps its claim, runs unwatched, and only
    then frees the slot; the slot is never held by a turn that no longer exists.
    """
    import contextlib

    import httpx

    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "service_turn_admission_timeout_seconds", 30.0)

    async def _run() -> None:
        app = _app()
        # Zero permits: the turn parks on the semaphore *after* taking the session's slot.
        app.state.turn_semaphore = asyncio.Semaphore(0)
        async with asgi_client(app) as client:
            session_id = (await client.post("/sessions")).json()["session_id"]
            waiting = asyncio.create_task(
                client.post(f"/sessions/{session_id}/messages", json={"message": "hi"})
            )
            async with asyncio.timeout(5):
                while session_id not in app.state.active_turns:  # parked mid-admission
                    await asyncio.sleep(0.01)
            waiting.cancel()  # the client goes away; the turn does not
            with contextlib.suppress(asyncio.CancelledError, httpx.HTTPError):
                await waiting

            # The detached turn still holds the session — queued, not leaked.
            assert session_id in app.state.active_turns
            app.state.turn_semaphore.release()  # capacity returns...
            # ...and the abandoned turn runs to completion unwatched, freeing the slot at its
            # true end — the transcript, not this socket, is where its answer went.
            async with asyncio.timeout(5):
                while session_id in app.state.active_turns:
                    await asyncio.sleep(0.01)
            res = await client.post(f"/sessions/{session_id}/messages", json={"message": "hi"})
            assert res.status_code == 200  # the session is usable, not 409-bricked

    asyncio.run(_run())


def test_a_session_with_a_turn_in_flight_is_pinned_against_eviction(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A session with a turn in flight is pinned against eviction from the live cache.

    Evicting it would rehydrate a second handle over the same history while the first still
    streams; the cache briefly holds over capacity instead.
    """
    from chemclaw.core.config import settings

    monkeypatch.setattr(settings, "service_max_live_sessions", 1)

    async def _run() -> None:
        gate = asyncio.Event()
        started = asyncio.Event()
        app = _app(_gated_agent(gate, started, "long"), owner_store=_FakeOwnerStore())
        async with asgi_client(app) as client:
            first = (await client.post("/sessions")).json()["session_id"]
            original = app.state.live_sessions.get(first).session
            turn = asyncio.create_task(
                client.post(f"/sessions/{first}/messages", json={"message": "long"})
            )
            await asyncio.wait_for(started.wait(), timeout=5)  # the turn is mid-stream
            # One slot: creating a second session is exactly the pressure that used to evict.
            await client.post("/sessions")
            # Touch the first session the way any request would; on the unpinned code this is
            # the moment a second handle is minted over the same durable id.
            transcript = await client.get(f"/sessions/{first}/messages")
            assert transcript.status_code == 200
            entry = app.state.live_sessions.get(first)
            assert entry is not None, "the in-flight session was evicted under capacity pressure"
            assert entry.session is original, (
                "a second live handle was minted for a session whose turn is still streaming"
            )
            gate.set()
            assert (await turn).status_code == 200

    asyncio.run(_run())


def test_event_streams_are_capped_per_user(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Past the per-user cap, another push-back stream is refused with 429 (DB-load guard).

    Each stream polls the database for its whole lifetime; unbounded streams from one user are
    a connection-exhaustion vector against the shared session store.
    """
    import contextlib

    import httpx

    import chemclaw.api.app as app_module
    from chemclaw.agent.session_events import SessionEvent
    from chemclaw.core.config import settings

    async def _idle_stream(session_id: str, **_: object) -> object:
        while True:  # holds the stream open without ever delivering
            await asyncio.sleep(3600)
            yield SessionEvent(session_id=session_id, kind="job_completed", payload={})

    monkeypatch.setattr(app_module, "stream_new_events", _idle_stream)
    monkeypatch.setattr(settings, "service_max_event_streams_per_user", 1)

    async def _run() -> None:
        app = _app()
        async with asgi_client(app) as client:
            session_id = (await client.post("/sessions")).json()["session_id"]
            first = asyncio.create_task(client.get(f"/sessions/{session_id}/events"))
            async with asyncio.timeout(5):
                while not app.state.event_streams:  # the first stream is admitted and counted
                    await asyncio.sleep(0.01)
            second = await client.get(f"/sessions/{session_id}/events")
            assert second.status_code == 429  # the per-user cap binds
            # It carries `Retry-After`: the UI treats a 429 without one as an exhausted budget and
            # locks the composer, while this cap lifts as soon as a stream closes.
            assert second.headers.get("retry-after"), (
                "a 429 with no Retry-After renders as a permanent budget_exhausted in the UI"
            )
            first.cancel()
            with contextlib.suppress(asyncio.CancelledError, httpx.HTTPError):
                await first
            async with asyncio.timeout(5):
                while app.state.event_streams:  # closing the stream freed the user's slot
                    await asyncio.sleep(0.01)

    asyncio.run(_run())


def test_events_route_claims_a_named_set_of_kinds_and_not_every_kind(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The push-back route claims a named set of kinds, because claiming is destructive.

    Claiming marks rows consumed, so claiming every kind would destroy other consumers' events. The
    set is `job_completed`, `job_failed` and `awaiting-answer`, asserted exactly so `DIGEST_KIND`,
    claimed per principal by `GET /digests`, stays out.
    """
    import chemclaw.api.app as app_module
    from chemclaw.agent.session_events import SessionEvent
    from chemclaw.durable.awaiting import AWAITING_KIND
    from chemclaw.durable.digest import DIGEST_KIND
    from chemclaw.exhibits.models import PUSH_KIND

    captured: dict[str, object] = {}

    async def _fake_stream(session_id: str, **kwargs: object) -> object:
        captured.update(kwargs)
        yield SessionEvent(session_id=session_id, kind="job_completed", payload={"job_id": "j1"})

    monkeypatch.setattr(app_module, "stream_new_events", _fake_stream)
    with _client(_FakeAgent()) as client:
        session_id = client.post("/sessions").json()["session_id"]
        with client.stream("GET", f"/sessions/{session_id}/events") as res:
            for _line in res.iter_lines():
                pass
    claimed = captured["kinds"]
    assert isinstance(claimed, tuple)
    assert set(claimed) == {"job_completed", "job_failed", AWAITING_KIND, PUSH_KIND}
    assert DIGEST_KIND not in claimed


def test_a_failed_job_reaches_the_asker_with_its_reason(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A durable job that fails after its turn reaches the asker with its reason.

    Otherwise the "job started" promise stands forever, and an outcome that says nothing is read as
    success.
    """
    import chemclaw.api.app as app_module
    from chemclaw.agent.session_events import SessionEvent

    async def _fake_stream(session_id: str, **kwargs: object) -> object:
        yield SessionEvent(
            session_id=session_id,
            kind="job_failed",
            payload={
                "job_id": "calc-compare_solvents-abc",
                "reason": "unknown ALPB solvent '2-methyltetrahydrofuran'",
            },
        )

    monkeypatch.setattr(app_module, "stream_new_events", _fake_stream)
    with _client(_FakeAgent()) as client:
        session_id = client.post("/sessions").json()["session_id"]
        with client.stream("GET", f"/sessions/{session_id}/events") as res:
            body = "".join(line for line in res.iter_lines())

    assert "job_failed" in body
    assert "calc-compare_solvents-abc" in body
    assert "2-methyltetrahydrofuran" in body, (
        "the reason must survive to the client, not just the type"
    )


def test_a_session_with_no_plan_has_nothing_to_decide_on() -> None:
    """`POST /plan/decision` refuses a session that is proposing nothing, with 409.

    The empty plan hashes to a constant shared by every session, so a decision on it means nothing.
    The hash posted is the real one `GET /plan` reports, so this cannot pass by mismatch.
    """
    with _client(_FakeAgent()) as client:
        session_id = client.post("/sessions").json()["session_id"]
        plan = client.get(f"/sessions/{session_id}/plan").json()
        assert plan["plan"] == [], "the precondition is a session with no plan"
        res = client.post(
            f"/sessions/{session_id}/plan/decision",
            json={"approved": True, "plan_hash": plan["plan_hash"]},
        )
        assert res.status_code == 409, f"the empty plan was decided on: {res.status_code}"
        assert client.get(f"/sessions/{session_id}/plan").json()["decided_by"] is None, (
            "a decision was recorded against the empty plan"
        )
        # And a row that exists anyway — written before this refused, and durable — must not come
        # back as an approval either, or the display disagrees with the gate that refuses it.
        asyncio.run(
            client.app.state.plan_approvals.record(  # type: ignore[attr-defined]
                session_id, plan["plan_hash"], "someone", True, ()
            )
        )
        assert client.get(f"/sessions/{session_id}/plan").json()["approved"] is False, (
            "the front door reported the empty plan as approved"
        )


def test_every_session_scoped_route_is_ownership_gated() -> None:
    """Every route carrying a session id resolves ownership — a non-owner gets 404 on all of them.

    Enumerates the app's routes, so a new session-scoped route forces an inventory update and is
    then swept.
    """
    from fastapi.routing import APIRoute

    from chemclaw.api.auth import Principal, require_principal

    app = _app()
    session_routes = [
        route
        for route in app.routes
        if isinstance(route, APIRoute) and "{session_id}" in route.path
    ]
    inventory = {
        (route.path, method)
        for route in session_routes
        for method in (route.methods or set()) - {"HEAD", "OPTIONS"}
    }
    assert inventory == {
        ("/sessions/{session_id}/messages", "POST"),
        ("/sessions/{session_id}/messages", "GET"),
        ("/sessions/{session_id}/events", "GET"),
        ("/sessions/{session_id}/attachments", "POST"),
        # The pre-execution approval gate (REV-1, D-137). Both must be owner-scoped: reading a
        # plan leaks what another chemist is doing, and deciding on one would let a stranger
        # authorize it.
        ("/sessions/{session_id}/plan", "GET"),
        ("/sessions/{session_id}/plan/decision", "POST"),
        # The stored full text of what one tool returned. Owner-scoped for the reason the route
        # is hung off a session at all: a ref is the SHA-256 of a result's own text, so it is
        # unguessable but not secret, and this gate — not the ref — is what says who may read it.
        ("/sessions/{session_id}/tool-results/{ref}", "GET"),
        # The explicit stop. Owner-scoped, since cancelling another's turn is interference; 404
        # either way, so a stranger cannot learn whether a session is mid-turn.
        ("/sessions/{session_id}/turn/stop", "POST"),
        # Fork and delete go through the same gate as reading: a caller who cannot read a session
        # must not delete it, and a fork copies the whole transcript, so ungated it would be a read.
        # 404 either way, so a stranger cannot learn which ids exist.
        ("/sessions/{session_id}/fork", "POST"),
        ("/sessions/{session_id}", "DELETE"),
        # Who else may reach the session (`D-2026-09-27-in-a-shared-session-the-sender-governs`).
        # A stranger is 404 on all three like every route above; what a *member* may do on them —
        # and on delete, fork and stop above — is `tests/test_shared_sessions.py`'s subject.
        ("/sessions/{session_id}/members", "GET"),
        ("/sessions/{session_id}/members/{actor}", "PUT"),
        ("/sessions/{session_id}/members/{actor}", "DELETE"),
        # The session's line and the running turn's live view. A stranger is 404 on all of them;
        # `tests/test_session_turn_queue.py` covers what a member may do.
        ("/sessions/{session_id}/queue", "GET"),
        ("/sessions/{session_id}/queue/{ticket}", "DELETE"),
        ("/sessions/{session_id}/turn/stream", "GET"),
        # The artefacts beside the chat. A stranger is 404 on reads and writes alike;
        # `tests/test_exhibit_routes.py` covers members.
        ("/sessions/{session_id}/exhibits", "GET"),
        ("/sessions/{session_id}/exhibits", "POST"),
        ("/sessions/{session_id}/exhibits/{exhibit_id}", "GET"),
        ("/sessions/{session_id}/exhibits/{exhibit_id}/revisions", "GET"),
        ("/sessions/{session_id}/exhibits/{exhibit_id}/revisions", "POST"),
        ("/sessions/{session_id}/exhibits/{exhibit_id}/diff", "GET"),
        ("/sessions/{session_id}/exhibits/{exhibit_id}/export.{fmt}", "GET"),
    }, (
        "new session-scoped route detected — it MUST resolve ownership via _resolve_session, "
        "and this inventory + the non-owner sweep below must cover it"
    )

    alice = Principal(oid="alice", upn="a@corp", roles=frozenset())
    bob = Principal(oid="bob", upn="b@corp", roles=frozenset())
    client = TestClient(app)
    app.dependency_overrides[require_principal] = lambda: alice
    session_id = client.post("/sessions").json()["session_id"]

    app.dependency_overrides[require_principal] = lambda: bob
    for route in session_routes:
        for method in (route.methods or set()) - {"HEAD", "OPTIONS"}:
            # `ref`, `actor` and `ticket` are supplied for the routes that take them and ignored by
            # the rest; `str.format` drops the surplus keyword rather than complaining, so one line
            # still builds a URL for all of them.
            url = route.path.format(
                session_id=session_id,
                ref="0" * 64,
                actor=bob.oid,
                ticket=1,
                exhibit_id="xb-0000000000000000",
                fmt="csv",
            )
            # The upload route takes multipart, the others JSON; send whichever the route expects so
            # a 404 here proves the *ownership* gate rather than a body-parsing rejection.
            if url.endswith("/attachments"):
                res = client.request(method, url, files={"file": ("a.txt", b"x", "text/plain")})
            elif url.endswith("/plan/decision"):
                res = client.request(method, url, json={"approved": True, "plan_hash": "x"})
            elif url.endswith("/exhibits") and method == "POST":
                spec = {"kind": "document", "markdown": "x"}
                res = client.request(
                    method, url, json={"kind": "document", "title": "t", "spec": spec}
                )
            elif url.endswith("/revisions") and method == "POST":
                spec = {"kind": "document", "markdown": "x"}
                res = client.request(method, url, json={"parent_revision": 1, "spec": spec})
            else:
                res = client.request(method, url, json={"message": "x"})
            assert res.status_code == 404, (
                f"{method} {route.path} answered {res.status_code} for a non-owner — "
                "it must resolve ownership (404, no existence leak) before doing anything"
            )


def test_the_per_connector_health_gauge_actually_renders_a_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`chemclaw_connector_unhealthy` renders a series per connector.

    An unbound gauge family renders nothing, so the panel could never draw. Asserted over the
    rendered exposition.
    """
    from chemclaw.api import app as service_app
    from chemclaw.connectors.health import ConnectorHealth

    async def _mixed() -> list[ConnectorHealth]:
        return [
            ConnectorHealth(name="calc", state="unreachable", detail="connection refused"),
            ConnectorHealth(name="molfp", state="healthy"),
            ConnectorHealth(name="results", state="unprobed"),
        ]

    monkeypatch.setattr(service_app, "probe_connectors", _mixed)
    monkeypatch.setattr(service_app, "check_connectors_at_startup", _mixed)
    monkeypatch.setattr(settings, "session_store", "memory")

    with TestClient(service_app.create_app(connector_factory=_no_connectors)) as client:
        exposition = client.get("/metrics").text
    assert 'chemclaw_connector_unhealthy{connector="calc"} 1' in exposition, exposition
    assert 'chemclaw_connector_unhealthy{connector="molfp"} 0' in exposition, exposition
    # `unprobed` is deliberately 0 rather than absent: `connectors/health.py` does not count it as
    # unhealthy, and omitting it would make "no series" mean both "reachable" and "never asked".
    assert 'chemclaw_connector_unhealthy{connector="results"} 0' in exposition, exposition
    # And the unlabelled count it has to agree with, from the same probe result.
    assert "chemclaw_connectors_unhealthy 1" in exposition, exposition


def test_readyz_does_not_name_the_connector_fleet_to_an_unauthenticated_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/readyz` does not name the connector fleet to an unauthenticated caller.

    The route is open by necessity, so its body is public; it carries the verdict and a count. Names
    stay on `/metrics` and in the per-connector WARNING logs.
    """
    from chemclaw.api import app as service_app
    from chemclaw.connectors.health import ConnectorHealth

    async def _degraded() -> list[ConnectorHealth]:
        return [
            ConnectorHealth(name="calc", state="unreachable", detail="connection refused"),
            ConnectorHealth(name="molfp", state="healthy"),
        ]

    monkeypatch.setattr(service_app, "probe_connectors", _degraded)
    monkeypatch.setattr(service_app, "check_connectors_at_startup", _degraded)
    monkeypatch.setattr(settings, "session_store", "memory")

    with TestClient(service_app.create_app(connector_factory=_no_connectors)) as client:
        body = client.get("/readyz").json()
    assert body["status"] == "ready"
    rendered = json.dumps(body)
    assert "calc" not in rendered and "molfp" not in rendered, rendered
    assert "connection refused" not in rendered, rendered
    assert body["connectors_unhealthy"] == 1


async def test_the_thread_pool_covers_the_tool_calls_one_admitted_turn_can_fan_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The thread pool covers the tool calls one admitted turn can fan out.

    Each admitted turn may hold up to `agent_max_parallel_tool_calls` threads, so the reserved count
    is turns times that, plus parses, before `service_thread_pool_headroom` is added. Asserted
    against the product read from settings, on the argument the real lifespan passes.
    """
    from concurrent.futures import ThreadPoolExecutor

    from chemclaw.api import app as app_module

    real = app_module.install_default_executor
    seen: list[ThreadPoolExecutor] = []

    def _spy(*, component: str, reserved: int) -> ThreadPoolExecutor:
        executor = real(component=component, reserved=reserved)
        seen.append(executor)
        return executor

    monkeypatch.setattr(app_module, "install_default_executor", _spy)
    app = app_module.create_app(connector_factory=_no_connectors)

    async with app.router.lifespan_context(app):
        pass

    fan_out = settings.service_max_concurrent_turns * max(1, settings.agent_max_parallel_tool_calls)
    caps = fan_out + settings.attachment_max_concurrent_parses
    floor = caps + settings.service_thread_pool_headroom
    assert seen, "the lifespan installed no executor"
    assert seen[0]._max_workers >= floor, (
        f"the front door sized its shared to_thread pool at {seen[0]._max_workers}; its own caps "
        f"can occupy {caps} of it, leaving nothing of the "
        f"{settings.service_thread_pool_headroom} threads reserved for token validation"
    )


def _held_keepalive_sockets(port: int, count: int) -> list[socket.socket]:
    """Open `count` connections, complete one request on each, and leave them idle and open.

    Idle keep-alives, not in-flight requests: the whole point is that uvicorn's connection limit
    counts a socket nobody is using, which is exactly what an open browser tab leaves behind.
    """
    held: list[socket.socket] = []
    for _ in range(count):
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        client.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
        assert b"200" in client.recv(256)
        held.append(client)
    return held


def test_the_connection_limit_refuses_the_liveness_probe_above_the_app() -> None:
    """The uvicorn connection limit refuses `/healthz` too, above the app.

    It counts open sockets, including idle keep-alives, so at the limit a busy pod fails liveness.
    Driven on a bare ASGI app since this is a transport property; `Settings` cross-checks the caps
    so the shipped front door cannot reach it. If uvicorn exempts a path, relax that check.
    """

    async def app(scope: Any, receive: Any, send: Any) -> None:
        assert scope["type"] == "http"
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    limit = 4
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, limit_concurrency=limit, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "uvicorn did not come up"

    held: list[socket.socket] = []
    try:
        # `limit - 1`: the arriving connection is already counted, so usable capacity is one below
        # the configured number; `service_connection_headroom` absorbs that.
        held = _held_keepalive_sockets(port, limit - 1)
        fresh = socket.create_connection(("127.0.0.1", port), timeout=5)
        held.append(fresh)
        fresh.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
        assert b"503" in fresh.recv(256), (
            "uvicorn served /healthz above its connection limit; the cross-check in "
            "core/config/__init__.py is guarding against something that no longer happens"
        )
    finally:
        for client in held:
            client.close()
        server.should_exit = True
        thread.join(timeout=10)


def test_the_socket_budget_cannot_be_reached_by_the_caps_it_is_meant_to_cover() -> None:
    """The socket budget cannot be reached by the stream and turn caps it is meant to cover.

    Otherwise a lost replica pushes all streams onto the survivor and liveness kills it.
    """
    from chemclaw.core.config import Settings

    # A turn is its sender's stream plus its watchers; waiting messages are charged per process at
    # the bound the turn route enforces.
    per_turn = 1 + settings.service_turn_max_watchers
    waiters = settings.service_max_concurrent_turns * settings.service_turn_queue_max
    assert (
        settings.service_max_connections
        >= settings.service_max_event_streams_total
        + settings.service_max_concurrent_turns * per_turn
        + waiters
        + settings.service_connection_headroom
    )
    with pytest.raises(ValueError) as excinfo:
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            service_max_connections=256,
            service_max_event_streams_total=200,
            service_max_concurrent_turns=8,
        )
    message = str(excinfo.value)
    assert "service_max_connections" in message and "service_max_event_streams_total" in message
    assert "/healthz" in message


def test_a_line_whose_poll_outlasts_its_lease_is_refused_at_startup() -> None:
    """A waiter refreshes its place each time it asks, so a poll at or above the lease is refused.

    Asked less often than the lease, every place lapses between asks and every queued message
    reads as withdrawn without anybody withdrawing it (#503).
    """
    from chemclaw.core.config import Settings

    assert settings.service_turn_queue_poll_seconds < settings.service_turn_claim_lease_seconds
    with pytest.raises(ValueError) as excinfo:
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            service_turn_queue_poll_seconds=60.0,
            service_turn_claim_lease_seconds=60.0,
        )
    message = str(excinfo.value)
    assert "service_turn_queue_poll_seconds" in message
    assert "service_turn_claim_lease_seconds" in message
