"""Artifact eviction reclaims by cost, and never touches an answer (STO-6).

`durable/retention.py` prunes by age and explicitly refuses `calculation_results`, because
evicting a cached result silently turns a cache hit into a recomputation (D-011) — a cost question,
not a retention clock. That refusal is only *survivable* because something else bounds the growth
D-124 introduced, and this job is that something else: it reclaims **blobs**, whose loss costs at
most a recomputation of something the system already knows how to redo.

Two kinds of test live here, and the second kind exists because the first kind is not enough.
The substring assertions below pin the statements' *shape* — which tables they name, that they
report what they removed. They are cheap and they read well, and every one of them survives a
rewrite of the `WHERE` clause that changes which rows are deleted, because a substring cannot see
a predicate's meaning. Four mutations proved that in this campaign, including one that deleted
every blob in the store and one that evicted the most expensive artifacts first — the exact
inversion of the policy the file's own docstrings argue for.

So the ordering and the windowing are pinned against a real database, seeding blobs with known
costs and idle times and asserting on *which ones survive* — the shape
`tests/test_retention.py` takes, and for the same reason: a policy is only stated by the rows it
leaves behind. The Postgres round trip skips offline like every other PG test.
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

    D-011 ("never compute twice") and `durable/retention.py`'s standing refusal to prune the
    calculation cache both remain literally true *only* because eviction targets blobs alone. A
    statement that reached `calculation_results` would quietly convert a cache hit into an HPC run
    and make two written decisions false at once.
    """
    for statement in _STATEMENTS:
        assert "calculation_results" not in statement
        assert "DELETE FROM artifact_blobs" in statement


def test_the_link_rows_are_left_to_the_foreign_key() -> None:
    """`calculation_artifacts.content_hash` is `ON DELETE CASCADE` (migration 019).

    So a blob's link rows go with it and no dangling reference can survive. Deleting them here as
    well would be a second, drifting definition of what a reclaim means — and it is the `DELETE`
    this job must *not* contain that proves the cascade is being relied on.
    """
    for statement in _STATEMENTS:
        assert "DELETE FROM calculation_artifacts" not in statement


def test_eviction_is_ordered_by_what_a_blob_cost_to_produce() -> None:
    """The cost policy `retention.py` named and correctly refused to fake with an age cutoff.

    D-124 started recording `compute_seconds` for exactly this. An eviction that ordered by age
    alone would reclaim a four-minute Hessian before a cheap geometry simply because it was written
    first, which is the failure mode that made "a cache is bounded by cost, not by a clock" worth
    writing down.
    """
    assert "compute_seconds" in _EVICT_TO_FIT
    assert "last_access_at" in _EVICT_TO_FIT  # cost *over idle time*, not cost alone


def test_both_triggers_are_off_until_a_deployment_states_a_policy() -> None:
    """Nothing is reclaimed until a deployment says what it can afford to lose.

    Inheriting a deletion default from code is wrong here for the same reason it is in
    `retention.py`: a deployment chooses what it can afford to recompute, and silence is not a
    choice. Both knobs default to 0, and 0 means the corresponding sweep does not run.
    """
    assert settings.artifact_store_max_bytes == 0
    assert settings.artifact_evict_idle_days == 0


def test_with_no_policy_the_job_reclaims_nothing_and_says_so() -> None:
    """Runs for real: with both triggers off it returns before opening a connection.

    Reporting the skips rather than returning an empty success is the point — an operator reading
    the job's own result should be able to tell "nothing was old enough" from "this was never
    switched on".
    """
    outcome = asyncio.run(evict_cold_artifacts())
    assert (outcome.idle_blobs, outcome.oversize_blobs) == (0, 0)
    assert (outcome.idle_bytes, outcome.oversize_bytes) == (0, 0)
    assert outcome.skipped == ["artifact eviction disabled (no idle window, no size ceiling)"]


def test_the_size_sweep_selects_by_a_running_total_rather_than_a_fixed_count() -> None:
    """Evicting until the store fits is a statement about bytes, not about rows.

    A top-N delete would reclaim a fixed number of blobs regardless of their size, so a store over
    its ceiling by one large artifact would need many passes — or would over-evict small ones. The
    window function is what makes one statement land exactly at the point the store fits again.
    """
    assert "SUM(b.stored_bytes) OVER" in _EVICT_TO_FIT
    assert "cumulative >" in _EVICT_TO_FIT


def test_every_reclaim_reports_what_it_removed() -> None:
    """A deletion that leaves no record is not auditable, which `retention.py` also insists on."""
    for statement in _STATEMENTS:
        assert "RETURNING stored_bytes" in statement


# --- the policy itself, against a real database -------------------------------------------------
#
# Everything above reads the SQL as text. What follows reads the rows the SQL leaves behind, which
# is the only place the *policy* is observable.


