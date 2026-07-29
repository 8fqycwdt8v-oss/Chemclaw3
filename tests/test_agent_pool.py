"""One agent per concurrent turn, so two streams never share a chat client (D-123).

The unit half of the fix for a defect that only a live concurrent run exposed:
`agent_framework_anthropic` keeps the identity of the tool call it is currently parsing on the
*client instance* (`self._last_call_id_name`), and an argument delta carries `name=""` and recovers
its identity from that attribute. Two turns streaming through one client interleave, one turn's
arguments are filed under the other's call id, and an assistant `tool_use` block goes out with an
empty name — which Anthropic rejects outright. It killed 20% of turns in a live 50-user run.

These tests assert the property that makes that impossible — *no agent is ever held by two turns at
once* — without needing a model, because the exclusivity is what the fix is; the corruption is
someone else's code.
"""

import asyncio

import pytest

from agents.agent_pool import AgentPool


class _Agent:
    """A stand-in agent that only needs to be distinguishable from its siblings."""

    def __init__(self, index: int) -> None:
        self.index = index


def test_a_lease_is_returned_and_reused() -> None:
    """Sequential turns reuse one agent — the pool is not a per-turn factory.

    This is the whole reason it is a pool: a fresh client per turn would mean a fresh
    `AsyncAnthropic`, and so a fresh connection pool and TLS handshake, on every turn.
    """
    built = 0

    def factory(_profile: str | None) -> _Agent:
        nonlocal built
        built += 1
        return _Agent(built)

    pool = AgentPool(factory, size=4)

    async def _run() -> list[int]:
        seen = []
        for _ in range(5):
            async with pool.lease() as agent:
                seen.append(id(agent))
        return seen

    seen = asyncio.run(_run())
    assert len(set(seen)) == 1, "sequential turns should hand the same agent back and forth"
    assert built == 1, "only one agent should ever have been built"


def test_concurrent_turns_never_share_an_agent() -> None:
    """The property the live defect violated: no agent is held by two turns at once.

    Counterfactually verified — leasing the *same* object to every caller (the pre-D-123 shared
    agent) fails this immediately.
    """
    pool = AgentPool(lambda _profile: _Agent(0), size=8)
    held: set[int] = set()
    overlaps: list[int] = []

    async def turn() -> None:
        async with pool.lease() as agent:
            if id(agent) in held:
                overlaps.append(id(agent))
            held.add(id(agent))
            await asyncio.sleep(0.01)  # hold it while the others are also inside their leases
            held.discard(id(agent))

    asyncio.run(_gather(turn, 8))
    assert overlaps == [], "an agent was leased to two concurrent turns"


def test_the_pool_is_bounded_and_waits_rather_than_building_more() -> None:
    """Past its size the pool queues, so a burst cannot multiply clients without limit.

    Sized to `service_max_concurrent_turns`, so in production admission control is the queue and
    this bound is never the thing a turn waits on.
    """
    built = 0

    def factory(_profile: str | None) -> _Agent:
        nonlocal built
        built += 1
        return _Agent(built)

    pool = AgentPool(factory, size=2)

    async def turn() -> None:
        async with pool.lease():
            await asyncio.sleep(0.01)

    asyncio.run(_gather(turn, 6))
    assert built == 2, f"pool of 2 built {built} agents for 6 concurrent turns"


def test_a_failing_turn_returns_its_lease() -> None:
    """A turn that raises must not retire an agent, or a pod bleeds capacity until it deadlocks."""
    pool = AgentPool(lambda _profile: _Agent(0), size=1)

    async def _run() -> str:
        with pytest.raises(RuntimeError):
            async with pool.lease():
                raise RuntimeError("turn blew up")
        # If the lease leaked, the pool of one is empty and this waits forever.
        async with asyncio.timeout(1):
            async with pool.lease() as agent:
                return type(agent).__name__

    assert asyncio.run(_run()) == "_Agent"


def test_profiles_get_their_own_agents() -> None:
    """A profile is a different tool surface, so its agents are a different pool."""
    pool = AgentPool(lambda profile: _Agent(0), size=2)

    async def _run() -> tuple[int, int]:
        async with pool.lease("admin") as a, pool.lease(None) as b:
            return id(a), id(b)

    first, second = asyncio.run(_run())
    assert first != second


def test_a_pool_must_hold_at_least_one_agent() -> None:
    """A size of zero would deadlock the first turn; reject it at construction."""
    with pytest.raises(ValueError, match="at least 1"):
        AgentPool(lambda _profile: _Agent(0), size=0)


def test_a_failing_factory_does_not_burn_a_slot() -> None:
    """A factory that raises must not consume pool capacity — or the pod deadlocks permanently.

    Counting the agent before building it meant a raising factory left `_built` describing an
    agent that was never created and never returned to the free queue. After `size` such failures
    the pool could neither build (it believed it was full) nor hand one out (nothing was ever put),
    so every later turn blocked until the front door's turn timeout, forever, on a pod still
    reporting healthy.

    This is reachable: `build_agent` constructs the chat client, which reads the TLS CA bundle from
    disk and requires a credential — so a pod taking its first turns before its secret volume is
    populated hits exactly this. The failures here exhaust `size` twice over to prove the slot is
    genuinely reclaimed rather than merely delayed.
    """
    attempts = 0

    def _factory(_profile: str | None) -> _Agent:
        nonlocal attempts
        attempts += 1
        if attempts <= 4:  # twice the pool size, all failing
            raise RuntimeError("credential volume not ready")
        return _Agent(attempts)

    pool = AgentPool(_factory, size=2)

    async def _run() -> object:
        for _ in range(4):
            with pytest.raises(RuntimeError, match="credential volume not ready"):
                async with pool.lease():
                    pass
        # The pool must still be able to build once the transient cause clears.
        async with pool.lease() as agent:
            return agent

    assert asyncio.run(asyncio.wait_for(_run(), timeout=5)) is not None


async def _gather(turn: object, count: int) -> None:
    """Run `count` copies of `turn` concurrently."""
    await asyncio.gather(*(turn() for _ in range(count)))  # type: ignore[operator]
