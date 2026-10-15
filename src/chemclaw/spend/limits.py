"""Every spend limit in one policy, and the per-session and per-user budget that enforces them.

`SpendPolicy.current()` reads the limits from settings at the moment of a decision, so no site holds
a stale copy: model calls and billed tokens per turn (enforced by `spend.guard`), turns and tokens
per session and per user window, the conversation's size ceiling, and the window itself.

`BudgetTracker.check()` runs before a turn and `record()` after it, so these caps bound a sequence
of turns (429 when exceeded). The per-session scope is in-process; the per-user scope is one
Postgres row per principal on a rolling window wherever `session_store == "postgres"`, combined with
the local count by `max()`: the local counter has this pod's just-ended turn at once, the durable
row has every pod's turns a moment later. Off by default (`budget_enabled`).
"""

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import timedelta
from types import ModuleType
from typing import Any, NamedTuple

from chemclaw.core import bookkeeping
from chemclaw.core.bounded import BoundedLru
from chemclaw.core.config import settings
from chemclaw.core.metrics_bridge import degraded, record_metric

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SpendPolicy:
    """Every limit one reading of the settings states; a budget cap of 0 is unlimited.

    A snapshot, built per decision by `current()`, so a changed setting is seen by the next
    decision and no site keeps a copy. `model_calls_per_turn` is at least 1 by its setting;
    `billed_tokens_per_turn` of 0 switches that cap off.
    """

    model_calls_per_turn: int
    billed_tokens_per_turn: int
    budget_enabled: bool
    turns_per_session: int
    tokens_per_session: int
    turns_per_user: int
    tokens_per_user: int
    window_hours: float
    warn_fraction: float
    thread_bytes: int

    @classmethod
    def current(cls) -> "SpendPolicy":
        """The limits as the settings now state them."""
        return cls(
            model_calls_per_turn=settings.harness_max_loop_iterations,
            billed_tokens_per_turn=settings.agent_max_turn_billed_tokens,
            budget_enabled=settings.budget_enabled,
            turns_per_session=settings.budget_max_turns_per_session,
            tokens_per_session=settings.budget_max_tokens_per_session,
            turns_per_user=settings.budget_max_turns_per_user,
            tokens_per_user=settings.budget_max_tokens_per_user,
            window_hours=settings.budget_window_hours,
            warn_fraction=settings.budget_warn_fraction,
            thread_bytes=settings.session_max_thread_bytes,
        )

    @property
    def window_seconds(self) -> float:
        """The user window's length, for the in-process clock."""
        return self.window_hours * 3600.0

    @property
    def window(self) -> timedelta:
        """The user window as the interval the durable statements bind."""
        return timedelta(hours=self.window_hours)

    def cap(self, scope: str, unit: str) -> int:
        """The cap for one scope (`"session"` or `"user"`) and unit (`"turns"` or `"tokens"`)."""
        if scope == "session":
            return self.turns_per_session if unit == "turns" else self.tokens_per_session
        return self.turns_per_user if unit == "turns" else self.tokens_per_user


class BudgetExceeded(Exception):
    """A turn is refused because it would exceed a session or user budget (maps to HTTP 429).

    Not a `ChemclawError`: a capacity refusal, not bad input, so no reject-and-continue boundary may
    swallow it.
    """


@dataclass
class _Counter:
    """Turns and metered tokens booked against one scope (a session or a user) in one window."""

    turns: int = 0
    tokens: int = 0
    #: Monotonic start of the window these counts belong to, for the *user* scope only — a session
    #: counter carries one too and nothing reads it, because `_book` only rolls what it is told to.
    started: float = field(default_factory=time.monotonic)
    #: The durable window (`Window.start`) this counter has been reconciled with, or `None` while
    #: it has only ever been booked in process. User scope only.
    window: float | None = None


def _rolled(counter: _Counter) -> bool:
    """Whether this counter's window has expired, so its counts no longer bind.

    The in-process counter must roll with the durable row, or `max()` would pin a long-lived pod's
    principal at their lifetime spend while other pods admit them.
    """
    return time.monotonic() - counter.started >= SpendPolicy.current().window_seconds


