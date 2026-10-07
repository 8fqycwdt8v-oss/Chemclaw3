"""The shared `to_thread` pool is sized for what a turn can fan out to, not for what it is.

One pool serves token validation, retrieval, embeddings and attachment parses. An admitted turn
may run `agent_max_parallel_tool_calls` offloading tool calls at once, so the caps admit
`turns x parallel + parses` offloads. `tests/test_concurrency_claims.py` takes the reservation as
given; this file checks the reservation itself, with a counterfactual at the narrower width.
"""

import asyncio
import threading
import time
from dataclasses import dataclass

import pytest

from chemclaw.core.config import settings
from chemclaw.core.executor import front_door_reserved, install_default_executor

#: A backstop, not a measurement: every wait ends on an event the code produces, and this only
#: bounds how long a broken pool can hang the suite. Far from the passing case on purpose.
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

    The tiny call stands in for `api/auth.py`'s token validation. What is returned is whether it
    could run while every offload was still in flight, observed rather than timed: a clock measured
    a race with the event loop's scheduling, not the pool's width.

    Every offload holds its thread on a gate. The short call is submitted while the held count is
    the whole truth about the pool:

    - if the fan-out holds every thread, the short call runs only after the gate opens and reports
      it open;
    - if a thread is free, the short call is awaited before the gate opens and reports it shut.

    Only `_BACKSTOP_SECONDS` remains as timing, which a correct run never approaches.
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
    """A turn permit licenses `agent_max_parallel_tool_calls` offloads, not one.

    Asserted as the relation, since the three settings all move.
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
    """A short call queues at the old width and does not at this one.

    Both arms run the same fan-out the front door's caps admit and differ only in pool width. At the
    old width the fan-out holds every thread; at `front_door_reserved()` threads stay free and the
    short call runs beside it. Asserted as the property, not a duration.
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
    """The installed pool is the reserved width plus the headroom.

    The headroom is what a short call lands in, so it must stay outside the fan-out term.
    """

    async def install() -> int:
        return install_default_executor(
            component="front-door", reserved=front_door_reserved()
        )._max_workers

    assert asyncio.run(install()) == (front_door_reserved() + settings.service_thread_pool_headroom)


def test_a_cap_change_moves_the_reservation_with_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reservation is read from settings at call time, not frozen at import.

    Raising `CHEMCLAW_SERVICE_MAX_CONCURRENT_TURNS` must widen the pool with it.
    """
    monkeypatch.setattr(settings, "service_max_concurrent_turns", 3)
    monkeypatch.setattr(settings, "agent_max_parallel_tool_calls", 5)
    monkeypatch.setattr(settings, "attachment_max_concurrent_parses", 2)

    assert front_door_reserved() == 17
