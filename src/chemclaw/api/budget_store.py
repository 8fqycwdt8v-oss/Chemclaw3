"""The per-user spend window, in Postgres, so a quota survives a restart and spans pods.

`api/budget.py`'s in-process counters reset on restart and are per pod; this is the durable half
behind the same `check`/`record` seam.

One row per principal, reset in place inside the upsert's `ON CONFLICT` arm, so the table is
bounded by the number of principals and needs no sweep, and no process sees a half-reset row. The
window is rolling and anchored at first use, so allowances do not all reset at one instant; a user
who spends everything at once waits the full window. Only the user scope is durable: a session
dies with its process, so a durable session counter would outlive what it meters.
"""

from datetime import timedelta
from typing import Any, NamedTuple

from chemclaw.core import db
from chemclaw.core.config import settings

# Book one turn against a principal, resetting the window first if it has rolled over.
#
# The `CASE` arms repeat one predicate rather than use a CTE, keeping the reset in the same
# statement. Under concurrency a conflicting writer blocks on the row lock and re-evaluates against
# the latest committed row, not its snapshot, so no update is lost; `tests/test_budget_window.py`
# pins this.
_BOOK = """
    INSERT INTO budget_usage (actor, window_start, turns, tokens)
    VALUES (%(actor)s, now(), 1, %(tokens)s)
    ON CONFLICT (actor) DO UPDATE SET
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

# A principal's row, live or expired, with where its window started and how old it is.
#
# Returned even when expired, so `BudgetTracker._reconcile` can tell "window ended" from "never
# booked". The age uses Postgres's `now()`, the same clock as `_BOOK`.
_USAGE = """
    SELECT turns, tokens,
        EXTRACT(EPOCH FROM window_start), EXTRACT(EPOCH FROM now() - window_start)
    FROM budget_usage WHERE actor = %(actor)s
"""


class Window(NamedTuple):
    """What a principal has spent in the current durable window, and which window that is.

    `start` identifies the window (`window_start` as epoch seconds, identical on every pod); `age`
    is
    its elapsed seconds by Postgres's clock, which the in-process counter uses to roll with it.
    """

    turns: int
    tokens: int
    #: `None` when the principal has no row at all — nothing has ever been booked durably.
    start: float | None = None
    age: float = 0.0

    @property
    def expired(self) -> bool:
        """Whether a row exists and its window has run out, so the next booking opens a new one."""
        return self.start is not None and self.age >= _window().total_seconds()


def _from_row(row: tuple[Any, ...] | None) -> Window:
    """A `Window` from `(turns, tokens, start, age)`, with an expired window's counts read as zero.

    `Any` because that is psycopg's row type; `EXTRACT` arrives as a `Decimal`.
    """
    if row is None:
        return Window(0, 0)
    turns, tokens, start, age = row
    window = Window(int(turns), int(tokens), float(start), float(age))
    return window._replace(turns=0, tokens=0) if window.expired else window


def _dsn() -> str:
    """The database the session tier already uses — a budget window has a session's lifetime."""
    return settings.session_store_dsn or settings.postgres_dsn


def _window() -> timedelta:
    """The rolling window, as the interval both statements bind."""
    return timedelta(hours=settings.budget_window_hours)


async def usage(actor: str) -> Window:
    """What this principal has spent in the current window; zero counts if it has rolled.

    Unknown and expired both read zero; `start` tells them apart for the in-process counter.
    """
    async with db.connection(_dsn()) as conn:
        cursor = await conn.execute(_USAGE, {"actor": actor})
        row = await cursor.fetchone()
    return _from_row(row)


async def book(actor: str, tokens: int) -> Window:
    """Add one turn and its tokens to this principal's window; return the window's new totals.

    `tokens` is clamped at zero (the table's `CHECK` refuses negatives too). The totals come from
    `RETURNING`, so the caller's warning check sees exactly what this write produced.
    """
    async with db.connection(_dsn()) as conn:
        cursor = await conn.execute(
            _BOOK, {"actor": actor, "tokens": max(tokens, 0), "window": _window()}
        )
        row = await cursor.fetchone()
    return _from_row(row)
