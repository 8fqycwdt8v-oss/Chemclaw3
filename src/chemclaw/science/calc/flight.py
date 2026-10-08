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
from chemclaw.core.errors import ChemclawError
from chemclaw.core.metrics_bridge import degraded, record_metric

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: The `LISTEN` channel. Database-wide, so the payload (a digest of the key) only selects which
#: local waiters to wake; a wake-up for another key's release costs a waiter one cheap re-check.
CHANNEL = "calc_flight"

#: The lease is refreshed this many times per lease, and a waiter polls this many times per lease.
_BEATS_PER_LEASE = 3
_POLLS_PER_LEASE = 6

#: Characters of a failed attempt's description kept for its waiters: the retry class, the
#: exception's name and the start of its message. Enough to act on; the full text is in the holder's
#: log, and the row is readable by every session that asks for the same calculation.
_ERROR_CHARS = 300


class PeerComputationFailed(RuntimeError):
    """The pod computing this calculation failed in a way a retry may fix; nothing was cached.

    Retryable, as the holder's own failure was. A refusal reaches waiters as
    `PeerCalculationRefused` instead.
    """


class PeerCalculationRefused(ChemclawError):
    """The pod computing this calculation was refused or given bad data, so a retry fails alike."""


class PeerWaitTimeout(TimeoutError):
    """Another pod is still computing this calculation and this caller's wait budget ran out."""


@dataclass(frozen=True)
class Claim:
    """The right to compute one key, held until `release` or `fail`, or until the lease lapses."""

    slot: str
    attempt: str
    taken_over: bool


@dataclass(frozen=True)
class Observed:
    """The row a claimant lost to: who holds the key and in what state."""

    attempt: str
    state: str
    error: str