def _live_counts(counter: _Counter | None) -> tuple[int, int]:
    """A user counter's `(turns, tokens)`, or zeros once its own window has expired."""
    if counter is None or _rolled(counter):
        return 0, 0
    return counter.turns, counter.tokens


def _over(cap: int, used: int) -> bool:
    """Whether `used` has reached `cap`, treating a cap of 0 as unlimited."""
    return cap > 0 and used >= cap


def _near(cap: int, used: int) -> bool:
    """Whether `used` has reached the warning fraction of `cap` without reaching `cap` itself.

    A cap of 0 is unlimited, and usage at the cap is the refusal's business.
    """
    fraction = SpendPolicy.current().warn_fraction
    if cap <= 0 or fraction <= 0:
        return False
    return used >= cap * fraction and used < cap


def _durable() -> bool:
    """Whether the per-user window is backed by Postgres.

    Derived from the session store the deployment chose, not a flag of its own.
    """
    return settings.session_store == "postgres"


def _warn(scope: str, identity: str, turns: int, tokens: int, booked: int) -> None:
    """Say so on the one turn that carries a scope's usage across the warning fraction of a cap.

    The log line carries `identity` because the alert metric is unlabelled (an oid or session id
    would be unbounded cardinality); the log is how an operator finds who. Called from `record`
    only, since `check` runs twice per turn. Edge-triggered — `booked` lets the previous total be
    derived without per-principal state — so one crossing produces one warning, as the alert rule
    assumes.
    """
    policy = SpendPolicy.current()
    caps = (
        ("turns", turns, turns - 1, policy.cap(scope, "turns")),
        ("tokens", tokens, tokens - booked, policy.cap(scope, "tokens")),
    )
    for unit, used, before, cap in caps:
        if not _near(cap, used) or _near(cap, before):
            continue
        record_metric(lambda m: m.increment("chemclaw_budget_warnings_total"))
        logger.warning(
            "%s %s budget %.0f%% spent (%d of %d) for %s — the next turns will be refused at "
            "the cap",
            scope,
            unit,
            100 * used / cap,
            used,
            cap,
            identity,
        )


def _book(
    counters: BoundedLru[str, _Counter], key: str, tokens: int, *, rolls: bool = False
) -> _Counter:
    """Add one turn and its (non-negative) tokens to `key`, evicting the LRU past capacity.

    `rolls` starts a fresh window when the counter's has expired; passed for the user scope only,
    which the durable row windows. A session is not windowed: its cap bounds one conversation, not a
    rate. The map is a `BoundedLru` so a long-lived pod does not keep a counter per session or user
    ever seen; capacity is read live from config.

    Returns the updated counter so the caller can warn off it without a second lookup.
    """
    counter = counters.get(key)
    if counter is None or (rolls and _rolled(counter)):
        counter = _Counter()
    counter.turns += 1
    counter.tokens += max(tokens, 0)
    counters.put(key, counter)
    return counter


