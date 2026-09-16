"""A calculation reaches an external database and can be queried out of it again.

**The only test in this suite that assembles the whole path** — projector, outbox, drain, driver
and the shipped DDL — and it exists because assembling it is what found the last two defects.
Everything upstream of here tests one piece against a fixture I chose:

- `test_publish_projection.py` calls `project()` directly, so it was green while nothing called it.
- `test_publish_outbox.py` uses a stub sink, so it was green while the shipped driver could not
  satisfy the shipped sink's own runtime check and every real delivery failed at the connect.

Neither is wrong. Both are blind in the same direction, and this is the file that is not: it builds
`SqlResultSink` over `PostgresWarehouse`, applies `schema/result-store/` to a *second* schema
standing in for a database this system does not own, and asks the questions in SQL.
"""

from typing import Any

import psycopg
import pytest

from chemclaw.core.config import settings
from chemclaw.publish import outbox
from chemclaw.publish.driver import SinkRejectedError
from chemclaw.publish.drivers.sql import SqlResultSink
from chemclaw.science.calc.models import SolventComparisonResult, SolventEffect
from tests.pg import migrated_db_or_skip

# The stand-in for the site's own results database. A separate schema rather than a separate
# database, because the point is that the writer holds no DDL rights on it and names no table this
# repository does not ship — not that it is a separate server.
_STORE = "test_publish_e2e"


async def _create_store(dsn: str) -> None:
    """Apply the shipped DDL and the generated registry seed to a fresh schema.

    Exactly what a site does: `make sink-schema --all`, then apply. Loading them here rather than
    hand-writing a fixture is deliberate — a fixture that drifted from the shipped files would test
    a database nobody deploys.
    """
    from chemclaw.cli.sink_schema import ddl, seed

    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_STORE} CASCADE")
        await conn.execute(f"CREATE SCHEMA {_STORE}")
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute(f"SET search_path={_STORE}")
        await conn.execute(ddl())
        await conn.execute(seed())


def _screen() -> SolventComparisonResult:
    """A solvent comparison — the composite shape, and the one that decomposes.

    THF is named by its **alias** deliberately. `ALPB_SOLVENTS` accepts `thf` and
    `tetrahydrofuran` and the name reaches the calculation key verbatim, so a store that kept the
    given name answers "every reaction in THF" with a confident subset. The query below asks by the
    canonical id, and it only returns this row because the alias table resolved it.
    """
    return SolventComparisonResult(
        reactants=["C=C", "C=CC=C"],
        products=["C1CCCCC1"],
        method="GFN2-xTB",
        temperature_k=298.15,
        level="standard",
        effects=[
            SolventEffect(
                solvent="tetrahydrofuran",
                delta_e_kcal=-38.0,
                delta_h_kcal=-36.0,
                delta_g_kcal=-24.0,
            ),
            SolventEffect(
                solvent="toluene", delta_e_kcal=-40.0, delta_h_kcal=-38.0, delta_g_kcal=-28.9
            ),
        ],
        best_solvent="toluene",
        spread_kcal=4.9,
        uncertainty_kcal=3.0,
    )


