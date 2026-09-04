"""`chemclaw.durable.heartbeat.beating` — the shared heartbeat-while-waiting idiom (Conn-F2).

Extracted once it had three independent copies: `connectors.calc`'s two CREST jobs (REV-3,
D-136), and `connectors.bo`'s BoFire fit/acquisition step. This is the generic timer behavior;
each caller's own wiring (that it passes *its* configured heartbeat timeout through) is pinned
where that caller lives (`tests/test_calc_heartbeat.py`, `tests/test_bo_heartbeat.py`).
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

    Each caller passes its own configured setting (`xtb_job_heartbeat_timeout_seconds`,
    `bo_activity_heartbeat_timeout_seconds`, ...) rather than a fixed constant, so a deployment
    that shortens one caller's timeout must shorten only that caller's beat interval. Proven by
    running the same 1.3 s wait against two different timeouts: a short one (interval ~1 s) beats
    at least once, a long one (interval well past 1.3 s) never does.
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
    """The floor, asserted as a rate rather than as an expression.

    `max(1.0, timeout / 4)` is the whole reason the hand-rolled copies had to go: three modules
    derived the interval as `timeout / 3` with no floor, so a deployment that set the timeout to a
    fraction of a second turned a sibling task into several `activity.heartbeat()` calls per second
    against the Temporal server for the whole chunk. A 0.4 s timeout would give a 0.13 s interval
    without the floor — about ten beats across the wait below; with it, at most one.

    The floor and the schema constraint are not the same guard, which is why both exist.
    `document_sync_heartbeat_timeout_seconds` now carries `Field(gt=0)` — it was declared bare —
    so a zero or negative timeout is refused at load rather than surviving into an unbounded busy
    loop. A *positive* sub-second timeout is still a legal setting, and this floor is what keeps it
    from flooding the server; the constraint cannot express that, because the right value depends
    on the beat rate rather than on the number.
    """
    beats: list[str] = []
    monkeypatch.setattr(activity, "heartbeat", lambda *a: beats.append(str(a[0])))

    async def _slow() -> str:
        await asyncio.sleep(1.3)
        return "done"

    asyncio.run(beating(_slow(), "tiny timeout", 0.4))
    assert len(beats) <= 1, f"a 0.4s heartbeat timeout beat {len(beats)} times in 1.3s"


def test_cancelling_the_wrapper_cancels_the_work_it_wraps(monkeypatch: pytest.MonkeyPatch) -> None:
    """`beating(x)` must behave like `await x` under cancellation, or adopting it is a regression.

    The awaitable has to run as a task so the timer can run beside it, and `asyncio.wait` does not
    cancel what it was waiting on when the waiter is cancelled. The two sync activities that
    adopted this helper previously awaited their work directly, so a cancelled activity interrupted
    a half-written chunk; without this, the chunk would have kept running — and kept committing —
    after the activity returned.
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

    async def _cancel_mid_flight() -> None:
        wrapped = asyncio.ensure_future(beating(_slow(), "interrupted", 600.0))
        await asyncio.sleep(0.05)
        wrapped.cancel()
        with pytest.raises(asyncio.CancelledError):
            await wrapped
        # No sleep here, deliberately. A third version of this test had one — "let the inner task
        # act on its cancellation" — and that sleep was doing the waiting the helper should do:
        # `task.cancel()` only *requests* cancellation, so the wrapper was unwinding while the
        # work was still in its `except`/`finally`. The helper now awaits the cancelled task, so
        # by the time `await wrapped` returns the work has already finished unwinding.
        # Asserted *inside* the loop, deliberately: `asyncio.run` cancels every pending task on
        # its way out, so a check placed after it sees the work cancelled either way. That was the
        # second version of this test, and it also passed against the unfixed helper.
        assert reached_cancel, "the wrapped work was left running after the wrapper was cancelled"

    asyncio.run(_cancel_mid_flight())


def test_cancellation_waits_for_the_work_to_finish_unwinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The order, not just the fact: the work's cleanup completes *before* the caller unwinds.

    `task.cancel()` alone only files a request. Measured on the same loop, with the work holding a
    50 ms cleanup in its `except` block:

        cancel + raise    -> ['wrapper-returned', 'cleanup-start', 'cleanup-done']
        plain `await x`   -> ['cleanup-start', 'cleanup-done', 'wrapper-returned']

    That difference is the whole reason `eln_sync` and `document_sync` adopting this helper had to
    be checked: both previously awaited their work directly, where a cancelled activity does not
    return until the chunk's `finally` — the DB commit — is done. Anything less makes the window
    "the length of the work's cleanup" instead of closing it.
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

    async def _cancel_mid_flight() -> None:
        wrapped = asyncio.ensure_future(beating(_slow(), "interrupted", 600.0))
        await asyncio.sleep(0.05)
        wrapped.cancel()
        with pytest.raises(asyncio.CancelledError):
            await wrapped
        events.append("wrapper-returned")
        assert events == ["cleanup-start", "cleanup-done", "wrapper-returned"], events

    asyncio.run(_cancel_mid_flight())


