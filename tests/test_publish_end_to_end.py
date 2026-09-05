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

import asyncio
from typing import Any

import psycopg
import pytest

from chemclaw.core.config import settings
from chemclaw.publish import outbox
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


def test_a_composite_reaches_an_external_database_and_answers_a_question(
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

    async def _run() -> None:
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
                actor="chemist@example.com", job_id="job-e2e-1", rationale="which solvent"
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
                conn, "SELECT actor, tenant_id FROM calculation_publication LIMIT 1"
            )
            assert publication[0] == ("chemist@example.com", "site-a"), (
                "who ran it and under which deployment belongs on the publication row, not the "
                "calculation — two chemists running one calculation share its calc_ref"
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

    asyncio.run(_run())


async def _rows(conn: psycopg.AsyncConnection[Any], sql: str) -> list[Any]:
    """Run one question and return its rows."""
    cursor = await conn.execute(sql)
    return list(await cursor.fetchall())


def test_a_same_named_table_in_another_schema_does_not_decide_the_columns(
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

    async def _run() -> None:
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

    asyncio.run(_run())


def test_a_schema_cannot_smuggle_a_second_libpq_option_past_the_timeout_bound() -> None:
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

    async def _run() -> None:
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

    asyncio.run(_run())


def test_a_missing_table_is_probed_once_per_pass_not_once_per_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A site that has not run the DDL must cost one probe, not one per record per attempt.

    `_known_columns` caches its answer on `self._columns` **only on success**, and a target missing
    a table raises before that assignment. The drain then replays the batch record by record — the
    right behaviour for a refusal about *one* record, and this refusal is about all of them — so
    every record re-probed `information_schema`. Measured against a target with no tables:
    **48 round trips for 5 rows**, and that was one attempt of the eight the row used to get.

    Both halves are fixed here. The refusal is remembered for the sink's lifetime, which is one
    drain pass (the drain builds a sink per run so a DBA who applies the DDL is picked up on the
    next pass, not the next restart); and a `SinkRejectedError` no longer spends the retry budget
    at all, so what used to be 8 attempts x 48 probes is one probe.
    """
    from chemclaw.durable import publish_results
    from chemclaw.publish.drivers.postgres import PostgresWarehouse
    from chemclaw.publish.drivers.sql import SqlResultSink

    probes: list[str] = []

    async def _run() -> None:
        await migrated_db_or_skip()
        dsn = settings.postgres_dsn
        monkeypatch.setattr(outbox, "publishing_enabled", lambda: True)
        monkeypatch.setattr(outbox, "enabled_names", lambda: ["nostore"])
        async with outbox._connect("test_fixture") as conn:
            await conn.execute("DELETE FROM result_publications")
            await conn.commit()
        for index in range(5):
            await outbox.enqueue_payload(
                calc_ref=f"job-missing-{index}",
                calc_type="calc.compare_solvents",
                payload_kind="SolventComparisonResult",
                payload=_screen().model_dump(mode="json"),
            )

        sink = SqlResultSink(
            name="nostore",
            tenant_id="site-a",
            connection={
                "driver": "chemclaw.publish.drivers.postgres:PostgresWarehouse",
                "dsn": dsn,
                # An empty schema the DDL was never applied to: every table is missing.
                "schema": "chemclaw_no_result_store",
            },
        )
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            await conn.execute("CREATE SCHEMA IF NOT EXISTS chemclaw_no_result_store")

        # Counted at the driver, so what is measured is *round trips to the database* rather than
        # calls to a method that may answer from its own memory.
        cursor_factory = PostgresWarehouse.cursor

        def _counting(self: Any) -> Any:
            probes.append("cursor")
            return cursor_factory(self)

        monkeypatch.setattr(PostgresWarehouse, "cursor", _counting)
        outcome = await publish_results._drain_one("nostore", sink, 50)
        await sink.aclose()

        # Five queued screens decompose into three records each — the aggregate and its two parts.
        assert outcome.failed == 15 and outcome.delivered == 0
        assert len(probes) == 1, (
            "one probe for the pass: the refusal must be remembered rather than re-asked of the "
            f"database once per replayed record, got {len(probes)}"
        )
        async with outbox._connect("test_fixture") as conn:
            cursor = await conn.execute(
                "SELECT DISTINCT state, attempts FROM result_publications WHERE sink = 'nostore'"
            )
            assert await cursor.fetchall() == [("failed", 1)], (
                "a sink that has answered about this content answers identically forever"
            )

    asyncio.run(_run())


def test_the_connected_preflight_finds_registry_drift_the_manifest_check_cannot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A property this release publishes that the site's registry does not hold.

    The one class of drift with no detector anywhere. `property_value.property` REFERENCES
    `property_definition`, and those rows come from a **separate manual command**
    (`cli/sink_schema.py --seed`) that a DBA runs by hand — while `publish/dialect.definition_for`
    checks only the *local* registry. So a release that adds a property publishes rows the site's
    foreign key rejects, and nothing notices until the drain dead-letters them: `sink-validate`
    deliberately does not connect, and the sink's own probe reads `information_schema.columns`,
    which says nothing about the rows in a table.

    `--preflight` is a separate, opt-in command precisely because `sink-validate` refusing to
    connect is a deliberate decision, not an oversight.
    """
    from chemclaw.cli.validate_sinks import _preflight_problems
    from chemclaw.publish.drivers.sql import SqlResultSink
    from chemclaw.publish.manifest import ResultSinkManifest

    manifest = ResultSinkManifest(
        name="drifted",
        description="a site one release behind",
        driver="chemclaw.publish.drivers.sql:SqlResultSink",
    )

    async def _run() -> None:
        await migrated_db_or_skip()
        dsn = settings.postgres_dsn
        await _create_store(dsn)
        # The site is one release behind: a property this writer can publish is not seeded.
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            await conn.execute(f"SET search_path={_STORE}")
            await conn.execute(
                "DELETE FROM property_definition WHERE property = 'gibbs_free_energy'"
            )

        monkeypatch.setattr(
            "chemclaw.publish.registry.build",
            lambda _manifest: SqlResultSink(
                name="drifted",
                tenant_id="site-a",
                connection={
                    "driver": "chemclaw.publish.drivers.postgres:PostgresWarehouse",
                    "dsn": dsn,
                    "schema": _STORE,
                },
            ),
        )
        found = await _preflight_problems(manifest)
        assert len(found) == 1, found
        assert "gibbs_free_energy" in found[0]
        assert "sink_schema --seed" in found[0], "the report has to name the command that fixes it"

        # And with the seed applied it is clean, so the check is not simply always loud.
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            from chemclaw.cli.sink_schema import seed

            await conn.execute(f"SET search_path={_STORE}")
            await conn.execute(seed())
        assert await _preflight_problems(manifest) == []

    asyncio.run(_run())
