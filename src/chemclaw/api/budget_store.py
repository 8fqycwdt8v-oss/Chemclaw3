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

from chemclaw.core import db
from chemclaw.core.config import settings

#: Book one turn against a principal, resetting the window first if it has rolled over.
#:
#: The `CASE` arms are repeated rather than factored into a CTE because all three test the same
#: predicate against `budget_usage.window_start` — the row's value *before* this statement — and
#: Postgres evaluates every `ON CONFLICT DO UPDATE` assignment against that same pre-image. So the
#: three arms cannot disagree with each other, and a CTE would buy a name at the cost of making the
#: reset a second statement another connection could interleave with.
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
    RETURNING turns, tokens
"""

#: What a principal has spent inside the *current* window.
#:
#: The `window_start >` predicate is what makes a stale row read as zero without anything having to
#: rewrite it: an expired window is simply not selected, and the next `_BOOK` resets it. So a reader
#: and a writer agree about when a window ended with no clock passed between them — both ask
#: Postgres's `now()`, which is the one clock a fleet of pods shares.
_USAGE = """
    SELECT turns, tokens FROM budget_usage
    WHERE actor = %(actor)s AND window_start > now() - %(window)s::interval
"""


def _dsn() -> str:
    """The database the session tier already uses — a budget window has a session's lifetime."""
    return settings.session_store_dsn or settings.postgres_dsn


def _window() -> timedelta:
    """The rolling window, as the interval both statements bind."""
    return timedelta(hours=settings.budget_window_hours)


async def usage(actor: str) -> tuple[int, int]:
    """`(turns, tokens)` this principal has spent in the current window; zeros if it has rolled.

    Zeros for an unknown principal and for one whose window expired are deliberately the same
    answer, because they are the same fact: nothing is booked against this window. Distinguishing
    them would need the caller to care about a difference that cannot change its decision.
    """
    async with db.connection(_dsn()) as conn:
        cursor = await conn.execute(_USAGE, {"actor": actor, "window": _window()})
        row = await cursor.fetchone()
    if row is None:
        return 0, 0
    return int(row[0]), int(row[1])


async def book(actor: str, tokens: int) -> tuple[int, int]:
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
    if row is None:  # pragma: no cover - an upsert with RETURNING always yields its row
        return 0, 0
    return int(row[0]), int(row[1])