async def test_a_composite_reaches_an_external_database_and_answers_a_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enqueue a composite the way a finished job does, drain it, then query it back out.

    Three assertions, each about a claim the seam is built on:

    1. **The composite arrives at all.** Its `calc_type` is `<connector>.<job>`, which matches no
       projector prefix — only the `payload_kind` on the envelope routes it.
    2. **The parts arrive with it**, edged back to the aggregate, so "what was ΔG in THF" is
       answerable and not only "which solvent won".
    3. **The alias resolves**, so the canonical-id query returns a run submitted under another name.
    """
    from chemclaw.durable import publish_results
    from chemclaw.publish.drivers.sql import SqlResultSink
    from chemclaw.publish.record import Publication

    await migrated_db_or_skip()
    dsn = settings.postgres_dsn
    await _create_store(dsn)

    monkeypatch.setattr(outbox, "publishing_enabled", lambda: True)
    monkeypatch.setattr(outbox, "enabled_names", lambda: ["e2e"])
    async with outbox._connect("test_fixture") as conn:
        await conn.execute("DELETE FROM result_publications")
        await conn.commit()

    queued = await outbox.enqueue_payload(
        calc_ref="job-e2e-1",
        calc_type="calc.compare_solvents",
        payload_kind="SolventComparisonResult",
        payload=_screen().model_dump(mode="json"),
        publication=Publication(
            actor="chemist@example.com",
            job_id="job-e2e-1",
            rationale="which solvent",
            note_id="job-result-e2e",
        ),
    )
    assert queued == 3, "the comparison and both of its parts must be queued"

    # **No `writer_version`**, exactly as the shipped `sink.yaml` declares none: the column is
    # asserted below to carry the deployment's revision rather than the empty string.
    sink = SqlResultSink(
        name="e2e",
        tenant_id="site-a",
        connection={
            "driver": "chemclaw.publish.drivers.postgres:PostgresWarehouse",
            "dsn": dsn,
            "schema": _STORE,
        },
    )
    outcome = await publish_results._drain_one("e2e", sink, 50)
    assert outcome.failed == 0, f"delivery failed: {outcome.reason}"
    assert outcome.delivered == 3

    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute(f"SET search_path={_STORE}")
        rows = await _rows(
            conn,
            """
                SELECT pv.solvent_id, pv.value_canonical
                FROM property_value pv
                WHERE pv.property = 'reaction_delta_g'
                ORDER BY pv.value_canonical
                """,
        )
        assert [(row[0], row[1]) for row in rows] == [("toluene", -28.9), ("thf", -24.0)], (
            "both parts must be answerable on their own, and the THF row must have been "
            "resolved from the alias it was submitted under"
        )

        edges = await _rows(
            conn,
            "SELECT calc_ref, depends_on_calc_ref FROM calculation_input ORDER BY calc_ref",
        )
        assert [row[1] for row in edges] == ["job-e2e-1", "job-e2e-1"], (
            "each part must edge back to the aggregate, or the verdict is untraceable"
        )

        stamped = await _rows(conn, "SELECT DISTINCT writer_version FROM calculation")
        assert stamped == [(settings.deployment_revision,)], (
            "which ChemClaw3 wrote the row is what makes 'why is in_domain null for "
            "everything before March' answerable; nothing computed it, so every row said '' "
            "— recorded, and blank"
        )

        publication = await _rows(
            conn, "SELECT actor, tenant_id, note_id FROM calculation_publication LIMIT 1"
        )
        assert publication[0] == ("chemist@example.com", "site-a", "job-result-e2e"), (
            "who ran it, under which deployment, and what note it produced belong on the "
            "publication row rather than on the calculation — two chemists running one "
            "calculation share its calc_ref. The note is the one *structured* link back to "
            "the work, and the seam carried four weaker ones without it "
            "(D-2026-09-13-a-publication-carries-the-link-the-system-already-holds)"
        )

    # Redelivery converges: every key is a content hash, so a second drain writes nothing new.
    async with outbox._connect("test_fixture") as conn:
        await conn.execute("UPDATE result_publications SET state='pending', attempts=0")
        await conn.commit()
    again = await publish_results._drain_one("e2e", sink, 50)
    assert again.failed == 0
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute(f"SET search_path={_STORE}")
        counted = await _rows(conn, "SELECT count(*) FROM calculation")
        assert counted[0][0] == 3, "a redelivery must be a no-op, not a duplicate"
    # The drain closes its sinks; this test drives `_drain_one` directly, so it closes its own.
    await sink.aclose()


async def _rows(conn: psycopg.AsyncConnection[Any], sql: str) -> list[Any]:
    """Run one question and return its rows."""
    cursor = await conn.execute(sql)
    return list(await cursor.fetchall())


async def test_a_same_named_table_in_another_schema_does_not_decide_the_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe asks `information_schema` by table *name*; the writes go through `search_path`.

    Unqualified, the two do not agree about which table they are talking about the moment the
    target database holds a same-named relation in another schema the runtime role can see — an
    archive of last year's shape, a staging copy, a second tenant. `found["calculation"]` becomes
    the *union*, so the writer keeps a column the site's own table does not have and every
    `calculation` row is refused by the server. The mirror case is worse: the DDL applied to a
    schema that is not on `search_path` makes the "the target has no …" guard pass while every
    write fails.

    Reproduced here as the realistic half — a site one release behind, beside an archive schema
    that still carries the column it dropped.
    """
    from chemclaw.publish.drivers.sql import SqlResultSink
    from chemclaw.publish.project import project

    other = f"{_STORE}_archive"

    await migrated_db_or_skip()
    dsn = settings.postgres_dsn
    await _create_store(dsn)
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        # The site is one release behind: its `calculation` has no `compute_seconds`.
        await conn.execute(f"ALTER TABLE {_STORE}.calculation DROP COLUMN compute_seconds")
        # And an archive schema, visible to the same role, still does.
        await conn.execute(f"DROP SCHEMA IF EXISTS {other} CASCADE")
        await conn.execute(f"CREATE SCHEMA {other}")
        await conn.execute(
            f"CREATE TABLE {other}.calculation "
            "(calc_ref VARCHAR(512) PRIMARY KEY, compute_seconds DOUBLE PRECISION)"
        )

    record = project(
        calc_ref="probe-1",
        calc_type="reaction.solvent_screen",
        payload=_screen().model_dump(mode="json"),
        payload_kind="SolventComparisonResult",
        compute_seconds=12.5,
    )
    sink = SqlResultSink(
        name="probe",
        tenant_id="site-a",
        connection={
            "driver": "chemclaw.publish.drivers.postgres:PostgresWarehouse",
            "dsn": dsn,
            "schema": _STORE,
        },
    )
    try:
        # Must not raise: the probe has to be qualified by the same schema the writes are.
        await sink.deliver([record])
    finally:
        await sink.aclose()

    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute(f"SET search_path={_STORE}")
        landed = await _rows(conn, "SELECT calc_ref FROM calculation")
        assert landed == [("probe-1",)]
        await conn.execute(f"DROP SCHEMA IF EXISTS {other} CASCADE")


