"""Integration tests for the durable job-record store (`infra/sql/023_job_records.sql`).

Against a real database (skips without one): the nested result JSON round-trips, a re-run of the
same job id updates its row rather than forking it, and search finds a past run by its reason.
"""

import asyncio

import pytest

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


async def test_a_campaigns_whole_history_survives_the_round_trip() -> None:
    """The point of the table: what Temporal's expiring history was the only copy of."""
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


async def test_re_running_a_job_updates_its_row_rather_than_forking_it() -> None:
    """The job id is the idempotency key, and an activity is at-least-once: one run, one row."""
    sink = await _sink_or_skip()
    await sink.record(_CAMPAIGN)
    await sink.record(
        _CAMPAIGN.model_copy(update={"summary": "re-run after the objective was fixed"})
    )

    stored = await read_job_record("pg-bo-campaign-1")
    assert stored is not None
    assert stored.summary == "re-run after the objective was fixed"
    matches = (await read_job_record_summaries("", "bo", 50)).hits
    assert [m.job_id for m in matches].count("pg-bo-campaign-1") == 1


async def test_the_plan_step_survives_the_round_trip_and_reaches_the_listing() -> None:
    """The job↔step join (D-2026-08-27): the record keeps both halves, the summary shows the step.

    The listing carries `plan_step` so "which step was this run for" needs no second lookup;
    `plan_hash` stays on the full record, where a reader matching a superseded plan revision goes.
    """
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
    summaries = (await read_job_record_summaries("", "bo", 50)).hits
    by_id = {s.job_id: s for s in summaries}
    assert by_id["pg-plan-step-1"].plan_step == "run the optimization campaign"


async def test_a_past_run_is_found_by_the_reason_it_was_run() -> None:
    """The retrospective question is "why did we do this", so the reason has to be searchable."""
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

    by_reason = (await read_job_record_summaries("dissolves the amine", "", 50)).hits
    assert [m.job_id for m in by_reason] == ["pg-bo-campaign-1"]
    # A listing carries the reason itself, so a hit is recognisable without a second lookup.
    assert by_reason[0].rationale.startswith("the Tuesday batch stalled")

    by_connector = (await read_job_record_summaries("", "calc", 50)).hits
    assert [m.job_id for m in by_connector] == ["pg-qm-barrier-1"]

    # Both filters empty = the recent runs, newest first, bounded by the limit.
    recent = (await read_job_record_summaries("", "", 1)).hits
    assert len(recent) == 1


