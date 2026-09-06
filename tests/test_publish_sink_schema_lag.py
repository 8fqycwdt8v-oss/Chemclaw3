"""What the SQL sink does about a store that is not the store this release expects.

Two faults that both used to end with a positive claim, driven against a real Postgres running the
**shipped** `schema/result-store/` DDL:

- a store built the way `CLAUDE.md` points a site at it — *"the schema ships in
  `schema/result-store/` and a site creates it"* — which is the DDL and **no registry rows**;
- a store one migration behind on the columns that carry the measurement.

The rule the two share is the one the omission filter was missing: *writing down to the schema you
find* is right for a column that merely records more, and wrong for a column the value lives in.
"""

import asyncio
from pathlib import Path
from typing import Any

import psycopg
import pytest

from chemclaw.core.config import settings
from chemclaw.publish.driver import SinkRejectedError
from chemclaw.publish.drivers.sql import SqlResultSink
from chemclaw.publish.record import (
    Conditions,
    PropertyFact,
    ResultRecord,
    Subject,
    SubjectMember,
    TheoryLevel,
)
from tests.pg import TEST_SCHEMA, migrated_db_or_skip

_DDL = Path(__file__).resolve().parents[1] / "schema" / "result-store"


def _record() -> ResultRecord:
    """One calculation carrying one measurement — the smallest thing with a fact in it."""
    return ResultRecord(
        calc_ref="schema-lag-probe",
        calc_type="xtb.sp",
        subject=Subject(
            kind="molecule",
            members=[SubjectMember(ordinal=0, role="subject", smiles="CCO")],
            label="CCO",
        ),
        conditions=Conditions(),
        level=TheoryLevel(method="GFN2-xTB"),
        properties=[
            PropertyFact(
                property="total_energy",
                value=-154.1,
                unit="hartree",
                reported_value=-154.1,
                uncertainty_kind="reported",
                scope="calculation",
            )
        ],
    )


def _sink(schema: str) -> SqlResultSink:
    """The real SQL sink, pointed at one test's own store through the real Postgres driver."""
    return SqlResultSink(
        name="lagprobe",
        tenant_id="test",
        connection={
            "driver": "chemclaw.publish.drivers.postgres:PostgresWarehouse",
            "dsn": settings.postgres_dsn,
            "schema": schema,
        },
    )


async def _build_store(conn: psycopg.AsyncConnection[Any], schema: str, *, seed: bool) -> None:
    """Build one test's own result store from the shipped DDL, optionally seeded.

    A schema per test rather than one shared store, because each of these tests *mutates* the
    schema — dropping a column is the fault under test — and a shared one would make the second
    test's `ALTER` decide the third's outcome. Found exactly that way: the provenance-column test
    failed on a `value_canonical` its own body never dropped.
    """
    await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    await conn.execute(f'CREATE SCHEMA "{schema}"')
    await conn.execute(f'SET search_path = "{schema}"')
    for path in sorted(_DDL.glob("*.sql")):
        await conn.execute(path.read_text(encoding="utf-8"))
    if seed:
        from chemclaw.cli.sink_schema import seed as seed_sql

        await conn.execute(seed_sql())
    await conn.commit()


async def _counts(conn: psycopg.AsyncConnection[Any]) -> tuple[int, int]:
    """`(calculation rows, property_value rows)` — the spine and the facts about it."""
    calculations = await (await conn.execute("SELECT count(*) FROM calculation")).fetchone()
    values = await (await conn.execute("SELECT count(*) FROM property_value")).fetchone()
    return int(calculations[0]), int(values[0])


