"""The exclusion guarantees, exercised concurrently rather than in sequence.

Every guard here is documented as holding *under contention*, and every existing test drives it one
call at a time. That gap is not academic: a sequential test of `claim`/`release` passes identically
whether the SQL is one atomic statement or a read followed by a write, which is the only
distinction the guard is for. The same is true of the budget tracker's lock and of the submit
`flock`, whose docstring says it "genuinely excludes other processes" — a claim no test in this
repository has ever put a second process behind.

So each test here creates the race the prose describes and then asserts what the prose promises.
Three of them can only fail intermittently by construction, which is why the contention is made
large rather than symbolic: 32 racing claimants, not two.

**Bounds are pinned where the design accepts an overshoot.** `BudgetTracker` documents that up to
`service_max_concurrent_turns` turns may pass `check` before any of them `record` — an accepted
best-effort property, not a bug. An accepted bound that nothing measures is indistinguishable from
an unbounded one the day it changes, so it is asserted at its stated width.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from chemclaw.agent.session_store import SessionTurnClaims
from chemclaw.api.budget import BudgetExceeded, BudgetTracker
from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.core.executor import install_default_executor
from chemclaw.kg.git_submitter import GitSubmitError, _checkout_lock
from tests.pg import migrated_db_or_skip


async def _claims_or_skip() -> SessionTurnClaims:
    """A turn-claim store over a migrated database, or a skip when none is reachable."""
    await migrated_db_or_skip()
    return SessionTurnClaims()


def test_only_one_of_many_racing_workers_claims_a_session() -> None:
    """Thirty-two workers reach for one session at once; exactly one may get it.

    The sequential version of this test (`test_session_store.py`) passes whether `claim` is one
    `INSERT … ON CONFLICT … WHERE expires_at <= now() RETURNING` or a `SELECT` followed by an
    `INSERT`, because with one caller there is no gap to interleave. This is the test that can
    tell them apart, and it matters at the width the chart ships: two front-door replicas, each
    admitting turns concurrently, against one shared row.

    Every claimant is its own store — a separate connection, as a separate pod would be — because
    a shared connection would serialize them in the client and test nothing.
    """

    async def _run() -> None:
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
            assert sum(winners) == 1, (
                f"{sum(winners)} of {len(holders)} workers claimed one session"
            )
        finally:
            for holder in holders:
                await SessionTurnClaims().release(session_id, holder)

    asyncio.run(_run())


def test_a_lapsed_holder_can_neither_refresh_nor_release_the_new_owners_claim() -> None:
    """The two guards that keep a slow worker from corrupting the one that replaced it.

    Both operations are `WHERE session_id = %s AND holder = %s`, and both docstrings explain why:
    a worker whose lease lapsed and was taken over must not extend — or delete — the new owner's
    claim, because doing so hands a second turn onto a conversation that already has one. The
    interesting case is not that they are no-ops when the row is *gone*; it is that they are
    no-ops when the row is *someone else's*, which is the only state that can cause damage.
    """

    async def _run() -> None:
        claims = await _claims_or_skip()
        session_id = "sess-race-stale-holder"
        await claims.release(session_id, "slow")
        await claims.release(session_id, "new")

        assert await claims.claim(session_id, "slow", -1.0) is True  # already lapsed
        assert await claims.claim(session_id, "new", 60.0) is True  # taken over

        # The lapsed worker, still running, doing exactly what a live holder does. `refresh`
        # reports that the claim is no longer its own — the signal `_hold_turn_claim` acts on, and
        # the thing that was silently discarded until the 2026-08-05 review: the UPDATE matched no
        # row, raised nothing, and the caller could not tell a takeover from a healthy heartbeat.
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

    asyncio.run(_run())


@pytest.fixture
def _budgeted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Budgets on, only the per-session turn cap tightened — the one these tests race against.

    Enabled explicitly rather than skipped on the default, because `budget_enabled` is off in dev
    and a concurrency test that only runs where nobody runs it is not a test. The tracker's own
    behaviour under the flag is `tests/test_budget.py`'s question; this file's is what happens when
    several threads ask at once.
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

    Real threads, not coroutines: the tracker's guard is a `threading.Lock`, and an asyncio-only
    race would never enter it. A barrier rather than a stagger, so every caller is inside `check`
    at the same moment — the whole window this measures is the one between a check and a booking.
    """
    admitted = 0
    lock = threading.Lock()
    barrier = threading.Barrier(threads)

    def one_turn() -> None:
        barrier.wait(timeout=30)
        try:
            tracker.check(session_id, None)
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
    """No amount of concurrency gets a turn past a cap that is already reached.

    The half of the guard that must be exact. The overshoot below is accepted because it is
    bounded by what can be in flight; *this* is not an overshoot at all — the usage is booked, the
    cap is reached, and a race that let one through would be a lock that does not hold.
    """
    tracker = BudgetTracker()
    session_id = "sess-budget-at-cap"
    for _ in range(settings.budget_max_turns_per_session):
        tracker.record(session_id, None, tokens=0)

    admitted = _race_checks(tracker, session_id, settings.service_max_concurrent_turns)
    assert admitted == 0, f"{admitted} turns were admitted past a cap that was already reached"


