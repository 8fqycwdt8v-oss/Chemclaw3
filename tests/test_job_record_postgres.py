"""Integration tests for the durable job-record store (D-157, `infra/sql/023_job_records.sql`).

Runs against a real database (CI provides Postgres; the offline sandbox has none, so these skip).
What is proven here is what only a database can prove: the round-trip keeps the nested result JSON
intact, a re-run of the same job id updates its row rather than forking it, and the search finds a
past run by the words a chemist would actually remember — the *reason* it was run.
"""

import asyncio

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
