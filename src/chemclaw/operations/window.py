"""The time window an operational answer covered, carried beside the answer.

**Why this is a type and not two `datetime` arguments.** The distinction an operational reading
has to preserve is between *nothing happened* and *nothing was looked at*: "no hazard flags last
quarter" is a finding, and "no hazard flags" from a query that scanned a week is not. Every reader
in `chemclaw.operations` therefore returns the window it ran over, and the window says in words how
it was asked for, so an answer can quote the span rather than restate a pair of timestamps the
chemist never supplied.

The window is half-open — `since <= ts < until` — for the ordinary reason: two adjacent windows
must not both claim a row on the boundary, or a month-on-month comparison double-counts midnight.

`until` is bound once, at construction, rather than being read as `now()` inside each query. A
report that fans five queries out concurrently would otherwise have five different upper bounds,
and the one row that lands between the first and the last would be in some sections and not others.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

#: How far back a window may be asked to reach.
#:
#: **The reason first written here was false, and the number outlived it.** It said `audit_events`
#: and `turn_costs` "are pruned by `durable/retention.py`, so a window older than any retained row
#: would answer 'nothing happened' about a period whose rows were deleted". Both tables are in
#: `retention._NOT_PRUNED`, explicitly *refused* — as are `job_records`, `note_proposals`,
#: `plan_approvals` and `effects`. **None of the six tables this package reads has any configured
#: retention**, so the honesty bound it claimed to be was guarding against a deletion that does not
#: happen, and the clamp silently truncated a legitimate three-year question against rows that are
#: still there.
#:
#: **The replacement reason was wrong too, in the same direction, and this is the third attempt.**
#: It said "every read here is an unindexed-range aggregate", which is false for three of the four
#: tables `Window` governs: `audit_events (ts)`, `job_records (completed_at DESC)` and
#: `turn_costs (recorded_at DESC)` all have a leading-column index on exactly the column the range
#: is over, and `EXPLAIN` returns an index scan for each. Only `note_proposals.submitted_at` has
#: none. The docstring this replaced said "the indexes carry far more than this", which was the
#: accurate half, and deleting it was the mistake.
#:
#: So the honest reason is narrow: it is a bound on how much a single request may aggregate, on
#: tables that only grow and are never pruned, under `db.connection`'s statement timeout. Two years
#: is a policy choice about what a *reading* is for rather than a limit anything technical imposes;
#: a caller who needs more is asking for a report. `Coverage` carries the clamped window into the
#: answer, so a truncated question is visible in its own result.
MAX_WINDOW_DAYS = 730


@dataclass(frozen=True, slots=True)
class Window:
    """A half-open span of time, and the phrase that asked for it."""

    since: datetime
    until: datetime
    #: How the caller expressed it ("the last 30 days"), for quoting back in an answer.
    described: str

    @classmethod
    def trailing(cls, days: int, *, now: datetime | None = None) -> "Window":
        """The `days` ending at `now`, clamped to at least one day and `MAX_WINDOW_DAYS`.

        Clamping rather than raising: a caller asking for 0 or 5,000 days wants a reading, and a
        window that says what it actually covered is a better answer than a refusal. The phrase in
        `described` is built from the clamped number, so it can never overstate the span.
        """
        span = max(1, min(int(days), MAX_WINDOW_DAYS))
        until = now or datetime.now(UTC)
        return cls(
            since=until - timedelta(days=span),
            until=until,
            described=f"the last {span} days",
        )

    @property
    def days(self) -> int:
        """The window's span in whole days, rounded up, for a rate an answer can state."""
        seconds = (self.until - self.since).total_seconds()
        return max(1, int(-(-seconds // 86_400)))

    def preceding(self) -> "Window":
        """The window of equal length immediately before this one — the quarter-on-quarter half.

        A trend is the one thing an operational reading is asked for that a single window cannot
        give, and deriving the comparison span here keeps both halves the same length by
        construction.
        """
        span = self.until - self.since
        return Window(
            since=self.since - span,
            until=self.since,
            described=f"the {self.days} days before {self.described}",
        )