def test_the_overshoot_at_the_boundary_never_exceeds_the_admission_cap(_budgeted: None) -> None:
    """One turn short of the cap, every concurrent turn checks at once: how many get through?

    This is the documented TOCTOU window. `check` and `record` are separate calls, so every turn
    admitted before any of them books sees the same usage — `BudgetTracker`'s own docstring calls
    it "a bounded overshoot acceptable for a best-effort guard, not an exact accountant" and names
    the bound: `service_max_concurrent_turns`, since nothing more than that can be in flight.

    An accepted bound that nothing measures is indistinguishable from an unbounded one the day it
    changes. So the bound is asserted and the exact figure is not — that is a scheduling artefact,
    and pinning it would make this a flaky test about the GIL rather than a check on the design.
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


# Run in a *child interpreter*, because that is the whole point: an `flock` is tied to the open
# file description, so a second attempt from this same process on a new file object is a genuine
# second holder, but a second *process* is what the docstring promises and what a second replica
# sharing a PVC actually is.
_SECOND_PROCESS = """
import sys
from chemclaw.kg.git_submitter import GitSubmitError, _checkout_lock

try:
    with _checkout_lock(sys.argv[1]):
        print("ACQUIRED")
except GitSubmitError as exc:
    print("REFUSED")
"""


def test_the_submit_lock_excludes_a_second_operating_system_process(tmp_path: Path) -> None:
    """A second process cannot take the checkout lock while this one holds it.

    `_checkout_lock`'s docstring is explicit — "it genuinely excludes other processes" — and until
    now nothing put a process behind that claim. It stayed load-bearing when the submission moved
    into its own worktree (D-2026-08-05): two submitters sharing `note_repo_dir` both mutate
    `.git/worktrees/` and the ref store, and — the part that makes it structural — each submission
    sweeps every worktree under the shared root, which is only safe because no other submission can
    own one.

    The child is a real interpreter, not a thread and not a second file object in this process,
    since only a separate process tests what the sentence says.
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
    with pytest.raises(GitSubmitError, match="cannot open submit lock"):
        with _checkout_lock(str(tmp_path / "not-a-checkout")):
            pass


# How long each stand-in for "a corpus parse on an executor thread" blocks. Stated once so the
# assertions below read as fractions of it rather than as absolute milliseconds.
_BLOCK_SECONDS = 0.3


def _queued_short_call_ms(reserved: int, *, install: bool) -> float:
    """Saturate the process's `to_thread` pool with `reserved` blocking calls, then time a tiny one.

    The tiny call stands in for `api/auth.py`'s `await asyncio.to_thread(validate_token, ...)`,
    which every authenticated request makes; the blocking ones stand in for the corpus parses,
    embeddings and attachment parses that share the same pool. What is measured is the thing an
    operator actually feels: how long authentication waits when the admission cap's worth of
    chemistry is already in flight.
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
    """Authentication latency must not be a function of corpus size.

    `asyncio.to_thread` is `run_in_executor(None, ...)` — the loop's single default pool, sized
    `min(32, cpu_count + 4)`, i.e. **8 on a 4-CPU pod**, which is exactly the shipped
    `service_max_concurrent_turns`. So the admission cap could fill the whole offload budget on its
    own and every subsequent request queued its token validation behind a note-corpus parse; the
    audit measured a queued short call at 0.2 ms with 1 concurrent `load_notes`, 565.5 ms with 8
    and 813.4 ms with 16. The fix is that the process states its own caps and gets a pool wider
    than them, so this test drives exactly that: the caps' worth of blocking work, then one short
    call.
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
