"""How many Postgres pools each `CHEMCLAW_COMPONENT` role opens, measured on the real roots.

This is the multiplier `pg_fleet_pools` uses and `chemclaw.fleetPools` renders. A process is not a
pool: `core/db` keys a pool on `(loop, dsn, libpq options, requested max_size)`, so each role's
composition root is driven and its pools counted. Postgres-backed (`tests/pg.py`); `max_size` is
read because everything here is a ceiling.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from chemclaw.core import db
from chemclaw.core.config import pg_endpoint, settings
from tests.pg import migrated_db_or_skip

# The three a front-door process holds, and what each is for. Named here so a failure reads as
# "which one went missing" rather than "3 != 2".
FRONT_DOOR_POOLS = (
    "the stores' pool, at `pg_statement_timeout_seconds`",
    "the `/readyz` probe's, at `service_readiness_db_timeout_seconds` and one connection wide — a "
    "distinct pool key, and deliberately so: sharing the stores' key answers 503 while the pool is "
    "merely busy",
    "the LangGraph checkpointer's registered autocommit pool (`agent/checkpointer.py`)",
)


# What a front-door process may open: two pools at `pg_pool_max_size` and the readiness probe's
# one. Written as the sum rather than `3 × max_size` because that product is what the fleet budget
# used to compute — 208 declared for a fleet opening 166, which refused a legal `maxReplicas: 9`.
def _front_door_ceiling() -> int:
    """The connections a front-door process may hold, as a sum over pools of unequal width."""
    return 2 * settings.pg_pool_max_size + 1


def _modules_containing(*needles: str) -> set[str]:
    """Every `src/chemclaw` module whose source contains any of `needles`, package-relative.

    A source scan, so the assertion fails on the edit a reviewer makes rather than on a driven path.
    """
    package = Path(db.__file__).parent.parent
    return {
        path.relative_to(package).as_posix()
        for path in package.rglob("*.py")
        if any(needle in path.read_text(encoding="utf-8") for needle in needles)
    }


async def _touch_stores() -> None:
    """Borrow once per DSN a role's stores resolve to, on the ordinary defaulted-timeout path."""
    for dsn in {settings.postgres_dsn, settings.session_store_dsn or settings.postgres_dsn}:
        async with db.connection(dsn, operation="fleet-pool-probe") as conn:
            await conn.execute("SELECT 1")


def test_a_front_door_process_holds_three_pools(monkeypatch: pytest.MonkeyPatch) -> None:
    """A front-door process holds three pools.

    Driven through `create_app`'s lifespan, a `/readyz` request and a turn's checkpointer: the real
    composition root, which makes `POOLS_PER_FRONT_DOOR` in `tests/test_deploy_chart.py` a
    measurement. `session_store="postgres"` as the chart ships; under `"memory"` it holds two.
    """
    monkeypatch.setattr(settings, "session_store", "postgres")
    monkeypatch.setattr(settings, "service_host", "127.0.0.1")

    async def _run() -> tuple[int, int]:
        await migrated_db_or_skip()
        from chemclaw.agent.checkpointer import close_checkpointer
        from chemclaw.api.app import create_app
        from chemclaw.api.runner import _turn_checkpointer

        app = create_app()
        async with app.router.lifespan_context(app):
            await _touch_stores()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://probe"
            ) as client:
                assert (await client.get("/readyz")).status_code in (200, 503)
            await _turn_checkpointer()
            try:
                return len(db._all_pools()), db._process_max_connections()
            finally:
                await close_checkpointer()

    pools, ceiling = asyncio.run(_run())
    assert pools == len(FRONT_DOOR_POOLS), (
        f"a front-door process opened {pools} pools, not {len(FRONT_DOOR_POOLS)}: "
        + "; ".join(FRONT_DOOR_POOLS)
        + ". The fleet connection budget multiplies this number, so a change here without a "
        "matching change to POOLS_PER_FRONT_DOOR and chemclaw.fleetPools mis-declares every "
        "release's Postgres ceiling."
    )
    assert ceiling == _front_door_ceiling(), (
        f"a front-door process declared {ceiling} connections, not {_front_door_ceiling()}: two "
        "pools at pg_pool_max_size plus the readiness probe's one. Settings."
        "fleet_connections_per_server charges exactly this per front-door replica, so a change "
        "here without a matching change there mis-declares every release's Postgres ceiling."
    )