def test_an_unseeded_store_is_refused_before_a_spine_row_is_written() -> None:
    """Applying the directory alone must fail loudly, not leave calculations with no facts.

    `schema/result-store/` holds the DDL; the registry rows come from `sink_schema --seed`, which
    only `README.md` mentions. A site that applies just the directory gets a store that accepts
    every spine row and refuses every fact row on a foreign key — and the missing-*table* probe
    cannot see it, because every table is there.

    Measured on the unfixed driver: `SinkRejectedError` on the `property_value` foreign key, with
    the far side left holding **`calculation 1 / property_value 0`** — a calculation row a
    `GROUP BY` over the facts reads as a calculation that produced nothing. `SinkRejectedError` is
    permanent, so the retry never completes it and the local row dead-letters saying "not
    published" while that orphan sits there.

    The assertion is therefore about the *residue* as much as the raise.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        conn = await psycopg.AsyncConnection.connect(settings.postgres_dsn)
        try:
            schema = f"{TEST_SCHEMA}_unseeded"
            await _build_store(conn, schema, seed=False)
            sink = _sink(schema)
            with pytest.raises(SinkRejectedError) as refusal:
                await sink.deliver([_record()])
            await sink.aclose()
            assert "--seed" in str(refusal.value), (
                "a bootstrap failure must name the command that fixes it"
            )
            assert await _counts(conn) == (0, 0), (
                "the refusal must come before the first write: the sink writes row-by-row on an "
                "autocommit connection, so a spine row written ahead of the refusal is permanent"
            )
        finally:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await conn.commit()
            await conn.close()

    asyncio.run(_run())


def test_a_store_missing_a_measurement_column_is_refused_rather_than_written_down_to() -> None:
    """A column the value lives in is not an optional column, and its absence is not "not recorded".

    Measured on the unfixed driver against a `property_value` without `value_canonical` and
    `uncertainty`: one WARNING per row, `deliver()` **returned**, the outbox booked `delivered`,
    and the row written said `uncertainty_kind='reported'` while holding no uncertainty and no
    `value_canonical` — the column the DDL calls *"THE predicate column"*, so the store's headline
    range query silently returns nothing for it.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        conn = await psycopg.AsyncConnection.connect(settings.postgres_dsn)
        try:
            schema = f"{TEST_SCHEMA}_novalue"
            await _build_store(conn, schema, seed=True)
            await conn.execute("ALTER TABLE property_value DROP COLUMN value_canonical")
            await conn.execute("ALTER TABLE property_value DROP COLUMN uncertainty")
            await conn.commit()

            sink = _sink(schema)
            with pytest.raises(SinkRejectedError) as refusal:
                await sink.deliver([_record()])
            await sink.aclose()
            assert "value_canonical" in str(refusal.value)
            assert "sink_schema" in str(refusal.value)
            assert await _counts(conn) == (0, 0), "nothing may be written down to a hole"
        finally:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await conn.commit()
            await conn.close()

    asyncio.run(_run())


def test_a_store_missing_only_a_provenance_column_still_publishes() -> None:
    """The additive-migration rule stands: an *optional* column absent is "not recorded".

    This is the case the omission filter exists for, and it must not be collateral damage of the
    two refusals above — a site one release behind on a provenance column must keep publishing its
    science rather than lose all of it.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        conn = await psycopg.AsyncConnection.connect(settings.postgres_dsn)
        try:
            schema = f"{TEST_SCHEMA}_optional"
            await _build_store(conn, schema, seed=True)
            await conn.execute("ALTER TABLE property_value DROP COLUMN in_domain")
            await conn.commit()

            sink = _sink(schema)
            await sink.deliver([_record()])
            await sink.aclose()
            assert await _counts(conn) == (1, 1), (
                "an optional column's absence must cost the row that column, not the row"
            )
        finally:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await conn.commit()
            await conn.close()

    asyncio.run(_run())


def test_the_schema_lag_report_is_once_per_table_not_once_per_row(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A hundred rows behind one migration produced a hundred identical lines per table per pass.

    And none of them was counted — it was a bare `logger.warning`, so nothing alerted on a site
    that had been quietly dropping a column for a month. `degraded()` counts first, then logs.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        conn = await psycopg.AsyncConnection.connect(settings.postgres_dsn)
        try:
            schema = f"{TEST_SCHEMA}_flood"
            await _build_store(conn, schema, seed=True)
            await conn.execute("ALTER TABLE property_value DROP COLUMN in_domain")
            await conn.commit()

            sink = _sink(schema)
            with caplog.at_level("WARNING"):
                for ordinal in range(5):
                    record = _record().model_copy(update={"calc_ref": f"lag-{ordinal}"})
                    await sink.deliver([record])
            await sink.aclose()
        finally:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await conn.commit()
            await conn.close()

        lag_lines = [r for r in caplog.records if "in_domain" in r.getMessage()]
        assert len(lag_lines) == 1, (
            f"the schema lag was reported {len(lag_lines)} times for one sink and one table; at "
            "result_publish_batch_size=100 that is a log flood, not a signal"
        )

    asyncio.run(_run())
