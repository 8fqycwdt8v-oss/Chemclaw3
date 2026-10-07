"""Per-session and per-user turn/token budgets — the runaway-cost guard across turns.

The loop cap bounds one turn's iterations, not the number of turns: a client or a push-back loop
could keep posting turns. The front door meters each turn's tokens, counts turns per session and per
user, and refuses (429) a turn that would exceed a cap. `check()` runs before a turn and `record()`
after it, so these caps bound a sequence of turns; one turn's own spend is bounded by
`agent/spend_cap.py`.

The per-user scope is durable (`api/budget_store.py`, one Postgres row per principal on a rolling
window) wherever `session_store == "postgres"`; the per-session scope is in-process because a
session dies with its process. The in-process and durable counts are combined with `max()`: the
local counter has this pod's just-ended turn at once, the durable row has every pod's turns a
moment later. Off by default (`budget_enabled`).
"""

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from chemclaw.core.bounded import BoundedLru
from chemclaw.core.config import settings
from chemclaw.core.metrics_bridge import degraded, record_metric

if TYPE_CHECKING:
    from chemclaw.api import budget_store

logger = logging.getLogger(__name__)

# Strong references to in-flight durable writes, as in `agent/turn_cost.py`, so a write is not
# garbage-collected mid-statement.
_PENDING: set[asyncio.Task[None]] = set()


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
    #: The durable window (`budget_store.Window.start`) this counter has been reconciled with, or
    #: `None` while it has only ever been booked in process. User scope only.
    window: float | None = None


def _rolled(counter: _Counter) -> bool:
    """Whether this counter's window has expired, so its counts no longer bind.

    The in-process counter must roll with the durable row, or `max()` would pin a long-lived pod's
    principal at their lifetime spend while other pods admit them.
    """
    return time.monotonic() - counter.started >= settings.budget_window_hours * 3600.0


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
    fraction = settings.budget_warn_fraction
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
    caps = (
        ("turns", turns, turns - 1, _cap(scope, "turns")),
        ("tokens", tokens, tokens - booked, _cap(scope, "tokens")),
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


def _cap(scope: str, unit: str) -> int:
    """The configured cap for one scope and unit, so the warning and the refusal read one source."""
    if scope == "session":
        return (
            settings.budget_max_turns_per_session
            if unit == "turns"
            else settings.budget_max_tokens_per_session
        )
    if unit == "turns":
        return settings.budget_max_turns_per_user
    return settings.budget_max_tokens_per_user


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
        if not settings.budget_enabled:
            return
        with self._lock:
            session = self._sessions.get(session_id)
            local = self._users.get(user) if user is not None else None
        self._check_scope(
            session,
            "session",
            settings.budget_max_turns_per_session,
            settings.budget_max_tokens_per_session,
        )
        if user is None:
            return
        turns, tokens = _live_counts(local)
        if _durable():
            stored = await self._stored(user)
            if stored is not None:
                turns, tokens = _live_counts(self._reconcile(user, stored))
                turns, tokens = max(turns, stored.turns), max(tokens, stored.tokens)
        self._check_scope(
            _Counter(turns=turns, tokens=tokens),
            "user",
            settings.budget_max_turns_per_user,
            settings.budget_max_tokens_per_user,
        )

    @staticmethod
    async def _stored(user: str) -> "budget_store.Window | None":
        """This principal's durable window, or `None` if the database cannot answer.

        `None` rather than zeros, so a failed read leaves this pod's counter as it was.
        """
        from chemclaw.api import budget_store

        try:
            return await budget_store.usage(user)
        except Exception:
            degraded(
                logger,
                "budget_window",
                "could not read the durable budget window for %s; bounding on this pod's counters "
                "alone, which a restart or an eviction has reset",
                user,
            )
            return None

    def _reconcile(self, user: str, stored: "budget_store.Window") -> _Counter | None:
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
        span = settings.budget_window_hours * 3600.0
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

    def record(self, session_id: str, user: str | None, tokens: int) -> None:
        """Book one completed turn and its metered tokens against the session and the user.

        No-op when `budget_enabled` is off. A failed turn is still booked: it consumed tokens.
        Synchronous by contract (see the class); the in-process counters update now and the durable
        write is scheduled as its own task.
        """
        if not settings.budget_enabled:
            return
        with self._lock:
            session = _book(self._sessions, session_id, tokens)
            local = _book(self._users, user, tokens, rolls=True) if user is not None else None
        booked = max(tokens, 0)
        _warn("session", session_id, session.turns, session.tokens, booked)
        if user is None:
            return
        if not _durable():
            if local is not None:
                _warn("user", user, local.turns, local.tokens, booked)
            return
        self._schedule(user, tokens)

    def _schedule(self, user: str, tokens: int) -> None:
        """Book the durable window off the hot path, warning off the totals it returns.

        A failed write is logged and lost rather than failing an answered turn; the in-process
        counter still holds the turn.
        """
        from chemclaw.api import budget_store

        async def _write() -> None:
            try:
                stored = await budget_store.book(user, tokens)
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

        try:
            task = asyncio.get_running_loop().create_task(_write())
        except RuntimeError:  # no running loop — a synchronous caller has nowhere to schedule
            logger.debug("no event loop to book the durable budget window for %s", user)
            return
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)


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
    cap = settings.session_max_thread_bytes
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


async def drain_pending(timeout: float = 5.0) -> None:
    """Wait for the in-flight durable bookings, so an orderly shutdown does not drop them.

    Called from the front door's lifespan after the turns drain; otherwise every rollout hands back
    the last booking of each in-flight principal. Bounded, since the pod is inside its termination
    grace.
    """
    if not _PENDING:
        return
    pending = tuple(_PENDING)
    _, still_running = await asyncio.wait(pending, timeout=timeout)
    if still_running:
        degraded(
            logger,
            "budget_window",
            "%d durable budget booking(s) did not land within %.1fs of shutdown",
            len(still_running),
            timeout,
        )