async def test_a_schema_cannot_smuggle_a_second_libpq_option_past_the_timeout_bound() -> None:
    """The `schema:` a manifest writes reaches libpq's `options`, so it is an identifier or nothing.

    `PostgresWarehouse.__init__` range-checks `query_timeout_seconds` three lines before it builds
    the options string, and its comment says why: `statement_timeout=0` is Postgres' spelling of
    *no* timeout, so the check exists specifically to keep that value out. libpq splits `options`
    on whitespace and the **last** `-c` wins, so a `schema` carrying a space set the very value the
    check refuses — measured against this server before the fix:

        options='-c statement_timeout=60000 -c search_path=public -c statement_timeout=0'
        SHOW statement_timeout -> '0'

    Both directions are asserted live rather than by reading the options string, because the string
    is not the control: what the *server* ends up with is. Every other field of a `connection:`
    block is checked — `_env` names against `check_env_name`, the whole block against the driver's
    signature, every binding identifier against `check_identifier` — and this was the one that
    reaches a process argument rather than a statement.
    """
    from chemclaw.publish.connect import SinkConnectionError
    from chemclaw.publish.drivers.postgres import PostgresWarehouse

    await migrated_db_or_skip()
    dsn = settings.postgres_dsn

    with pytest.raises(SinkConnectionError, match="plain SQL identifier"):
        PostgresWarehouse(dsn=dsn, schema="public -c statement_timeout=0")

    # The legitimate path still reaches the server with the timeout the driver declared, which
    # is what makes the refusal above a narrowing rather than a breakage.
    benign = PostgresWarehouse(dsn=dsn, schema="public", query_timeout_seconds=60)
    try:
        async with benign.cursor() as cursor:
            await cursor.execute("SHOW statement_timeout", [])
            assert await cursor.fetchall() == [{"statement_timeout": "1min"}]
    finally:
        await benign.aclose()


