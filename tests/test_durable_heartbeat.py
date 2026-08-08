"""`chemclaw.durable.heartbeat.beating` — the shared heartbeat-while-waiting idiom (Conn-F2).

Extracted once it had three independent copies: `connectors.calc`'s two CREST jobs (REV-3,
D-136), and `connectors.bo`'s BoFire fit/acquisition step. This is the generic timer behavior;
each caller's own wiring (that it passes *its* configured heartbeat timeout through) is pinned
where that caller lives (`tests/test_calc_heartbeat.py`, `tests/test_bo_heartbeat.py`).
"""

import ast
import asyncio
from pathlib import Path

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
    derived the interval as `timeout / 3` with no floor, `document_sync_heartbeat_timeout_seconds`
    is declared as a bare `float` with no `Field(gt=0)`, and a deployment that set either to a
    fraction of a second turned a sibling task into several `activity.heartbeat()` calls per second
    against the Temporal server for the whole chunk. A 0.4 s timeout would give a 0.13 s interval
    without the floor — about ten beats across the wait below; with it, at most one.
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
        await asyncio.sleep(0.05)  # let the inner task act on its cancellation
        # Asserted *inside* the loop, deliberately: `asyncio.run` cancels every pending task on
        # its way out, so a check placed after it sees the work cancelled either way. That was the
        # second version of this test, and it also passed against the unfixed helper.
        assert reached_cancel, "the wrapped work was left running after the wrapper was cancelled"

    asyncio.run(_cancel_mid_flight())


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
