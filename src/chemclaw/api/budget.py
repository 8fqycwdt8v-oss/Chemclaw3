"""Per-session and per-user turn/token budgets — the runaway-cost guard (plan F-budget).

A single agent turn is iteration-capped (`harness_max_loop_iterations`), so one turn cannot loop
forever. But nothing caps the *number* of
turns, so a client — or an automated job→session push-back loop re-waking a session — could keep
posting turns and accumulate unbounded LLM spend. This tracker is the missing ceiling above the
per-turn loop cap: the front door meters each turn's reported token usage and counts turns per
session and per user, and refuses (HTTP 429) a turn that would exceed a configured cap.

**One turn cannot loop forever; one turn can spend without a bound, and this module cannot see it.**
That sentence used to read "so one turn cannot loop forever" with the *therefore* left implicit, and
the implication is false: an iteration is not a unit of cost. `check()` runs before a turn against
usage already booked and `record()` books a turn after it ended, so the single thing neither half
observes is a turn spending while it runs — measured 2026-09-06 at **250,000 tokens in one turn
against a 1,000-token session cap**, refused only on the turn after. That is the "$400 in twenty
minutes" failure this module was written against, arriving through the door it left open, and it is
`agent/spend_cap.py` that closes it: a per-turn ceiling enforced in `before_model` and metered off
the response, configured by `agent_max_turn_billed_tokens`. It ships at 0 (no cap) for the reason
that setting states, so **in the shipped configuration nothing bounds a single turn's spend** and
`budget_max_tokens_per_user` is the only real ceiling. Read this module's caps as bounding a
*sequence* of turns, never one of them.

**The per-user ceiling is durable since
`D-2026-09-15-a-budget-a-restart-resets-is-not-a-quota`; the per-session one is not, deliberately.**
This paragraph used to say the counters "reset on restart" and call a durable rolling window a
conscious deferral, which was true and is the failure it described: a cap read as "this user may
spend 20,000,000 tokens" in fact meant "per pod, between restarts", and a three-replica deployment
with a nightly roll multiplies it by the replica count and again by the roll. `api/budget_store.py`
now backs the *user* scope with one Postgres row per principal on a rolling window, engaged wherever
`session_store == "postgres"` — the same switch the audit sink and the turn-cost ledger read, rather
than a flag beside it. A session stays in-process because a session dies with the process that holds
it, so a durable counter for one would outlive the thing it meters.

**The two halves are combined with `max()`, not replaced**, and that is what keeps the guard correct
across the fire-and-forget write below: the in-process counter has this pod's just-ended turn
immediately, the durable row has every pod's turns a moment later, and neither is a superset of the
other. `agent/spend_cap.py` reads its own two sources the same way and for the same reason.

Off by default (`budget_enabled`), so a deployment opts in.
"""

import asyncio
import logging
import threading
from dataclasses import dataclass

from chemclaw.core.bounded import BoundedLru
from chemclaw.core.config import settings
from chemclaw.core.metrics_bridge import degraded, record_metric

logger = logging.getLogger(__name__)

# Strong references to the in-flight durable writes, for the reason `agent/turn_cost.py` holds the
# same set: `create_task` alone keeps no reference, so a write scheduled off a teardown can be
# garbage-collected mid-statement and lose the booking silently.
_PENDING: set[asyncio.Task[None]] = set()


class BudgetExceeded(Exception):
    """A turn is refused because it would exceed a session or user budget (maps to HTTP 429).

    Deliberately not a `ChemclawError`: this is a capacity/policy refusal (like admission control),
    not bad input, so it must never be swallowed by a reject-and-continue boundary.
    """


@dataclass
class _Counter:
    """Cumulative turns and metered tokens booked against one scope (a session or a user)."""

    turns: int = 0
    tokens: int = 0


def _over(cap: int, used: int) -> bool:
    """Whether `used` has reached `cap`, treating a cap of 0 as unlimited."""
    return cap > 0 and used >= cap


def _near(cap: int, used: int) -> bool:
    """Whether `used` has reached the warning fraction of `cap` without reaching `cap` itself.

    Both ends are excluded deliberately. A cap of 0 is unlimited, so there is nothing to approach;
    and usage at or past the cap is the *refusal's* business, which says more than a warning would
    and says it to the caller rather than only to a log.
    """
    fraction = settings.budget_warn_fraction
    if cap <= 0 or fraction <= 0:
        return False
    return used >= cap * fraction and used < cap


def _durable() -> bool:
    """Whether the per-user window is backed by Postgres.

    Derived from the store the deployment already chose rather than from a flag of its own — the
    argument `core/config/service.py` records beside `budget_window_hours`.
    """
    return settings.session_store == "postgres"


