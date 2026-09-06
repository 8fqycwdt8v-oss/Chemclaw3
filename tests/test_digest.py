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

The second half of this file is about the *other* end of the same watermark: for the whole first
life of this job nothing could read what it delivered, so the watermark advanced past matches no
surface could show and `_is_new` could never re-qualify them
(`D-2026-08-27-a-digest-nobody-can-read-is-not-delivered`). `GET /digests` is the reader, and these
tests pin what makes the acknowledgement above honest — one owner reads their own mailbox and no
one else's, the read is the consume, the claim is kind-scoped, retention can then age the row out,
and the oid the route derives is the oid a watch is saved under.
"""

import asyncio
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from chemclaw.agent.session_events import claim_unconsumed, record_session_event
from chemclaw.agent.subscriptions import Subscription, for_owner, watch_for
from chemclaw.api.app import create_app
from chemclaw.api.auth import Principal, require_principal
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.durable.digest import DIGEST_KIND, _is_new, _matches, collect_digests, digest_channel
from chemclaw.durable.retention import prune_expired_rows
from chemclaw.kg.graph import invalidate_cache
from chemclaw.kg.note import Note
from chemclaw.kg.render import render_note
from chemclaw.kg.search import query_terms
from tests.pg import migrated_db_or_skip


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


def _digest_client(oid: str) -> TestClient:
    """The front door with `oid` as the authenticated caller — the only thing `/digests` reads.

    No graph factory and no connectors: this route touches neither, and giving it a fake agent
    would be scenery around the one thing under test.
    """
    app = create_app(connector_factory=lambda _profile: [])
    app.dependency_overrides[require_principal] = lambda: Principal(oid=oid, upn=f"{oid}@corp")
    return TestClient(app)


async def _consumed_at(channel: str) -> list[object]:
    """Every row in `channel`'s mailbox with its `consumed_at` — read without claiming anything."""
    async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT consumed_at FROM session_events WHERE session_id = %s ORDER BY id", (channel,)
        )
        return [row[0] for row in await cur.fetchall()]


def test_a_digest_is_read_by_its_owner_and_by_nobody_else() -> None:
    """`GET /digests` delivers the caller's own mailbox, once, and never another chemist's.

    The reproduction this closes (`D-2026-08-27-a-digest-nobody-can-read-is-not-delivered`): the
    only consumer of a digest row was `GET /sessions/{id}/events`, which 404s the synthetic
    `digest-<owner>` id and claims a kind set that never includes this one — so the exact claim it
    makes returned `[]` against a real digest row and left it unconsumed, while
    `acknowledge_digest` had already moved the watermark past the notes it named.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        alice, bob = "digest-alice", "digest-bob"
        for owner in (alice, bob):
            await claim_unconsumed(digest_channel(owner))  # start clean
        await record_session_event(
            digest_channel(alice), DIGEST_KIND, {"query": "suzuki", "note_ids": ["reaction-1"]}
        )

        with _digest_client(bob) as client:
            assert client.get("/digests").json() == [], "bob read alice's digest"
        # Not merely filtered out of bob's answer — untouched, so it is still alice's to read.
        assert await _consumed_at(digest_channel(alice)) == [None]

        with _digest_client(alice) as client:
            first = client.get("/digests")
            second = client.get("/digests")
        assert first.json() == [{"query": "suzuki", "note_ids": ["reaction-1"]}]
        assert second.json() == [], "the claim is the consume; a digest must not re-deliver"
        assert await _consumed_at(digest_channel(alice)) != [None], "the row was left unconsumed"

    asyncio.run(_run())


def test_only_the_digest_kind_is_claimed_from_the_mailbox() -> None:
    """The claim is destructive, so this route must scope it — job push-back is not its to consume.

    A digest mailbox is per *user* and a job's is per *session*, so today no row of another kind
    shares this channel. That is a property of who writes, not of this route: claiming everything
    here would destroy any future one silently, which is precisely how the mailbox's own docstring
    says a kind-selective consumer must not be written.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        owner = "digest-mixed"
        channel = digest_channel(owner)
        await claim_unconsumed(channel)
        await record_session_event(channel, DIGEST_KIND, {"query": "q", "note_ids": ["n-1"]})
        await record_session_event(channel, "job_completed", {"job_id": "j-1"})

        with _digest_client(owner) as client:
            assert client.get("/digests").json() == [{"query": "q", "note_ids": ["n-1"]}]

        leftover = await claim_unconsumed(channel)
        assert [event.kind for event in leftover] == ["job_completed"]

    asyncio.run(_run())


