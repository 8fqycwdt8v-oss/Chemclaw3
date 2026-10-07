"""The exclusion guarantees, exercised concurrently rather than in sequence.

A sequential test passes whether a guard is one atomic statement or a read then a write, so each
test here creates the race and asserts the promise, with large contention (32 claimants) because
some failures are intermittent by construction. Where the design accepts an overshoot
(`BudgetTracker` admits up to `service_max_concurrent_turns` before any records), the bound is
asserted at its stated width.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from chemclaw.agent.session_store import SessionTurnClaims
from chemclaw.api.budget import BudgetExceeded, BudgetTracker
from chemclaw.api.state import claim_holder
from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.core.executor import install_default_executor
from chemclaw.kg.git_writer import GitWriteError, _checkout_lock
from tests.pg import migrated_db_or_skip


async def _claims_or_skip() -> SessionTurnClaims:
    """A turn-claim store over a migrated database, or a skip when none is reachable."""
    await migrated_db_or_skip()
    return SessionTurnClaims()


async def test_only_one_of_many_racing_workers_claims_a_session() -> None:
    """Thirty-two workers reach for one session at once; exactly one may get it.

    Each claimant is its own store and connection, as separate pods are; a shared connection would
    serialize them in the client.
    """
    await _claims_or_skip()
    session_id = "sess-race-exclusive"
    # Any residue from an earlier run would decide the outcome before the race starts.
    async with await connect(settings.postgres_dsn) as conn:
        await conn.execute("DELETE FROM session_turns WHERE session_id = %s", (session_id,))
        await conn.commit()

    holders = [f"worker-{index}" for index in range(32)]
    winners = await asyncio.gather(
        *(SessionTurnClaims().claim(session_id, holder, 60.0) for holder in holders)
    )
    try:
        assert sum(winners) == 1, f"{sum(winners)} of {len(holders)} workers claimed one session"
    finally:
        for holder in holders:
            await SessionTurnClaims().release(session_id, holder)


async def test_a_lapsed_holder_can_neither_refresh_nor_release_the_new_owners_claim() -> None:
    """A lapsed holder can neither refresh nor release the new owner's claim.

    Both are `WHERE session_id = %s AND holder = %s`; the damaging state is a row that is someone
    else's, not a missing one.
    """
    claims = await _claims_or_skip()
    session_id = "sess-race-stale-holder"
    await claims.release(session_id, "slow")
    await claims.release(session_id, "new")

    assert await claims.claim(session_id, "slow", -1.0) is True  # already lapsed
    assert await claims.claim(session_id, "new", 60.0) is True  # taken over

    # The lapsed worker, still running, refreshes as a live holder would; `refresh` reports the
    # claim is no longer its own, which `_hold_turn_claim` acts on.
    assert await claims.refresh(session_id, "slow", 600.0) is False
    await claims.release(session_id, "slow")

    # And the other direction, or `is False` above would pass against a `refresh` that always
    # said no — which would stop every heartbeat in the system on its first beat.
    assert await claims.refresh(session_id, "new", 60.0) is True

    # If either had landed, this would succeed — the slot would be free (release) or held by
    # a holder nobody is running (refresh under the wrong name).
    assert await claims.claim(session_id, "third", 60.0) is False, (
        "a lapsed holder's refresh or release reached the new owner's claim"
    )
    await claims.release(session_id, "new")


@pytest.fixture
def _budgeted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Budgets on, only the per-session turn cap tightened — the one these tests race against.

    Enabled explicitly, since `budget_enabled` is off in dev. Single-threaded tracker behaviour is
    `tests/test_budget.py`'s.
    """
    monkeypatch.setattr(settings, "budget_enabled", True)
    monkeypatch.setattr(settings, "budget_max_turns_per_session", 4)
    for field in (
        "budget_max_tokens_per_session",
        "budget_max_turns_per_user",
        "budget_max_tokens_per_user",
    ):
        monkeypatch.setattr(settings, field, 0)  # 0 == unlimited