async def test_the_seeded_no_conditions_row_is_the_one_the_writer_points_at() -> None:
    """The seed must name the id the projector derives, or it seeds a row nothing joins to.

    `condition_set` is content-addressed like every other key here: a calculator with no conditions
    of its own publishes `Conditions()`, whose `condition_id` is a hash. The seed wrote the literal
    `cond_unspecified` while the DDL comment told consumers that row is "the condition set every
    calculator with no conditions of its own points at" — so a consumer following that advice
    joined on a name no calculation has ever carried and got zero rows, forever, with a second
    all-null row sitting beside it under the derived id. Asserted against a database with the
    shipped DDL and seed actually applied, which is what the earlier claim would have survived.
    """
    from chemclaw.publish.record import Conditions

    await migrated_db_or_skip()
    dsn = settings.postgres_dsn
    await _create_store(dsn)

    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute(f"SET search_path={_STORE}")
        seeded = await _rows(conn, "SELECT condition_id FROM condition_set ORDER BY 1")

    assert [row[0] for row in seeded] == [Conditions().condition_id], (
        "the seeded no-conditions row must carry the id `Conditions()` derives, or nothing "
        "the writer publishes ever points at it"
    )


async def test_a_finished_job_publishes_the_note_it_produced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`calculation_publication` recorded four weak links to a run and dropped the structured one.

    That table exists to answer "what question was this meant to answer" — it carries the session,
    the job, the actor, the correlation id and the rationale. `job_records.note_id` holds the note a
    finished connector job's envelope produced, written by `finished_job_record` from the same
    `ConnectorJobResult` that `_publish_result` is handed, and both publish paths had it in hand and
    did not carry it (`D-2026-09-13-a-publication-carries-the-link-the-system-already-holds`).

    Driven through the **real workflow**, on a real broker, because the producer is the thing under
    test: a test that built a `Publication` itself would assert that a field it filled arrives,
    which is true of a field nothing fills. The fixture job returns a note with a known id, so what
    the outbox holds afterwards is either that id or the empty string the seam shipped with.
    """
    from temporalio.worker import UnsandboxedWorkflowRunner, Worker

    from chemclaw.durable.connector_job import ConnectorJobWorkflow
    from chemclaw.durable.job_record import record_job
    from chemclaw.durable.publish_results import publish_job_result
    from tests.temporal_env import pydantic_client, start_local_env_or_skip
    from tests.test_durable_observability import _JOB, _until_not_running

    published: list[Any] = []

    async def _capture(**kwargs: Any) -> int:
        published.append(kwargs["publication"])
        return 1

    from tests.fixtures.connectors.fixture.workflows import FixtureJobWorkflow

    await migrated_db_or_skip()
    monkeypatch.setattr(outbox, "enqueue_payload", _capture)
    async with await start_local_env_or_skip() as env:
        client = pydantic_client(env)
        wrapper = Worker(
            client,
            task_queue=settings.background_task_queue,
            workflows=[ConnectorJobWorkflow],
            activities=[record_job, publish_job_result],
            workflow_runner=UnsandboxedWorkflowRunner(),
        )
        bundle = Worker(client, task_queue="connector-fixture", workflows=[FixtureJobWorkflow])
        async with wrapper, bundle:
            handle = await client.start_workflow(
                ConnectorJobWorkflow.run,
                _JOB.model_copy(
                    update={
                        "workflow": "FixtureJobWorkflow",
                        "task_queue": "connector-fixture",
                        "payload": {"subject": "benzene"},
                        "publish_to_graph": False,
                        "session_id": "",
                    }
                ),
                id="publish-note-probe",
                task_queue=settings.background_task_queue,
            )
            await _until_not_running(handle, timeout=60.0)

    assert published, "the job published nothing at all, so the assertion below proves nothing"
    assert published[0].note_id == "fixture-benzene", (
        "the publication row names the session, the job, the actor and the rationale, and drops "
        "the one structured link to what the run produced"
    )


# --- The drain's round trips -------------------------------------------------------------------
#
# `SqlResultSink.deliver` groups a whole batch into one statement per `(table, column set)` and
# sends each with all of its parameter sets through `execute_many`. That helper only reaches
# psycopg's pipeline when the driver's cursor offers `executemany` — an *optional* capability,
# because `D-2026-08-26-the-driver-s-signature-is-the-schema` means a site brings its own driver
# and this repository cannot require a method of a class it does not ship.
#
# `_CountingCursor` and `_BatchingCursor` below exist to *count* round trips, and nothing else:
# both delegate to the shipped driver's own cursor, which is `_PostgresCursor`. That is the whole
# of this comment's history and the reason it was rewritten — it used to say the batching cursor
# was a copy of `_PostgresCursor` kept here "only because nothing in this tree has claimed that
# file yet", and that file had claimed it since the commit that added it. The copy reached past
# the wrapper into the raw psycopg cursor, so these tests exercised the test's own code and no
# production code: driven, `_PostgresCursor.executemany` could be **deleted** and all 286 publish
# tests still passed, over the method whose own commit message says the batching change was inert
# in production without it.
#
# The three tests below now go through it, so deleting it turns them red — two by the sink
# reaching a cursor that no longer offers the capability, and one by naming it outright.

_ROUND_TRIPS: dict[str, int] = {"execute": 0, "executemany": 0}


class _CountingCursor:
    """A `WarehouseCursor` that records how many times the sink went to the server."""

    def __init__(self, inner: Any) -> None:
        """Wrap the driver's own cursor."""
        self._inner = inner

    async def execute(self, sql: str, params: Any) -> None:
        """One statement, one round trip."""
        _ROUND_TRIPS["execute"] += 1
        await self._inner.execute(sql, params)

    async def fetchall(self) -> list[dict[str, Any]]:
        """Every remaining row of the last statement."""
        rows: list[dict[str, Any]] = await self._inner.fetchall()
        return rows


