"""`chemclaw.durable.heartbeat.beating` — the shared heartbeat-while-waiting idiom.

The generic timer behaviour; each caller's wiring (passing its own configured heartbeat timeout)
is pinned where the caller lives (`tests/test_calc_heartbeat.py`, `tests/test_bo_heartbeat.py`).
"""

import ast
import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from temporalio import activity

from chemclaw.durable.heartbeat import beating

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src" / "chemclaw"


def test_a_long_run_heartbeats_while_it_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run longer than the heartbeat timeout keeps beating instead of being declared dead.

    4 s timeout -> a 1 s beat interval (the helper divides, with a 1 s floor so production never
    beats more often than that). The sleep must clear one interval for a beat to be observable.
    """
    beats: list[str] = []
    monkeypatch.setattr(activity, "heartbeat", lambda *a: beats.append(str(a[0])))

    async def _slow() -> str:
        await asyncio.sleep(1.3)
        return "done"

    result = asyncio.run(beating(_slow(), "a long search", 4.0))
    assert result == "done"
    assert beats, "a run longer than the heartbeat timeout produced no heartbeat at all"
    assert all("still running" in b for b in beats)


def test_beating_returns_immediately_for_quick_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fast run pays nothing: no spurious beats, and the result comes straight back."""
    beats: list[str] = []
    monkeypatch.setattr(activity, "heartbeat", lambda *a: beats.append(str(a[0])))

    async def _quick() -> str:
        return "fast"

    assert asyncio.run(beating(_quick(), "quick", 600.0)) == "fast"
    assert beats == []


def test_beating_propagates_the_failure_it_wraps(monkeypatch: pytest.MonkeyPatch) -> None:
    """The heartbeat wrapper must not swallow the calculation's error."""
    monkeypatch.setattr(activity, "heartbeat", lambda *a: None)

    async def _boom() -> str:
        raise ValueError("crest failed")

    with pytest.raises(ValueError, match="crest failed"):
        asyncio.run(beating(_boom(), "boom", 600.0))


def test_the_beat_interval_tracks_the_timeout_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    """The beat cadence scales with the caller's *own* `heartbeat_timeout_seconds` argument.

    So shortening one caller's timeout shortens only its interval. The same 1.3 s wait against two
    timeouts: the short one beats at least once, the long one never.
    """

    async def _slow() -> str:
        await asyncio.sleep(1.3)
        return "done"

    beats: list[str] = []
    monkeypatch.setattr(activity, "heartbeat", lambda *a: beats.append(str(a[0])))
    asyncio.run(beating(_slow(), "short timeout", 4.0))  # interval = max(1.0, 1.0) = 1.0
    assert beats, "a 4s heartbeat_timeout must beat during a 1.3s wait"

    beats.clear()
    asyncio.run(beating(_slow(), "long timeout", 40.0))  # interval = max(1.0, 10.0) = 10.0
    assert beats == [], "a 40s heartbeat_timeout must not beat during a 1.3s wait"