async def _seed_blob(
    content_hash: str, stored_bytes: int, idle_days: int, compute_seconds: float | None
) -> None:
    """Insert one blob with a chosen size and idle age, plus the link row carrying its cost.

    The cost lives on `calculation_artifacts`, not on the blob, so a blob only becomes rankable
    through its link — seeding one without the other would silently test the `COALESCE(..., 0)`
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

    Both eviction statements are global, so a blob another test left behind lands inside the
    ranking and shifts every cumulative total — the same reason `test_retention.py` clears whole
    tables rather than a prefix.
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


def test_the_size_sweep_keeps_the_valuable_blobs_and_drops_the_cheap_idle_ones() -> None:
    """The ordering, asserted on rows: an expensive artifact outlives a cheap one of equal size.

    Four blobs of 400 bytes each, all idle for ten days, differing only in what they cost to
    produce. With an 800-byte ceiling exactly two must survive, and *which* two is the whole
    policy: `retention.py` refused to prune the cache by age precisely because an age cutoff would
    reclaim a four-minute Hessian before a cheap geometry that happened to be written later.

    A substring assertion cannot see this. Replacing the selection predicate with
    `cumulative >= 0 AND %s IS NOT NULL` (deletes every blob) or reversing the value `ORDER BY`
    to `ASC` (evicts the most expensive first) leaves every text assertion in this file green.
    """

    async def _run() -> tuple[set[str], EvictionOutcome]:
        await migrated_db_or_skip()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(settings, "artifact_store_max_bytes", 800)
        monkeypatch.setattr(settings, "artifact_evict_idle_days", 0)
        try:
            await _clear_artifacts()
            # value = compute_seconds / idle days; all idle 10 days, so ordering is by cost.
            await _seed_blob("expensive", 400, 10, 300.0)
            await _seed_blob("moderate", 400, 10, 100.0)
            await _seed_blob("cheap", 400, 10, 1.0)
            await _seed_blob("uncosted", 400, 10, None)
            outcome = await evict_cold_artifacts()
            return await _surviving_blobs(), outcome
        finally:
            monkeypatch.undo()

    surviving, outcome = asyncio.run(_run())
    assert surviving == {"expensive", "moderate"}, (
        f"eviction kept {sorted(surviving)}; the ceiling must be met by dropping the least "
        "valuable blobs, not the most valuable ones and not all of them"
    )
    assert (outcome.oversize_blobs, outcome.oversize_bytes) == (2, 800)


def test_a_cheap_blob_read_yesterday_outranks_an_expensive_one_nobody_has_opened() -> None:
    """Value is cost *per idle day*, not cost — the second half of the ranking expression.

    Ten seconds of compute read yesterday beats a hundred seconds unread for a hundred days,
    because the ranking divides by idle time. The two axes disagree here on purpose: a test where
    the cheap blob is also the stale one cannot tell the divisor from the tiebreaker, and
    `MAX(a.compute_seconds)` alone survives it (measured — it did).
    """

    async def _run() -> set[str]:
        await migrated_db_or_skip()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(settings, "artifact_store_max_bytes", 400)
        monkeypatch.setattr(settings, "artifact_evict_idle_days", 0)
        try:
            await _clear_artifacts()
            await _seed_blob("cheap-read-yesterday", 400, 1, 10.0)
            await _seed_blob("costly-unread-for-months", 400, 100, 100.0)
            await evict_cold_artifacts()
            return await _surviving_blobs()
        finally:
            monkeypatch.undo()

    assert asyncio.run(_run()) == {"cheap-read-yesterday"}


def test_the_idle_sweep_removes_only_blobs_past_the_stated_window() -> None:
    """The idle trigger is a window, not a switch.

    A blob inside the window stays however cheap it was, because the ceiling is the instrument for
    "too much"; idle eviction only answers "nobody wants this any more". Widening the predicate to
    `last_access_at < now()` — which reads almost identically — would reclaim the whole store on
    every pass, and no assertion on the statement text would notice.
    """

    async def _run() -> tuple[set[str], EvictionOutcome]:
        await migrated_db_or_skip()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(settings, "artifact_evict_idle_days", 30)
        monkeypatch.setattr(settings, "artifact_store_max_bytes", 0)
        try:
            await _clear_artifacts()
            await _seed_blob("stale", 700, 90, 5.0)
            await _seed_blob("fresh", 900, 2, 5.0)
            outcome = await evict_cold_artifacts()
            return await _surviving_blobs(), outcome
        finally:
            monkeypatch.undo()

    surviving, outcome = asyncio.run(_run())
    assert surviving == {"fresh"}
    assert (outcome.idle_blobs, outcome.idle_bytes) == (1, 700)
    assert outcome.skipped == ["size ceiling disabled"]


def test_an_evicted_blob_takes_its_link_row_and_leaves_the_answer() -> None:
    """The load-bearing property, asserted on rows rather than on the absence of a substring.

    Two halves, and each fails differently. `calculation_results` must survive: evicting an answer
    turns a D-011 cache hit into an HPC re-run, and `retention.py`'s standing refusal to prune the
    cache is only true because this job cannot reach it. And the link row must *not* survive: the
    `ON DELETE CASCADE` in migration 019 is what stops `list_for` handing back a ref whose bytes
    are gone, and a test that only asserts the job contains no `DELETE FROM calculation_artifacts`
    passes just as well if that cascade were dropped from the schema tomorrow.
    """

    async def _run() -> tuple[int, int]:
        await migrated_db_or_skip()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(settings, "artifact_evict_idle_days", 30)
        monkeypatch.setattr(settings, "artifact_store_max_bytes", 0)
        try:
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
                await cur.execute(
                    "SELECT count(*) FROM calculation_results WHERE key = 'evict-probe'"
                )
                answers = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) FROM calculation_artifacts WHERE content_hash = 'doomed'"
                )
                links = await cur.fetchone()
            return (int(answers[0]) if answers else -1, int(links[0]) if links else -1)
        finally:
            monkeypatch.undo()

    surviving_answers, surviving_links = asyncio.run(_run())
    assert surviving_answers == 1, "eviction destroyed a cached answer; D-011 says it never can"
    assert surviving_links == 0, (
        "the link row outlived its blob — `list_for` can now hand back a ref"
    )