def test_a_worker_process_holds_one_pool() -> None:
    """Every Temporal worker pools once: one DSN, one statement timeout, no checkpointer.

    Driven through `db.pooling()`, the context `serve_worker` enters for every worker role. A second
    pool opened inside `serve_worker` itself is invisible here, so the module scan below covers it.
    """

    async def _run() -> int:
        await migrated_db_or_skip()
        async with db.pooling():
            await _touch_stores()
            return db._process_max_connections()

    assert asyncio.run(_run()) == settings.pg_pool_max_size, (
        "a worker opened more than one pool's worth of connections; chemclaw.fleetPools counts it "
        "as one"
    )
    # And the worker's own module borrows nothing of its own. Every pool a worker holds comes from
    # the stores it drives through the shared context above; a `db.connection(` or a `_pool_for(`
    # in `durable/serve.py` is a pool the fleet budget counts for nobody.
    serve = (Path(db.__file__).parent.parent / "durable" / "serve.py").read_text(encoding="utf-8")
    assert "db.connection(" not in serve and "_pool_for(" not in serve, (
        "durable/serve.py opens a pool of its own; a worker is counted as one pool, so this is a "
        "fleet-budget change and POOLS_PER_FRONT_DOOR's sibling constants have to move with it"
    )


def test_a_connector_server_holds_one_pool() -> None:
    """A connector server holds one pool; the mcp-face reuses the same lifespan.

    `create_face_app` calls `connector_app`, so one measurement covers both chart terms.
    """

    async def _run() -> int:
        await migrated_db_or_skip()
        from mcp.server.fastmcp import FastMCP

        from chemclaw.connectors.server import connector_app

        # A fresh `FastMCP`: `StreamableHTTPSessionManager.run()` refuses a second call on one
        # instance, and other test files run the bundles' module-level apps.
        app = connector_app(FastMCP("probe-fleet-pools"), name="probe")
        async with app.router.lifespan_context(app):
            await _touch_stores()
            return db._process_max_connections()

    assert asyncio.run(_run()) == settings.pg_pool_max_size


def test_the_readiness_probe_is_the_only_call_site_that_mints_a_second_pool() -> None:
    """The readiness probe is the only call site that mints a second pool.

    `options` carries only the statement timeout, so each call site passing
    `statement_timeout_seconds=` costs its process a whole `pg_pool_max_size`. Another such site in
    the front door means four pools, and `POOLS_PER_FRONT_DOOR` and `chemclaw.fleetPools` must move.
    Read off the source so it fails on the addition.
    """
    assert _modules_containing("statement_timeout_seconds=", "pool_max_size=") == {
        "core/db.py",
        "api/routes/ops.py",
    }, (
        "the set of call sites borrowing with an explicit statement timeout or an explicit pool "
        "size changed. Each one outside core/db.py is a distinct pool key and so an extra pool on "
        "every process that reaches it — update POOLS_PER_FRONT_DOOR, chemclaw.fleetPools and "
        "postgres.maxConnections together, or route the call through the defaults. A sizing call "
        "site is scanned for beside a timeout one because it moves the same budget by a different "
        "keyword: `Settings.fleet_connections_per_server` charges one narrow pool per front-door "
        "replica, so a *second* narrow pool would be counted at full width."
    )


