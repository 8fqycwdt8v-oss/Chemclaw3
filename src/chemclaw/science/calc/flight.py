"""Cross-process single-flight for calculation misses, over `calculation_claims`.

A miss is claimed in Postgres before it is computed (D-011 extended: a result being computed by
someone else is awaited, not recomputed). The winner heartbeats a lease while it computes; the
others wait for its result, woken by `NOTIFY` and by a bounded poll for the notifications a dropped
connection loses.

Invariants: every lease comparison is the database's `now()`, never a process clock; a waiter holds
no claim, so its timeout or cancellation leaves the holder's work and the row untouched; a holder's
failure reaches its waiters instead of being retried by each; a holder that is cancelled or killed
hands the key to exactly one waiter, because the takeover is one conflicting `UPDATE`.
"""

import asyncio
import hashlib
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from os import getpid
from socket import gethostname
from typing import Protocol, TypeVar, runtime_checkable
from weakref import WeakKeyDictionary

import psycopg
from psycopg.rows import TupleRow

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.metrics_bridge import degraded, record_metric

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: The `LISTEN` channel. Database-wide, so the payload (a digest of the key) only selects which
#: local waiters to wake; a wake-up for another key's release costs a waiter one cheap re-check.
CHANNEL = "calc_flight"

#: The lease is refreshed this many times per lease, and a waiter polls this many times per lease.
_BEATS_PER_LEASE = 3
_POLLS_PER_LEASE = 6

#: Characters of a failed attempt's description shown to its waiters.
_ERROR_CHARS = 500


class PeerComputationFailed(RuntimeError):
    """The pod computing this calculation failed; nothing was cached and nothing was retried."""


class PeerWaitTimeout(TimeoutError):
    """Another pod is still computing this calculation and this caller's wait budget ran out."""


@dataclass(frozen=True)
class Claim:
    """The right to compute one key, held until `release` or `fail`, or until the lease lapses."""

    slot: str
    holder: str
    taken_over: bool


@dataclass(frozen=True)
class Observed:
    """The row a claimant lost to: who holds the key and in what state."""

    holder: str
    state: str
    error: str


def _holder_id() -> str:
    """A fresh id for one attempt, naming where it runs for the operator reading a row."""
    return f"{gethostname()}:{getpid()}:{uuid.uuid4().hex[:12]}"


def _topic(slot: str) -> str:
    """The `NOTIFY` payload for `slot`: a fixed-size digest (a payload is capped at 8000 bytes)."""
    return hashlib.sha256(slot.encode()).hexdigest()


def _record(outcome: str) -> None:
    """Count one claim outcome on `chemclaw_calc_claims_total`."""
    record_metric(lambda m: m.increment("chemclaw_calc_claims_total", labels={"outcome": outcome}))


_CLAIM = """
    INSERT INTO calculation_claims AS c (key, attempt, state, error, lease_until, claimed_at)
    VALUES (%(key)s, %(attempt)s, 'running', '', now() + make_interval(secs => %(lease)s), now())
    ON CONFLICT (key) DO UPDATE
       SET attempt = EXCLUDED.attempt, state = 'running', error = '',
           lease_until = EXCLUDED.lease_until, claimed_at = EXCLUDED.claimed_at
     WHERE (c.state = 'running' AND c.lease_until < now())
        OR (c.state = 'failed' AND %(retry_failed)s)
    RETURNING (xmax <> 0) AS replaced
"""
_OBSERVE = "SELECT attempt, state, error FROM calculation_claims WHERE key = %s"
_BEAT = """
    UPDATE calculation_claims
       SET lease_until = now() + make_interval(secs => %s)
     WHERE key = %s AND attempt = %s AND state = 'running'
"""
_RELEASE = "DELETE FROM calculation_claims WHERE key = %s AND attempt = %s"
_FAIL = """
    UPDATE calculation_claims SET state = 'failed', error = %s WHERE key = %s AND attempt = %s
"""
_NOTIFY = "SELECT pg_notify(%s, %s)"


