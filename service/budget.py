"""Per-session and per-user turn/token budgets — the runaway-cost guard (plan F-budget).

A single agent turn is already iteration-capped (`harness_max_loop_iterations`, or MAF's 40
roundtrips for the classic agent), so one turn cannot loop forever. But nothing caps the *number* of
turns, so a client — or an automated job→session push-back loop re-waking a session — could keep
posting turns and accumulate unbounded LLM spend. This tracker is the missing ceiling above the
per-turn loop cap: the front door meters each turn's reported token usage and counts turns per
session and per user, and refuses (HTTP 429) a turn that would exceed a configured cap.

Scope is deliberately in-process and best-effort — the counters reset on restart. That bounds a
running process's runaway (the "$400 in twenty minutes" failure), which is what the per-turn loop
cap leaves open; a durable, rolling-window per-user quota that survives restarts is a larger piece,
consciously deferred (see DEFERRED.md). Off by default (`budget_enabled`), so a deployment opts in.
"""

import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from chemclaw.config import settings


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


class _BoundedCounters:
    """An LRU-bounded map of scope key → `_Counter`, so the tracker cannot grow forever.

    The tracker lives for the pod's whole lifetime; without a bound every session/user ever seen
    keeps a counter (a slow memory leak in the long-lived front door). Past capacity the
    least-recently-active scope is evicted — its budget resets, which is the documented best-effort
    trade (same as the restart reset); the durable rolling-window quota stays deferred. Capacity is
    read per call so the config stays live/env-overridable.
    """

    def __init__(self, capacity: Callable[[], int]) -> None:
        """Create the map with `capacity` returning the current entry cap (config-backed)."""
        self._capacity = capacity
        self._entries: OrderedDict[str, _Counter] = OrderedDict()

    def get(self, key: str) -> _Counter | None:
        """Return the counter for `key` (marking it recently active), or None if untracked."""
        counter = self._entries.get(key)
        if counter is not None:
            self._entries.move_to_end(key)
        return counter

    def book(self, key: str, tokens: int) -> None:
        """Add one turn and its (non-negative) tokens to `key`, evicting the LRU past capacity."""
        counter = self._entries.get(key)
        if counter is None:
            counter = self._entries[key] = _Counter()
        self._entries.move_to_end(key)
        counter.turns += 1
        counter.tokens += max(tokens, 0)
        while len(self._entries) > self._capacity():
            self._entries.popitem(last=False)


class BudgetTracker:
    """In-process meter + admission gate for agent-turn cost, keyed by session and by user.

    `check` refuses (pre-turn) a turn that would breach a cap; `record` books a completed turn's
    turn-count and token usage. A lock guards the counters because the ASGI server runs turns for
    different sessions concurrently. `check` and `record` are separate calls, so up to
    `service_max_concurrent_turns` in-flight turns may pass `check` before any of them `record` — a
    bounded overshoot acceptable for a best-effort guard, not an exact accountant. Both counter maps
    are LRU-bounded (sessions by `service_max_live_sessions` — a budget counter lives as long as the
    live session it meters can — users by `budget_max_tracked_users`), so the tracker never grows
    unbounded in the long-lived front door.
    """

    def __init__(self) -> None:
        """Start with empty, LRU-bounded per-session and per-user counters."""
        self._sessions = _BoundedCounters(lambda: settings.service_max_live_sessions)
        self._users = _BoundedCounters(lambda: settings.budget_max_tracked_users)
        self._lock = threading.Lock()

    def check(self, session_id: str, user: str | None) -> None:
        """Raise `BudgetExceeded` if the next turn would exceed a session or user cap.

        No-op when `budget_enabled` is off. Checked against usage *already booked*, so the first
        turn that reaches a cap is the one refused (a cap of 100 allows 100 turns, refuses no. 101).
        """
        if not settings.budget_enabled:
            return
        with self._lock:
            self._check_scope(
                self._sessions.get(session_id),
                "session",
                settings.budget_max_turns_per_session,
                settings.budget_max_tokens_per_session,
            )
            if user is not None:
                self._check_scope(
                    self._users.get(user),
                    "user",
                    settings.budget_max_turns_per_user,
                    settings.budget_max_tokens_per_user,
                )

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
        """
        if not settings.budget_enabled:
            return
        with self._lock:
            self._sessions.book(session_id, tokens)
            if user is not None:
                self._users.book(user, tokens)