def test_only_the_front_door_reaches_the_checkpointers_pool() -> None:
    """Only the front door reaches the checkpointer's pool.

    A worker that gained a turn would open it only when an activity ran, after any startup
    measurement, so this checks who imports the pool-opening functions. `api/` is the front door and
    `cli/chat.py` the local chat. `durable/retention.py` imports only `CHECKPOINT_TABLES`, which is
    why the scan names the two functions rather than the module.
    """
    openers = _modules_containing(
        "import checkpointer", "import memory_store", "process_checkpointer"
    )
    assert {module.split("/")[0] for module in openers} <= {"agent", "api", "cli"}, (
        f"a module outside the front door reaches the checkpointer's pool: {sorted(openers)}. "
        "That role now holds a pool chemclaw.fleetPools does not count for it."
    )


def test_the_readiness_probes_pool_is_one_connection_wide(monkeypatch: pytest.MonkeyPatch) -> None:
    """The readiness probe's pool is one connection wide on both ends.

    `Settings.fleet_connections_per_server` charges one connection per front door for it. `min_size`
    is asserted too: psycopg refuses `min_size > max_size`, and that error on the request path would
    make `/readyz` 500. One warm connection also avoids a cold handshake.
    """
    monkeypatch.setattr(settings, "session_store", "postgres")
    monkeypatch.setattr(settings, "service_host", "127.0.0.1")

    async def _run() -> list[tuple[int, int]]:
        await migrated_db_or_skip()
        from chemclaw.api.app import create_app

        app = create_app()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://probe"
            ) as client:
                assert (await client.get("/readyz")).status_code in (200, 503)
            # Keyed on the requested size, so the probe's pool is the one whose key asks for 1.
            return [
                (int(pool.min_size), int(pool.max_size))
                for key, pool in db._POOLS.items()
                if key[3] == 1
            ]

    sized = asyncio.run(_run())
    assert sized == [(1, 1)], (
        f"the readiness probe's pool is {sized}, not [(1, 1)]. Settings."
        "fleet_connections_per_server charges one connection per front-door replica for it; a "
        "wider pool means every release under-declares its Postgres ceiling, and min_size above "
        "max_size makes /readyz raise a ValueError nothing on that path catches."
    )