def _race_checks(tracker: BudgetTracker, session_id: str, threads: int) -> int:
    """How many of `threads` simultaneous `check` calls were admitted.

    Real threads, because the guard is a `threading.Lock`; a barrier puts every caller inside
    `check` at once.
    """
    admitted = 0
    lock = threading.Lock()
    barrier = threading.Barrier(threads)

    def one_turn() -> None:
        barrier.wait(timeout=30)
        try:
            # `check` is a coroutine since the per-user window went durable; each thread drives
            # it on a loop of its own, which keeps this a test of the `threading.Lock` around the
            # counters rather than of an event loop's serialization.
            asyncio.run(tracker.check(session_id, None))
        except BudgetExceeded:
            return
        with lock:
            nonlocal admitted
            admitted += 1

    workers = [threading.Thread(target=one_turn) for _ in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)
    return admitted


def test_a_session_already_at_its_cap_refuses_every_simultaneous_turn(_budgeted: None) -> None:
    """A session already at its cap refuses every simultaneous turn.

    This half must be exact: usage is booked and the cap reached, so any admission is a broken lock.
    """
    tracker = BudgetTracker()
    session_id = "sess-budget-at-cap"
    for _ in range(settings.budget_max_turns_per_session):
        tracker.record(session_id, None, tokens=0)

    admitted = _race_checks(tracker, session_id, settings.service_max_concurrent_turns)
    assert admitted == 0, f"{admitted} turns were admitted past a cap that was already reached"


def test_the_overshoot_at_the_boundary_never_exceeds_the_admission_cap(_budgeted: None) -> None:
    """The overshoot at the boundary never exceeds the admission cap.

    `check` and `record` are separate, so concurrent turns see the same usage; `BudgetTracker`
    documents the bound as `service_max_concurrent_turns`. The bound is asserted, not the exact
    count, which is a scheduling artefact.
    """
    concurrent = settings.service_max_concurrent_turns
    tracker = BudgetTracker()
    session_id = "sess-budget-boundary"
    for _ in range(settings.budget_max_turns_per_session - 1):
        tracker.record(session_id, None, tokens=0)

    admitted = _race_checks(tracker, session_id, concurrent)
    assert 1 <= admitted <= concurrent, (
        f"{admitted} turns passed `check` with one slot left; the documented bound is "
        f"service_max_concurrent_turns={concurrent}"
    )


# Run in a child interpreter: a second process is what the lock promises to exclude and what a
# second replica sharing a PVC is.
_SECOND_PROCESS = """
import sys
from chemclaw.kg.git_writer import GitWriteError, _checkout_lock

try:
    with _checkout_lock(sys.argv[1]):
        print("ACQUIRED")
except GitWriteError as exc:
    print("REFUSED")
"""


