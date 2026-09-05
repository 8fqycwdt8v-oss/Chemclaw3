"""The shared `to_thread` pool is sized for what a turn can *fan out to*, not for what it is.

`core/executor.py` exists because `asyncio.to_thread` is one pool per process and this system
spends it on four unrelated things at once — token validation on every request, the retrieval and
knowledge-graph legs, embeddings, attachment parses. Its own docstring records the measurement that
motivated it, and `tests/test_concurrency_claims.py` holds the two claims that came out of it: a
short call must not queue behind a full admission cap, and the installed pool must be wider than
the caps that can fill it.

**This file is about the number those two tests take as given.** Both compute
`service_max_concurrent_turns + attachment_max_concurrent_parses` — the value `api/app.py` passes —
and then prove the pool is wider than *that*. Neither could see that the number is not the ceiling:
an admitted turn may run `agent_max_parallel_tool_calls` tool calls at once, and several of the
tools a turn reaches offload, so the caps admit `turns x parallel + parses` simultaneous offloads
and the pool was sized for `turns + parses`. A test that saturates with the sum and then asserts
the sum fits is self-consistent whatever the real fan-out is; the counterfactual below is what makes
the difference visible.
"""

import asyncio
import statistics
import threading
import time

import pytest

from chemclaw.core.config import settings
from chemclaw.core.executor import front_door_reserved, install_default_executor

#: How long each stand-in for "a corpus parse on an executor thread" blocks. Half of it holds the
#: GIL and half releases it, because that is the shape of `load_notes`/`build_graph` and because a
#: pure `time.sleep` would understate what a queued caller waits for.
_BLOCK_SECONDS = 0.2


def _block(started: threading.Semaphore | None = None) -> None:
    """One offloaded parse: a GIL-holding half and a file-I/O half.

    `started` is released the instant this lands on a thread, which is what lets the caller wait
    for the pool to be *actually* full rather than sleep and hope.
    """
    if started is not None:
        started.release()
    end = time.perf_counter() + _BLOCK_SECONDS / 2
    while time.perf_counter() < end:
        pass
    time.sleep(_BLOCK_SECONDS / 2)


def _short_call_ms(*, pool_reserved: int, offloads: int, trials: int = 3) -> float:
    """Saturate a pool sized for `pool_reserved` with `offloads` parses, then time a tiny call.

    The tiny call stands in for `api/auth.py`'s `await asyncio.to_thread(validate_token, ...)`,
    which every authenticated request makes. What is returned is the wait an operator feels.

    **The pool is saturated by waiting for it, not by sleeping at it — and that was the defect.**
    This used to `await asyncio.sleep(0.05)` and assume all `offloads` had reached the executor.
    `asyncio.to_thread` submits when its coroutine first runs, so on a loaded machine a fixed 50 ms
    leaves most of them unsubmitted, the narrow pool is *not* full, and the short call sails
    through. Measured on CI: **1.1 ms** on an arm whose entire purpose is to show a short call
    waiting, which this test then reported as "no longer reproducing the queuing it exists to fix"
    — a true statement about that run and a false one about the code. Every worker now releases a
    semaphore as it lands, and the caller waits for as many as the pool can run at once, so
    "saturated" is a fact of the run rather than a hope about its speed.

    That is also why the earlier attempts to fix this with statistics did not hold. A single sample
    became the best of five, which reads what a configuration *achieves* — right for the wide arm,
    wrong for the narrow one, where the best of five is precisely the run that failed to saturate.
    The repeat stays, at the median, because the wide arm still ranges 20-235 ms on one idle box
    and a lone sample is not an estimate; but the race is fixed where it lives.
    """

    async def scenario() -> float:
        pool = install_default_executor(component="front-door", reserved=pool_reserved)
        width = pool._max_workers
        started = threading.Semaphore(0)
        blocking = [
            asyncio.create_task(asyncio.to_thread(_block, started)) for _ in range(offloads)
        ]
        # Every thread the pool has is now running a block, so the next submission must queue.
        # `min` because a pool wider than the fan-out never fills, which is the wide arm's point.
        occupied = min(width, offloads)
        await asyncio.to_thread(lambda: [started.acquire() for _ in range(occupied)])
        submitted = time.perf_counter()
        await asyncio.to_thread(lambda: None)
        waited = (time.perf_counter() - submitted) * 1000
        await asyncio.gather(*blocking)
        return waited

    return statistics.median(asyncio.run(scenario()) for _ in range(trials))