class BudgetTracker:
    """Meter + admission gate for agent-turn cost, keyed by session and by user.

    `check` refuses a turn before it runs; `record` books it after. A lock guards the in-process
    counters. Since the two are separate calls, turns admitted concurrently may overshoot a cap by
    up to `service_max_concurrent_turns` plus any detached turns still running — which holds only
    because the front door re-checks after taking the admission permit
    (`chemclaw.api.routes.turns.post_message`). Both maps are LRU-bounded
    (`service_max_live_sessions`, `budget_max_tracked_users`).

    `record` is synchronous because its caller, `api/runner._book_turn_spend`, runs in teardown
    where an `await` would re-raise a pending cancellation; it schedules the durable write as a
    task. `check` is async so it can read the durable row before admitting a turn.
    """

    def __init__(self) -> None:
        """Start with empty, LRU-bounded per-session and per-user counters."""
        self._sessions: BoundedLru[str, _Counter] = BoundedLru(
            lambda: settings.service_max_live_sessions
        )
        self._users: BoundedLru[str, _Counter] = BoundedLru(
            lambda: settings.budget_max_tracked_users
        )
        self._lock = threading.Lock()

    async def check(self, session_id: str, user: str | None) -> None:
        """Raise `BudgetExceeded` if the next turn would exceed a session or user cap.

        No-op when `budget_enabled` is off. Checked against usage already booked, so a cap of 100
        allows 100 turns and refuses the 101st. The user scope takes the larger of the in-process
        and durable counts; an unreachable database degrades to the in-process count rather than
        failing the turn.
        """
        policy = SpendPolicy.current()
        if not policy.budget_enabled:
            return
        with self._lock:
            session = self._sessions.get(session_id)
            local = self._users.get(user) if user is not None else None
        self._check_scope(session, "session", policy.turns_per_session, policy.tokens_per_session)
        if user is None:
            return
        turns, tokens = _live_counts(local)
        if _durable():
            stored = await self._stored(user)
            if stored is not None:
                turns, tokens = _live_counts(self._reconcile(user, stored))
                turns, tokens = max(turns, stored.turns), max(tokens, stored.tokens)
        policy = SpendPolicy.current()  # read after the await: the user caps are not held across it
        self._check_scope(
            _Counter(turns=turns, tokens=tokens),
            "user",
            policy.turns_per_user,
            policy.tokens_per_user,
        )

    @staticmethod
    async def _stored(user: str) -> "Window | None":
        """This principal's durable window, or `None` if the database cannot answer.

        `None` rather than zeros, so a failed read leaves this pod's counter as it was.
        """
        load_db()
        try:
            return await usage(user)
        except Exception:
            degraded(
                logger,
                "budget_window",
                "could not read the durable budget window for %s; bounding on this pod's counters "
                "alone, which a restart or an eviction has reset",
                user,
            )
            return None

    def _reconcile(self, user: str, stored: "Window") -> _Counter | None:
        """Re-anchor this pod's counter for `user` to the durable window; return the counter.

        The local counter starts when this pod first books the user, which may be long after the
        durable window opened, so it is aligned with that window:

        - the durable window has expired: drop local counts and restart now;
        - the durable window is newer than the one last reconciled, or opened after the counter
          started: adopt the durable counts, which already hold every turn in the new window;
        - otherwise keep the local counts (the unwritten turns `max()` exists for) and adopt the
          window's start so both roll together.

        No durable row (`start is None`) leaves the counter alone. Comparing the window's identity
        rather than its start on the local clock makes the decision once per window.
        """
        if stored.start is None:
            with self._lock:
                return self._users.get(user)
        now = time.monotonic()
        span = SpendPolicy.current().window_seconds
        with self._lock:
            local = self._users.get(user)
            if local is None:
                return None
            if stored.expired:
                if local.window == stored.start or local.started < now - (stored.age - span):
                    local = _Counter(started=now)
                    self._users.put(user, local)
                return local
            if local.window == stored.start:
                return local
            opened = now - stored.age
            if local.window is not None or local.started < opened:
                local = _Counter(stored.turns, stored.tokens, opened, stored.start)
            else:
                local.started, local.window = opened, stored.start
            self._users.put(user, local)
            return local

    @staticmethod
    def _check_scope(counter: _Counter | None, scope: str, max_turns: int, max_tokens: int) -> None:
        """Refuse if this scope's booked turns or tokens have reached either cap."""
        if counter is None:
            return
        if _over(max_turns, counter.turns):
            raise BudgetExceeded(f"{scope} turn budget exhausted ({counter.turns} turns)")
        if _over(max_tokens, counter.tokens):
            raise BudgetExceeded(f"{scope} token budget exhausted ({counter.tokens} tokens)")

    def record(self, session_id: str, user: str | None, tokens: int) -> asyncio.Task[None] | None:
        """Book one completed turn and its metered tokens against the session and the user.

        No-op when `budget_enabled` is off. A failed turn is still booked: it consumed tokens.
        Synchronous by contract (see the class); the in-process counters update now and the durable
        write is scheduled as its own task, returned for a caller that can wait on it
        (`core.bookkeeping.settle`). `None` when there is no durable write to wait for.
        """
        if not SpendPolicy.current().budget_enabled:
            return None
        with self._lock:
            session = _book(self._sessions, session_id, tokens)
            local = _book(self._users, user, tokens, rolls=True) if user is not None else None
        booked = max(tokens, 0)
        _warn("session", session_id, session.turns, session.tokens, booked)
        if user is None:
            return None
        if not _durable():
            if local is not None:
                _warn("user", user, local.turns, local.tokens, booked)
            return None
        return self._schedule(user, tokens)

    def _schedule(self, user: str, tokens: int) -> asyncio.Task[None] | None:
        """Book the durable window off the hot path, warning off the totals it returns.

        A failed write is logged and lost rather than failing an answered turn; the in-process
        counter still holds the turn.
        """
        load_db()

        async def _write() -> None:
            try:
                stored = await book(user, tokens)
            except asyncio.CancelledError:
                # Cancellation is not an `Exception` and is the usual loss mode here, so record it
                # separately.
                degraded(
                    logger,
                    "budget_window",
                    "the durable budget booking for %s was cancelled before it landed; this turn "
                    "is counted on this pod only",
                    user,
                )
                raise
            except Exception:
                degraded(
                    logger,
                    "budget_window",
                    "could not book a turn against the durable budget window for %s; this turn is "
                    "counted on this pod only",
                    user,
                )
                return
            self._reconcile(user, stored)
            _warn("user", user, stored.turns, stored.tokens, max(tokens, 0))

        task = bookkeeping.schedule(_write())
        if task is None:
            logger.debug("no event loop to book the durable budget window for %s", user)
        return task


