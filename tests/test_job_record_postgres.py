"""Integration tests for the durable job-record store (D-157, `infra/sql/023_job_records.sql`).

Runs against a real database (CI provides Postgres; the offline sandbox has none, so these skip).
What is proven here is what only a database can prove: the round-trip keeps the nested result JSON
intact, a re-run of the same job id updates its row rather than forking it, and the search finds a
past run by the words a chemist would actually remember — the *reason* it was run.
"""

import asyncio

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.durable.job_record import JobRecord
from chemclaw.durable.job_record_store import (
    PostgresJobRecordSink,
    read_job_record,
    read_job_record_summaries,
)
from tests.pg import migrated_db_or_skip

_CAMPAIGN = JobRecord(
    job_id="pg-bo-campaign-1",
    connector="bo",
    job="start_optimization_campaign",
    rationale="the Tuesday batch stalled at 60% — find a solvent that dissolves the amine",
    requested_by="oid-42",
    session_id="sess-7",
    correlation_id="turn-9",
    payload={"objective_name": "solubility_max", "n_rounds": 4},
    summary="campaign finished after 9 evaluation(s)",
    result={"best": {"value": -1.2}, "history": [{"value": -3.0}, {"value": -1.2}]},
    note_id="bo-solubility-max-abc123",
)


async def _sink_or_skip() -> PostgresJobRecordSink:
    """A migrated store, or skip when no database is reachable."""
    await migrated_db_or_skip()
    return PostgresJobRecordSink()


def test_a_campaigns_whole_history_survives_the_round_trip() -> None:
    """The point of the table: what Temporal's expiring history was the only copy of."""

    async def _run() -> None:
        sink = await _sink_or_skip()
        await sink.record(_CAMPAIGN)

        stored = await read_job_record("pg-bo-campaign-1")
        assert stored is not None
        assert stored.rationale == _CAMPAIGN.rationale
        assert stored.payload == {"objective_name": "solubility_max", "n_rounds": 4}
        # Nested JSON, unflattened — every observation the campaign paid for.
        assert stored.result["history"] == [{"value": -3.0}, {"value": -1.2}]
        assert stored.note_id == "bo-solubility-max-abc123"
        assert stored.requested_by == "oid-42" and stored.session_id == "sess-7"
        # Stamped by the database's own clock, so rows order by the same clock that wrote them.
        assert stored.completed_at is not None

    asyncio.run(_run())


def test_re_running_a_job_updates_its_row_rather_than_forking_it() -> None:
    """The job id is the idempotency key, and an activity is at-least-once: one run, one row."""

    async def _run() -> None:
        sink = await _sink_or_skip()
        await sink.record(_CAMPAIGN)
        await sink.record(
            _CAMPAIGN.model_copy(update={"summary": "re-run after the objective was fixed"})
        )

        stored = await read_job_record("pg-bo-campaign-1")
        assert stored is not None
        assert stored.summary == "re-run after the objective was fixed"
        matches = await read_job_record_summaries("", "bo", 50)
        assert [m.job_id for m in matches].count("pg-bo-campaign-1") == 1

    asyncio.run(_run())


def test_the_plan_step_survives_the_round_trip_and_reaches_the_listing() -> None:
    """The job↔step join (D-2026-08-27): the record keeps both halves, the summary shows the step.

    The listing carries `plan_step` so "which step was this run for" needs no second lookup;
    `plan_hash` stays on the full record, where a reader matching a superseded plan revision goes.
    """

    async def _run() -> None:
        sink = await _sink_or_skip()
        stamped = _CAMPAIGN.model_copy(
            update={
                "job_id": "pg-plan-step-1",
                # Its own reason, so the search-by-reason test's term matches exactly one row.
                "rationale": "step two of the approved plan wants the campaign run",
                "plan_step": "run the optimization campaign",
                "plan_hash": "plan-rev-abc",
            }
        )
        await sink.record(stamped)

        stored = await read_job_record("pg-plan-step-1")
        assert stored is not None
        assert stored.plan_step == "run the optimization campaign"
        assert stored.plan_hash == "plan-rev-abc"
        summaries = await read_job_record_summaries("", "bo", 50)
        by_id = {s.job_id: s for s in summaries}
        assert by_id["pg-plan-step-1"].plan_step == "run the optimization campaign"

    asyncio.run(_run())


def test_a_past_run_is_found_by_the_reason_it_was_run() -> None:
    """The retrospective question is "why did we do this", so the reason has to be searchable."""

    async def _run() -> None:
        sink = await _sink_or_skip()
        await sink.record(_CAMPAIGN)
        await sink.record(
            _CAMPAIGN.model_copy(
                update={
                    "job_id": "pg-qm-barrier-1",
                    "connector": "calc",
                    "job": "sample_conformers",
                    "rationale": "the reviewer questioned the reported barrier",
                    "note_id": "",
                }
            )
        )

        by_reason = await read_job_record_summaries("dissolves the amine", "", 50)
        assert [m.job_id for m in by_reason] == ["pg-bo-campaign-1"]
        # A listing carries the reason itself, so a hit is recognisable without a second lookup.
        assert by_reason[0].rationale.startswith("the Tuesday batch stalled")

        by_connector = await read_job_record_summaries("", "calc", 50)
        assert [m.job_id for m in by_connector] == ["pg-qm-barrier-1"]

        # Both filters empty = the recent runs, newest first, bounded by the limit.
        recent = await read_job_record_summaries("", "", 1)
        assert len(recent) == 1

    asyncio.run(_run())


