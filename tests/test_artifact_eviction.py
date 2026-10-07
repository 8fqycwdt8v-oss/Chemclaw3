"""Artifact eviction reclaims by cost, and never touches an answer (STO-6).

`durable/retention.py` refuses to prune `calculation_results` (D-011); this job bounds growth by
reclaiming blobs, whose loss costs at most a recomputation. Substring tests pin the statements'
shape; the policy (ordering, windowing) is pinned against a real database by which seeded rows
survive, since a substring cannot see a predicate's meaning. The Postgres tests skip offline.
"""

import asyncio

import pytest
from psycopg.types.json import Jsonb

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.durable.artifact_eviction import (
    _EVICT_IDLE,
    _EVICT_TO_FIT,
    EvictionOutcome,
    evict_cold_artifacts,
)
from tests.pg import migrated_db_or_skip

_STATEMENTS = (_EVICT_IDLE, _EVICT_TO_FIT)


def test_nothing_this_job_deletes_is_a_calculation_result() -> None:
    """The load-bearing property, asserted against the SQL itself.

    Eviction targets blobs alone; reaching `calculation_results` would turn cache hits into re-runs.
    """
    for statement in _STATEMENTS:
        assert "calculation_results" not in statement
        assert "DELETE FROM artifact_blobs" in statement


def test_the_link_rows_are_left_to_the_foreign_key() -> None:
    """`calculation_artifacts.content_hash` is `ON DELETE CASCADE` (migration 019).

    So link rows go with their blob; deleting them here too would be a second definition of a
    reclaim.
    """
    for statement in _STATEMENTS:
        assert "DELETE FROM calculation_artifacts" not in statement


def test_eviction_is_ordered_by_what_a_blob_cost_to_produce() -> None:
    """Eviction is ordered by what a blob cost to produce.

    Ordering by age would reclaim an expensive Hessian before a cheap geometry written later
    (`compute_seconds`, D-124).
    """
    assert "compute_seconds" in _EVICT_TO_FIT
    assert "last_access_at" in _EVICT_TO_FIT  # cost *over idle time*, not cost alone


def test_both_triggers_are_off_until_a_deployment_states_a_policy() -> None:
    """Nothing is reclaimed until a deployment says what it can afford to lose.

    Both knobs default to 0, meaning the sweep does not run.
    """
    assert settings.artifact_store_max_bytes == 0
    assert settings.artifact_evict_idle_days == 0


def test_with_no_policy_the_job_reclaims_nothing_and_says_so() -> None:
    """Runs for real: with both triggers off it returns before opening a connection.

    It reports the skips, so an operator can tell "nothing old enough" from "never switched on".
    """
    outcome = asyncio.run(evict_cold_artifacts())
    assert (outcome.idle_blobs, outcome.oversize_blobs) == (0, 0)
    assert (outcome.idle_bytes, outcome.oversize_bytes) == (0, 0)
    assert outcome.skipped == ["artifact eviction disabled (no idle window, no size ceiling)"]


# The size sweep's running-total selection is asserted on rows, in
# `test_the_size_sweep_keeps_the_valuable_blobs_and_drops_the_cheap_idle_ones`, not on SQL text.


def test_every_reclaim_reports_what_it_removed() -> None:
    """A deletion that leaves no record is not auditable, which `retention.py` also insists on."""
    for statement in _STATEMENTS:
        assert "RETURNING stored_bytes" in statement


# --- the policy itself, against a real database -------------------------------------------------
#
# What follows asserts on the rows the SQL leaves behind, where the policy is observable.


async def _seed_blob(
    content_hash: str, stored_bytes: int, idle_days: int, compute_seconds: float | None
) -> None:
    """Insert one blob with a chosen size and idle age, plus the link row carrying its cost.

    The cost lives on the link row; seeding a blob without it would test the `COALESCE(..., 0)`
    branch instead of the ordering.
    """
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO artifact_blobs "
                "(content_hash, codec, byte_size, stored_bytes, data, last_access_at) "
                "VALUES (%s, 'none', %s, %s, %s, now() - make_interval(days => %s))",
                (content_hash, stored_bytes, stored_bytes, b"x", idle_days),
            )
            if compute_seconds is not None:
                await cur.execute(
                    "INSERT INTO calculation_artifacts "
                    "(calc_key, name, content_hash, compute_seconds) VALUES (%s, %s, %s, %s)",
                    (f"calc-{content_hash}", "hessian", content_hash, compute_seconds),
                )
        await conn.commit()


async def _clear_artifacts() -> None:
    """Empty the blob table (and, by cascade, its link rows) before seeding.

    Both eviction statements are global, so a leftover blob would shift every cumulative total.
    """
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM artifact_blobs")
        await conn.commit()


async def _surviving_blobs() -> set[str]:
    """The content hashes still stored."""
    async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
        await cur.execute("SELECT content_hash FROM artifact_blobs")
        return {str(row[0]) for row in await cur.fetchall()}


