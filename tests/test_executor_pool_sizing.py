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
import time

import pytest

from chemclaw.core.config import settings
from chemclaw.core.executor import front_door_reserved, install_default_executor

#: How long each stand-in for "a corpus parse on an executor thread" blocks. Half of it holds the
#: GIL and half releases it, because that is the shape of `load_notes`/`build_graph` and because a
#: pure `time.sleep` would understate what a queued caller waits for.
_BLOCK_SECONDS = 0.2


def _block() -> None:
    """One offloaded parse: a GIL-holding half and a file-I/O half."""
    end = time.perf_counter() + _BLOCK_SECONDS / 2
    while time.perf_counter() < end:
        pass
    time.sleep(_BLOCK_SECONDS / 2)


def _short_call_ms(*, pool_reserved: int, offloads: int) -> float:
    """Saturate a pool sized for `pool_reserved` with `offloads` parses, then time a tiny call.

    The tiny call stands in for `api/auth.py`'s `await asyncio.to_thread(validate_token, ...)`,
    which every authenticated request makes. What is returned is the wait an operator feels.
    """

    async def scenario() -> float:
        install_default_executor(component="front-door", reserved=pool_reserved)
        blocking = [asyncio.create_task(asyncio.to_thread(_block)) for _ in range(offloads)]
        # Let every blocking call reach a thread (or the queue) before the short one is submitted.
        await asyncio.sleep(0.05)
        started = time.perf_counter()
        await asyncio.to_thread(lambda: None)
        waited = (time.perf_counter() - started) * 1000
        await asyncio.gather(*blocking)
        return waited

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

    Measured on a 4-core sandbox at 96 offloads of 200 ms: 762.7 ms against 123.2 ms worst case.
    The assertion is a ratio against `_BLOCK_SECONDS` rather than either figure, because absolute
    milliseconds on shared CI hardware are not a claim anybody can keep true.
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
