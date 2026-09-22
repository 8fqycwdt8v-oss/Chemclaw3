"""Async primitives that keep working when one process runs more than one event loop.

**A module-level `asyncio.Lock()` is not process-scoped state, although it looks exactly like it.**
`Lock.acquire` resolves its event loop lazily *and only on the contended path* — the uncontended
fast path marks the lock held and returns without ever calling `_get_loop()` — so it binds itself
to the first loop that races on it, which is to say the first time it does its job. Every use
before that is silent about the binding.

What happens next is not the error it sounds like. A second loop's *waiter* raises
`RuntimeError: <Lock object ...> is bound to a different event loop`, while the *holder* keeps the
lock; `asyncio.run` then tries to cancel that holder on its way out and the cancellation does not
complete, so the observable symptom is a **hang with no exception**, and the lock is left
permanently `[locked]` for every later user in that process. Measured on `kg/git_writer`'s write
lock: one `asyncio.run` of two concurrent writes passes in 3.4 s, and the identical second call in
the same process never returns.

**One loop per process is this deployment's shape and that is not the whole story.** The API
serves on one loop, a Temporal worker runs one, and a CLI command is one `asyncio.run`. But a test
harness that calls `pytest.main()` repeatedly is one process with many loops — `mutmut` does
exactly this, once for stats, once for the clean baseline and once per mutant — and so is any
command that calls `asyncio.run` twice. The cost of being wrong there is not a slow path, it is a
wedge.

So a lock here is resolved **per running loop**, created on first use and dropped with the loop
that owns it. Two loops in one process do not serialize against each other, which is a real
weakening of "serializes every write in this process" and is stated where each one is declared: it
is reachable only from two *simultaneous* loops in separate threads, where the callers here are
already protected by an OS-level `flock` and a connect that costs a second channel rather than
correctness. Sequential loops — the case that actually occurs — get exactly the old semantics.
"""

from __future__ import annotations

import asyncio
from types import TracebackType


class LoopLocalLock:
    """An `asyncio.Lock` per running event loop, resolved on use rather than at import.

    A drop-in replacement for a module-level `asyncio.Lock` at the call site: it is an async
    context manager, so `async with _WRITE_LOCK:` is unchanged. The loop is resolved in both
    `__aenter__` and `__aexit__` rather than remembered between them, because a context manager's
    two halves run in one task on one loop — so re-resolving is the same dictionary lookup and
    leaves nothing to get out of step.

    **A plain dict that discards closed loops, and not a `WeakKeyDictionary`, which cannot work
    here.** A weak mapping keyed by the loop is the obvious shape and it leaks by construction: the
    *value* is an `asyncio.Lock`, and a contended one stores a reference to its own loop — its own
    key — so the entry keeps itself alive forever. Measured over three `asyncio.run` calls, with a
    collection in between: **0** entries survive when the lock is never contended and **3** when it
    is, each one's value holding its own key. The contended case is the only one this exists for,
    so the weak
    mapping would have released exactly the entries that do not matter.

    Discarding closed loops on resolve holds the same property by a route that is checkable: a
    closed loop's lock can never be used again, so the map is pruned each time it is read and holds
    one entry in the shape that actually occurs. The cost is an `is_closed()` per entry on a path
    that already takes a lock.
    """

    __slots__ = ("_locks", "_name")

    def __init__(self, name: str) -> None:
        """Create the holder.

        Args:
            name: What this lock serializes, for the error a use outside a running loop raises.
                A bare `RuntimeError: no running event loop` from inside a library says nothing
                about which lock was reached from synchronous code.
        """
        self._name = name
        self._locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}

    def _lock(self) -> asyncio.Lock:
        """The lock belonging to the loop running right now, created if this is its first use."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RuntimeError(f"{self._name} was reached outside a running event loop") from exc
        for known in [known for known in self._locks if known.is_closed()]:
            del self._locks[known]
        lock = self._locks.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[loop] = lock
        return lock

    def locked(self) -> bool:
        """Whether this loop's lock is held, mirroring `asyncio.Lock.locked`."""
        return self._lock().locked()

    async def __aenter__(self) -> None:
        """Acquire this loop's lock."""
        await self._lock().acquire()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release this loop's lock."""
        self._lock().release()
