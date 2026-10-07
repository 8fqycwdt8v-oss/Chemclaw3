"""One sink may not hold the drain, and a driver that hangs may not starve the ones after it.

`durable/publish_results.py` iterates enabled sinks sequentially, treating each as its own failure
domain, so each `deliver` and `aclose` is bounded by `result_publish_timeout_seconds`. Driven
through the real `registry.build`, where the bound lives, so every caller gets it.
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
    """A hanging sink gives up at the per-sink ceiling.

    The timeout is retryable, which also records the reason in `result_publications.last_error`.
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

    It runs from the drain's `finally`, so a driver that will not release a connection must neither
    starve the next sink nor turn a successful pass into a raised one.
    """
    started = time.perf_counter()
    asyncio.run(bounded_sink.aclose())
    assert time.perf_counter() - started < 5.0, "an unbounded aclose starves the next sink"


def test_the_bound_is_the_seam_s_rather_than_a_caller_s(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every sink `build` returns is bounded, so no caller can forget to wrap one.

    The backfill CLI and later callers get the guarantee, and the activity's `x len(sinks)` budget
    is a sum of per-sink budgets rather than a pool.
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
    """A sink's held connection is counted where the connection budget applies.

    `D-2026-09-13-a-connection-counted-where-the-budget-applies`. `PostgresWarehouse` holds an
    un-pooled connection, which `chemclaw_pg_pool_max_size` must count as one backend (connections
    have no `max_size`). Both directions: a sink on another server counts zero against the primary's
    budget, and only one on `postgres_dsn`'s server raises it. Read through
    `_process_max_connections`, the sum the alert reads.
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

        # The same driver at a host `pg_endpoint` reads as a different server. It never connects; a
        # warehouse elsewhere contributes nothing whether reachable or not.
        elsewhere = PostgresWarehouse(
            host="a-warehouse-of-its-own.invalid", port=5432, database="results", schema="public"
        )
        db.register_connection(_StillOpen(), elsewhere._conninfo())
        off_primary = db._process_max_connections()

        # The drain builds a new driver every pass so rotated credentials apply, so `aclose` must
        # release its registry entry. Counted with no gauge read in between, since the read-time
        # prune would hide a leak.
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

    The registry reads only `closed`, and the endpoint comes from the conninfo beside it; the
    subject is the arithmetic over the endpoint.
    """

    closed = False
