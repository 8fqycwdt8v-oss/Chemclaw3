"""Lease one agent — and with it one chat client — to one turn at a time (D-123).

**Why this exists, precisely.** `agent_framework_anthropic`'s streaming parser keeps the identity
of the tool call it is currently reading on the *client instance*:

```python
case "tool_use": self._last_call_id_name = (content_block.id, content_block.name)
...
case "input_json_delta":
    call_id = self._last_call_id_name[0] if self._last_call_id_name else ""
    contents.append(Content.from_function_call(call_id=call_id, name="", ...))
```

An argument delta carries `name=""` by design and recovers its identity from that attribute. So two
turns streaming through one client interleave: turn B's `tool_use` overwrites `_last_call_id_name`
between turn A's `tool_use` and A's deltas, A's arguments are filed under B's call id, and A's
assistant message goes out with a `tool_use` block whose name is the empty string. Anthropic rejects
it — `tool_use.name: String should have at least 1 character` — and the turn dies.

Measured against live Haiku, 8 attempts per configuration: sequential turns never failed (24/24
across three variants); 8 concurrent turns on one shared agent failed **8/8**; 8 concurrent turns on
per-turn agents passed **8/8**; and 8 concurrent turns with per-turn agents but a **shared client**
failed **8/8** — which is what names the client, not the agent, as the thing that cannot be shared.

**Why a pool rather than building per turn.** Building is not expensive enough to fear (~90 ms for
the agent, ~95 ms for the client) but a fresh client means a fresh `AsyncAnthropic`, and therefore a
fresh connection pool and TLS handshake, on every turn — reintroducing exactly the per-call
handshake churn D-119 removed from Postgres. A lease keeps connections warm across turns while
guaranteeing that no two *concurrent* turns touch one client, which is the only thing that matters.

**Why the size is the admission cap.** `service_max_concurrent_turns` already bounds how many turns
run at once, so a pool of that size is never the queue — the semaphore is. Sized larger it would
waste clients; smaller it would serialise turns behind a resource the admission gate already
counted.

This is a workaround for an upstream defect and is written to be deleted: when the parser keeps that
state per stream, the pool can collapse back to one shared agent per profile and this module goes
away. `DEFERRED.md` records the trigger.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

logger = logging.getLogger(__name__)


class AgentPool:
    """A bounded set of agents per profile, leased to one turn at a time.

    Agents are built lazily — a deployment that never uses a profile never pays for it — and kept
    for the process's life once built, so a turn's cost is a queue `get`, not a construction.
    """

    def __init__(self, factory: Callable[[str | None], Any], size: int) -> None:
        """Lease from `factory`, holding at most `size` agents per profile.

        Args:
            factory: Builds an agent for one profile name (`None` = the default profile). The same
                factory the front door used when it cached a single agent per profile.
            size: Agents per profile, normally `service_max_concurrent_turns`.
        """
        if size < 1:
            raise ValueError(f"agent pool size must be at least 1, got {size}")
        self._factory = factory
        self._size = size
        self._free: dict[str | None, asyncio.LifoQueue[Any]] = {}
        self._built: dict[str | None, int] = {}
        # Guards the lazy-build bookkeeping only, never held across a turn.
        self._lock = asyncio.Lock()

    async def _checkout(self, profile: str | None) -> Any:
        """Take a free agent for `profile`, building one if the pool has not reached its size."""
        async with self._lock:
            free = self._free.setdefault(profile, asyncio.LifoQueue())
            built = self._built.get(profile, 0)
            if free.empty() and built < self._size:
                self._built[profile] = built + 1
                # Built outside the queue so a first turn does not wait on a `put`; released back
                # into it when the turn ends, like any other member.
                logger.debug("building agent %d/%d for profile %r", built + 1, self._size, profile)
                return self._factory(profile)
        # Pool is at size: wait for a turn to finish. Under normal operation this does not block,
        # because admission control already caps concurrency at the same number.
        return await self._free[profile].get()

    @asynccontextmanager
    async def lease(self, profile: str | None = None) -> AsyncIterator[Any]:
        """Hold one agent for the duration of a turn, returning it even if the turn raises.

        The release is unconditional: a turn that fails, is cancelled by a disconnecting client, or
        times out must not retire an agent from the pool, or a pod would bleed capacity until it
        deadlocked on a queue nothing ever fills.
        """
        agent = await self._checkout(profile)
        try:
            yield agent
        finally:
            self._free[profile].put_nowait(agent)
