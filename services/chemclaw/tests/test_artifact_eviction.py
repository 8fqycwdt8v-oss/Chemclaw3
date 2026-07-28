"""Artifact eviction reclaims by cost, and never touches an answer (STO-6).

`workflows/retention.py` prunes by age and explicitly refuses `calculation_results`, because
evicting a cached result silently turns a cache hit into a recomputation (D-011) — a cost question,
not a retention clock. That refusal is only *survivable* because something else bounds the growth
D-124 introduced, and this job is that something else: it reclaims **blobs**, whose loss costs at
most a recomputation of something the system already knows how to redo.

The Postgres round trip skips offline, like every other PG test, so these pin the *policy the job
encodes* — the same shape `tests/test_retention.py` takes, and for the same reason: the risk lives
in what the statements target and when they are allowed to run, not in psycopg.
"""

import asyncio

from chemclaw.config import settings
from workflows.artifact_eviction import _EVICT_IDLE, _EVICT_TO_FIT, evict_cold_artifacts

_STATEMENTS = (_EVICT_IDLE, _EVICT_TO_FIT)


def test_nothing_this_job_deletes_is_a_calculation_result() -> None:
    """The load-bearing property, asserted against the SQL itself.

    D-011 ("never compute twice") and `workflows/retention.py`'s standing refusal to prune the
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
