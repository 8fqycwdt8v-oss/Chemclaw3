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


class BudgetTracker:
    """In-process meter + admission gate for agent-turn cost, keyed by session and by user.

    `check` refuses (pre-turn) a turn that would breach a cap; `record` books a completed turn's
    turn-count and token usage. A lock guards the counters because the ASGI server runs turns for
    different sessions concurrently. `check` and `record` are separate calls, so up to
    `service_max_concurrent_turns` in-flight turns may pass `check` before any of them `record` — a
    bounded overshoot acceptable for a best-effort guard, not an exact accountant.
    """

    def __init__(self) -> None:
        """Start with empty per-session and per-user counters."""
        self._sessions: dict[str, _Counter] = {}
        self._users: dict[str, _Counter] = {}
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
            self._book(self._sessions, session_id, tokens)
            if user is not None:
                self._book(self._users, user, tokens)

    @staticmethod
    def _book(scope_map: dict[str, _Counter], key: str, tokens: int) -> None:
        """Increment a scope's turn count and add its (non-negative) token usage."""
        counter = scope_map.setdefault(key, _Counter())
        counter.turns += 1
        counter.tokens += max(tokens, 0)