def test_the_search_is_a_substring_search_and_the_index_serves_that_predicate() -> None:
    """What `081` accelerated, and the semantics it must not have changed.

    `_SEARCH` is a leading-wildcard `ILIKE` over `rationale`, `summary` and `job`, and the agent
    calls it. A leading wildcard is unindexable by a btree, so a term that matches *nothing* reads
    the whole table while holding one of `pg_pool_max_size` connections: measured at 500 000 rows
    through psycopg with the shipped statement, 1 036 ms and 19 920 buffers, against 1.09 ms once
    `gin_trgm_ops` indexes are present — and 0.89 ms either way for a term that hits, because the
    `completed_at` index lets a hit stop early. That asymmetry is why nothing ever saw this: an
    agent inventing a phrase produces the miss, and every test and demo produces the hit.

    **Trigrams accelerate the same predicate; a `tsvector` would answer a different question.** The
    docstring of `search_job_records` promises words looked for *in* the reason, the summary or the
    job name, and the four cases below are what that means and what a stemmed, `websearch`-widened
    rewrite would break: a match inside a word, a phrase that must be contiguous, case
    insensitivity, and a two-word query that is not two independent terms. They are asserted
    against the live statement rather than against a plan, because a plan at fixture scale is a
    sequential scan whatever indexes exist — so the index itself is checked in the catalog, where
    the fact is scale-free.
    """

    async def _run() -> tuple[list[list[str]], set[str]]:
        sink = await _sink_or_skip()
        await sink.record(
            _CAMPAIGN.model_copy(
                update={
                    "job_id": "pg-trgm-1",
                    "rationale": "the polymorph screen needs a Class 2 antisolvent",
                    "summary": "3 forms, Form II stable above 40 C",
                }
            )
        )
        found = [
            # A substring inside a word: `ILIKE '%morph%'` matches "polymorph", a stem-based
            # search does not.
            [m.job_id for m in await read_job_record_summaries("morph", "", 50)],
            # A phrase is contiguous: these three words all appear, in this order, apart.
            [m.job_id for m in await read_job_record_summaries("screen antisolvent", "", 50)],
            # Case-insensitive, which is the `I` in ILIKE and not a property of the index.
            [m.job_id for m in await read_job_record_summaries("POLYMORPH SCREEN", "", 50)],
            # A miss stays a miss — the case that used to cost a full table read.
            [m.job_id for m in await read_job_record_summaries("no such run anywhere", "", 50)],
        ]
        async with db.connection(settings.postgres_dsn) as conn:
            cursor = await conn.execute(
                "SELECT a.attname FROM pg_index x "
                "JOIN pg_class i ON i.oid = x.indexrelid "
                "JOIN pg_class t ON t.oid = x.indrelid "
                "JOIN pg_am m ON m.oid = i.relam "
                "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(x.indkey) "
                "WHERE t.relname = 'job_records' AND m.amname = 'gin' "
                "AND t.relnamespace = current_schema()::regnamespace"
            )
            indexed = {str(row[0]) for row in await cursor.fetchall()}
        return found, indexed

    (inside_word, phrase, upper, miss), indexed = asyncio.run(_run())
    assert "pg-trgm-1" in inside_word, "a substring inside a word stopped matching"
    assert phrase == [], "the search matched a phrase whose words are not contiguous"
    assert "pg-trgm-1" in upper, "the search stopped being case-insensitive"
    assert miss == [], "a term nothing carries came back with a hit"
    assert {"rationale", "summary", "job"} <= indexed, (
        "the three searched columns are not all covered by a GIN index, so the OR of three ILIKEs "
        f"cannot be planned as a BitmapOr and a miss scans the table; GIN covers {sorted(indexed)}"
    )


def test_an_unknown_job_id_reads_as_absent_rather_than_raising() -> None:
    """`get_durable_job_status` distinguishes "expired" from "never existed" on this answer."""

    async def _run() -> None:
        await _sink_or_skip()
        assert await read_job_record("pg-no-such-job") is None

    asyncio.run(_run())


def test_a_second_run_under_one_id_does_not_keep_the_first_runs_attribution() -> None:
    """A row must not carry run 2's reason beside run 1's name (review of D-157).

    Reachable, and on exactly the horizon this table exists for: once Temporal has expired an
    execution, the identical payload derives the same workflow id, runs again, and upserts. The
    first version of the upsert refreshed `rationale`, `summary` and `result` but not
    `requested_by`/`session_id`/`correlation_id`, so the row said Bob's question was asked by
    Alice — the worst possible answer for the field an audit joins on.
    """

    async def _run() -> None:
        sink = await _sink_or_skip()
        first = _CAMPAIGN.model_copy(
            update={
                "job_id": "pg-reattributed-1",
                "rationale": "alice: does 2-MeTHF dissolve the amine",
                "requested_by": "oid-alice",
                "session_id": "sess-alice",
                "correlation_id": "turn-alice",
            }
        )
        await sink.record(first)
        await sink.record(
            first.model_copy(
                update={
                    "rationale": "bob: re-run now the objective is fixed",
                    "requested_by": "oid-bob",
                    "session_id": "sess-bob",
                    "correlation_id": "turn-bob",
                    "summary": "re-run",
                    "result": {"best": {"value": -0.4}},
                }
            )
        )

        stored = await read_job_record("pg-reattributed-1")
        assert stored is not None
        # The row is one run's story throughout, not a splice of two.
        assert stored.rationale.startswith("bob:")
        assert stored.requested_by == "oid-bob"
        assert stored.session_id == "sess-bob"
        assert stored.correlation_id == "turn-bob"
        assert stored.result == {"best": {"value": -0.4}}

    asyncio.run(_run())