def test_a_sub_second_timeout_still_beats_no_faster_than_once_a_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sub-second timeout still beats no faster than once a second.

    `max(1.0, timeout / 4)`: without the floor a sub-second timeout would heartbeat the Temporal
    server several times a second. `Field(gt=0)` on the setting refuses zero or negative at load; a
    positive sub-second timeout is legal, and only the floor keeps it from flooding the server.
    Asserted as a rate.
    """
    beats: list[str] = []
    monkeypatch.setattr(activity, "heartbeat", lambda *a: beats.append(str(a[0])))

    async def _slow() -> str:
        await asyncio.sleep(1.3)
        return "done"

    asyncio.run(beating(_slow(), "tiny timeout", 0.4))
    assert len(beats) <= 1, f"a 0.4s heartbeat timeout beat {len(beats)} times in 1.3s"


async def test_cancelling_the_wrapper_cancels_the_work_it_wraps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling the wrapper cancels the work it wraps.

    The work runs as a task beside the timer, and `asyncio.wait` does not cancel what it waits on;
    `beating(x)` must behave like `await x`, or a cancelled activity keeps committing chunks after
    it returns.
    """
    monkeypatch.setattr(activity, "heartbeat", lambda *a: None)
    # Asked of the *wrapped coroutine*, not of a wall-clock guess: a first version of this test
    # slept 50 ms and asserted the work had not finished, which a 5 s sleep satisfies whether it
    # was cancelled or not — it passed against the unfixed helper and measured nothing.
    reached_cancel = False

    async def _slow() -> str:
        nonlocal reached_cancel
        try:
            await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            reached_cancel = True
            raise
        return "done"

    wrapped = asyncio.ensure_future(beating(_slow(), "interrupted", 600.0))
    await asyncio.sleep(0.05)
    wrapped.cancel()
    with pytest.raises(asyncio.CancelledError):
        await wrapped
    # No sleep here: the helper itself awaits the cancelled task, so the work has finished unwinding
    # when `await wrapped` returns. Asserted inside the loop, because `asyncio.run` cancels pending
    # tasks on exit and a later check would pass either way.
    assert reached_cancel, "the wrapped work was left running after the wrapper was cancelled"


async def test_cancellation_waits_for_the_work_to_finish_unwinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The order, not just the fact: the work's cleanup completes *before* the caller unwinds.

    `task.cancel()` only files a request:

        cancel + raise    -> ['wrapper-returned', 'cleanup-start', 'cleanup-done']
        plain `await x`   -> ['cleanup-start', 'cleanup-done', 'wrapper-returned']

    The sync activities rely on the second order, so a cancelled activity returns only after the
    chunk's `finally` (the DB commit) is done.
    """
    monkeypatch.setattr(activity, "heartbeat", lambda *a: None)
    events: list[str] = []

    async def _slow() -> str:
        try:
            await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            events.append("cleanup-start")
            await asyncio.sleep(0.05)  # stands in for the chunk's commit
            events.append("cleanup-done")
            raise
        return "done"

    wrapped = asyncio.ensure_future(beating(_slow(), "interrupted", 600.0))
    await asyncio.sleep(0.05)
    wrapped.cancel()
    with pytest.raises(asyncio.CancelledError):
        await wrapped
    events.append("wrapper-returned")
    assert events == ["cleanup-start", "cleanup-done", "wrapper-returned"], events


async def test_a_failing_heartbeat_does_not_leave_the_work_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing heartbeat does not leave the work running.

    `activity.heartbeat` can raise (outside an activity context, or on an unserialisable payload);
    that exit must cancel the work too, so the cleanup is in a `finally` rather than a
    `CancelledError` handler.
    """

    def _boom(*_: object) -> None:
        raise RuntimeError("Not in activity context")

    monkeypatch.setattr(activity, "heartbeat", _boom)
    outcome: list[str] = []

    async def _slow() -> str:
        try:
            await asyncio.sleep(1.4)
        except asyncio.CancelledError:
            outcome.append("cancelled")
            raise
        outcome.append("ran to completion after the wrapper raised")
        return "done"

    # A 4 s timeout gives a 1 s beat, so the first beat lands while the work waits and exits through
    # the non-cancellation path; the work outlives the wrapper by 0.4 s, making "still running"
    # observable.
    with pytest.raises(RuntimeError, match="Not in activity context"):
        await beating(_slow(), "interrupted", 4.0)
    await asyncio.sleep(0.8)
    assert outcome == ["cancelled"], outcome


def test_the_beat_interval_has_exactly_one_derivation_in_the_tree() -> None:
    """The beat interval has exactly one derivation in the tree.

    Hand-rolled copies diverged from the helper (a different divisor, no floor); the helper existing
    does not prevent the next copy, this does.
    """
    offenders: list[str] = []
    for f in sorted(_SRC_ROOT.rglob("*.py")):
        if f == _SRC_ROOT / "durable" / "heartbeat.py":
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)):
                continue
            divided = ast.dump(node.left)
            if "heartbeat_timeout_seconds" in divided:
                offenders.append(f"{f.relative_to(_REPO_ROOT).as_posix()}:{node.lineno}")
    assert not offenders, (
        "heartbeat beat interval derived outside `durable.heartbeat.beating` — pass the timeout "
        f"to the helper instead: {offenders}"
    )


