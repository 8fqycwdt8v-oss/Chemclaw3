"""The two CREST jobs heartbeat while they run (REV-3, D-136).

Every other xTB task reports progress *between* units of work — one species, one solvent, one scan
point — and passes `activity.heartbeat` down as that callback. A CREST search has no unit boundary:
it is a single subprocess. So `run_cached_ensemble`/`run_cached_interaction` take no `progress`
argument, and the only beat these two jobs ever produced was the `"starting {kind}"` line at the
top of the activity.

That is the wrong way round. They are the only two jobs marked `expensive: true`, and their own
manifest says a search's cost "is not bounded by the input's size". Against a 600 s heartbeat
timeout a longer run was declared dead and retried from zero — the store is written only on
completion — so roughly fifty minutes of saturated CPU was spent failing a calculation that would
have succeeded.
"""

import asyncio

import pytest
from temporalio import activity

from chemclaw.connectors.calc import activities
from chemclaw.core.config import settings


def test_a_long_crest_run_heartbeats_while_it_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """A search longer than the heartbeat timeout keeps beating instead of being declared dead.

    The two CREST jobs are the only ones marked `expensive: true`, and their manifest says a
    search's cost is not bounded by the input's size. They took no `progress` callback — there is
    no unit boundary inside a single subprocess to report at — so the only beat was the
    `"starting {kind}"` line. Against a 600 s heartbeat timeout, a ten-minute search was declared
    dead and retried from zero up to five times.

    The timeout is shrunk here so the test costs milliseconds rather than minutes; what it pins is
    that beats arrive *during* the awaited work, which is the property that was missing.
    """
    beats: list[str] = []
    # 4 s timeout -> a 1 s beat interval (the helper divides, with a 1 s floor so production never
    # beats more often than that). The sleep must clear one interval for a beat to be observable.
    monkeypatch.setattr(settings, "xtb_job_heartbeat_timeout_seconds", 4.0)
    monkeypatch.setattr(activity, "heartbeat", lambda *a: beats.append(str(a[0])))

    async def _slow() -> str:
        await asyncio.sleep(1.3)
        return "done"

    result = asyncio.run(activities._beating(_slow(), "a long search"))
    assert result == "done"
    assert beats, "a run longer than the heartbeat timeout produced no heartbeat at all"
    assert all("still running" in b for b in beats)


def test_beating_returns_immediately_for_quick_work(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fast run pays nothing: no spurious beats, and the result comes straight back."""
    beats: list[str] = []
    monkeypatch.setattr(settings, "xtb_job_heartbeat_timeout_seconds", 600.0)
    monkeypatch.setattr(activity, "heartbeat", lambda *a: beats.append(str(a[0])))

    async def _quick() -> str:
        return "fast"

    assert asyncio.run(activities._beating(_quick(), "quick")) == "fast"
    assert beats == []


def test_beating_propagates_the_failure_it_wraps(monkeypatch: pytest.MonkeyPatch) -> None:
    """The heartbeat wrapper must not swallow the calculation's error."""
    monkeypatch.setattr(settings, "xtb_job_heartbeat_timeout_seconds", 600.0)
    monkeypatch.setattr(activity, "heartbeat", lambda *a: None)

    async def _boom() -> str:
        raise ValueError("crest failed")

    with pytest.raises(ValueError, match="crest failed"):
        asyncio.run(activities._beating(_boom(), "boom"))
