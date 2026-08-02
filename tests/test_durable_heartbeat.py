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