async def test_the_readiness_probe_never_holds_two_connections_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The readiness probe never holds two connections at once.

    One connection suffices only because `_shared_probe` collapses concurrent callers. Asserted as a
    peak (no two probes overlap), with the cache window off, since a count depends on loop batching.
    """
    monkeypatch.setattr(settings, "session_store", "postgres")
    monkeypatch.setattr(settings, "service_host", "127.0.0.1")
    monkeypatch.setattr(settings, "service_readiness_cache_seconds", 0.0)

    live = 0
    peak = 0
    probes = 0
    real = db.connection

    @asynccontextmanager
    async def counting(dsn: str, **kwargs: Any) -> AsyncIterator[Any]:
        nonlocal live, peak, probes
        if kwargs.get("operation") != "readyz_probe":
            async with real(dsn, **kwargs) as conn:
                yield conn
            return
        live += 1
        probes += 1
        peak = max(peak, live)
        try:
            async with real(dsn, **kwargs) as conn:
                yield conn
        finally:
            live -= 1

    monkeypatch.setattr(db, "connection", counting)

    await migrated_db_or_skip()
    from chemclaw.api.app import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://probe"
        ) as client:
            for _ in range(5):
                await asyncio.gather(*(client.get("/readyz") for _ in range(20)))

    assert probes >= 2, (
        f"only {probes} probe(s) ran across five waves with the cache window off, so this run is "
        "not evidence about overlap"
    )
    assert peak == 1, (
        f"{peak} readiness probes held a connection at once across 100 requests. The probe's pool "
        "is one connection wide, so the second would wait pg_pool_timeout_seconds and answer 503 "
        "on an unauthenticated route — either restore the single-flight or widen the pool and the "
        "fleet budget together."
    )


def test_a_split_session_store_adds_one_pool_to_every_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A split session store adds one pool to every role.

    `core/db` keys pools on the DSN string, so a session DSN distinct from `postgres_dsn` splits
    every key resolving `session_store_dsn or postgres_dsn`. The front door goes to four: `/readyz`
    and the checkpointer move to the session DSN and the stores' session pool is new.
    """
    from psycopg import conninfo

    monkeypatch.setattr(settings, "session_store", "postgres")
    monkeypatch.setattr(settings, "service_host", "127.0.0.1")
    # A second endpoint, not just a second string: the placement assertion compares the address
    # `pg_endpoint` reports, and the loopback aliases differ by address while still connecting.
    host = str(conninfo.conninfo_to_dict(settings.postgres_dsn).get("host") or "").lower()
    if host not in {"localhost", "127.0.0.1"}:
        pytest.skip(f"needs a loopback postgres_dsn to spell twice; this one dials {host!r}")
    monkeypatch.setattr(
        settings,
        "session_store_dsn",
        conninfo.make_conninfo(
            settings.postgres_dsn, host="127.0.0.1" if host == "localhost" else "localhost"
        ),
    )

    async def _front_door() -> tuple[int, list[tuple[tuple[str, str] | None, int]]]:
        from chemclaw.agent.checkpointer import close_checkpointer
        from chemclaw.api.app import create_app
        from chemclaw.api.runner import _turn_checkpointer

        app = create_app()
        async with app.router.lifespan_context(app):
            await _touch_stores()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://probe"
            ) as client:
                assert (await client.get("/readyz")).status_code in (200, 503)
            await _turn_checkpointer()
            try:
                return len(db._all_pools()), [
                    (pg_endpoint(str(pool.conninfo)), int(pool.max_size))
                    for pool in db._all_pools()
                ]
            finally:
                await close_checkpointer()

    async def _worker() -> int:
        async with db.pooling():
            await _touch_stores()
            return len(db._all_pools())

    async def _run() -> tuple[int, int, list[tuple[tuple[str, str] | None, int]]]:
        await migrated_db_or_skip()
        front, placement = await _front_door()
        return front, await _worker(), placement

    front_door, worker, placement = asyncio.run(_run())
    # Where each pool dials, not just how many there are: counting alone passes when `/readyz`
    # probes the wrong DSN.
    session = pg_endpoint(settings.session_store_dsn)
    assert sorted(placement) == sorted(
        [(pg_endpoint(settings.postgres_dsn), settings.pg_pool_max_size)]
        + [(session, settings.pg_pool_max_size), (session, 1), (session, settings.pg_pool_max_size)]
    ), (
        f"a split front door's pools dial {sorted(placement)}: one full pool on postgres_dsn and, "
        "on the session store, the stores' pool, /readyz's single connection and the checkpointer's"
    )
    assert (front_door, worker) == (len(FRONT_DOOR_POOLS) + 1, 2), (
        f"a split session store gave the front door {front_door} pools and a worker {worker}, not "
        f"{len(FRONT_DOOR_POOLS) + 1} and 2. Settings.fleet_connections_per_server puts one full "
        "pool per pooled process on postgres_dsn's server and everything else on the session "
        "store's; a different split makes both declared ceilings wrong."
    )


# --- one server, two spellings: the measured identity ------------------------------------------


def _one_spelling_each_way() -> tuple[str, str]:
    """`postgres_dsn` and the same server spelled the other loopback way, or skip."""
    from psycopg import conninfo

    host = str(conninfo.conninfo_to_dict(settings.postgres_dsn).get("host") or "").lower()
    if host not in {"localhost", "127.0.0.1"}:
        pytest.skip(f"needs a loopback postgres_dsn to spell twice; this one dials {host!r}")
    other = "127.0.0.1" if host == "localhost" else "localhost"
    return settings.postgres_dsn, conninfo.make_conninfo(settings.postgres_dsn, host=other)


@pytest.fixture
def _forget_identities() -> Iterator[None]:
    """Each test starts with nothing learned, because the cache is process-wide and by design."""
    db._SERVER_IDENTITY.clear()
    db._IDENTITY_UNREADABLE.clear()
    yield
    db._SERVER_IDENTITY.clear()
    db._IDENTITY_UNREADABLE.clear()