class PostgresClaims:
    """The claim table's operations, one short transaction each, and the wake-ups waiters get."""

    def __init__(self, dsn: str) -> None:
        """Claim against the database at `dsn`, the one holding `calculation_results`."""
        self._dsn = dsn

    @staticmethod
    def lease_seconds() -> float:
        """The configured lease, read per call."""
        return settings.calc_claim_lease_seconds

    async def claim(self, slot: str, *, retry_failed: bool) -> Claim | None:
        """Claim `slot`, or return `None` when another attempt holds it.

        One `INSERT … ON CONFLICT`: a vacant key is inserted, a lapsed row is replaced in place, a
        live one is left alone, and a failed one is replaced only for a caller starting afresh
        (`retry_failed`) — never for a waiter, who must receive that failure. Concurrent claimants
        serialise on the row, so exactly one of them gets a row back.
        """
        holder = _holder_id()
        async with db.connection(self._dsn, operation="calc_claim") as conn:
            cur = await conn.execute(
                _CLAIM,
                {
                    "key": slot,
                    "attempt": holder,
                    "lease": self.lease_seconds(),
                    "retry_failed": retry_failed,
                },
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return Claim(slot=slot, holder=holder, taken_over=bool(row[0]))

    async def observe(self, slot: str) -> Observed | None:
        """The row holding `slot`, or `None` when nobody does."""
        async with db.connection(self._dsn, operation="calc_claim") as conn:
            cur = await conn.execute(_OBSERVE, (slot,))
            row = await cur.fetchone()
        return None if row is None else Observed(holder=row[0], state=row[1], error=row[2])

    async def heartbeat(self, claim: Claim) -> bool:
        """Extend the lease; `False` when the claim is no longer this attempt's."""
        async with db.connection(self._dsn, operation="calc_claim") as conn:
            cur = await conn.execute(_BEAT, (self.lease_seconds(), claim.slot, claim.holder))
            return cur.rowcount > 0

    async def release(self, claim: Claim) -> None:
        """Remove the claim and wake the waiters; they find the result, or take the key over."""
        async with db.connection(self._dsn, operation="calc_claim") as conn:
            await conn.execute(_RELEASE, (claim.slot, claim.holder))
            await conn.execute(_NOTIFY, (CHANNEL, _topic(claim.slot)))

    async def fail(self, claim: Claim, error: str) -> None:
        """Record that this attempt failed, for its waiters, and wake them."""
        async with db.connection(self._dsn, operation="calc_claim") as conn:
            await conn.execute(_FAIL, (error[:_ERROR_CHARS], claim.slot, claim.holder))
            await conn.execute(_NOTIFY, (CHANNEL, _topic(claim.slot)))

    @asynccontextmanager
    async def watch(self, slot: str) -> AsyncIterator[asyncio.Event]:
        """An event set whenever `slot`'s claim is released or fails, until the block ends.

        Never the only way to learn of a release: the caller also polls, so a missed notification
        costs one poll interval, not a hang.
        """
        listener = _listener_for(self._dsn)
        event = await listener.subscribe(_topic(slot))
        try:
            yield event
        finally:
            await listener.unsubscribe(_topic(slot), event)


class _Listener:
    """One dedicated `LISTEN` connection per event loop, shared by every waiter in it.

    Started by the first subscriber and stopped with the last, so an idle process holds no extra
    connection. A broken connection wakes every subscriber (they re-check) and is reopened.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
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
                await conn.execute(f"LISTEN {CHANNEL}")
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
                    "calc_claim",
                    "the claim listener lost its connection; waiters fall back to polling",
                    level=logging.WARNING,
                )
            finally:
                ready.set()
                if conn is not None:
                    with suppress(Exception):
                        await conn.close()
            reconnecting = True
            self._wake(None)
            await asyncio.sleep(PostgresClaims.lease_seconds() / _POLLS_PER_LEASE)


_LISTENERS: "WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, _Listener]]" = (
    WeakKeyDictionary()
)


def _listener_for(dsn: str) -> _Listener:
    """This loop's listener for `dsn`, created on first use (a task belongs to one loop)."""
    per_dsn = _LISTENERS.setdefault(asyncio.get_running_loop(), {})
    return per_dsn.setdefault(dsn, _Listener(dsn))


def _describe(exc: BaseException) -> str:
    """One line naming a failure for the waiters on it."""
    return f"{type(exc).__name__}: {exc}"


def wait_budget(wait_seconds: float | None) -> float:
    """The longest a caller waits on another's computation: its own, else the calculation default.

    The default is `calc_server_timeout_seconds`, the bound a computer's own call has.
    """
    return wait_seconds if wait_seconds is not None else settings.calc_server_timeout_seconds


@runtime_checkable
class ClaimsProvider(Protocol):
    """A result store that can coordinate misses across processes."""

    def claims(self) -> PostgresClaims | None:
        """The claim ledger beside this store's results, or `None` when it cannot coordinate."""
        ...


async def single_flight(
    claims: PostgresClaims,
    slot: str,
    *,
    lookup: Callable[[], Awaitable[T | None]],
    produce: Callable[[], Awaitable[T]],
    wait_seconds: float,
) -> tuple[T, bool]:
    """Return `(value, computed_here)` for `slot`, computing it here only if nobody else is.

    `lookup` reads the persisted result; `produce` computes and persists it (so the claim is
    released only after the result is readable). A caller that does not win the claim waits for the
    holder at most `wait_seconds` — the budget a computer would have had — and then raises
    `PeerWaitTimeout` without disturbing the holder.

    Raises:
        PeerComputationFailed: the holder's attempt failed while this caller waited on it.
        PeerWaitTimeout: the holder was still working when the budget ran out.
    """
    claim = await claims.claim(slot, retry_failed=True)
    if claim is None:
        started = time.monotonic()
        _record("awaited")
        try:
            async with claims.watch(slot) as woken:
                waited = await _wait(claims, slot, woken, started, lookup, wait_seconds)
        finally:
            seconds = time.monotonic() - started
            record_metric(lambda m: m.observe("chemclaw_calc_claim_wait_seconds", seconds))
        if not isinstance(waited, Claim):
            return waited, False
        claim = waited
    return await _lead(claims, claim, lookup, produce)


async def _wait(
    claims: PostgresClaims,
    slot: str,
    woken: asyncio.Event,
    started: float,
    lookup: Callable[[], Awaitable[T | None]],
    wait_seconds: float,
) -> T | Claim:
    """Wait for `slot`'s holder; return its result, or the claim once the key is free to take.

    Ends when the result is readable, the holder's attempt failed or lapsed or was abandoned, or
    the budget is spent. Each pass looks up the result before claiming, so a release whose wake-up
    was missed is seen on the next poll, and never claims over a failed row: that failure is
    what this caller was waiting for.
    """
    poll = claims.lease_seconds() / _POLLS_PER_LEASE
    while True:
        woken.clear()
        found = await lookup()
        if found is not None:
            return found
        claim = await claims.claim(slot, retry_failed=False)
        if claim is not None:
            return claim
        seen = await claims.observe(slot)
        if seen is not None and seen.state == "failed":
            _record("peer_failed")
            raise PeerComputationFailed(
                "another worker's computation of this calculation failed, so nothing was cached: "
                f"{seen.error}. Ask again to start a fresh attempt."
            )
        if seen is None:
            continue  # the holder released between the claim and the read; try again at once
        remaining = wait_seconds - (time.monotonic() - started)
        if remaining <= 0:
            _record("wait_timed_out")
            raise PeerWaitTimeout(
                f"another worker is still computing {slot} and the {wait_seconds:g} s allowed for "
                "this call ran out. Its work continues and the result is cached when it ends; "
                "ask again."
            )
        with suppress(TimeoutError):
            await asyncio.wait_for(woken.wait(), min(poll, remaining))


async def _lead(
    claims: PostgresClaims,
    claim: Claim,
    lookup: Callable[[], Awaitable[T | None]],
    produce: Callable[[], Awaitable[T]],
) -> tuple[T, bool]:
    """Compute under `claim`, heartbeating, and settle the claim whichever way it ends.

    The result is looked up once more first: the previous holder may have persisted it between this
    caller's miss and its claim. A failure is recorded for the waiters; a cancellation removes the
    claim so one of them takes the key over.
    """
    _record("taken_over" if claim.taken_over else "won")
    beating = asyncio.get_running_loop().create_task(_beat(claims, claim))
    try:
        found = await lookup()
        if found is not None:
            value, computed = found, False
        else:
            value, computed = await produce(), True
    except Exception as exc:
        await _settle(claims.fail(claim, _describe(exc)), claim, "record the failure of")
        raise
    except BaseException:
        await _settle(claims.release(claim), claim, "release")
        raise
    else:
        await _settle(claims.release(claim), claim, "release")
        return value, computed
    finally:
        beating.cancel()
        with suppress(asyncio.CancelledError):
            await beating


async def _settle(step: Awaitable[None], claim: Claim, what: str) -> None:
    """Run a claim-closing step to the end even if the caller is being cancelled, never raising.

    A step that fails leaves a claim that lapses on its own lease, which is the designed fallback,
    and must not replace the error the caller is already propagating.
    """
    try:
        await asyncio.shield(step)
    except Exception:
        degraded(
            logger,
            "calc_claim",
            "could not %s the claim on %s; it lapses after its lease",
            what,
            claim.slot,
            level=logging.WARNING,
        )


async def _beat(claims: PostgresClaims, claim: Claim) -> None:
    """Refresh the lease until cancelled; stops, counting it, if the claim was taken away."""
    interval = claims.lease_seconds() / _BEATS_PER_LEASE
    while True:
        await asyncio.sleep(interval)
        try:
            if not await claims.heartbeat(claim):
                _record("lost")
                logger.warning(
                    "the claim on %s was taken over while its holder was still computing; "
                    "finishing anyway, the results are identical",
                    claim.slot,
                )
                return
        except Exception:
            degraded(
                logger,
                "calc_claim",
                "could not refresh the claim on %s",
                claim.slot,
                level=logging.WARNING,
            )
