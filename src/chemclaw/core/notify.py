"""Wake-ups over Postgres `LISTEN/NOTIFY`, in front of rows a waiter would otherwise poll.

A notification carries only a topic (which local waiters to wake); what changed is always re-read
from the rows, so a notification lost while the connection was down costs the waiter one fallback
poll, never a wrong answer. One dedicated `LISTEN` connection per (event loop, DSN, channel), opened
by the first subscriber and closed with the last, so a process with nobody waiting holds none. It
must dial a session-mode or direct endpoint, never a transaction-mode pooler
(`D-2026-10-08-the-pool-count-stays-and-a-pooler-gets-a-session-endpoint`).
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from weakref import WeakKeyDictionary

import psycopg
from psycopg import sql
from psycopg.rows import TupleRow

from chemclaw.core import db
from chemclaw.core.metrics_bridge import degraded

logger = logging.getLogger(__name__)

#: The statement a writer runs in its own transaction, so the wake-up is delivered on commit.
NOTIFY = "SELECT pg_notify(%s, %s)"


class Listener:
    """One `LISTEN` connection on one channel, shared by every waiter in its event loop.

    A broken connection wakes every subscriber (they re-read) and is reopened after
    `retry_seconds()`.
    """

    def __init__(self, dsn: str, channel: str, retry_seconds: Callable[[], float]) -> None:
        """Listen on `channel` at `dsn` once somebody subscribes; nothing is opened yet."""
        self._dsn = dsn
        self._channel = channel
        self._retry_seconds = retry_seconds
        self._events: dict[str, set[asyncio.Event]] = {}
        self._task: asyncio.Task[None] | None = None
        self._ready: asyncio.Event | None = None

    async def subscribe(self, topic: str) -> asyncio.Event:
        """Register for `topic` and return once the connection is listening (or has failed)."""
        event = asyncio.Event()
        self._events.setdefault(topic, set()).add(event)
        if self._task is None:
            self._ready = asyncio.Event()
            self._task = asyncio.get_running_loop().create_task(self._run(self._ready))
        ready = self._ready
        if ready is not None:
            try:
                await ready.wait()
            except BaseException:
                # Cancelled while connecting: the caller never entered its block, so nothing else
                # would forget this registration.
                await self.unsubscribe(topic, event)
                raise
        return event

    async def unsubscribe(self, topic: str, event: asyncio.Event) -> None:
        """Forget `event`; the last one out closes the connection."""
        waiting = self._events.get(topic)
        if waiting is not None:
            waiting.discard(event)
            if not waiting:
                del self._events[topic]
        if self._events or self._task is None:
            return
        task, self._task, self._ready = self._task, None, None
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task

    def _wake(self, topic: str | None) -> None:
        """Set the events waiting on `topic`, or every event when `topic` is `None`."""
        pools = self._events.values() if topic is None else [self._events.get(topic, set())]
        for events in pools:
            for event in events:
                event.set()

    async def _run(self, ready: asyncio.Event) -> None:
        """Listen until cancelled, reconnecting after a failure."""
        reconnecting = False
        while True:
            conn: psycopg.AsyncConnection[TupleRow] | None = None
            try:
                conn = await db.connect(self._dsn)
                await conn.set_autocommit(True)
                await conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(self._channel)))
                ready.set()
                if reconnecting:
                    self._wake(None)
                async for note in conn.notifies():
                    self._wake(note.payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                degraded(
                    logger,
                    "pg_listen",
                    "the %s listener lost its connection; waiters fall back to polling",
                    self._channel,
                    level=logging.WARNING,
                )
            finally:
                ready.set()
                if conn is not None:
                    with suppress(Exception):
                        await conn.close()
            reconnecting = True
            self._wake(None)
            await asyncio.sleep(self._retry_seconds())


_LISTENERS: "WeakKeyDictionary[asyncio.AbstractEventLoop, dict[tuple[str, str], Listener]]" = (
    WeakKeyDictionary()
)


def listener_for(dsn: str, channel: str, retry_seconds: Callable[[], float]) -> Listener:
    """This loop's listener for `channel` on `dsn`, created on first use (a task is loop-bound)."""
    per_loop = _LISTENERS.setdefault(asyncio.get_running_loop(), {})
    return per_loop.setdefault((dsn, channel), Listener(dsn, channel, retry_seconds))


@asynccontextmanager
async def woken_by(
    dsn: str, channel: str, topic: str, retry_seconds: Callable[[], float]
) -> AsyncIterator[asyncio.Event]:
    """An event set whenever `topic` is notified on `channel`, until the block ends.

    Subscribe before reading the rows it guards, and `clear()` before each read: a notification
    between the read and the wait then sets the event, rather than being missed.
    """
    listener = listener_for(dsn, channel, retry_seconds)
    event = await listener.subscribe(topic)
    try:
        yield event
    finally:
        await listener.unsubscribe(topic, event)


async def wait_woken(event: asyncio.Event, timeout: float) -> None:
    """Park until `event` is set or `timeout` seconds pass, whichever is first."""
    with suppress(TimeoutError):
        await asyncio.wait_for(event.wait(), timeout)