class ThreadTooLong(BudgetExceeded):
    """A turn refused because its session's stored conversation is at `session_max_thread_bytes`.

    A `BudgetExceeded`, so every handler answers it the same way, but its own class so it is counted
    apart from token-budget refusals, whose remedy does not apply.
    """


def refused_metric(exc: BudgetExceeded) -> str:
    """The counter a refused turn is booked on, by which budget refused it."""
    if isinstance(exc, ThreadTooLong):
        return "chemclaw_turns_refused_thread_size_total"
    return "chemclaw_turns_refused_budget_total"


async def check_thread_size(session_id: str) -> None:
    """Raise `ThreadTooLong` if this session's stored conversation is at its size ceiling.

    Every turn loads the whole thread, so a turn's memory cost grows with the conversation; enough
    concurrent turns on large threads can OOM the pod. Reads the stored thread, which every replica
    sees. Not behind `budget_enabled`: it is a memory bound. Called at request entry (clean 429
    before a claim) and again after the permit (the binding check). An unreachable database admits
    the turn.
    """
    cap = SpendPolicy.current().thread_bytes
    if not cap:
        return
    from chemclaw.agent.checkpointer import stored_thread_bytes

    try:
        stored = await stored_thread_bytes(session_id)
    except Exception:
        degraded(
            logger,
            "thread_size",
            "could not read the stored size of session %s; admitting its turn unbounded",
            session_id,
        )
        return
    if stored >= cap:
        raise ThreadTooLong(
            f"This conversation has reached its size limit ({stored / 1024**2:.1f} MiB stored, "
            f"against {cap / 1024**2:.1f} MiB). Start a new session to continue — this one's "
            "transcript stays readable."
        )


# --- the per-user window, in Postgres ---------------------------------------------------------
#
# One row per principal, reset in place inside the upsert's `ON CONFLICT` arm, so the table is
# bounded by the number of principals and needs no sweep, and no process sees a half-reset row. The
# window is rolling and anchored at first use, so allowances do not all reset at one instant; a user
# who spends everything at once waits the full window. Only the user scope is durable: a session
# dies with its process.