class _BatchingCursor(_CountingCursor):
    """`_CountingCursor` plus the presence of the one method that makes N statements one round trip.

    It counts and delegates. The JSON adaptation, the pipeline call and the error mapping are all
    `_PostgresCursor.executemany`'s — which is the point: a wrapper that reimplemented them would
    make every assertion below a claim about this file, and that is exactly what it was.

    Declaring the method here is still what decides whether the capability is *offered*, since
    `execute_many` probes a `runtime_checkable` Protocol and so tests member presence on the object
    the sink holds. That is what lets the `batching=False` arm below be the same driver with the
    capability withheld, which is the comparison the round-trip counts rest on.
    """

    async def executemany(self, sql: str, params_seq: Any) -> None:
        """Count the batched round trip and hand it to the shipped driver's own cursor."""
        _ROUND_TRIPS["executemany"] += 1
        await self._inner.executemany(sql, params_seq)


def _warehouse(dsn: str, *, schema: str, batching: bool) -> Any:
    """The shipped Postgres driver, with its cursor counted and optionally able to batch."""
    from contextlib import asynccontextmanager

    from chemclaw.publish.drivers.postgres import PostgresWarehouse

    class _Counted(PostgresWarehouse):
        @asynccontextmanager
        async def cursor(self) -> Any:
            async with super().cursor() as inner:
                yield (_BatchingCursor if batching else _CountingCursor)(inner)

    return _Counted(dsn=dsn, schema=schema)


