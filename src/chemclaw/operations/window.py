"""The time window an operational answer covered, carried beside the answer.

A type rather than two datetimes so every reading can distinguish *nothing happened* from
*nothing was looked at*: each reader returns the window it ran over, with a phrase saying how it
was asked for.

Half-open (`since <= ts < until`) so adjacent windows never both claim a boundary row. `until`
is bound once at construction, so concurrent queries in one report share one upper bound.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

#: How far back a window may be asked to reach.
#:
#: A policy bound on how much a single request may aggregate over tables that only grow and are
#: never pruned, under `db.connection`'s statement timeout. The ranges are index scans; a caller
#: needing more is asking for a report. `Coverage` carries the clamped window into the answer.
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

        Clamping rather than raising: a window that says what it covered beats a refusal, and
        `described` is built from the clamped number so it never overstates the span.
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
        """The window of equal length immediately before this one, for trend comparisons."""
        span = self.until - self.since
        return Window(
            since=self.since - span,
            until=self.since,
            described=f"the {self.days} days before {self.described}",
        )