#: Book one turn against a principal, resetting the window first if it has rolled over.
#:
#: The `CASE` arms repeat one predicate rather than use a CTE, keeping the reset in the same
#: statement. Under concurrency a conflicting writer blocks on the row lock and re-evaluates against
#: the latest committed row, not its snapshot, so no update is lost; `tests/test_budget_window.py`
#: pins this.
_BOOK = """
    INSERT INTO budget_usage (actor, window_start, turns, tokens)
    VALUES (%(actor)s, now(), 1, %(tokens)s)
    ON CONFLICT ON CONSTRAINT budget_usage_pkey DO UPDATE SET
        window_start = CASE
            WHEN budget_usage.window_start <= now() - %(window)s::interval
            THEN now() ELSE budget_usage.window_start END,
        turns = CASE
            WHEN budget_usage.window_start <= now() - %(window)s::interval
            THEN 1 ELSE budget_usage.turns + 1 END,
        tokens = CASE
            WHEN budget_usage.window_start <= now() - %(window)s::interval
            THEN %(tokens)s ELSE budget_usage.tokens + %(tokens)s END,
        updated_at = now()
    RETURNING turns, tokens,
        EXTRACT(EPOCH FROM window_start), EXTRACT(EPOCH FROM now() - window_start)
"""

#: A principal's row, live or expired, with where its window started and how old it is.
#:
#: Returned even when expired, so `BudgetTracker._reconcile` can tell "window ended" from "never
#: booked". The age uses Postgres's `now()`, the same clock as `_BOOK`.
_USAGE = """
    SELECT turns, tokens,
        EXTRACT(EPOCH FROM window_start), EXTRACT(EPOCH FROM now() - window_start)
    FROM budget_usage WHERE actor = %(actor)s
"""


class Window(NamedTuple):
    """What a principal has spent in the current durable window, and which window that is.

    `start` identifies the window (`window_start` as epoch seconds, identical on every pod); `age`
    is its elapsed seconds by Postgres's clock, which the in-process counter uses to roll with it.
    """

    turns: int
    tokens: int
    #: `None` when the principal has no row at all — nothing has ever been booked durably.
    start: float | None = None
    age: float = 0.0

    @property
    def expired(self) -> bool:
        """Whether a row exists and its window has run out, so the next booking opens a new one."""
        return self.start is not None and self.age >= SpendPolicy.current().window.total_seconds()


def _from_row(row: tuple[Any, ...] | None) -> Window:
    """A `Window` from `(turns, tokens, start, age)`, with an expired window's counts read as zero.

    `Any` because that is psycopg's row type; `EXTRACT` arrives as a `Decimal`.
    """
    if row is None:
        return Window(0, 0)
    turns, tokens, start, age = row
    window = Window(int(turns), int(tokens), float(start), float(age))
    return window._replace(turns=0, tokens=0) if window.expired else window


def load_db() -> ModuleType:
    """The connection-pool module, imported on first use so a memory-store process never loads it.

    Callers invoke it outside their `try`, so a broken install raises where the import is written
    instead of being read as a database that cannot answer and admitting the turn.
    """
    from chemclaw.core import db

    return db


def _dsn() -> str:
    """The database the session tier already uses — a budget window has a session's lifetime."""
    return settings.session_store_dsn or settings.postgres_dsn


async def usage(actor: str) -> Window:
    """What this principal has spent in the current window; zero counts if it has rolled.

    Unknown and expired both read zero; `start` tells them apart for the in-process counter.
    """
    db = load_db()
    async with db.connection(_dsn()) as conn:
        cursor = await conn.execute(_USAGE, {"actor": actor})
        row = await cursor.fetchone()
    return _from_row(row)


async def book(actor: str, tokens: int) -> Window:
    """Add one turn and its tokens to this principal's window; return the window's new totals.

    `tokens` is clamped at zero (the table's `CHECK` refuses negatives too). The totals come from
    `RETURNING`, so the caller's warning check sees exactly what this write produced.
    """
    db = load_db()
    async with db.connection(_dsn()) as conn:
        cursor = await conn.execute(
            _BOOK,
            {"actor": actor, "tokens": max(tokens, 0), "window": SpendPolicy.current().window},
        )
        row = await cursor.fetchone()
    return _from_row(row)
