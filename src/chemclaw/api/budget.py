"""Per-session and per-user turn/token budgets — the runaway-cost guard (plan F-budget).

A single agent turn is already iteration-capped (`harness_max_loop_iterations`), so one turn
cannot loop forever. But nothing caps the *number* of
turns, so a client — or an automated job→session push-back loop re-waking a session — could keep
posting turns and accumulate unbounded LLM spend. This tracker is the missing ceiling above the
per-turn loop cap: the front door meters each turn's reported token usage and counts turns per
session and per user, and refuses (HTTP 429) a turn that would exceed a configured cap.

Scope is deliberately in-process and best-effort — the counters reset on restart. That bounds a
running process's runaway (the "$400 in twenty minutes" failure), which is what the per-turn loop
cap leaves open; a durable, rolling-window per-user quota that survives restarts is a larger piece,
consciously deferred (see docs/planning/DEFERRED.md). Off by default (`budget_enabled`), so a
deployment opts in.
"""

import threading
from dataclasses import dataclass

from chemclaw.core.bounded import BoundedLru
from chemclaw.core.config import settings


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


def _book(counters: BoundedLru[str, _Counter], key: str, tokens: int) -> None:
    """Add one turn and its (non-negative) tokens to `key`, evicting the LRU past capacity.

    The map itself is `chemclaw.core.bounded.BoundedLru` (S2) — the tracker lives for the pod's
    whole lifetime, and without a bound every session/user ever seen would keep a counter (a slow
    memory leak in the long-lived front door). Eviction resets that scope's budget, which is the
    documented best-effort trade (same as the restart reset); the durable rolling-window quota
    stays deferred. Capacity is a live config read, so the caps stay ENV-overridable.
    """
    counter = counters.get(key)
    if counter is None:
        counter = _Counter()
    counter.turns += 1
    counter.tokens += max(tokens, 0)
    counters.put(key, counter)


class BudgetTracker:
    """In-process meter + admission gate for agent-turn cost, keyed by session and by user.

    `check` refuses (pre-turn) a turn that would breach a cap; `record` books a completed turn's
    turn-count and token usage. A lock guards the counters because the ASGI server runs turns for
    different sessions concurrently. `check` and `record` are separate calls, so a bounded number
    of in-flight turns may pass `check` before any of them `record` — an overshoot acceptable for a
    best-effort guard, not an exact accountant. **That bound is a property of where `check` is
    called, not of this class**, and it was false until the front door re-checked *after* taking an
    admission permit: checking only at request entry made the overshoot the number of concurrent
    requests instead (measured: 40 turns against a 1-turn cap with 8 permits). It is
    `service_max_concurrent_turns` plus however many turns are running *detached*, which give their
    permit back at the disconnect and keep spending — not the flat `service_max_concurrent_turns`
    this paragraph used to name. See `chemclaw.api.routes.turns.post_message`. Both counter maps
    are LRU-bounded (sessions by `service_max_live_sessions` — a budget counter lives as long as the
    live session it meters can — users by `budget_max_tracked_users`), so the tracker never grows
    unbounded in the long-lived front door.
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
            _book(self._sessions, session_id, tokens)
            if user is not None:
                _book(self._users, user, tokens)