def test_a_read_digest_becomes_prunable_and_an_unread_one_does_not() -> None:
    """Reading is what lets retention age a digest out — and not reading is what kept it forever.

    `durable/retention.py`'s `session_events` predicate is `consumed_at IS NOT NULL`, which is
    right (an undelivered `job_completed` must survive its window). Its consequence, while nothing
    could read a digest, was that every digest row ever written was immortal.
    """

    async def _run() -> tuple[list[object], list[object]] | None:
        await migrated_db_or_skip()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(settings, "retention_session_events_days", 7)
        monkeypatch.setattr(settings, "retention_session_messages_days", 0)
        monkeypatch.setattr(settings, "retention_tool_results_days", 0)
        monkeypatch.setattr(settings, "retention_checkpoints_days", 0)
        try:
            read_owner, unread_owner = "digest-read", "digest-unread"
            for owner in (read_owner, unread_owner):
                await claim_unconsumed(digest_channel(owner))
            for owner in (read_owner, unread_owner):
                await record_session_event(
                    digest_channel(owner), DIGEST_KIND, {"query": "q", "note_ids": ["n-1"]}
                )
            with _digest_client(read_owner) as client:
                assert len(client.get("/digests").json()) == 1
            # Older than the window, so age is not what separates the two rows below.
            async with db.connection(settings.postgres_dsn) as conn:
                await conn.execute(
                    "UPDATE session_events SET created_at = now() - make_interval(days => 90) "
                    "WHERE session_id = ANY(%s)",
                    ([digest_channel(read_owner), digest_channel(unread_owner)],),
                )
                await conn.commit()

            await prune_expired_rows()
            return (
                await _consumed_at(digest_channel(read_owner)),
                await _consumed_at(digest_channel(unread_owner)),
            )
        finally:
            monkeypatch.undo()

    outcome = asyncio.run(_run())
    assert outcome is not None
    read_rows, unread_rows = outcome
    assert read_rows == [], "a digest the owner read was still not prunable"
    assert unread_rows == [None], "an unread digest was destroyed before anyone could read it"


def test_a_watch_is_owned_by_the_oid_the_route_reads() -> None:
    """The two ends of the mailbox address agree, and neither restates the other.

    `/digests` derives its channel from `principal.oid`; the digest job addresses one from
    `subscriptions.owner`, which `watch_for` takes from `require_actor()`. Those are the same
    identity only because `require_principal` binds the principal into the identity context
    (`api/middleware.bind_request_actor` → `set_current_identity`) — an invariant worth a test
    rather than a paragraph, since a mismatch would leave every digest written to a mailbox the
    owner cannot name and would look exactly like the defect this route closes.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        oid = "digest-oid-8e1f"
        tokens = set_current_identity(oid, frozenset())
        try:
            await watch_for("suzuki biaryl")
        finally:
            reset_current_identity(tokens)
        saved = [s for s in await for_owner(oid) if s.query == "suzuki biaryl"]
        assert [s.owner for s in saved] == [oid]
        # And that owner is what the digest job would address, which is what the route reads.
        assert digest_channel(saved[0].owner) == digest_channel(
            Principal(oid=oid, upn="x@corp").oid
        )

    asyncio.run(_run())