def _records(count: int) -> list[Any]:
    """`count` solvent comparisons, each a different calculation — a realistic drain batch."""
    from chemclaw.publish.project import project

    out = []
    for index in range(count):
        screen = _screen()
        screen = screen.model_copy(update={"temperature_k": 298.15 + index})
        out.append(
            project(
                calc_ref=f"batch-{index}",
                calc_type="calc.compare_solvents",
                payload=screen.model_dump(mode="json"),
                payload_kind="SolventComparisonResult",
                calc_version="GFN2-xTB",
            )
        )
    return out


async def test_a_batch_is_one_statement_per_table_and_column_set_not_one_per_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same batch, through a driver that can batch and one that cannot, lands the same rows.

    **The round-trip counts are the measurement and the assertion.** Twenty records here are 300
    statements row-at-a-time and 9 batched — nine because `_batches` groups table-major across the
    *whole* batch rather than per record, which is the difference between 9 and 180. On the full
    `result_publish_batch_size` of 100 the same shape measured, over three runs each, 1 500 round
    trips and 5.55-5.68 s row-at-a-time against 9 and 0.34-0.48 s batched on this server; twenty is
    used here because the point is the ratio and a test is not a benchmark.

    Asserted as a bound rather than as an equality, because the exact count is a function of how
    many distinct column sets the projector emits and a new optional column would move it without
    anything being wrong.
    """
    await migrated_db_or_skip()
    dsn = settings.postgres_dsn
    records = _records(20)
    landed: dict[str, list[Any]] = {}
    counts: dict[str, int] = {}

    for label, batching in (("plain", False), ("batching", True)):
        await _create_store(dsn)
        _ROUND_TRIPS.update(execute=0, executemany=0)
        sink = SqlResultSink(
            name="roundtrips",
            tenant_id="site-a",
            connection={
                "driver": "tests.test_publish_end_to_end:_warehouse",
                "dsn": dsn,
                "schema": _STORE,
                "batching": batching,
            },
        )
        try:
            # The schema probe is three statements of its own; only the writes are counted.
            await sink._known_columns(sink._connect())
            _ROUND_TRIPS.update(execute=0, executemany=0)
            await sink.deliver(records)
        finally:
            await sink.aclose()
        counts[label] = _ROUND_TRIPS["execute"] + _ROUND_TRIPS["executemany"]
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            await conn.execute(f"SET search_path={_STORE}")
            landed[label] = await _rows(
                conn,
                "SELECT calc_ref, property, solvent_id, value_canonical FROM property_value "
                "ORDER BY calc_ref, property, solvent_id",
            )

    assert landed["plain"], "the fixture must actually publish something"
    assert landed["batching"] == landed["plain"], (
        "an optional capability that changes what is stored is not an optimisation; the "
        "batched path must be indistinguishable from the row-at-a-time one in the database"
    )
    assert counts["plain"] >= 20 * 10, (
        "the row-at-a-time path is one round trip per row and must stay measured as one, or "
        "the ratio below is a claim about nothing"
    )
    assert counts["batching"] <= 40, (
        f"20 records took {counts['batching']} round trips; the whole point of grouping "
        "table-major across the batch is that the statement count is a function of the "
        "projector's column sets and not of the number of records"
    )
    assert counts["batching"] * 5 < counts["plain"], (
        f"batched {counts['batching']} vs row-at-a-time {counts['plain']}: a drain pass is "
        "order 10^3 round trips and this is the whole reason the change exists"
    )


async def test_the_shipped_postgres_cursor_is_what_offers_the_batching_capability() -> None:
    """`_PostgresCursor` is the object `execute_many` probes, and this is what says so.

    The two tests above count round trips through a wrapper, and a wrapper can be wrong about what
    it wraps: before this, `_BatchingCursor` reached past `_PostgresCursor` into the raw psycopg
    cursor, so the batching *tests* were green over a production method that could be deleted
    outright — 286 publish tests still passed with it gone, over the method whose own commit says
    the batching change was inert in production without it.

    So this one names the class, takes its real cursor, and drives `execute_many` over it: the
    `isinstance` is the same `runtime_checkable` probe `execute_many` performs, and the rows are
    what say the pipeline call actually ran rather than merely being offered. Deleting
    `_PostgresCursor.executemany` fails the probe here and the `execute_many` call in the two tests
    above, which is the point of writing it three times over.
    """
    await migrated_db_or_skip()
    dsn = settings.postgres_dsn
    await _create_store(dsn)

    from chemclaw.ingest.eln.warehouse.driver import BatchingCursor, execute_many
    from chemclaw.publish.drivers.postgres import PostgresWarehouse, _PostgresCursor

    warehouse = PostgresWarehouse(dsn=dsn, schema=_STORE)
    try:
        async with warehouse.cursor() as cursor:
            assert isinstance(cursor, _PostgresCursor), (
                "this test is about the shipped driver's own cursor; it has been given another"
            )
            assert isinstance(cursor, BatchingCursor), (
                "`execute_many` probes this Protocol to decide whether a batch is one round trip "
                "or N; the shipped Postgres driver must be on the batching side of it"
            )
            await execute_many(
                cursor,
                "INSERT INTO solvent (solvent_id, display_name, smiles) VALUES (%s, %s, %s)",
                [(f"s-{index}", f"solvent {index}", "CCO") for index in range(4)],
            )
            # The shipped seed fills this table, so the read is scoped to what this test inserted.
            await cursor.execute(
                "SELECT solvent_id FROM solvent WHERE solvent_id LIKE %s ORDER BY solvent_id",
                ["s-%"],
            )
            landed = [row["solvent_id"] for row in await cursor.fetchall()]
    finally:
        await warehouse.aclose()

    assert landed == ["s-0", "s-1", "s-2", "s-3"], (
        "the batched insert must land every parameter set, through the driver this repository "
        f"ships rather than a test's copy of it; got {landed}"
    )


async def test_a_refused_row_inside_a_batch_is_still_named_with_its_table_and_calc_ref() -> None:
    """A batch shares a failure; the error must not. Driven with one poisoned row in a group.

    `SinkRejectedError` naming the table and the `calc_ref` is what an operator acts on, and it is
    also a retry contract: `durable/publish.py` marks it non-retryable **by class name**, and
    `_drain_one` reads anything that is not a `SinkUnavailableError` as one record's fault and
    replays the batch a record at a time. A group-level message would have kept both behaviours
    and lost the only part of them that identifies the row.

    The poison is a CHECK constraint the test adds, because that is the shape of the real fault
    this arm exists for — a site whose store refuses a value this release writes — and it fails one
    row of a group whose other rows are perfectly good.
    """
    await migrated_db_or_skip()
    dsn = settings.postgres_dsn
    await _create_store(dsn)
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute(
            f"ALTER TABLE {_STORE}.calculation "
            "ADD CONSTRAINT no_poison CHECK (calc_ref <> 'batch-1')"
        )

    records = _records(3)
    for batching in (True, False):
        sink = SqlResultSink(
            name="poison",
            tenant_id="site-a",
            connection={
                "driver": "tests.test_publish_end_to_end:_warehouse",
                "dsn": dsn,
                "schema": _STORE,
                "batching": batching,
            },
        )
        try:
            with pytest.raises(SinkRejectedError) as raised:
                await sink.deliver(records)
        finally:
            await sink.aclose()
        message = str(raised.value)
        assert "calculation" in message and "batch-1" in message, (
            f"the refusal must name the table and the record that caused it; got {message!r}"
        )

    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute(f"SET search_path={_STORE}")
        kept = await _rows(conn, "SELECT calc_ref FROM calculation ORDER BY calc_ref")
        assert kept == [("batch-0",)], (
            "the row-at-a-time replay must write the group's good rows up to the poison, so "
            "the partial state a retry completes is the same one the row-at-a-time writer "
            "left — psycopg rolls a refused `executemany` back whole, which is why the replay "
            "is what puts them there"
        )
