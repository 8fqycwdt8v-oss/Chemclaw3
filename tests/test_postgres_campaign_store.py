"""Integration tests for the Postgres BO campaign store (`infra/sql/031_bo_campaigns.sql`, R1.5).

`PostgresCampaignStore` had no direct test: `test_bo_campaign_record.py` exercises the *contract*
(`CampaignStore` Protocol) entirely through `InMemoryCampaignStore`, which is correct for pinning
the identity and append-only design, but it means the durable backend an actual deployment runs —
the upsert that keeps the original opener, the append-only suggestion sequence, the `LIMIT`/`ORDER
BY` a resumed session relies on — has never touched a database in a test.

Follows `tests/test_postgres_store.py`'s pattern: `migrated_db_or_skip()` skips cleanly offline and
runs for real in CI; each test is a sync `def` wrapping an inner `async def _run()` driven by
`asyncio.run`; isolation comes from the session-scoped schema redirect in `conftest.py`, with a
distinct `campaign_id` per test on top so tests sharing one schema cannot see each other's rows.
"""

import asyncio

from chemclaw.science.bo.campaign_record import Campaign, Suggestion
from chemclaw.science.bo.campaign_record_store import PostgresCampaignStore
from chemclaw.science.bo.problem import Candidate, Observation
from tests.pg import migrated_db_or_skip


async def _store_or_skip() -> PostgresCampaignStore:
    """Return a migrated Postgres campaign store, or skip if no database is reachable."""
    await migrated_db_or_skip()
    return PostgresCampaignStore()


def _campaign(campaign_id: str, opened_by: str = "chemist-a") -> Campaign:
    """A minimal campaign row for `campaign_id`."""
    return Campaign(
        campaign_id=campaign_id,
        objective="yield",
        direction="maximize",
        problem={"parameters": [], "objective": {"name": "yield", "direction": "maximize"}},
        opened_by=opened_by,
    )


def test_read_campaign_miss_returns_none() -> None:
    """A campaign id nothing ever wrote answers `None`, matching the in-memory backend."""

    async def _run() -> None:
        store = await _store_or_skip()
        assert await store.read_campaign("pgcamp-never-written") is None

    asyncio.run(_run())


def test_upsert_then_read_round_trips_every_field() -> None:
    """The row read back must carry exactly what was written, plus what Postgres stamps."""

    async def _run() -> None:
        store = await _store_or_skip()
        campaign_id = "pgcamp-roundtrip-1"
        await store.record(_campaign(campaign_id), Suggestion(campaign_id=campaign_id))

        got = await store.read_campaign(campaign_id)
        assert got is not None
        assert got.campaign_id == campaign_id
        assert (got.objective, got.direction, got.opened_by) == ("yield", "maximize", "chemist-a")
        assert got.created_at is not None
        assert got.last_asked_at is not None

    asyncio.run(_run())


def test_a_second_upsert_keeps_the_original_opener() -> None:
    """Whoever framed the campaign framed it; a later asker refreshes activity, not authorship.

    Mirrors `test_a_later_asker_does_not_become_the_campaign_s_author` in
    `test_bo_campaign_record.py`, against the durable backend that test never touches.
    """

    async def _run() -> None:
        store = await _store_or_skip()
        campaign_id = "pgcamp-opener-1"
        await store.record(
            _campaign(campaign_id, opened_by="chemist-a"), Suggestion(campaign_id=campaign_id)
        )
        first = await store.read_campaign(campaign_id)

        await store.record(
            _campaign(campaign_id, opened_by="chemist-b"), Suggestion(campaign_id=campaign_id)
        )
        second = await store.read_campaign(campaign_id)

        assert first is not None and second is not None
        assert second.opened_by == "chemist-a"
        assert second.created_at == first.created_at
        assert second.last_asked_at is not None and first.last_asked_at is not None
        assert second.last_asked_at >= first.last_asked_at

    asyncio.run(_run())


def test_record_is_append_only_and_returns_increasing_ids() -> None:
    """A second suggestion is a new row, never an edit of the first (031's design)."""

    async def _run() -> None:
        store = await _store_or_skip()
        campaign_id = "pgcamp-append-1"
        # No separate campaign-creation call: `record` upserts the campaign as part of the same
        # transaction, so a setup line here would be a third suggestion row, not a fixture.
        first_id, _ = await store.record(
            _campaign(campaign_id), Suggestion(campaign_id=campaign_id)
        )
        second_id, _ = await store.record(
            _campaign(campaign_id), Suggestion(campaign_id=campaign_id)
        )

        assert first_id != second_id
        assert len(await store.suggestions_for(campaign_id, 10)) == 2

    asyncio.run(_run())


def test_a_suggestion_round_trips_its_candidates_observations_and_provenance() -> None:
    """The JSONB columns must give back the same structures, not merely the same row count."""

    async def _run() -> None:
        store = await _store_or_skip()
        campaign_id = "pgcamp-fields-1"
        await store.record(
            _campaign(campaign_id),
            Suggestion(
                campaign_id=campaign_id,
                candidates=[Candidate(params={"temperature": 95.0, "ligand": "dppf"})],
                observations=[
                    Observation(params={"temperature": 40.0, "ligand": "PPh3"}, value=55.0)
                ],
                calc_refs=["xtb@v1:aaa:bbb"],
                actor="chemist-a",
                session_id="session-7",
                correlation_id="corr-9",
            ),
        )

        [suggestion] = await store.suggestions_for(campaign_id, 10)
        assert suggestion.candidates[0].params["temperature"] == 95.0
        assert suggestion.observations[0].value == 55.0
        assert suggestion.calc_refs == ["xtb@v1:aaa:bbb"]
        assert (suggestion.actor, suggestion.session_id, suggestion.correlation_id) == (
            "chemist-a",
            "session-7",
            "corr-9",
        )
        assert suggestion.proposed_at is not None

    asyncio.run(_run())