def test_a_failing_heartbeat_does_not_leave_the_work_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation is not the only way out of the loop, and the other way leaked the same work.

    `activity.heartbeat` raises outside an activity context, and can raise inside one if the
    details payload fails to serialise. With the handler keyed on `CancelledError`, that exception
    left the wrapper while the wrapped task ran on detached — measured, the work printed
    "completed" a second *after* the wrapper had already raised, which is the exact defect the
    cancellation fix was written to close, reached through a different door. `finally` covers both.
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

    async def _run() -> None:
        # 4 s timeout -> a 1 s beat interval, so the first beat lands while the work is still
        # waiting and takes the wrapper out through the non-cancellation path. The work then
        # outlives the wrapper by 0.4 s, which is what makes "still running" observable rather
        # than a wall-clock guess — the trap the two earlier versions of the sibling test fell in.
        with pytest.raises(RuntimeError, match="Not in activity context"):
            await beating(_slow(), "interrupted", 4.0)
        await asyncio.sleep(0.8)
        assert outcome == ["cancelled"], outcome

    asyncio.run(_run())


def test_the_beat_interval_has_exactly_one_derivation_in_the_tree() -> None:
    """No module may divide a heartbeat timeout by hand again — that is what diverged.

    `durable/heartbeat.py` was extracted at the Rule of Three and then three further copies were
    written beside it (`durable/document_sync.py` twice, `durable/eln_sync.py` once), each dividing
    by 3 where the helper divides by 4 and none carrying its floor. The helper existing is not what
    prevents the next copy; this is.
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
    """The one multi-hour activity in the tree that neither beat nor had a budget of its own.

    `RepublishResultsWorkflow` runs a full scan of two never-pruned tables and gave its single
    activity **the parent's whole `connector_job_timeout_seconds`** — the same number
    `ConnectorJobWorkflow` uses as the child's execution timeout, so the two expired within
    milliseconds of each other and the activity's `BAD_DATA_RETRY` could never be spent. It also
    carried no `heartbeat_timeout` and never heartbeated, unlike every other long activity here
    (`calc`, `bo`, ELN, label, corpus, document, template steps), so a worker killed ten minutes
    into the walk went unnoticed for the whole five hours.

    Both halves are asserted here: the arguments the workflow hands the activity, and that the walk
    actually beats while it runs.
    """
    from chemclaw.connectors.results import workflows as republish
    from chemclaw.connectors.results.specs import RepublishSpec
    from chemclaw.core.config import settings

    # --- the budget the workflow declares -------------------------------------------------
    captured: dict[str, Any] = {}

    async def _capture(*args: Any, **kwargs: Any) -> dict[str, int]:
        captured.update(kwargs)
        return dict.fromkeys(
            (
                "requeued",
                "calculations_seen",
                "calculations_queued",
                "calculations_skipped",
                "jobs_seen",
                "jobs_queued",
                "jobs_skipped",
            ),
            0,
        )

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

    async def _slow_walk(**kwargs: object) -> tuple[int, int, int]:
        await asyncio.sleep(1.3)
        return (0, 0, 0)

    async def _fast_walk(**kwargs: object) -> tuple[int, int, int]:
        return (0, 0, 0)

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
    """The one background sweep on this queue that had no heartbeat at all.

    `prune_expired_rows`, `reindex_notes_activity` and `drain_result_publications` are its three
    siblings and every one of them beats; `evict_cold_artifacts` ran two unbounded `DELETE`
    statements over the whole `artifact_blobs`x`calculation_artifacts` join under a ten-minute
    `retention_timeout_seconds` and reported nothing in between. So a worker killed thirty seconds
    into a pass was invisible for the remaining nine and a half minutes, on a job whose entire
    purpose is to run unattended on a Schedule.

    No new setting: the activity is already budgeted by `retention_timeout_seconds`, and
    `Settings._the_heartbeat_fits_inside_the_budget_it_reports_within` already keeps
    `background_activity_heartbeat_timeout_seconds` strictly below it — so the beat this asserts is
    covered by the guard that already exists rather than by one more number to keep in step.

    Both halves, exactly as the republish walk above: the arguments the workflow hands the
    activity, and that the sweep actually beats while it runs.
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