def test_the_submit_lock_excludes_a_second_operating_system_process(tmp_path: Path) -> None:
    """A second process cannot take the checkout lock while this one holds it.

    Submitters sharing `note_repo_dir` mutate `.git/worktrees/` and the ref store, and each
    submission sweeps every worktree under the shared root, which is safe only under exclusion. The
    child is a real interpreter, not a thread.
    """
    repo = tmp_path / "clone"
    (repo / ".git").mkdir(parents=True)

    with _checkout_lock(str(repo)):
        completed = subprocess.run(
            [sys.executable, "-c", _SECOND_PROCESS, str(repo)],
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
        )
        assert "REFUSED" in completed.stdout, (
            f"a second process took the submit lock while it was held: {completed.stdout!r}"
        )

    # And the lock is genuinely released, not merely held for the process's lifetime.
    completed = subprocess.run(
        [sys.executable, "-c", _SECOND_PROCESS, str(repo)],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    assert "ACQUIRED" in completed.stdout


def test_the_submit_lock_reports_a_missing_checkout_rather_than_proceeding(tmp_path: Path) -> None:
    """No `.git/` means no lock file, and that must be an error rather than an unlocked run.

    The failure-open shape: if a missing lock path were treated as "nothing to exclude", a
    misconfigured `note_repo_dir` would silently remove the exclusion the control depends on.
    """
    with pytest.raises(GitWriteError, match="cannot open submit lock"):
        with _checkout_lock(str(tmp_path / "not-a-checkout")):
            pass


# How long each stand-in for "a corpus parse on an executor thread" blocks. Stated once so the
# assertions below read as fractions of it rather than as absolute milliseconds.
_BLOCK_SECONDS = 0.3


def _queued_short_call_ms(reserved: int, *, install: bool) -> float:
    """Saturate the process's `to_thread` pool with `reserved` blocking calls, then time a tiny one.

    The tiny call stands in for `api/auth.py`'s `to_thread(validate_token, ...)`; the blocking ones
    for corpus parses and embeddings sharing the pool.
    """

    async def _scenario() -> float:
        if install:
            install_default_executor(component="front-door", reserved=reserved)
        blocking = [asyncio.create_task(asyncio.to_thread(time.sleep, _BLOCK_SECONDS))]
        blocking += [
            asyncio.create_task(asyncio.to_thread(time.sleep, _BLOCK_SECONDS))
            for _ in range(reserved - 1)
        ]
        # Let every blocking call actually reach a thread before the short one is submitted.
        await asyncio.sleep(0.05)
        started = time.perf_counter()
        await asyncio.to_thread(lambda: None)
        waited = (time.perf_counter() - started) * 1000
        await asyncio.gather(*blocking)
        return waited

    return asyncio.run(_scenario())


def test_a_short_call_does_not_queue_behind_a_full_admission_cap_of_blocking_work() -> None:
    """A short call does not queue behind a full admission cap of blocking work.

    The default `to_thread` pool is `min(32, cpu_count + 4)`, which the admission cap can fill
    alone, queueing token validation behind corpus parses. The process sizes its pool wider than its
    caps; this drives the caps' worth of blocking work, then one short call.
    """
    reserved = settings.service_max_concurrent_turns + settings.attachment_max_concurrent_parses

    waited_ms = _queued_short_call_ms(reserved, install=True)

    assert waited_ms < _BLOCK_SECONDS * 1000 / 2, (
        f"a token validation queued {waited_ms:.1f} ms behind {reserved} blocking offloads; the "
        "process's to_thread pool is no wider than its own admission caps"
    )


def test_the_installed_pool_is_wider_than_the_caps_that_can_fill_it() -> None:
    """The width itself, pinned — the behavioural test above cannot say *why* it passed.

    Written because the obvious wrong sizing — a pool exactly as wide as the admission cap — passes
    a one-short-call probe on a quiet loop and still leaves nothing for the second request.
    """
    reserved = settings.service_max_concurrent_turns + settings.attachment_max_concurrent_parses

    async def _install() -> int:
        return install_default_executor(component="front-door", reserved=reserved)._max_workers

    assert asyncio.run(_install()) == reserved + settings.service_thread_pool_headroom


async def test_two_turns_in_one_process_are_two_holders_not_one() -> None:
    """Two turns in one process are two holders, not one.

    The test above uses two holder names; production has both turns in one process. A per-process
    holder would let a lapsed turn refresh and release its successor's claim. This drives both turns
    through `api/state.claim_holder`, as the routes do.
    """
    claims = await _claims_or_skip()
    session_id = "sess-race-same-worker"
    first = claim_holder(uuid.uuid4().hex)
    second = claim_holder(uuid.uuid4().hex)
    await claims.release(session_id, first)
    await claims.release(session_id, second)

    assert first != second, (
        "two turns in one process resolved to one holder, so the durable claim cannot tell "
        "a lapsed turn's teardown from the live turn's"
    )
    assert await claims.claim(session_id, first, -1.0) is True  # already lapsed
    assert await claims.claim(session_id, second, 60.0) is True  # a successor took the slot

    assert await claims.refresh(session_id, first, 600.0) is False, (
        "the lapsed turn extended its successor's lease"
    )
    await claims.release(session_id, first)
    assert await claims.claim(session_id, "another-replica", 60.0) is False, (
        "the lapsed turn's teardown deleted the live turn's claim, so a second replica was "
        "admitted beside it"
    )
    await claims.release(session_id, second)
