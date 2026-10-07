"""Async primitives that keep working when one process runs more than one event loop.

A module-level `asyncio.Lock()` binds to the first loop that contends on it; a later loop's waiter
then raises while the holder can never be cancelled, so the process hangs with the lock held. One
process can run many loops in sequence (a test harness calling `pytest.main()` repeatedly, a
command calling `asyncio.run` twice), so locks here are resolved per running loop. Two
simultaneous loops in separate threads do not serialize against each other; each declaration
states why that is acceptable for its callers.
"""

from __future__ import annotations

import asyncio
from types import TracebackType


class LoopLocalLock:
    """An `asyncio.Lock` per running event loop, resolved on use rather than at import.

    A drop-in for a module-level lock: `async with _WRITE_LOCK:` is unchanged. Both halves of the
    context manager run on one loop, so re-resolving in `__aexit__` is safe.

    A plain dict pruned of closed loops on every resolve, not a `WeakKeyDictionary`: a contended
    lock references its own loop (its key), so a weak mapping would keep exactly those entries
    alive.
    """

    __slots__ = ("_locks", "_name")

    def __init__(self, name: str) -> None:
        """Create the holder.

        Args:
            name: What this lock serializes, named in the error raised when it is used outside a
                running loop.
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
