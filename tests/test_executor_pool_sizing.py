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
import threading
import time
from dataclasses import dataclass

import pytest

from chemclaw.core.config import settings
from chemclaw.core.executor import front_door_reserved, install_default_executor

#: How long any wait in this file may take before it is a failure rather than a wait. Nothing here
#: is *measured* against it: every wait below ends on an event the code under test produces, and
#: this only bounds how long a broken pool — one that never starts an offload, or never runs the
#: short call — can hang the suite. It sits far from the passing case (milliseconds) on purpose,
#: which is lesson 59's rule: a deadline separates two outcomes, not two speeds.
_BACKSTOP_SECONDS = 30.0


@dataclass(frozen=True)
class _Saturation:
    """What one arm observed about its pool.

    How wide it was, how many threads the fan-out held, and whether the short call had to wait
    for the fan-out to let go before it could run.
    """

    width: int
    held: int
    queued: bool


def _short_call_under_fan_out(*, pool_reserved: int, offloads: int) -> _Saturation:
    """Fill a pool sized for `pool_reserved` with `offloads` parses, then submit one tiny call.

    The tiny call stands in for `api/auth.py`'s `await asyncio.to_thread(validate_token, ...)`,
    which every authenticated request makes. What is returned is not how long it waited but
    **whether it could run at all while every offload was still in flight** — the property the
    pool's width decides, observed rather than inferred from a clock.

    **Why not a clock, which is what this used to be.** It timed the short call and asserted the
    narrow arm waited more than half a block, and CI kept sampling the narrow arm at **1.1 ms** —
    most recently on PR #469 and on `main` run 36247322939, green on rerun. The offloads ended on a
    wall-clock deadline, while the short call was submitted whenever the event loop next got the GIL
    back from twenty-odd threads contending for it — so what the arm measured was a race between the
    fan-out draining and the loop being scheduled, which the pool's width does not decide. Measured
    in the gate container (8 cores, the old width, 98 offloads, twelve runs per arm): the lag from
    "the pool is saturated" to "the short call is submitted" was **81-416 ms** idle and
    **570-1,462 ms** with twelve CPU-spinning processes beside it, the queue in front of the call
    fell from **52-72** items to as few as **12**, and the call's wait from ~0.9-1.3 s to
    **197 ms**. A runner loaded further than that reaches an empty queue, and 1.1 ms is what an
    empty queue looks like. Best-of-five, then the median, then a start semaphore each narrowed
    the race and none removed it, because each still read the answer off elapsed time.

    **So every offload now holds its thread on a gate, not on a sleep.** Nothing finishes until the
    gate opens, the count of held threads is read from the loop without borrowing one, and the short
    call is submitted while that count is the whole truth about the pool. Then:

    - if the fan-out holds **every** thread, the short call cannot run until the gate opens — it
      reports the gate open when it finally does, which is its own record of having queued;
    - if a thread is **free**, the short call is awaited *before* the gate opens, so it can only
      complete by running beside the held fan-out, and it reports the gate still shut.

    Both outcomes are decided by the width and by nothing about the machine's speed. The only
    timing left is `_BACKSTOP_SECONDS`, which a correct run never approaches.
    """

    async def scenario() -> _Saturation:
        loop = asyncio.get_running_loop()
        pool = install_default_executor(component="front-door", reserved=pool_reserved)
        width = pool._max_workers
        gate = threading.Event()
        lock = threading.Lock()
        started = 0

        def hold() -> None:
            nonlocal started
            with lock:
                started += 1
            gate.wait(_BACKSTOP_SECONDS)

        blocking = [loop.run_in_executor(None, hold) for _ in range(offloads)]
        try:
            # A pool wider than the fan-out never fills, which is the wide arm's whole point.
            target = min(width, offloads)
            deadline = time.monotonic() + _BACKSTOP_SECONDS
            while True:
                with lock:
                    held = started
                if held >= target:
                    break
                assert time.monotonic() < deadline, (
                    f"only {held} of {target} offloads reached a thread in {_BACKSTOP_SECONDS}s"
                )
                await asyncio.sleep(0.001)
            # `run_in_executor` submits synchronously, so the call is in the pool's queue — or on a
            # free thread — before anything else here runs.
            short = loop.run_in_executor(None, gate.is_set)
            if held < width:
                await asyncio.wait_for(asyncio.shield(short), _BACKSTOP_SECONDS)
        finally:
            gate.set()
        ran_after_release = await asyncio.wait_for(short, _BACKSTOP_SECONDS)
        await asyncio.gather(*blocking)
        return _Saturation(width=width, held=held, queued=ran_after_release)

    return asyncio.run(scenario())


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

    Asserted as the property rather than as a duration (`_short_call_under_fan_out` says why the
    duration kept lying): at the old width the fan-out holds every thread and the short call runs
    only once it lets go; at this width the fan-out leaves threads free and the short call runs
    beside it. Measured on a 4-core sandbox at 96 offloads of 200 ms, the difference used to be
    762.7 ms against 123.2 ms worst case — which is what the queueing costs, and what this now
    proves happens rather than how long it takes.
    """
    offloads = front_door_reserved()
    old_width = settings.service_max_concurrent_turns + settings.attachment_max_concurrent_parses

    narrow = _short_call_under_fan_out(pool_reserved=old_width, offloads=offloads)
    wide = _short_call_under_fan_out(pool_reserved=offloads, offloads=offloads)

    assert offloads > narrow.width, (
        f"{offloads} offloads no longer overfill the old pool of {narrow.width} threads, so the "
        "counterfactual this test exists for cannot arise; the caps or the headroom moved"
    )
    assert narrow.held == narrow.width and narrow.queued, (
        f"at the old width the fan-out held {narrow.held} of {narrow.width} threads and the short "
        "call ran while it was still in flight; this test is no longer reproducing the queuing it "
        "exists to fix"
    )
    assert wide.held == offloads and not wide.queued, (
        f"widening the pool from {old_width} to {offloads} reserved threads left the short call "
        f"queued behind the fan-out ({wide.held} of {wide.width} threads held); sizing for the "
        "fan-out bought nothing and this repository should not pay for threads it does not need"
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