def test_the_front_door_reserves_for_the_fan_out_a_permit_licenses() -> None:
    """A turn permit is a licence to run `agent_max_parallel_tool_calls` offloads, not one.

    Asserted as the relation rather than as today's numbers: the three settings all move, and a
    transcribed 98 would be stale the first time a cap is tuned — which is the drift the whole
    `core/executor.py` docstring is written against.
    """
    expected = (
        settings.service_max_concurrent_turns * settings.agent_max_parallel_tool_calls
        + settings.attachment_max_concurrent_parses
    )
    assert front_door_reserved() == expected
    assert front_door_reserved() > (
        settings.service_max_concurrent_turns + settings.attachment_max_concurrent_parses
    ), (
        "front_door_reserved() is no larger than the sum api/app.py used to pass, so either "
        "agent_max_parallel_tool_calls has become 1 or the fan-out term was dropped; the pool is "
        "sized for a turn that cannot fan out"
    )


def test_a_short_call_queues_at_the_old_width_and_does_not_at_this_one() -> None:
    """The counterfactual, because the width only matters against the load it was wrong about.

    Both arms run the *same* fan-out — what the front door's own caps admit — and differ only in
    how wide the pool installed under it is. The first arm is the shipped sizing and is what a
    token validation waited behind; the second is `front_door_reserved()`.

    Measured on a 4-core sandbox at 96 offloads of 200 ms: 762.7 ms against 123.2 ms worst case.
    The assertion is a ratio against `_BLOCK_SECONDS` rather than either figure, because absolute
    milliseconds on shared CI hardware are not a claim anybody can keep true.

    **The ratio is not enough on its own, which cost two red builds.** Both arms are timing
    samples, so a runner that stalls the *wide* one inverts a ratio just as readily as it inflates
    an absolute — CI sampled 228.5 ms narrow against 636.0 ms wide, which reads as "widening bought
    nothing" and is a claim about the runner. `_short_call_ms` repeats each arm five times for that
    reason. It takes the **median** and not the minimum, which was the second red build: the
    minimum is the right reading of the wide arm and the wrong one of the narrow arm, whose point
    is that a short call waits — best-of-five found the lucky run at 1.1 ms and this test announced
    it was no longer reproducing its own premise.
    """
    offloads = front_door_reserved()
    old_width = settings.service_max_concurrent_turns + settings.attachment_max_concurrent_parses

    narrow = _short_call_ms(pool_reserved=old_width, offloads=offloads)
    wide = _short_call_ms(pool_reserved=offloads, offloads=offloads)

    assert narrow > _BLOCK_SECONDS * 1000 / 2, (
        f"a short call waited only {narrow:.1f} ms behind {offloads} offloads at the old pool "
        f"width of {old_width}; this test is no longer reproducing the queuing it exists to fix"
    )
    assert wide < narrow / 2, (
        f"widening the pool from {old_width} to {offloads} reserved threads moved the queued short "
        f"call from {narrow:.1f} ms only to {wide:.1f} ms; sizing for the fan-out bought nothing "
        "and this repository should not pay for threads it does not need"
    )


def test_the_installed_pool_is_the_reserved_width_plus_the_headroom() -> None:
    """The headroom is what a short call actually lands in, so it must survive the fan-out term.

    Pinned here as well as in `tests/test_concurrency_claims.py` because that file asserts it
    against the sum: if a future change made `reserved` mean something the headroom is folded
    into, the property would be lost where it is now stated.
    """

    async def install() -> int:
        return install_default_executor(
            component="front-door", reserved=front_door_reserved()
        )._max_workers

    assert asyncio.run(install()) == (front_door_reserved() + settings.service_thread_pool_headroom)


def test_a_cap_change_moves_the_reservation_with_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The number is read from settings at call time, not frozen at import.

    An operator raising `CHEMCLAW_SERVICE_MAX_CONCURRENT_TURNS` must widen the pool with it — that
    is the whole reason the reservation is derived from the caps rather than written down, and a
    module-level constant would silently keep the old width.
    """
    monkeypatch.setattr(settings, "service_max_concurrent_turns", 3)
    monkeypatch.setattr(settings, "agent_max_parallel_tool_calls", 5)
    monkeypatch.setattr(settings, "attachment_max_concurrent_parses", 2)

    assert front_door_reserved() == 17
