"""What the SQL sink does about a store that is not the store this release expects.

Driven against a real Postgres running the shipped `schema/result-store/` DDL, for a store with no
registry rows and a store one migration behind. Writing down to the schema found is right for a
column that merely records more, and wrong for a column the value lives in.
"""

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

    One schema per test, because each test mutates its schema and a shared one would let one test's
    `ALTER` decide another's outcome.
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
    # A bare `count(*)` always returns exactly one row; asserting it says so rather than
    # `# type: ignore`, which would also hide a query someone later made conditional.
    assert calculations is not None and values is not None
    return int(calculations[0]), int(values[0])


async def test_an_unseeded_store_is_refused_before_a_spine_row_is_written() -> None:
    """An unseeded store is refused before a spine row is written.

    Applying only the DDL (without `sink_schema --seed`) leaves every fact row failing a foreign key
    while spine rows succeed, stranding a calculation with no facts. The assertion covers the
    residue as well as the raise.
    """
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


async def test_a_store_missing_a_measurement_column_is_refused_rather_than_written_down_to() -> (
    None
):
    """A store missing a measurement column is refused rather than written down to.

    `value_canonical` is the predicate column and `uncertainty` belongs to the value; writing
    without them would book a delivery whose rows no range query can find.
    """
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


async def test_a_store_missing_only_a_provenance_column_still_publishes() -> None:
    """A store missing only a provenance column still publishes.

    An absent optional column means "not recorded", so a site one release behind keeps publishing.
    """
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


async def test_the_schema_lag_report_is_once_per_table_not_once_per_row(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A hundred rows behind one migration produced a hundred identical lines per table per pass.

    And none of them was counted — it was a bare `logger.warning`, so nothing alerted on a site
    that had been quietly dropping a column for a month. `degraded()` counts first, then logs.
    """
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