def test_the_search_is_a_substring_search_and_the_index_serves_that_predicate() -> None:
    """The search is a substring search, and the trigram indexes serve that predicate.

    `_SEARCH` is a leading-wildcard `ILIKE` over `rationale`, `summary` and `job`, which a btree
    cannot serve, so a miss would scan the table holding a pool connection; `gin_trgm_ops`
    accelerates the same predicate. The four cases pin the semantics a `tsvector` rewrite would
    break: a match inside a word, a contiguous phrase, case insensitivity, and a two-word query that
    is not two terms. The index is checked in the catalog, since a fixture-scale plan is a
    sequential scan regardless.
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
            [m.job_id for m in (await read_job_record_summaries("morph", "", 50)).hits],
            # A phrase is contiguous: these three words all appear, in this order, apart.
            [
                m.job_id
                for m in (await read_job_record_summaries("screen antisolvent", "", 50)).hits
            ],
            # Case-insensitive, which is the `I` in ILIKE and not a property of the index.
            [m.job_id for m in (await read_job_record_summaries("POLYMORPH SCREEN", "", 50)).hits],
            # A miss stays a miss — the case that used to cost a full table read.
            [
                m.job_id
                for m in (await read_job_record_summaries("no such run anywhere", "", 50)).hits
            ],
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


async def test_an_unknown_job_id_reads_as_absent_rather_than_raising() -> None:
    """`get_durable_job_status` distinguishes "expired" from "never existed" on this answer."""
    await _sink_or_skip()
    assert await read_job_record("pg-no-such-job") is None


async def test_a_second_run_under_one_id_does_not_keep_the_first_runs_attribution() -> None:
    """A second run under one id does not keep the first run's attribution.

    After Temporal expires an execution, the identical payload re-derives the same id and upserts,
    so `requested_by`, `session_id` and `correlation_id` must be refreshed with the rest.
    """
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


async def test_a_second_failed_template_run_states_its_own_steps_and_not_the_first_runs() -> None:
    """A second failed template run states its own steps, not the first run's.

    `template_job.failed_template_record` deliberately fills `result` with the steps that ran, and
    failed templates are re-run under `ALLOW_DUPLICATE_FAILED_ONLY`, so the failure upsert must
    refresh them. Both halves asserted: the second run's steps land and the first run's are gone.
    """
    sink = await _sink_or_skip()
    first = JobRecord(
        job_id="pg-failed-template-1",
        connector="template",
        job="hazard-briefing",
        requested_by="oid-alice",
        payload={"smiles": "run-1"},
        result={"steps": {"screen": "run1-screen", "write": "run1-write"}},
        payload_kind="template",
        state="failed",
        failure_reason="step 'review': boom-1",
    )
    await sink.record(first)
    await sink.record(
        first.model_copy(
            update={
                "requested_by": "oid-bob",
                "payload": {"smiles": "run-2"},
                "result": {"steps": {"screen": "run2-screen"}},
                "failure_reason": "step 'write': boom-2",
            }
        )
    )

    stored = await read_job_record("pg-failed-template-1")
    assert stored is not None
    assert stored.requested_by == "oid-bob"
    assert stored.failure_reason == "step 'write': boom-2"
    assert stored.result == {"steps": {"screen": "run2-screen"}}, (
        "the row states run 2's actor and failing step beside run 1's step results — a run "
        "that never produced them"
    )


async def test_a_failure_that_produced_nothing_still_never_erases_a_landed_result() -> None:
    """A failure that produced nothing never erases a landed result.

    `_record_run` can commit and then overrun its timeout; `failed_job_record` fills none of the
    five result columns, so it must refresh none. The same decision as the test above, from the
    other end.
    """
    sink = await _sink_or_skip()
    await sink.record(_CAMPAIGN.model_copy(update={"job_id": "pg-failure-over-result-1"}))
    await sink.record(
        JobRecord(
            job_id="pg-failure-over-result-1",
            connector="bo",
            job="start_optimization_campaign",
            requested_by="oid-42",
            state="failed",
            failure_reason="ValueError: the campaign blew up after recording",
        )
    )

    stored = await read_job_record("pg-failure-over-result-1")
    assert stored is not None
    assert stored.state == "failed"
    assert stored.failure_reason.startswith("ValueError:")
    assert stored.result == _CAMPAIGN.result, "the bookkeeping erased the science"
    assert stored.summary == _CAMPAIGN.summary
    assert stored.note_id == _CAMPAIGN.note_id


async def test_a_capped_search_says_it_was_capped_and_can_be_paged_past() -> None:
    """A capped search says it was capped and can be paged past.

    A full page says its count is a floor, and `after` reaches what it cut off. The keyset is the
    last row's `job_id`, so two runs landing in one `now()` are neither repeated nor skipped.
    """
    sink = await _sink_or_skip()
    for i in range(25):
        await sink.record(
            JobRecord(
                job_id=f"pg-page-{i:03d}",
                connector="bo",
                job="start_optimization_campaign",
                rationale=f"Suzuki coupling screen, round {i}",
                requested_by="oid-1",
                payload={"i": i},
                summary=f"campaign {i} finished",
            )
        )

    first = await read_job_record_summaries("Suzuki coupling", "", 10)
    assert len(first.hits) == 10
    assert first.hits_truncated is True, "a page that filled must say the count is a floor"
    assert "floor" in first.verdict

    seen = [hit.job_id for hit in first.hits]
    page = first
    while page.hits_truncated:
        page = await read_job_record_summaries(
            "Suzuki coupling", "", 10, after=page.hits[-1].job_id
        )
        seen.extend(hit.job_id for hit in page.hits)
    # Every row reached exactly once: no repeats across the boundary, nothing skipped.
    assert len(seen) == len(set(seen)) == 25
    assert page.hits_truncated is False


async def test_a_search_that_fits_is_not_reported_as_truncated() -> None:
    """The flag must be evidence, not decoration: an exact-fit page is complete, and says so."""
    sink = await _sink_or_skip()
    for i in range(3):
        await sink.record(
            JobRecord(
                job_id=f"pg-exact-{i}",
                connector="calc",
                job="sample_conformers",
                rationale=f"exact fit probe {i}",
                requested_by="oid-1",
                summary="done",
            )
        )
    found = await read_job_record_summaries("exact fit probe", "", 3)
    assert len(found.hits) == 3
    assert found.hits_truncated is False
    assert "floor" not in found.verdict


async def test_the_record_is_built_from_the_columns_by_name_and_not_by_their_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The record is built from the columns by name, not by their order.

    Many projected columns are `TEXT`, so positional reads would shift values silently. `class_row`
    passes each column as a keyword; a hostile column order must yield the same `JobRecord`.
    """
    from chemclaw.durable import job_record_store

    sink = await _sink_or_skip()
    await sink.record(_CAMPAIGN)
    straight = await read_job_record("pg-bo-campaign-1")

    columns = [name.strip() for name in job_record_store._COLUMNS.split(",")]
    reversed_list = ", ".join(reversed([*columns, "completed_at"]))
    monkeypatch.setattr(
        job_record_store,
        "_SELECT_ONE",
        f"SELECT {reversed_list} FROM job_records WHERE job_id = %s",
    )
    scrambled = await read_job_record("pg-bo-campaign-1")

    assert straight is not None and scrambled == straight, (
        "the column order must not be able to decide which field a value lands in"
    )


async def test_a_column_the_record_has_no_field_for_is_an_error_at_the_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A column the record has no field for is an error at the read.

    `JobRecord` is `extra="forbid"`, so a column added to `_COLUMNS` and not the model raises naming
    it instead of vanishing.
    """
    import pydantic

    from chemclaw.durable import job_record_store

    sink = await _sink_or_skip()
    await sink.record(_CAMPAIGN)
    monkeypatch.setattr(
        job_record_store,
        "_SELECT_ONE",
        f"SELECT {job_record_store._COLUMNS}, completed_at, session_id AS surplus "
        "FROM job_records WHERE job_id = %s",
    )
    with pytest.raises(pydantic.ValidationError, match="surplus"):
        await read_job_record("pg-bo-campaign-1")
