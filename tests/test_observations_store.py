"""The observations tier against a real database — the half no pure test can reach (D-161).

`tests/test_observations.py` covers the miners and the model, which is where the domain rules
live. It cannot cover the SQL, and the SQL here is not boilerplate: the upsert accumulates
`evidence_note_ids` and `projects_seen` through correlated `array_agg(DISTINCT …)` subqueries in
its `SET` clause, and the anti-feedback rule is a CHECK constraint. Both are the kind of thing that
is valid Python and wrong SQL, and both are load-bearing — accumulation is what makes support mean
anything across runs, and the constraint is the guarantee behind "an observation can never
corroborate itself".

Skipped where no Postgres is reachable, so this is the offline sandbox's blind spot and CI's job.
"""

import asyncio

import psycopg
import pytest

from chemclaw.core.config import settings
from chemclaw.memory import observations as store
from chemclaw.memory.observations import Observation
from tests.pg import migrated_db_or_skip


async def _clean_db_or_skip() -> None:
    """A migrated database with an empty `observations` table, or skip."""
    await migrated_db_or_skip()
    async with await psycopg.AsyncConnection.connect(settings.postgres_dsn) as conn:
        await conn.execute("DELETE FROM observations")
        await conn.commit()


def _finding(statement: str = "s", **overrides: object) -> Observation:
    """One observation, defaulting to a single-project single-note finding."""
    fields: dict[str, object] = {
        "statement": statement,
        "scope": "transformation:r1",
        "evidence_note_ids": ["reaction-r1"],
        "projects_seen": ["alpha"],
    }
    return Observation(**{**fields, **overrides})  # type: ignore[arg-type]


def test_a_second_sighting_accumulates_support_rather_than_restating_it() -> None:
    """The whole reason support means anything across runs.

    Same finding, different evidence: the row must end up backed by both notes and both projects.
    If the upsert replaced instead of merging, support would measure the last run rather than the
    corpus, and an observation could never cross a promotion threshold at all.
    """

    async def _run() -> None:
        await _clean_db_or_skip()
        await store.record([_finding()])
        await store.record([_finding(evidence_note_ids=["reaction-r2"], projects_seen=["beta"])])

        found = await store.open_observations()
        assert len(found) == 1  # one row, not two — the id is the scope
        assert found[0].evidence_note_ids == ["reaction-r1", "reaction-r2"]
        assert found[0].projects_seen == ["alpha", "beta"]
        assert found[0].support == 2

    asyncio.run(_run())


def test_the_statement_follows_the_evidence_it_accumulated() -> None:
    """A row backed by two projects must not still read as though it were backed by one.

    The statement is the mutable part now that identity is the scope, so the upsert refreshes it.
    Keeping the first run's wording would leave the tier saying one thing and its own evidence
    column saying another — and the statement is what a reviewer reads on a promotion PR.
    """

    async def _run() -> None:
        await _clean_db_or_skip()
        await store.record([_finding("seen in 1 project")])
        await store.record(
            [
                _finding(
                    "seen in 2 projects", evidence_note_ids=["reaction-r2"], projects_seen=["beta"]
                )
            ]
        )

        found = await store.open_observations()
        assert len(found) == 1
        assert found[0].statement == "seen in 2 projects"

    asyncio.run(_run())


def test_re_recording_an_identical_finding_changes_nothing_but_last_seen() -> None:
    """A nightly no-op must stay a no-op, or every run would look like new support."""

    async def _run() -> None:
        await _clean_db_or_skip()
        await store.record([_finding()])
        await store.record([_finding()])

        found = await store.open_observations()
        assert len(found) == 1
        assert found[0].evidence_note_ids == ["reaction-r1"]
        assert found[0].last_seen is not None and found[0].first_seen is not None

    asyncio.run(_run())