def _attempt_id() -> str:
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
_ABANDON = """
    UPDATE calculation_claims
       SET lease_until = now() + make_interval(secs => %s)
     WHERE key = %s AND attempt = %s AND state = 'running'
"""
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
        attempt = _attempt_id()
        async with db.connection(self._dsn, operation="calc_claim") as conn:
            cur = await conn.execute(
                _CLAIM,
                {
                    "key": slot,
                    "attempt": attempt,
                    "lease": self.lease_seconds(),
                    "retry_failed": retry_failed,
                },
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return Claim(slot=slot, attempt=attempt, taken_over=bool(row[0]))

    async def observe(self, slot: str) -> Observed | None:
        """The row holding `slot`, or `None` when nobody does."""
        async with db.connection(self._dsn, operation="calc_claim") as conn:
            cur = await conn.execute(_OBSERVE, (slot,))
            row = await cur.fetchone()
        return None if row is None else Observed(attempt=row[0], state=row[1], error=row[2])

    @classmethod
    def beat_bound(cls) -> float:
        """The longest one heartbeat may take, derived from the lease so the two cannot disagree.

        Pool wait, connect and statement all fit inside it (`_beat` enforces it); with a beat every
        `lease / 3`, several beats can fail inside one lease and a later one still lands.
        """
        return cls.lease_seconds() / _POLLS_PER_LEASE

    async def heartbeat(self, claim: Claim) -> bool:
        """Extend the lease; `False` when the claim is no longer this attempt's.

        Unbounded here on purpose: the caller (`_beat`) wraps the whole borrow-and-statement in
        `beat_bound`, so the shared pool's settings are used and no extra pool is minted. A
        timeout cancels the statement and the pool discards the interrupted connection.
        """
        async with db.connection(self._dsn, operation="calc_claim") as conn:
            cur = await conn.execute(_BEAT, (self.lease_seconds(), claim.slot, claim.attempt))
            return cur.rowcount > 0

    async def abandon(self, claim: Claim) -> None:
        """Give up a claim whose caller was cancelled: shorten the lease instead of deleting it.

        The cancelled call's server-side run is still unwinding as its session closes, so the key
        is offered to a waiter only after one beat interval. A waiter takes it over then; with no
        waiter the row simply lapses and the next claim replaces it.
        """
        async with db.connection(self._dsn, operation="calc_claim") as conn:
            await conn.execute(
                _ABANDON, (self.lease_seconds() / _BEATS_PER_LEASE, claim.slot, claim.attempt)
            )

    async def release(self, claim: Claim) -> None:
        """Remove the claim and wake the waiters; they find the result, or take the key over.

        Only the attempt's own row: a holder that was taken over cannot remove its successor's.
        """
        async with db.connection(self._dsn, operation="calc_claim") as conn:
            await conn.execute(_RELEASE, (claim.slot, claim.attempt))
            await conn.execute(_NOTIFY, (CHANNEL, _topic(claim.slot)))

    async def fail(self, claim: Claim, error: str) -> None:
        """Record that this attempt failed, for its waiters, and wake them."""
        async with db.connection(self._dsn, operation="calc_claim") as conn:
            await conn.execute(_FAIL, (error[:_ERROR_CHARS], claim.slot, claim.attempt))
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
    """A failure as stored for its waiters: `refused|` or `failed|`, the class and a short message.

    A `ValueError` (`ChemclawError`, a validation error) is a deterministic refusal that a retry
    repeats; anything else may succeed on retry. The waiter raises the matching class, so its
    activity retries exactly when the holder's would have.
    """
    kind = "refused" if isinstance(exc, ValueError) else "failed"
    return f"{kind}|{type(exc).__name__}: {' '.join(str(exc).split())}"


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
        PeerCalculationRefused: the holder was refused or given bad data while this caller waited.
        PeerComputationFailed: the holder failed in a way a retry may fix.
        PeerWaitTimeout: the holder was still working when the budget ran out, or too little of it
            remained to start the calculation here.
    """
    claim = await claims.claim(slot, retry_failed=True)
    if claim is None:
        started = time.monotonic()
        _record("awaited")
        waited: T | Claim | None = None
        try:
            async with claims.watch(slot) as woken:
                waited = await _wait(claims, slot, woken, started, lookup, wait_seconds)
        except BaseException:
            # A claim won in `_wait` and not yet led (the exit of the block above is a point where
            # this caller can be cancelled) has no heartbeat: give it back rather than let it lapse.
            if isinstance(waited, Claim):
                await _settle(claims.release(waited), waited, "give back")
            raise
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
    was missed is seen on the next poll, and never claims over a failed row: that failure is what
    this caller was waiting for.

    **A key is taken over only while at least one lease of the budget remains.** A calculation
    started under a deadline about to cancel it would be abandoned and handed on, one partial run
    after another, each leaving a server-side run behind; the waiter times out instead and the next
    caller, with a whole budget, takes the key.
    """
    lease = claims.lease_seconds()
    poll = lease / _POLLS_PER_LEASE
    while True:
        woken.clear()
        found = await lookup()
        if found is not None:
            return found
        remaining = wait_seconds - (time.monotonic() - started)
        can_start = remaining >= lease
        if can_start:
            claim = await claims.claim(slot, retry_failed=False)
            if claim is not None:
                return claim
        seen = await claims.observe(slot)
        if seen is not None and seen.state == "failed":
            _record("peer_failed")
            raise _peer_failure(seen.error)
        if seen is None and can_start:
            continue  # the holder released between the claim and the read; try again at once
        if remaining <= 0 or seen is None:
            _record("wait_timed_out")
            if seen is None:
                raise PeerWaitTimeout(
                    f"the worker computing {slot} stopped, but too little of the "
                    f"{wait_seconds:g} s allowed for this call remains to start the calculation "
                    "here. Ask again."
                )
            raise PeerWaitTimeout(
                f"another worker is still computing {slot} and the {wait_seconds:g} s allowed for "
                "this call ran out. Its work continues and the result is cached when it ends; "
                "ask again."
            )
        with suppress(TimeoutError):
            await asyncio.wait_for(woken.wait(), min(poll, remaining))


def _peer_failure(stored: str) -> Exception:
    """The error a waiter raises for a holder's recorded failure, of the holder's retry class."""
    kind, _, text = stored.partition("|")
    if not text:
        kind, text = "failed", stored
    if kind == "refused":
        return PeerCalculationRefused(
            f"another worker's attempt at this calculation was refused: {text}. Nothing was "
            "cached, and the same request is refused the same way."
        )
    return PeerComputationFailed(
        f"another worker's computation of this calculation failed, so nothing was cached: {text}. "
        "Ask again to start a fresh attempt."
    )


async def _lead(
    claims: PostgresClaims,
    claim: Claim,
    lookup: Callable[[], Awaitable[T | None]],
    produce: Callable[[], Awaitable[T]],
) -> tuple[T, bool]:
    """Compute under `claim`, heartbeating, and settle the claim whichever way it ends.

    The result is looked up once more first: the previous holder may have persisted it between this
    caller's miss and its claim. A failure is recorded for the waiters; a cancellation abandons the
    claim (`PostgresClaims.abandon`) so a waiter takes the key over after a short cooling-off. The
    heartbeat is stopped before the claim is settled, so a late beat cannot report a takeover.
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
        await _close(beating, claims.fail(claim, _describe(exc)[:_ERROR_CHARS]), claim, "fail")
        raise
    except BaseException:
        await _close(beating, claims.abandon(claim), claim, "abandon")
        raise
    await _close(beating, claims.release(claim), claim, "release")
    return value, computed


async def _close(
    beating: "asyncio.Task[None]", step: Awaitable[None], claim: Claim, what: str
) -> None:
    """Stop the heartbeat, then run the claim-closing `step`, even if the caller is cancelled."""
    try:
        await _stop(beating)
    finally:
        await _settle(step, claim, what)


async def _stop(task: "asyncio.Task[None]") -> None:
    """Cancel `task` and wait for it to unwind, without absorbing this caller's own cancellation.

    `asyncio.wait` returns when the task is done and does not raise its `CancelledError`; a
    cancellation of the caller, delivered at that await, still propagates.
    """
    task.cancel()
    await asyncio.wait({task})


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
    """Refresh the lease until cancelled; stops, counting it, if the claim was taken away.

    Each beat has a hard bound (`PostgresClaims.beat_bound`, covering the pool wait and connect as
    well as the statement). A beat that fails or times out is retried after that bound rather than
    after a whole interval, and a beat that lands after a lapse nobody exploited re-asserts the
    claim, because the statement matches on the attempt, not on the lease.
    """
    interval = claims.lease_seconds() / _BEATS_PER_LEASE
    bound = claims.beat_bound()
    delay = interval
    while True:
        await asyncio.sleep(delay)
        try:
            async with asyncio.timeout(bound):
                held = await claims.heartbeat(claim)
        except Exception:
            degraded(
                logger,
                "calc_claim",
                "could not refresh the claim on %s",
                claim.slot,
                level=logging.WARNING,
            )
            delay = bound
            continue
        if not held:
            _record("lost")
            logger.warning(
                "the claim on %s was taken over while its holder was still computing; "
                "finishing anyway, the results are identical",
                claim.slot,
            )
            return
        delay = interval