def test_suggestions_for_orders_newest_first_and_honours_the_limit() -> None:
    """A resumed session reads the *latest* evidence, and only as much of it as it asked for."""

    async def _run() -> None:
        store = await _store_or_skip()
        campaign_id = "pgcamp-order-1"
        for round_index in range(5):
            await store.record(
                _campaign(campaign_id),
                Suggestion(
                    campaign_id=campaign_id,
                    observations=[
                        Observation(params={"temperature": float(round_index)}, value=1.0)
                    ],
                ),
            )

        newest_two = await store.suggestions_for(campaign_id, 2)
        assert [s.observations[0].params["temperature"] for s in newest_two] == [4.0, 3.0]

        every = await store.suggestions_for(campaign_id, 100)
        assert [s.observations[0].params["temperature"] for s in every] == [
            4.0,
            3.0,
            2.0,
            1.0,
            0.0,
        ]

    asyncio.run(_run())


def test_distinct_campaigns_keep_independent_suggestion_histories() -> None:
    """One campaign's suggestions must never leak into another's `suggestions_for`."""

    async def _run() -> None:
        store = await _store_or_skip()
        a, b = "pgcamp-distinct-a", "pgcamp-distinct-b"

        await store.record(_campaign(a), Suggestion(campaign_id=a, calc_refs=["from-a"]))
        await store.record(_campaign(b), Suggestion(campaign_id=b, calc_refs=["from-b"]))
        await store.record(_campaign(b), Suggestion(campaign_id=b, calc_refs=["from-b-again"]))

        [only_a] = await store.suggestions_for(a, 10)
        both_b = await store.suggestions_for(b, 10)

        assert only_a.calc_refs == ["from-a"]
        assert len(both_b) == 2
        assert {s.calc_refs[0] for s in both_b} == {"from-b", "from-b-again"}

    asyncio.run(_run())


def test_a_retried_durable_write_hits_the_unique_index_instead_of_appending() -> None:
    """The idempotency the durable path needs, against the index that actually enforces it.

    `bo_suggestions_job_idx` is where this lives — the in-memory backend implements the same rule
    in Python, but a partial unique index and an `ON CONFLICT ... WHERE` inference are exactly the
    kind of thing that is right in prose and wrong in SQL, so it is asserted here against a real
    database. The retry must be *invisible*, not merely harmless: the caller gets back the id the
    first attempt got.
    """

    async def _run() -> None:
        store = await _store_or_skip()
        campaign_id = "pgcamp-retried-job"
        suggestion = Suggestion(
            campaign_id=campaign_id,
            candidates=[Candidate(params={"t": 70.0})],
            observations=[Observation(params={"t": 65.0}, value=0.8)],
            problem={"parameters": [], "objective": {"name": "yield", "direction": "maximize"}},
            job_id="bo-start_optimization_campaign-deadbeef",
        )
        first, created = await store.record(_campaign(campaign_id), suggestion)
        again, created_again = await store.record(_campaign(campaign_id), suggestion)
        assert again == first, "a retry must return the id the first attempt got, not a new row"
        # The other half of the same write, and it must *not* be idempotent in the same direction:
        # the first call created the campaign row and the retry found it, which is precisely the
        # signal `suggest_next_experiment` reports as `opened_new_campaign`. Reading it off a
        # `SELECT` before the write could not distinguish these two calls under concurrency.
        assert (created, created_again) == (True, False)
        assert len(await store.suggestions_for(campaign_id, limit=10)) == 1

        other_run = suggestion.model_copy(update={"job_id": "bo-start_optimization_campaign-cafe"})
        assert await store.record(_campaign(campaign_id), other_run) != first
        assert len(await store.suggestions_for(campaign_id, limit=10)) == 2

    asyncio.run(_run())


def test_the_inline_path_keeps_appending_because_its_run_id_is_empty() -> None:
    """The index is partial for this reason: a shared `''` would collapse a campaign's history.

    Two inline suggestions are two entries — "the sequence *is* the campaign's history" — and a
    unique index that did not exclude the empty run id would silently make the second one vanish.
    """

    async def _run() -> None:
        store = await _store_or_skip()
        campaign_id = "pgcamp-inline-appends"
        suggestion = Suggestion(campaign_id=campaign_id, observations=[])
        first = await store.record(_campaign(campaign_id), suggestion)
        second = await store.record(_campaign(campaign_id), suggestion)
        assert second != first
        assert len(await store.suggestions_for(campaign_id, limit=10)) == 2

    asyncio.run(_run())


def test_a_suggestion_round_trips_the_space_it_was_proposed_against() -> None:
    """The snapshot column, read back off a real row rather than out of the model's default."""

    async def _run() -> None:
        store = await _store_or_skip()
        campaign_id = "pgcamp-problem-snapshot"
        space = {"parameters": [], "objective": {"name": "yield", "direction": "maximize"}}
        await store.record(
            _campaign(campaign_id),
            Suggestion(campaign_id=campaign_id, problem=space, job_id="bo-job-snapshot"),
        )
        (recorded,) = await store.suggestions_for(campaign_id, limit=1)
        assert recorded.problem == space
        assert recorded.job_id == "bo-job-snapshot"

    asyncio.run(_run())
