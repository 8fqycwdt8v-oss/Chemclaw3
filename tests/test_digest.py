"""A standing query reports a note once — including the day it appeared (DARK-7).

`durable/digest.py` decides freshness on the note's own `valid_from`, which is a **date**, against
a `last_seen_at` watermark that is a timestamp. That mismatch is the whole problem, and both
obvious readings of it are wrong in opposite directions:

- `>` drops a note that appeared later on the same day the digest ran — the common case at the
  shipped hourly cadence, and the failure the feature exists to prevent;
- `>=` re-qualifies every same-day note on every run — up to 24 deliveries a day, against
  `agent/subscriptions.py`'s own promise that "asking twice does not double-notify".

The subscription therefore remembers which ids it sent *at the watermark's date*, which separates
"dated today and already sent" from "dated today and new" without choosing between the two
failures. These tests pin all three cases plus the bound on what is remembered.
"""

from datetime import UTC, date, datetime
from typing import Any

from chemclaw.agent.subscriptions import Subscription
from chemclaw.durable.digest import _is_new


def _note(note_id: str, valid_from: date | None) -> Any:
    """The slice of a note the freshness test reads: its id and when it became knowledge."""

    class _Note:
        id = note_id

    _Note.valid_from = valid_from  # type: ignore[attr-defined]
    return _Note()


def _subscription(seen_at: datetime | None, seen_ids: list[str] | None = None) -> Subscription:
    """A standing query with a watermark and what it has already delivered at that watermark."""
    return Subscription(
        id=1,
        owner="chemist-a",
        query="suzuki",
        last_seen_at=seen_at,
        last_seen_note_ids=seen_ids or [],
    )


_TODAY = datetime(2026, 7, 31, 9, 0, tzinfo=UTC)


def test_a_note_from_after_the_watermark_is_new() -> None:
    """The uncontroversial case, and the one the whole feature is for."""
    assert _is_new(_note("reaction-1", date(2026, 8, 1)), _subscription(_TODAY)) is True


def test_a_note_from_before_the_watermark_is_not() -> None:
    """A digest is a digest, not a re-send of the corpus."""
    assert _is_new(_note("reaction-1", date(2026, 7, 30)), _subscription(_TODAY)) is False


def test_a_same_day_note_is_reported_once_and_not_again() -> None:
    """The defect: at an hourly cadence this note was delivered on every run for a day."""
    same_day = _note("reaction-1", date(2026, 7, 31))

    assert _is_new(same_day, _subscription(_TODAY)) is True
    assert _is_new(same_day, _subscription(_TODAY, ["reaction-1"])) is False


def test_a_same_day_note_that_arrives_later_is_still_reported() -> None:
    """The other half, and the reason `>` is not the fix.

    A note whose `valid_from` is today but which reached the tree after this morning's digest must
    still be delivered — dropping it is the failure the ordering elsewhere in that module goes out
    of its way to avoid.
    """
    arrived_later = _note("reaction-2", date(2026, 7, 31))

    assert _is_new(arrived_later, _subscription(_TODAY, ["reaction-1"])) is True


def test_a_note_with_no_date_is_reported_once_rather_than_never() -> None:
    """An undated note has no watermark to compare against; silence is the worse answer."""
    assert _is_new(_note("playbook-1", None), _subscription(_TODAY)) is True
    assert _is_new(_note("playbook-1", date(2026, 7, 31)), _subscription(None)) is True
