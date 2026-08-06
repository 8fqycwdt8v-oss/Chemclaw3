"""`chemclaw.durable.heartbeat.beating` — the shared heartbeat-while-waiting idiom (Conn-F2).

Extracted once it had three independent copies: `connectors.calc`'s two CREST jobs (REV-3,
D-136), and `connectors.bo`'s BoFire fit/acquisition step. This is the generic timer behavior;
each caller's own wiring (that it passes *its* configured heartbeat timeout through) is pinned
where that caller lives (`tests/test_calc_heartbeat.py`, `tests/test_bo_heartbeat.py`).
"""

import asyncio

import pytest
from temporalio import activity

from chemclaw.durable.heartbeat import beating


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


def test_a_cancelled_wait_cancels_the_work_it_wraps(monkeypatch: pytest.MonkeyPatch) -> None:
    """The finding: `asyncio.wait` does not cancel what it waits on.

    A chemist's `cancel_job` cancels the activity, which raises `CancelledError` here — and the
    wrapped task simply kept running, unowned, with nothing left to await it. For calc that is a
    CREST search and for bo a surrogate fit; both were promised to stop and neither did.
    """
    monkeypatch.setattr(activity, "heartbeat", lambda *a: None)
    finished: list[str] = []
    cancelled: list[str] = []

    async def _long() -> str:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append("stopped")
            raise
        finished.append("ran to completion")
        return "done"

    async def _run() -> list[str]:
        wait = asyncio.ensure_future(beating(_long(), "a long search", 4.0))
        await asyncio.sleep(0.05)
        wait.cancel()
        with pytest.raises(asyncio.CancelledError):
            await wait
        # **Asserted inside the loop, and that is the whole test.** The first version asserted
        # after `asyncio.run` returned, and passed with the fix mutated out — because `asyncio.run`
        # cancels every pending task on shutdown, so the orphan was stopped by the loop closing
        # rather than by the code under test. Read there, the question is only "did the process
        # exit"; read here, it is "did `cancel_job` stop the search", which is the one asked.
        return list(cancelled)

    stopped_at_cancel = asyncio.run(_run())
    assert stopped_at_cancel == ["stopped"], "the wrapped work was orphaned rather than cancelled"
    assert finished == []


def test_a_failure_in_the_wait_also_stops_the_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other non-normal exit, and the one a `heartbeat` raise produces.

    `activity.heartbeat` raises when Temporal has already cancelled the activity, so the beat
    itself is a way out of this loop — and it left the same orphan.
    """
    cancelled: list[str] = []

    def _heartbeat_raises(*_args: object) -> None:
        raise RuntimeError("activity already cancelled")

    monkeypatch.setattr(activity, "heartbeat", _heartbeat_raises)

    async def _long() -> str:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append("stopped")
            raise
        return "done"

    async def _run() -> list[str]:
        with pytest.raises(RuntimeError, match="already cancelled"):
            await beating(_long(), "a long search", 4.0)
        # Inside the loop, for the reason the test above records.
        return list(cancelled)

    assert asyncio.run(_run()) == ["stopped"]


def test_the_normal_path_does_not_cancel_the_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bound: a cancel in the `finally` must not reach work that already finished.

    Without the `done()` guard this would cancel the completed task on every successful call —
    harmless for a finished future, and exactly the kind of "harmless" that stops being so the
    day the wrapped awaitable is a task someone else also holds.
    """
    monkeypatch.setattr(activity, "heartbeat", lambda *a: None)

    async def _quick() -> str:
        return "fast"

    assert asyncio.run(beating(_quick(), "quick", 600.0)) == "fast"
