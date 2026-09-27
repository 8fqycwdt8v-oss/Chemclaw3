"""The per-user spend window, in Postgres, so a quota survives a restart and spans pods.

`api/budget.py` meters turns and tokens in a `BoundedLru` that lives for the pod's lifetime. That
bounds *a running process's* runaway, which is what it was written for, and its own docstring says
what it costs: "the counters reset on restart", and an LRU eviction resets a scope's budget too. So
the guard a deployment believes it has — "this user may spend 20,000,000 tokens" — is in fact "this
user may spend 20,000,000 tokens *per pod, between restarts*". With three replicas and a nightly
roll that is an order of magnitude of slack in the one direction nobody wants it.

This module is the durable half the `DEFERRED.md` row named: *"back the counters with a Postgres
table and a windowed reset, reusing the same `check`/`record` seam"*.

**One row per principal, reset in place.** The obvious shape — a row per `(actor, window)` — grows
forever and needs a sweep, and a sweep that ships off (as retention does) means the table grows
forever in every shipped deployment. Resetting in place bounds the table by the number of distinct
principals ever served, which is the bound `budget_max_tracked_users` already names for the map this
backs. The reset is lazy and atomic: it happens inside the upsert's `ON CONFLICT` arm, so a stale
row and a fresh one are one statement apart, and no second process can observe the row half-reset.

**The window is rolling and anchored at first use, not calendar-aligned.** A calendar day boundary
would hand every user a full fresh allowance at the same instant, which is the load spike a rolling
window does not have; and "anchored at first use" is what the single `window_start` column can
express without a second row. The cost is stated because it is real: a user who spends their whole
allowance in one minute waits the full window, while one who spends it evenly is reset mid-spend.

**Only the user scope, and that is a design decision rather than an omission.** A session is
bounded by `service_max_live_sessions` and dies with the process that holds it, so a durable
session counter would outlive the thing it meters and refuse turns on behalf of a conversation
nobody can resume. The deferral asked for per-*user* fairness across restarts and pods; that is a
property of a principal.
"""

from datetime import timedelta
from typing import Any, NamedTuple

from chemclaw.core import db
from chemclaw.core.config import settings

#: Book one turn against a principal, resetting the window first if it has rolled over.
#:
#: The `CASE` arms are repeated rather than factored into a CTE because all three test the same
#: predicate against `budget_usage.window_start`, and every assignment in one `ON CONFLICT DO
#: UPDATE` sees the same `budget_usage` row. So the three arms cannot disagree with each other, and
#: a CTE would buy a name at the cost of making the reset a second statement another connection
#: could interleave with.
#:
#: **That row is the latest *committed* one, not this statement's snapshot**, which is the part
#: that makes this safe under concurrency and which this comment previously got wrong: a conflicting
#: writer blocks on the row lock and then re-evaluates its arms against what the first writer
#: committed (Postgres re-checks the conflicting row rather than reusing the command's own
#: snapshot). Measured with two real connections — 32 concurrent bookings on a fresh actor give
#: exactly (32, 320), 32 on a 25-hour-old row give (32, 320) with the reset applied *once*, and a
#: booking made while another transaction holds the row survives that transaction's commit. Under
#: the snapshot reading this comment used to assert, every one of those would have lost updates.
#: `tests/test_budget_window.py` pins it, because the whole design rests on it and nothing in this
#: repository owns the behaviour.
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

#: A principal's row, live or expired, with where its window started and how old it is.
#:
#: Selected whether or not the window has expired, because the in-process half of `api/budget.py`
#: has to tell "this window ended" apart from "nothing was ever booked" to re-anchor itself
#: (`BudgetTracker._reconcile`). The expiry decision is still one clock: the age is Postgres's
#: `now()` minus `window_start`, the same subtraction `_BOOK`'s `CASE` arms make, so a reader and a
#: writer agree about when a window ended with no pod clock passed between them.
_USAGE = """
    SELECT turns, tokens,
        EXTRACT(EPOCH FROM window_start), EXTRACT(EPOCH FROM now() - window_start)
    FROM budget_usage WHERE actor = %(actor)s
"""


class Window(NamedTuple):
    """What a principal has spent in the current durable window, and which window that is.

    `start` is the window's identity (`window_start` as epoch seconds, a stable value every pod
    reads identically) and `age` its elapsed seconds by Postgres's clock. The in-process counter
    needs both: the identity to tell whether it has already been reconciled with this window, and
    the age to place the window on its own monotonic clock so both halves roll together.
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

    `Any` because that is the row psycopg hands back; `EXTRACT` arrives as a `Decimal`.

    Zero rather than the stale totals because they are the same fact as an unknown principal's:
    nothing is booked against the current window.
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

    The counts are zero for an unknown principal and for one whose window expired, because for the
    cap they are the same fact. `start` is what tells them apart, for the one reader that needs
    to: the in-process counter, which must drop counts from a window the durable row has closed.
    """
    async with db.connection(_dsn()) as conn:
        cursor = await conn.execute(_USAGE, {"actor": actor})
        row = await cursor.fetchone()
    return _from_row(row)


async def book(actor: str, tokens: int) -> Window:
    """Add one turn and its tokens to this principal's window; return the window's new totals.

    `tokens` is clamped at zero here as well as by `api/budget.py::_book` for the in-process map; a
    negative would be a provider reporting nonsense, and the table's `CHECK` refuses to store one
    either way rather than letting a bad report credit a runaway.

    **Returning the totals is what makes the warning free.** The caller wants to know whether this
    turn crossed the warning fraction, which is a question about the counters *after* the write —
    so a `RETURNING` clause answers it inside the statement that caused it, where a second `SELECT`
    would both cost a round trip and be able to disagree with it under a concurrent booking.
    """
    async with db.connection(_dsn()) as conn:
        cursor = await conn.execute(
            _BOOK, {"actor": actor, "tokens": max(tokens, 0), "window": _window()}
        )
        row = await cursor.fetchone()
    return _from_row(row)
