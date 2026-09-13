"""One sink may not hold the drain, and a driver that hangs may not starve the ones after it.

`durable/publish_results.py` iterates the enabled sinks **sequentially**, and its module docstring
gives that shape a reason: *"two enabled destinations are two failure domains: one being
unreachable must not hold up the other."* That is true of the rows — one row per (sink, calc_ref) —
and it was false of the pass. The only ceiling over the loop was the activity's
`result_publish_timeout_seconds x len(sinks)`, one budget the first sink could drink entirely,
while the setting's own declaration calls it a per-`deliver` bound.

Measured on the unfixed seam with `alpha` hanging and `beta` healthy over eight passes: `beta` was
claimed **zero** times and its row sat at `attempts=0` with an empty `last_error`, so nothing even
distinguished "starved" from "nothing to send".

Driven through the real `registry.build`, because that is where the bound now lives: a sink is
bounded because the registry built it, not because a particular caller remembered to wrap it.
"""

import asyncio
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from chemclaw.core.config import settings
from chemclaw.publish import registry
from chemclaw.publish.driver import ResultSink, SinkUnavailableError
from chemclaw.publish.manifest import ResultSinkManifest
from chemclaw.publish.record import ResultRecord


class _Hanging:
    """A driver that never answers — a blackholed warehouse, a dropped SYN, a wedged endpoint."""

    def __init__(self, *, name: str, tenant_id: str, **_: Any) -> None:
        """Accept the seam's two mandatory keywords and ignore the rest."""
        self.name = name
        self.closed = False

    async def deliver(self, records: Sequence[ResultRecord]) -> None:
        """Never return."""
        await asyncio.sleep(3600)

    async def aclose(self) -> None:
        """Never return here either — a driver that will not let go of its connection."""
        self.closed = True
        await asyncio.sleep(3600)


def hanging(**kwargs: Any) -> ResultSink:
    """The `module:callable` a probe manifest names."""
    return _Hanging(**kwargs)


def _manifest() -> ResultSinkManifest:
    """A manifest naming the hanging driver in this very module."""
    return ResultSinkManifest.model_validate(
        {
            "name": "hangprobe",
            "description": "a sink that never answers",
            "driver": f"{__name__}:hanging",
            "config": {},
        }
    )


@pytest.fixture
def bounded_sink(monkeypatch: pytest.MonkeyPatch) -> ResultSink:
    """A real, registry-built sink over the hanging driver, with a short per-sink ceiling."""
    monkeypatch.setattr(settings, "manifest_driver_packages", "tests")
    monkeypatch.setattr(settings, "result_publish_timeout_seconds", 0.5)
    return registry.build(_manifest())


def test_a_hanging_sink_gives_up_at_the_per_sink_ceiling(bounded_sink: ResultSink) -> None:
    """`deliver` must return control at `result_publish_timeout_seconds`, not hold the pass.

    Retryable, because the destination did not answer — which is also what puts the reason into
    `result_publications.last_error`, where an operator reads it. The unfixed seam returned control
    only when the whole activity timed out, having claimed the rows and marked none of them.
    """
    started = time.perf_counter()
    with pytest.raises(SinkUnavailableError) as outage:
        asyncio.run(bounded_sink.deliver([]))
    elapsed = time.perf_counter() - started

    assert "result_publish_timeout_seconds" in str(outage.value), (
        "the failure must name the knob that produced it, or an operator cannot raise it"
    )
    assert elapsed < 5.0, f"the per-sink ceiling did not bound the delivery ({elapsed:.1f}s)"


def test_a_sink_that_will_not_close_does_not_cost_the_next_one_its_pass(
    bounded_sink: ResultSink,
) -> None:
    """`aclose` is bounded too, and swallows its timeout.

    The drain calls it from a `finally` that has nothing to do with delivery, so a driver refusing
    to release a connection must not become the same starvation by another route — and must not
    turn a successful pass into a raised one either.
    """
    started = time.perf_counter()
    asyncio.run(bounded_sink.aclose())
    assert time.perf_counter() - started < 5.0, "an unbounded aclose starves the next sink"


