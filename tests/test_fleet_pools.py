"""How many Postgres pools each `CHEMCLAW_COMPONENT` role opens, measured on the real roots.

This is the number `pg_fleet_pools` multiplies and `chemclaw.fleetPools` renders, and until
2026-09-05 nothing measured it: `Settings` computed `pooled_processes × pg_pool_max_size`, which
charged a front-door process 16 connections for the 48 it opens, and the shipped chart declared 136
against a real floor of 208. A *process* is not a pool — `core/db` keys a pool on
`(loop, dsn, libpq options)` and a process may also register a foreign one — so the only honest way
to know the multiplier is to drive each role's composition root and count what it holds.

Postgres-backed and skipped offline (`tests/pg.py`), because a pool that never opens is a pool this
file cannot see. Everything asserted here is a *ceiling* on connections, so `pool.max_size` is the
right reading rather than how many connections happen to be live.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from chemclaw.core import db
from chemclaw.core.config import settings
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

    A source scan rather than an import graph on purpose: what a reviewer adding a pool actually
    writes is one of these strings, and the assertion should fail on the edit rather than on
    whichever call path a test happened to drive.
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
    """Driven through `create_app`'s own lifespan, a `/readyz` request and a turn's checkpointer.

    The composition root, not a reconstruction of it: `tests/test_db_pool.py` already asserts that
    the *gauge* sums three hand-built pools, and that test passed throughout the period when the
    startup guard believed a front door held one. What was missing was a measurement of the real
    process, which is what makes `POOLS_PER_FRONT_DOOR` in `tests/test_deploy_chart.py` a fact
    rather than a guess.

    `session_store="postgres"` because that is what the chart ships and what makes the checkpointer
    exist; under `"memory"` a front door holds two and the chart over-declares, which is the safe
    direction.
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

    Driven through `db.pooling()` — the context `durable/serve.py::serve_worker` enters — because
    that is the one tail every worker runs through, core's `background-worker` and each bundle's
    `connector-worker-<name>` alike. Both chart roles are therefore this one measurement; a
    parametrization over their names would run the same code twice and read as coverage it is not.
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


def test_a_connector_server_holds_one_pool() -> None:
    """`connectors/server.py`'s lifespan, which the mcp-face reuses verbatim.

    Both roles are the same composition root — `create_face_app` calls `connector_app` — so one
    measurement covers the two chart terms. Neither serves a database readiness probe and neither
    takes a turn.
    """

    async def _run() -> int:
        await migrated_db_or_skip()
        from mcp.server.fastmcp import FastMCP

        from chemclaw.connectors.server import connector_app

        # A fresh `FastMCP` rather than an imported bundle's module-level `app`, because
        # `StreamableHTTPSessionManager.run()` refuses a second call on one instance and four other
        # test files run `molfp`'s. Importing it passed this file alone and failed the suite — an
        # order dependence in the test, not in the composition root, which is the same either way.
        app = connector_app(FastMCP("probe-fleet-pools"), name="probe")
        async with app.router.lifespan_context(app):
            await _touch_stores()
            return db._process_max_connections()

    assert asyncio.run(_run()) == settings.pg_pool_max_size