def test_the_republish_walk_beats_and_is_bounded_below_the_job_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The republish walk beats and is bounded below the job ceiling.

    A full scan of two never-pruned tables must not take the parent's whole
    `connector_job_timeout_seconds` (it would expire with the child and its retry could never be
    spent), and without a heartbeat a killed worker goes unnoticed for hours. Asserts both the
    arguments the workflow passes and that the walk beats.
    """
    from chemclaw.connectors.results import workflows as republish
    from chemclaw.connectors.results.specs import RepublishSpec
    from chemclaw.core.config import settings
    from chemclaw.publish.backfill import WalkCounts

    # A republish refuses before it scans when no sink is enabled, which is the shipped default.
    monkeypatch.setattr(republish, "unpublishable_reason", lambda: None)

    async def _empty_walk(**kwargs: object) -> WalkCounts:
        return WalkCounts()

    # --- the budget the workflow declares -------------------------------------------------
    captured: dict[str, Any] = {}

    async def _capture(*args: Any, **kwargs: Any) -> dict[str, int]:
        """Stand in for the activity, and answer with the report `_walk` itself produces.

        Not a dict literal, which would be a second definition of the report that goes stale when
        the walk counts something new.
        """
        captured.update(kwargs)
        monkeypatch.setattr(republish, "backfill_cached", _empty_walk)
        monkeypatch.setattr(republish, "backfill_jobs", _empty_walk)
        return await republish._walk(RepublishSpec())

    monkeypatch.setattr("temporalio.workflow.execute_activity", _capture)
    asyncio.run(republish.RepublishResultsWorkflow().run(RepublishSpec()))

    ceiling = timedelta(seconds=settings.connector_job_timeout_seconds)
    assert captured["start_to_close_timeout"] < ceiling, (
        f"the walk is budgeted {captured['start_to_close_timeout']} against a parent ceiling of "
        f"{ceiling}: they expire together, so the activity's retry policy is unreachable"
    )
    assert captured["heartbeat_timeout"] == timedelta(
        seconds=settings.result_republish_heartbeat_timeout_seconds
    )

    # --- and the walk itself beats -------------------------------------------------------
    beats: list[str] = []
    # `*a` with no index: the walk beats once eagerly with no details, because `beating()` waits a
    # whole interval before its first and a short walk would otherwise report nothing at all.
    monkeypatch.setattr(activity, "heartbeat", lambda *a: beats.append(str(a)))
    monkeypatch.setattr(settings, "result_republish_heartbeat_timeout_seconds", 4.0)

    async def _slow_walk(**kwargs: object) -> WalkCounts:
        await asyncio.sleep(1.3)
        return WalkCounts()

    async def _fast_walk(**kwargs: object) -> WalkCounts:
        return WalkCounts()

    monkeypatch.setattr(republish, "backfill_cached", _slow_walk)
    monkeypatch.setattr(republish, "backfill_jobs", _fast_walk)
    asyncio.run(republish.republish_stored_results(RepublishSpec()))

    assert len(beats) >= 2, (
        "a corpus walk that can take hours must beat *while it runs*, not only at the start: "
        f"beats={beats}"
    )
    assert any("still running" in beat for beat in beats)


def test_the_eviction_sweep_beats_inside_the_budget_it_reports_within(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The eviction sweep beats inside the budget it reports within.

    `evict_cold_artifacts` runs unbounded `DELETE`s under `retention_timeout_seconds`; without a
    beat a killed worker is invisible for the whole budget. No new setting:
    `Settings._the_heartbeat_fits_inside_the_budget_it_reports_within` already keeps the heartbeat
    timeout below that budget. Asserts the dispatch arguments and that the sweep beats.
    """
    from chemclaw.core.config import settings
    from chemclaw.durable import artifact_eviction

    captured: dict[str, Any] = {}

    async def _capture(*args: Any, **kwargs: Any) -> artifact_eviction.EvictionOutcome:
        captured.update(kwargs)
        return artifact_eviction.EvictionOutcome()

    monkeypatch.setattr("temporalio.workflow.execute_activity", _capture)
    asyncio.run(artifact_eviction.ArtifactEvictionWorkflow().run())

    budget = timedelta(seconds=settings.retention_timeout_seconds)
    assert captured["start_to_close_timeout"] == budget
    assert captured["heartbeat_timeout"] == timedelta(
        seconds=settings.background_activity_heartbeat_timeout_seconds
    ), (
        "the eviction sweep carries no heartbeat timeout, so the beats below buy nothing: a dead "
        f"worker is noticed only when {budget} of start-to-close budget expires"
    )
    assert captured["heartbeat_timeout"] < budget, (
        "a heartbeat timeout at or above the budget it sits under can never fire first"
    )

    # --- and the sweep itself beats -------------------------------------------------------
    beats: list[str] = []
    monkeypatch.setattr(activity, "heartbeat", lambda *a: beats.append(str(a)))
    monkeypatch.setattr(settings, "background_activity_heartbeat_timeout_seconds", 4.0)
    monkeypatch.setattr(settings, "artifact_evict_idle_days", 30)

    async def _slow_pass() -> artifact_eviction.EvictionOutcome:
        await asyncio.sleep(1.3)
        return artifact_eviction.EvictionOutcome(idle_blobs=1, idle_bytes=2)

    monkeypatch.setattr(artifact_eviction, "_evict_cold_artifacts", _slow_pass)
    outcome = asyncio.run(artifact_eviction.evict_cold_artifacts())

    assert outcome.idle_blobs == 1, "the wrapper swallowed the pass's own result"
    assert beats, (
        "a sweep that scans the whole blob store must beat *while it runs*, not only at the start"
    )
    assert any("still running" in beat for beat in beats)