def test_the_bound_is_the_seam_s_rather_than_a_caller_s(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every sink `build` returns is bounded, so no caller can forget to wrap one.

    Stated as a property of the returned object rather than of the drain loop: the backfill CLI and
    any later caller get the guarantee for free, and the activity's `x len(sinks)` budget becomes
    the honest sum of N per-sink budgets rather than a pool.
    """
    monkeypatch.setattr(settings, "manifest_driver_packages", "tests")
    built = registry.build(_manifest())
    assert not isinstance(built, _Hanging), (
        "build() handed back the raw driver; the per-sink ceiling is then whatever the caller "
        "remembers to impose, which is how one hanging destination starved every other one"
    )
    assert isinstance(built, ResultSink)


def test_a_driver_that_is_not_a_sink_is_still_named_before_it_is_wrapped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Protocol check runs on the driver, so its error names the driver's own failure."""
    monkeypatch.setattr(settings, "manifest_driver_packages", "builtins")
    manifest = ResultSinkManifest.model_validate(
        {
            "name": "notasink",
            "description": "not a sink at all",
            "driver": "builtins:dict",
            "config": {},
        }
    )
    with pytest.raises(registry.ResultSinkError) as refusal:
        registry.build(manifest)
    assert "did not build a ResultSink" in str(refusal.value)


def test_a_sinks_held_connection_is_counted_where_the_budget_applies() -> None:
    """A bare connection occupies a backend, and the process's own reading could only see pools.

    `D-2026-09-13-a-connection-counted-where-the-budget-applies`. `PostgresWarehouse` opens an
    un-pooled `AsyncConnection` and keeps it for the driver's life — in neither `db._POOLS` nor
    `db._FOREIGN_POOLS` — so `chemclaw_pg_pool_max_size` reported a ceiling one lower than the
    process could reach, for every enabled sink, while being the gauge an alert compares against
    `pg_fleet_max_connections`.

    **Registering it as a pool, which is what the `BACKLOG.md` row proposed, raises.**
    `_process_max_connections` sums `pool.max_size`; measured,
    `AttributeError: 'AsyncConnection' object has no attribute 'max_size'`. So the count is of
    *connections*, each worth one backend.

    **Both directions are asserted, because a blanket count is the same error in the other
    direction.** A result sink points by design at a database this system does not own
    (`D-2026-08-25-a-cache-is-not-a-record`), whose ceiling no deployment here declares — so a sink
    on its own server must count zero against the primary's budget, and only a sink that *is* on
    `postgres_dsn`'s server may raise it. A test asserting only the rise would pass a gauge that
    charged every warehouse in the world to this one ceiling.

    Read through `_process_max_connections` rather than off `_HELD_CONNECTIONS`: the registry is the
    mechanism, and what the alert reads is the sum.
    """
    from chemclaw.core import db
    from chemclaw.publish.drivers.postgres import PostgresWarehouse
    from tests.pg import migrated_db_or_skip

    async def _run() -> tuple[int, int, int, int, int]:
        await migrated_db_or_skip()
        before = db._process_max_connections()

        here = PostgresWarehouse(dsn=settings.postgres_dsn, schema="public")
        try:
            async with here.cursor() as cursor:
                await cursor.execute("SELECT 1", [])
                await cursor.fetchall()
            on_primary = db._process_max_connections()
        finally:
            await here.aclose()
        after_close = db._process_max_connections()

        # The same driver, dialled at a host string `pg_endpoint` reads as a different server. It
        # never connects — `_connection` is what registers, and nothing asks it to — which is the
        # honest arm: a sink whose warehouse is elsewhere contributes nothing here whether it is
        # reachable or not, and a reachable foreign server is not something this suite can assume.
        elsewhere = PostgresWarehouse(
            host="a-warehouse-of-its-own.invalid", port=5432, database="results", schema="public"
        )
        db.register_connection(_StillOpen(), elsewhere._conninfo())
        off_primary = db._process_max_connections()

        # **The drain builds a new driver every pass**, deliberately, so a rotated credential takes
        # effect on the next run (`PostgresWarehouse.aclose`'s own docstring). Without the release
        # in `aclose` the registry grows by one dead entry per pass, and the read-time prune is no
        # answer to that on its own: it only runs when somebody reads the gauge. Counted with no
        # read in between, which is what makes the release observable rather than believed.
        registered_before = len(db._HELD_CONNECTIONS)
        for _ in range(4):
            driver = PostgresWarehouse(dsn=settings.postgres_dsn, schema="public")
            async with driver.cursor() as cursor:
                await cursor.execute("SELECT 1", [])
                await cursor.fetchall()
            await driver.aclose()
        grew_by = len(db._HELD_CONNECTIONS) - registered_before
        return before, on_primary, after_close, off_primary, grew_by

    before, on_primary, after_close, off_primary, grew_by = asyncio.run(_run())

    assert grew_by == 0, (
        f"four open/close cycles left {grew_by} dead entries in the registry; the drain builds a "
        "driver per pass, so that is one per pass for the life of the worker"
    )

    assert on_primary == before + 1, (
        "a held connection on postgres_dsn's own server did not raise what this process reports it "
        f"may open: {before} -> {on_primary}"
    )
    assert after_close == before, (
        f"the connection kept counting after the driver closed it: {after_close} against {before}"
    )
    assert off_primary == before, (
        "a sink pointed at a warehouse of its own was charged to the primary server's budget: "
        f"{off_primary} against {before}"
    )


class _StillOpen:
    """A stand-in for a connection held open on a server this suite cannot dial.

    The registry reads exactly one thing off a connection — `closed` — and the endpoint it compares
    comes from the conninfo passed beside it. A double is right here for the reason a double is
    usually wrong: the subject is the *arithmetic over the endpoint*, and the alternative is asking
    this suite to reach a second Postgres it has no way to start.
    """

    closed = False