def test_the_database_refuses_an_observation_citing_an_observation() -> None:
    """The guarantee, not the courtesy.

    `Observation` refuses this at construction so a miner fails where it is written. That protects
    the path that goes through the model; this protects the *table*, including from a future
    writer that does not. The insert is made deliberately around the validator to prove it.
    """

    async def _run() -> None:
        await _clean_db_or_skip()
        async with await psycopg.AsyncConnection.connect(settings.postgres_dsn) as conn:
            with pytest.raises(psycopg.errors.CheckViolation):
                await conn.execute(
                    "INSERT INTO observations (id, statement, scope, evidence_note_ids, origin) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    ("observation-x", "s", "t", ["observation-y"], "corpus-mining"),
                )

    asyncio.run(_run())


def test_only_a_finding_over_both_thresholds_is_promotable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two thresholds because they answer different questions, and neither alone is enough.

    Ten notes from one project is a well-evidenced *episodic* fact, which the campaign layer
    already covers; two projects with one note each is a coincidence.
    """
    monkeypatch.setattr(settings, "observation_promote_min_evidence", 3)
    monkeypatch.setattr(settings, "observation_promote_min_projects", 2)

    async def _run() -> None:
        await _clean_db_or_skip()
        await store.record(
            [
                # Enough notes, one project.
                _finding(
                    "deep but local",
                    scope="a",
                    evidence_note_ids=["reaction-1", "reaction-2", "reaction-3"],
                    projects_seen=["alpha"],
                ),
                # Enough projects, too few notes.
                _finding(
                    "broad but thin",
                    scope="b",
                    evidence_note_ids=["reaction-4"],
                    projects_seen=["alpha", "beta"],
                ),
                # Both.
                _finding(
                    "real",
                    scope="c",
                    evidence_note_ids=["reaction-5", "reaction-6", "reaction-7"],
                    projects_seen=["alpha", "beta"],
                ),
            ]
        )
        assert [o.statement for o in await store.promotable()] == ["real"]

    asyncio.run(_run())


def test_a_promoted_observation_leaves_the_open_set() -> None:
    """Otherwise it would be re-promoted every night, opening the same PR forever."""

    async def _run() -> None:
        await _clean_db_or_skip()
        await store.record([_finding()])
        observation = (await store.open_observations())[0]

        await store.set_status(observation.id, "promoted")
        assert await store.open_observations() == []
        assert await store.promotable() == []

    asyncio.run(_run())


def test_retirement_spares_what_was_just_re_observed(monkeypatch: pytest.MonkeyPatch) -> None:
    """`last_seen` is refreshed by every run that still finds the finding.

    That is what makes retirement mean "the corpus stopped supporting this" rather than "this is
    old" — a finding the miners keep confirming must never age out from under them.
    """
    monkeypatch.setattr(settings, "observation_retire_after_days", 30)

    async def _run() -> None:
        await _clean_db_or_skip()
        await store.record([_finding()])
        assert await store.retire_stale() == 0  # just recorded, so nothing is stale
        assert len(await store.open_observations()) == 1

    asyncio.run(_run())


def test_retirement_is_off_when_the_window_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment that wants observations to persist indefinitely must be able to say so."""
    monkeypatch.setattr(settings, "observation_retire_after_days", 0)

    async def _run() -> None:
        await _clean_db_or_skip()
        await store.record([_finding()])
        assert await store.retire_stale() == 0
        assert len(await store.open_observations()) == 1

    asyncio.run(_run())


def test_the_best_supported_observation_is_read_first() -> None:
    """The page is small, so the ordering decides what is seen at all."""

    async def _run() -> None:
        await _clean_db_or_skip()
        await store.record(
            [
                _finding("thin", scope="a", evidence_note_ids=["reaction-1"]),
                _finding("solid", scope="b", evidence_note_ids=[f"reaction-{i}" for i in range(4)]),
            ]
        )
        assert [o.statement for o in await store.open_observations()] == ["solid", "thin"]

    asyncio.run(_run())