def _warn(scope: str, turns: int, tokens: int) -> None:
    """Say so once when a scope's usage crosses the warning fraction of either cap.

    Called from `record`, never from `check`: the front door checks twice per turn (a fast path
    before the admission permit and the binding one after it), so warning from `check` would double
    every count and every log line for a fact that changed once.
    """
    caps = (
        ("turns", turns, _cap(scope, "turns")),
        ("tokens", tokens, _cap(scope, "tokens")),
    )
    for unit, used, cap in caps:
        if not _near(cap, used):
            continue
        record_metric(lambda m: m.increment("chemclaw_budget_warnings_total"))
        logger.warning(
            "%s %s budget %.0f%% spent (%d of %d) — the next turns will be refused at the cap",
            scope,
            unit,
            100 * used / cap,
            used,
            cap,
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


def _book(counters: BoundedLru[str, _Counter], key: str, tokens: int) -> _Counter:
    """Add one turn and its (non-negative) tokens to `key`, evicting the LRU past capacity.

    The map itself is `chemclaw.core.bounded.BoundedLru` (S2) — the tracker lives for the pod's
    whole lifetime, and without a bound every session/user ever seen would keep a counter (a slow
    memory leak in the long-lived front door). Eviction resets that scope's in-process budget, which
    for a user is now only half the accounting: the durable row survives both the eviction and the
    restart. Capacity is a live config read, so the caps stay ENV-overridable.

    Returns the updated counter so the caller can warn off it without a second lookup.
    """
    counter = counters.get(key)
    if counter is None:
        counter = _Counter()
    counter.turns += 1
    counter.tokens += max(tokens, 0)
    counters.put(key, counter)
    return counter


class BudgetTracker:
    """Meter + admission gate for agent-turn cost, keyed by session and by user.

    `check` refuses (pre-turn) a turn that would breach a cap; `record` books a completed turn's
    turn-count and token usage. Neither runs *inside* a turn — see the module docstring for what
    that means and which guard covers it. A lock guards the in-process counters because the ASGI
    server runs turns for different sessions concurrently. `check` and `record` are separate calls,
    so a bounded number of in-flight turns may pass `check` before any of them `record` — an
    overshoot acceptable for a best-effort guard, not an exact accountant. **That bound is a
    property of where `check` is
    called, not of this class**, and it was false until the front door re-checked *after* taking an
    admission permit: checking only at request entry made the overshoot the number of concurrent
    requests instead (measured: 40 turns against a 1-turn cap with 8 permits). It is
    `service_max_concurrent_turns` plus however many turns are running *detached*, which give their
    permit back at the disconnect and keep spending — not the flat `service_max_concurrent_turns`
    this paragraph used to name. See `chemclaw.api.routes.turns.post_message`. Both counter maps
    are LRU-bounded (sessions by `service_max_live_sessions` — a budget counter lives as long as the
    live session it meters can — users by `budget_max_tracked_users`), so the tracker never grows
    unbounded in the long-lived front door.

    **`check` is async and `record` is not, and the asymmetry is forced rather than chosen.**
    `record`'s one production caller is `api/runner._book_turn_spend`, which runs from a
    `finally`-driven teardown where an `await` re-raises a pending cancellation and skips whatever
    follows it (D-130) — the same constraint that makes `agent/turn_cost.record_turn_cost`
    synchronous by contract, and this schedules its durable write exactly as that one does. `check`
    has no such caller: both sites are in `api/routes/turns.py`, one in an `async def` and one in an
    async generator, so it can read the durable row before admitting a turn.
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

        No-op when `budget_enabled` is off. Checked against usage *already booked*, so the first
        turn that reaches a cap is the one refused (a cap of 100 allows 100 turns, refuses no. 101).

        The user scope reads the durable window where one is configured and takes the larger of the
        two counts, for the reason the module docstring gives. A database that cannot be reached
        degrades to the in-process count rather than failing the turn: refusing a chemist's question
        because the meter is unreachable would make the guard an outage amplifier, and the
        in-process half still bounds this pod.
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
        turns, tokens = (local.turns, local.tokens) if local is not None else (0, 0)
        if _durable():
            stored_turns, stored_tokens = await self._stored(user)
            turns, tokens = max(turns, stored_turns), max(tokens, stored_tokens)
        self._check_scope(
            _Counter(turns=turns, tokens=tokens),
            "user",
            settings.budget_max_turns_per_user,
            settings.budget_max_tokens_per_user,
        )

    @staticmethod
    async def _stored(user: str) -> tuple[int, int]:
        """This principal's durable window, or zeros if the database cannot answer."""
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
            return 0, 0

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

        No-op when `budget_enabled` is off. A failed turn is still booked — it consumed tokens up to
        the failure, so its cost must count toward the next `check`.

        Synchronous by contract (see the class docstring). The in-process counters are booked here
        and now, so the very next `check` in this pod sees this turn even if the durable write has
        not landed; the durable write is scheduled as its own task and warns off what it returns.
        """
        if not settings.budget_enabled:
            return
        with self._lock:
            session = _book(self._sessions, session_id, tokens)
            local = _book(self._users, user, tokens) if user is not None else None
        _warn("session", session.turns, session.tokens)
        if user is None:
            return
        if not _durable():
            if local is not None:
                _warn("user", local.turns, local.tokens)
            return
        self._schedule(user, tokens)

    @staticmethod
    def _schedule(user: str, tokens: int) -> None:
        """Book the durable window off the hot path, warning off the totals it returns.

        A window that cannot be written is logged and lost, which is the same trade
        `agent/turn_cost.record_turn_cost` states: failing a turn that already answered, in order to
        meter what it cost, would be the tail wagging the dog. The in-process counter still holds
        this turn, so the loss is cross-pod visibility of one turn rather than of the budget.
        """
        from chemclaw.api import budget_store

        async def _write() -> None:
            try:
                turns, tokens_spent = await budget_store.book(user, tokens)
            except Exception:
                degraded(
                    logger,
                    "budget_window",
                    "could not book a turn against the durable budget window for %s; this turn is "
                    "counted on this pod only",
                    user,
                )
                return
            _warn("user", turns, tokens_spent)

        try:
            task = asyncio.get_running_loop().create_task(_write())
        except RuntimeError:  # no running loop — a synchronous caller has nowhere to schedule
            logger.debug("no event loop to book the durable budget window for %s", user)
            return
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)