def test_the_readiness_probe_is_the_only_call_site_that_mints_a_second_pool() -> None:
    """A new non-default statement timeout is a new pool, everywhere its role runs.

    `core/db` keys a pool on `(dsn, options)` and `options` carries only the statement timeout, so
    every call site that passes `statement_timeout_seconds=` explicitly costs its process a whole
    `pg_pool_max_size` for the life of the process. That is a fleet-budget change made by a keyword
    argument, and nothing said so — this is what says so. A second such call site in the front door
    makes it four pools, and `POOLS_PER_FRONT_DOOR` plus `chemclaw.fleetPools` have to move with it.

    Read off the source rather than the call graph because that is what a reviewer adding one would
    change, and because the point is to fail on the *addition*, not on a path a test happened to
    drive.
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
    """The third pool belongs to a turn, and no worker or connector server takes one.

    This is the reason every role but the front door counts as one, and it is a property of who
    *imports* `agent/checkpointer.py`'s pool-opening entry points rather than of anything a probe
    can drive: a worker that gained a turn would open the pool the first time an activity ran, long
    after any startup measurement. `api/` is the front door (and `cli/chat.py` is the local chat,
    which the chart does not pod); a `durable/`, `connectors/` or `ingest/` module appearing here
    makes that role three pools too.

    `durable/retention.py` imports `CHECKPOINT_TABLES` — a tuple of table names, no pool — which is
    why the scan names the two functions that build one rather than the module.
    """
    openers = _modules_containing(
        "import checkpointer", "import memory_store", "process_checkpointer"
    )
    assert {module.split("/")[0] for module in openers} <= {"agent", "api", "cli"}, (
        f"a module outside the front door reaches the checkpointer's pool: {sorted(openers)}. "
        "That role now holds a pool chemclaw.fleetPools does not count for it."
    )


def test_the_readiness_probes_pool_is_one_connection_wide(monkeypatch: pytest.MonkeyPatch) -> None:
    """The narrow pool is real, and it is narrow on both ends.

    `Settings.fleet_connections_per_server` charges one connection per front-door replica for this
    pool. That is a claim about `api/routes/ops.py`, made in `core/config`, and nothing but this
    connects the two: if the route stopped asking for a size the budget would under-declare every
    front door by `pg_pool_max_size - 1` and no other test would notice.

    `min_size` is asserted with it because psycopg refuses `min_size > max_size`, and
    `pg_pool_min_size` defaults to 2. Raised inside `_pool_for` that lands on the request path, is
    not a `psycopg.Error`, and `_probe_database` does not catch it — `/readyz` would 500 rather
    than answer. One warm connection is also what the probe wants: measured, a cold pool pays a
    14.6 ms handshake on its first checkout against 2.1 ms warm.
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


def test_the_readiness_probe_never_holds_two_connections_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One connection is enough only because `_shared_probe` collapses every concurrent caller.

    This is the measurement the size rests on, and it is asserted as a *peak* rather than a count:
    how many probes a fixed number of requests produces depends on how the loop batches them, so a
    faster machine would read as a regression. What must hold is that two probes never overlap —
    which is a property of the single-flight, not of the cache window, so the window is off here.
    Left unasserted, a future edit that dropped the single-flight would turn `pool_max_size=1` into
    a self-inflicted `pg_pool_timeout_seconds` wait on an unauthenticated route.
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

    async def _run() -> None:
        await migrated_db_or_skip()
        from chemclaw.api.app import create_app

        app = create_app()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://probe"
            ) as client:
                for _ in range(5):
                    await asyncio.gather(*(client.get("/readyz") for _ in range(20)))

    asyncio.run(_run())
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
    """The `+1` that `Settings.fleet_connections_per_server` puts on the second server.

    `core/db` keys a pool on the DSN *string*, so pointing the session layer anywhere else splits
    every key that resolves `session_store_dsn or postgres_dsn`. A second string over the same
    database is the whole condition and keeps the probe connectable; what it cannot show is the
    second *server*, which is why the ceiling for one is declared rather than derived.

    The front door goes to four because its `/readyz` and checkpointer pools move to the session
    DSN while the stores' session pool is new — the arithmetic in `core/config` says the primary
    keeps one full pool per pooled process, and this is what makes that a measurement.
    """
    from psycopg import conninfo

    monkeypatch.setattr(settings, "session_store", "postgres")
    monkeypatch.setattr(settings, "service_host", "127.0.0.1")
    monkeypatch.setattr(
        settings,
        "session_store_dsn",
        conninfo.make_conninfo(settings.postgres_dsn, application_name="chemclaw-split-probe"),
    )

    async def _front_door() -> int:
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
                return len(db._all_pools())
            finally:
                await close_checkpointer()

    async def _worker() -> int:
        async with db.pooling():
            await _touch_stores()
            return len(db._all_pools())

    async def _run() -> tuple[int, int]:
        await migrated_db_or_skip()
        return await _front_door(), await _worker()

    front_door, worker = asyncio.run(_run())
    assert (front_door, worker) == (len(FRONT_DOOR_POOLS) + 1, 2), (
        f"a split session store gave the front door {front_door} pools and a worker {worker}, not "
        f"{len(FRONT_DOOR_POOLS) + 1} and 2. Settings.fleet_connections_per_server puts one full "
        "pool per pooled process on postgres_dsn's server and everything else on the session "
        "store's; a different split makes both declared ceilings wrong."
    )