def test_every_beating_activity_in_durable_is_dispatched_with_a_heartbeat_timeout() -> None:
    """Every beating activity in `durable` is dispatched with a heartbeat timeout, and vice versa.

    Temporal checks only the interval a dispatch declares, so a beating activity without one is
    invisible for its whole budget, and one declared over an activity that never beats kills healthy
    work. Derived from the tree: activities whose body calls `activity.heartbeat()` or
    `beating(...)`, and the `execute_activity(<name>, ...)` sites naming them, so a new activity is
    covered when written.
    """
    beats: set[str] = set()
    dispatched: dict[str, set[str]] = {}
    for path in sorted((_SRC_ROOT / "durable").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                body = ast.dump(node)
                if "'heartbeat'" in body or "'beating'" in body:
                    beats.add(node.name)
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in {"execute_activity", "start_activity"}:
                continue
            if not (node.args and isinstance(node.args[0], ast.Name)):
                continue
            where = f"{path.name}:{node.lineno}"
            dispatched.setdefault(node.args[0].id, set()).add(
                where if any(kw.arg == "heartbeat_timeout" for kw in node.keywords) else f"!{where}"
            )

    unheard = sorted(
        f"{name} at {site[1:]}"
        for name, sites in dispatched.items()
        if name in beats
        for site in sites
        if site.startswith("!")
    )
    assert unheard == [], (
        "these activities heartbeat but are dispatched without a heartbeat_timeout, so the beat "
        f"is unobserved and a dead worker is invisible for the whole start-to-close: {unheard}"
    )
    silent = sorted(
        f"{name} at {site}"
        for name, sites in dispatched.items()
        if name not in beats
        for site in sites
        if not site.startswith("!")
    )
    assert silent == [], (
        "these dispatches declare a heartbeat_timeout over an activity that never beats, so "
        f"Temporal will kill work that is running normally: {silent}"
    )
