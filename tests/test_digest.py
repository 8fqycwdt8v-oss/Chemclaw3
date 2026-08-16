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

import asyncio
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from chemclaw.agent.subscriptions import Subscription
from chemclaw.core.config import settings
from chemclaw.durable.digest import _is_new, _matches, collect_digests
from chemclaw.kg.graph import invalidate_cache
from chemclaw.kg.note import Note
from chemclaw.kg.render import render_note
from chemclaw.kg.search import query_terms


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


def test_the_digest_reads_the_tree_the_notes_are_actually_written_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`collect_digests` resolves `settings.knowledge_path`, like every other reader.

    It resolved `Path(settings.knowledge_dir)` raw — a *relative* default, so it scanned whatever
    directory the process happened to be started in rather than the note repo. In the deployed
    shape (`note_repo_dir` a dedicated clone, the whole point of the property) that is a different
    tree from the one merged notes land in, and the failure is silent in the worst way available:
    an empty scan is indistinguishable from "no new matches", so every subscriber's standing query
    simply stops reporting and nothing anywhere says so.

    Exercised against a note repo laid out the way a pod's is — `note_repo_dir/knowledge_dir` —
    with a query token that appears in no shipped note, so reading the wrong tree yields nothing
    and the test fails rather than passing on the corpus that happens to be next to the CWD.
    """
    repo = tmp_path / "note-repo"
    note = Note(
        id="reaction-thermolysin-9", type="reaction", body="a thermolysin-catalysed coupling"
    )
    path = repo / "knowledge" / note.type / f"{note.id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_note(note), encoding="utf-8")
    invalidate_cache()

    monkeypatch.setattr(settings, "note_repo_dir", str(repo))
    monkeypatch.setattr(settings, "knowledge_dir", "knowledge")
    watching = Subscription(id=1, owner="chemist-a", query="thermolysin", last_seen_at=None)
    monkeypatch.setattr("chemclaw.durable.digest.all_subscriptions", lambda: _resolved([watching]))

    digests = asyncio.run(collect_digests())

    assert [item.note_ids for item in digests] == [["reaction-thermolysin-9"]], (
        "the digest scanned a different tree from the one notes are written to"
    )


async def _resolved(value: Any) -> Any:
    """An awaitable of an already-known value — `all_subscriptions` is async, this test is not."""
    return value


def test_matching_is_unchanged_by_tokenizing_the_query_once_per_subscription() -> None:
    """The query's terms are hoisted out of the per-note loop without changing a single verdict.

    `_matches` called `query_terms(subscription.query)` itself, once for every note in the corpus:
    50 subscriptions over 2,000 notes was 100,000 regex splits of a string that never varies, and
    hoisting it measured 352 ms to 225 ms on an hourly activity. Correctness is the thing at risk
    in a refactor like that, so the cases pinned here are the ones where the tokenizer does
    something other than split on spaces — stopwords, punctuation-bearing chemistry, a query that
    survives filtering as nothing, and the type filter that short-circuits ahead of the terms.
    """
    # A real `Note`, not the freshness tests' stub: `_matches` reads the whole searchable haystack
    # (`kg.search.search_text`), and a stub with two attributes would prove nothing about it.
    note = Note(
        id="reaction-1",
        type="reaction",
        body="a Pd(OAc)2 catalysed biaryl coupling in the flask",
    )
    for query, expected in [
        ("biaryl", True),
        ("the biaryl", True),  # the stopword must not be required as a term
        ("Pd(OAc)2", True),  # punctuation splits into parts the body holds
        ("biaryl nonexistentword", False),
        # Nothing survives filtering, so the whole query becomes the one term and is matched
        # literally — "a search for `the` is still a search" (`query_terms`). The body has one.
        ("the", True),
        ("the nonexistentword", False),  # and the fallback does not weaken the all-terms rule
        ("", False),  # a blank query asks for nothing and gets nothing
    ]:
        subscription = Subscription(id=1, owner="c", query=query, last_seen_at=None)
        assert _matches(note, subscription, query_terms(query)) is expected, query

    typed = Subscription(id=1, owner="c", query="biaryl", last_seen_at=None, note_type="playbook")
    assert _matches(note, typed, query_terms("biaryl")) is False