def test_two_spellings_of_one_server_read_as_two_until_a_borrow_says_otherwise(
    _forget_identities: None,
) -> None:
    """Two spellings of one server read as two until a borrow says otherwise.

    Before any borrow `same_server` must answer exactly what `pg_endpoint` answers. Both directions:
    the loopback pair reads as two, and a DSN compared with itself as one.
    """
    here, there = _one_spelling_each_way()
    assert pg_endpoint(here) != pg_endpoint(there)
    assert not db.same_server(here, there), "unmeasured, this must agree with the string compare"
    assert db.same_server(here, here)


def test_a_borrow_teaches_the_gauge_that_two_spellings_are_one_server(
    _forget_identities: None,
) -> None:
    """A borrow teaches the gauge that two spellings are one server.

    Driven against the real server: `localhost` and `127.0.0.1` are one box. The identity is read
    once per endpoint and kept.
    """
    here, there = _one_spelling_each_way()

    async def _run() -> bool:
        await migrated_db_or_skip()
        async with db.pooling():
            for dsn in (here, there):
                async with db.connection(dsn, operation="test_identity") as conn:
                    await (await conn.execute("select 1")).fetchone()
            return db.same_server(here, there)

    assert asyncio.run(_run()), (
        f"after borrowing against both spellings the identities were {db._SERVER_IDENTITY}; one "
        "server answered two different system_identifiers, which cannot happen — or the borrow "
        "did not learn them at all"
    )
    assert len(set(db._SERVER_IDENTITY.values())) == 1


def test_a_split_the_measurement_disproves_stops_carving_the_fleet_in_two(
    monkeypatch: pytest.MonkeyPatch, _forget_identities: None
) -> None:
    """A split the measurement disproves stops carving the fleet in two.

    `fleet_connections_per_server` decides from the DSN strings at import; once a borrow shows one
    server, the second server's carve-out is zero.
    """
    here, there = _one_spelling_each_way()
    monkeypatch.setattr(settings, "session_store", "postgres")
    monkeypatch.setattr(settings, "session_store_dsn", there)

    async def _run() -> tuple[int, int]:
        await migrated_db_or_skip()
        async with db.pooling():
            for dsn in (here, there):
                async with db.connection(dsn, operation="test_identity") as conn:
                    await (await conn.execute("select 1")).fetchone()
            return db._process_max_connections(), db._session_store_max_connections()

    total, carved = asyncio.run(_run())
    assert total > 0
    assert carved == 0, (
        f"{carved} of {total} connections are charged to a second server, and the two DSNs name "
        "one box — so that much is subtracted from the primary's ceiling and checked against a "
        "ceiling for a server that does not exist"
    )


def test_an_unreadable_identity_is_attempted_once_and_then_left_alone(
    _forget_identities: None,
) -> None:
    """An unreadable server identity is attempted once and then left alone.

    This runs on the borrow path, so a failing `pg_control_system()` (no grant, or a fork without
    it) must not cost a round trip on every checkout: one attempt, one warning, then string
    comparison.
    """

    class _Boom:
        def cursor(self) -> Any:
            raise RuntimeError("no pg_control_system() here")

    here, there = _one_spelling_each_way()

    async def _run() -> int:
        calls = 0

        class _Counting(_Boom):
            def cursor(self) -> Any:
                nonlocal calls
                calls += 1
                raise RuntimeError("no pg_control_system() here")

        conn = _Counting()
        for _ in range(3):
            await db._learn_server_identity(conn, here)
        return calls

    assert asyncio.run(_run()) == 1, "the failed probe must not be retried on every borrow"
    unreadable = pg_endpoint(here)
    assert unreadable is not None and unreadable in db._IDENTITY_UNREADABLE
    assert not db.same_server(here, there), "an unreadable identity falls back, it does not guess"