def test_the_size_sweep_keeps_the_valuable_blobs_and_drops_the_cheap_idle_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ordering, asserted on rows: an expensive artifact outlives a cheap one of equal size.

    Four 400-byte blobs, equally idle, differing only in cost; under an 800-byte ceiling exactly the
    two most valuable must survive. A predicate that deletes everything or an inverted `ORDER BY`
    would pass every text assertion.
    """
    monkeypatch.setattr(settings, "artifact_store_max_bytes", 800)
    monkeypatch.setattr(settings, "artifact_evict_idle_days", 0)

    async def _run() -> tuple[set[str], EvictionOutcome]:
        await migrated_db_or_skip()
        await _clear_artifacts()
        # value = compute_seconds / idle days; all idle 10 days, so ordering is by cost.
        await _seed_blob("expensive", 400, 10, 300.0)
        await _seed_blob("moderate", 400, 10, 100.0)
        await _seed_blob("cheap", 400, 10, 1.0)
        await _seed_blob("uncosted", 400, 10, None)
        outcome = await evict_cold_artifacts()
        return await _surviving_blobs(), outcome

    surviving, outcome = asyncio.run(_run())
    assert surviving == {"expensive", "moderate"}, (
        f"eviction kept {sorted(surviving)}; the ceiling must be met by dropping the least "
        "valuable blobs, not the most valuable ones and not all of them"
    )
    assert (outcome.oversize_blobs, outcome.oversize_bytes) == (2, 800)


def test_a_cheap_blob_read_yesterday_outranks_an_expensive_one_nobody_has_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Value is cost *per idle day*, not cost — the second half of the ranking expression.

    Ten seconds read yesterday beats a hundred seconds unread for a hundred days. The axes disagree
    on purpose, so ranking by cost alone fails.
    """
    monkeypatch.setattr(settings, "artifact_store_max_bytes", 400)
    monkeypatch.setattr(settings, "artifact_evict_idle_days", 0)

    async def _run() -> set[str]:
        await migrated_db_or_skip()
        await _clear_artifacts()
        await _seed_blob("cheap-read-yesterday", 400, 1, 10.0)
        await _seed_blob("costly-unread-for-months", 400, 100, 100.0)
        await evict_cold_artifacts()
        return await _surviving_blobs()

    assert asyncio.run(_run()) == {"cheap-read-yesterday"}


def test_the_idle_sweep_removes_only_blobs_past_the_stated_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The idle trigger is a window, not a switch.

    A blob inside the window stays however cheap; `last_access_at < now()` would reclaim everything.
    """
    monkeypatch.setattr(settings, "artifact_evict_idle_days", 30)
    monkeypatch.setattr(settings, "artifact_store_max_bytes", 0)

    async def _run() -> tuple[set[str], EvictionOutcome]:
        await migrated_db_or_skip()
        await _clear_artifacts()
        await _seed_blob("stale", 700, 90, 5.0)
        await _seed_blob("fresh", 900, 2, 5.0)
        outcome = await evict_cold_artifacts()
        return await _surviving_blobs(), outcome

    surviving, outcome = asyncio.run(_run())
    assert surviving == {"fresh"}
    assert (outcome.idle_blobs, outcome.idle_bytes) == (1, 700)
    assert outcome.skipped == ["size ceiling disabled"]


def test_an_evicted_blob_takes_its_link_row_and_leaves_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The load-bearing property, asserted on rows rather than on the absence of a substring.

    `calculation_results` must survive (D-011), and the link row must not: the `ON DELETE CASCADE`
    from migration 019 stops `list_for` returning a ref whose bytes are gone, and only rows can show
    the cascade is still there.
    """
    monkeypatch.setattr(settings, "artifact_evict_idle_days", 30)
    monkeypatch.setattr(settings, "artifact_store_max_bytes", 0)

    async def _run() -> tuple[int, int]:
        await migrated_db_or_skip()
        await _clear_artifacts()
        await _seed_blob("doomed", 100, 90, 42.0)
        async with db.connection(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO calculation_results "
                    "(key, calc_type, calc_version, input_hash, params_hash, result) "
                    "VALUES ('evict-probe', 'pka', 'v1', 'h', 'p', %s) "
                    "ON CONFLICT (key) DO NOTHING",
                    (Jsonb({"pka": 4.2}),),
                )
            await conn.commit()

        await evict_cold_artifacts()

        async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM calculation_results WHERE key = 'evict-probe'")
            answers = await cur.fetchone()
            await cur.execute(
                "SELECT count(*) FROM calculation_artifacts WHERE content_hash = 'doomed'"
            )
            links = await cur.fetchone()
        return (int(answers[0]) if answers else -1, int(links[0]) if links else -1)

    surviving_answers, surviving_links = asyncio.run(_run())
    assert surviving_answers == 1, "eviction destroyed a cached answer; D-011 says it never can"
    assert surviving_links == 0, (
        "the link row outlived its blob — `list_for` can now hand back a ref"
    )
