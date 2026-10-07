"""A standing query reports a note once — including the day it appeared.

`durable/digest.py` compares the note's `valid_from` (a **date**) against a `last_seen_at`
timestamp, and both obvious comparisons are wrong: `>` drops a note that appeared later on the
digest's own day, and `>=` re-reports every same-day note on every run. So the subscription
remembers which ids it sent at the watermark's date. These tests pin the three cases and the bound
on what is remembered.

The second half covers the reader, `GET /digests`
(`D-2026-08-27-a-digest-nobody-can-read-is-not-delivered`): an owner reads only their own mailbox,
the read is the consume, the claim is kind-scoped, retention can then age the row out, and the
route's oid is the oid a watch is saved under.
"""

import asyncio
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import chemclaw.durable.digest
from chemclaw.agent.session_events import claim_unconsumed, record_session_event
from chemclaw.agent.subscriptions import Subscription, for_owner, watch_for
from chemclaw.api.app import create_app
from chemclaw.api.auth import Principal, require_principal
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.durable.digest import (
    DIGEST_KIND,
    _digest_body,
    _is_new,
    _matches,
    collect_digests,
    digest_channel,
)
from chemclaw.durable.retention import prune_expired_rows
from chemclaw.kg.graph import invalidate_cache, load_notes
from chemclaw.kg.note import Note, Relation
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
    """A same-day note that arrives later is still reported — the reason `>` is not the fix."""
    arrived_later = _note("reaction-2", date(2026, 7, 31))

    assert _is_new(arrived_later, _subscription(_TODAY, ["reaction-1"])) is True


def test_an_undated_note_is_told_once_and_then_not_again() -> None:
    """An undated note is told once and then not again.

    A `None` `valid_from` is open-ended (`Note.is_current`), so the note did not become knowledge
    after the subscriber was last told; re-reporting it every run would break the "asking twice does
    not double-notify" promise. A subscriber never told anything still hears it once.
    """
    undated = _note("playbook-1", None)

    assert _is_new(undated, _subscription(None)) is True
    assert _is_new(undated, _subscription(_TODAY)) is False
    assert _is_new(_note("playbook-1", date(2026, 7, 31)), _subscription(None)) is True


def test_a_distilled_playbook_carries_the_day_it_was_minted() -> None:
    """A distilled playbook carries the day it was minted.

    A playbook is concluded by a miner, not written on a day; `minted_on` says it became knowledge
    when the miner ran, so the strict undated rule above does not hide it.
    """
    from chemclaw.memory.playbook import playbook_note

    minted = playbook_note("playbook-x", "it holds", ["reaction-1"], minted_on=date(2026, 7, 31))

    assert minted.valid_from == date(2026, 7, 31)
    assert _is_new(minted, _subscription(datetime(2026, 7, 30, 9, tzinfo=UTC))) is True
    assert _is_new(minted, _subscription(datetime(2026, 8, 1, 9, tzinfo=UTC))) is False


def test_the_digest_reads_the_tree_the_notes_are_actually_written_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`collect_digests` resolves `settings.knowledge_path`, like every other reader.

    A raw relative `knowledge_dir` would scan the process's working directory, and an empty scan is
    indistinguishable from "no new matches". Exercised against a pod-shaped note repo with a query
    token no shipped note contains, so reading the wrong tree fails.
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
    """Tokenizing the query once per subscription changes no verdict.

    The query's terms are hoisted out of the per-note loop. The pinned cases are where the tokenizer
    does more than split on spaces: stopwords, punctuation-bearing chemistry, a query that filters
    to nothing, and the type filter that short-circuits ahead of the terms.
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


async def test_a_digest_is_read_by_its_owner_and_by_nobody_else() -> None:
    """`GET /digests` delivers the caller's own mailbox, once, and never another chemist's.

    The digest job acknowledges (moves the watermark) when it writes, so the row must be readable by
    its owner, or the matches it names are never seen.
    """
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
    # The two fields `D-2026-09-15-a-digest-that-names-an-id-names-nothing` added are part
    # of the answer's shape, not decoration: written absent here, they must come back empty
    # rather than missing, which is what a client renders against.
    assert first.json() == [
        {
            "query": "suzuki",
            "note_ids": ["reaction-1"],
            "disputed": [],
            "headlines": {},
        }
    ]
    assert second.json() == [], "the claim is the consume; a digest must not re-deliver"
    assert await _consumed_at(digest_channel(alice)) != [None], "the row was left unconsumed"


async def test_only_the_digest_kind_is_claimed_from_the_mailbox() -> None:
    """The claim is destructive, so this route must scope it to the digest kind.

    No other kind shares this channel today, but claiming everything would silently destroy any
    future one.
    """
    await migrated_db_or_skip()
    owner = "digest-mixed"
    channel = digest_channel(owner)
    await claim_unconsumed(channel)
    await record_session_event(channel, DIGEST_KIND, {"query": "q", "note_ids": ["n-1"]})
    await record_session_event(channel, "job_completed", {"job_id": "j-1"})

    with _digest_client(owner) as client:
        assert client.get("/digests").json() == [
            {"query": "q", "note_ids": ["n-1"], "disputed": [], "headlines": {}}
        ]

    leftover = await claim_unconsumed(channel)
    assert [event.kind for event in leftover] == ["job_completed"]


def test_a_read_digest_becomes_prunable_and_an_unread_one_does_not() -> None:
    """A read digest becomes prunable and an unread one does not.

    `durable/retention.py` prunes `session_events` only where `consumed_at IS NOT NULL`, so reading
    is what lets retention age a digest out.
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


async def test_a_watch_is_owned_by_the_oid_the_route_reads() -> None:
    """A watch is owned by the oid the route reads.

    `/digests` uses `principal.oid`; the job addresses `subscriptions.owner`, which `watch_for`
    takes from `require_actor()`. They agree only because `require_principal` binds the principal
    into the identity context, so a mismatch would deliver every digest to a mailbox its owner
    cannot name.
    """
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
    assert digest_channel(saved[0].owner) == digest_channel(Principal(oid=oid, upn="x@corp").oid)


# --- the corpus disagreeing with itself ----------------------------------------------------------


def test_a_new_note_that_contradicts_an_existing_one_is_marked_in_the_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new note that contradicts an existing one is marked in the digest.

    Retrieval flags disputed notes for a chemist who queries; a chemist watching the subject must be
    told too. Driven over a real corpus with a real `contradicts` relation, so the sweep and the
    digest are shown to agree.
    """
    repo = tmp_path / "note-repo"
    established = Note(
        id="reaction-thermolysin-9", type="reaction", body="a thermolysin-catalysed coupling"
    )
    refutation = Note(
        id="reaction-thermolysin-10",
        type="reaction",
        body="the thermolysin-catalysed coupling did not proceed",
        relations=[Relation(rel="contradicts", to="reaction-thermolysin-9")],
    )
    for note in (established, refutation):
        path = repo / "knowledge" / note.type / f"{note.id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_note(note), encoding="utf-8")
    invalidate_cache()

    monkeypatch.setattr(settings, "note_repo_dir", str(repo))
    monkeypatch.setattr(settings, "knowledge_dir", "knowledge")
    watching = Subscription(id=1, owner="chemist-a", query="thermolysin", last_seen_at=None)
    monkeypatch.setattr("chemclaw.durable.digest.all_subscriptions", lambda: _resolved([watching]))

    item = asyncio.run(collect_digests())[0]

    assert item.note_ids == ["reaction-thermolysin-10", "reaction-thermolysin-9"]
    assert item.disputed == ["reaction-thermolysin-10", "reaction-thermolysin-9"]


def test_an_undisputed_digest_says_nothing_about_disputes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The notice is news, so a corpus that agrees with itself must not carry it.

    A line appended unconditionally would be a warning every reader learns to skip, which is the
    same harm as not warning them.
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

    item = asyncio.run(collect_digests())[0]

    assert item.disputed == []
    # The body is *exactly* the list: a guard against an extra line must check the line count, not
    # the absence of a chosen word.
    assert _digest_body(item.note_ids, item.disputed) == "- reaction-thermolysin-9"


def test_the_body_marks_a_dispute_in_place_and_says_how_many() -> None:
    """The body marks a dispute in place and says how many.

    A dispute is a property of an entry; the count is what a reader acts on, since a silent
    truncation reads as completeness.
    """
    body = _digest_body(["note-a", "note-b", "note-c"], ["note-b"])

    assert "- note-b (disputed)" in body
    assert "- note-a\n" in body and "(disputed)" not in body.split("- note-a")[1].split("\n")[0]
    assert "1 of 3 disagree" in body


def test_every_render_of_one_digest_says_the_same_thing() -> None:
    """Three call sites render this list and two of them disagreeing would be two answers.

    Asserted against the module's source rather than by calling each: the replay shim exists only
    to be replayed, so what is claimed is that no site builds the list itself.
    """
    source = Path(chemclaw.durable.digest.__file__).read_text(encoding="utf-8").split('"""', 2)[2]

    assert 'f"- {note_id}"' not in source, "a second renderer of the digest list has appeared"
    # One definition plus the two sites that render a *body*: the session mailbox carries the
    # two lists as structured fields rather than prose, so it is not a third renderer.
    assert source.count("_digest_body(") == 3


async def test_the_route_carries_the_dispute_flag_the_job_computed() -> None:
    """The route carries the dispute flag the job computed.

    With no delivery channels configured (the shipped default) `GET /digests` is the only path, so
    `disputed` must survive into `api/routes/streams.Digest`. Driven against a row written the way
    the job writes one, since a model assertion is satisfied by the field merely being declared.
    """
    await migrated_db_or_skip()
    owner = "digest-disputed-route"
    await claim_unconsumed(digest_channel(owner))
    await record_session_event(
        digest_channel(owner),
        DIGEST_KIND,
        {
            "query": "biaryl",
            "note_ids": ["playbook-a", "reaction-b"],
            "disputed": ["reaction-b"],
            "headlines": {"playbook-a": "Change the ligand before the temperature"},
        },
    )
    with _digest_client(owner) as client:
        answer = client.get("/digests").json()

    assert answer == [
        {
            "query": "biaryl",
            "note_ids": ["playbook-a", "reaction-b"],
            "disputed": ["reaction-b"],
            "headlines": {"playbook-a": "Change the ligand before the temperature"},
        }
    ], (
        "the route dropped what the job computed; a subscriber reading this surface cannot "
        "tell a contradiction from an ordinary find, which is the one thing in a digest that "
        "changes what they should do next"
    )


def test_a_digest_names_what_it_found_and_not_only_its_id() -> None:
    """A digest names what it found, not only its id.

    Driven through `_match_corpus` against notes on disk, since the job must carry the headline. The
    id stays beside it: it is what `GET /notes/{id}` takes and what the watermark works in.
    """
    corpus = Path(settings.knowledge_path)
    notes = [note for note in load_notes(corpus) if note.headline()]
    assert notes, f"no note under {corpus} has a body, so this test proves nothing about headlines"

    subject = notes[0]
    term = subject.id.split("-")[0]
    items = chemclaw.durable.digest._match_corpus(
        [Subscription(id=1, owner="o", query=term, note_type=None, last_seen_at=None)]
    )
    assert items, f"no subscription match for {term!r}; the fixture cannot show a headline"
    item = items[0]
    named = [note_id for note_id in item.note_ids if item.headlines.get(note_id)]
    assert named, (
        f"the digest carried {len(item.note_ids)} matches and named none of them; a subscriber is "
        "told a list of note ids and has to go and look up their own digest"
    )
    for note_id in named:
        assert "\n" not in item.headlines[note_id]
        assert item.headlines[note_id] != note_id

    body = chemclaw.durable.digest._digest_body(item.note_ids, item.disputed, item.headlines)
    for note_id in named:
        assert item.headlines[note_id] in body
        assert note_id in body, "the id must survive beside the headline; it is the handle"


async def test_a_watch_says_so_when_nothing_will_evaluate_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A watch says so when nothing will evaluate it.

    With `digest_enabled` off no `digest` Schedule exists, so `watch_for` must not promise "you'll
    be told". Both directions, since a tool that always warns would be a different defect.
    """
    await migrated_db_or_skip()
    tokens = set_current_identity("watch-truth", frozenset())
    try:
        monkeypatch.setattr(settings, "digest_enabled", False)
        off = await watch_for("biaryl coupling")
        monkeypatch.setattr(settings, "digest_enabled", True)
        on = await watch_for("biaryl coupling")
    finally:
        reset_current_identity(tokens)

    assert "turned off" in off and "nobody will be told" in off, (
        "a deployment with digests off answered a watch with a promise it cannot keep; "
        f"it said: {off!r}"
    )
    assert "turned off" not in on, (
        f"every watch is told its deployment is broken, including working ones: {on!r}"
    )
    # The row is saved either way — an operator turning digests on must have something to
    # deliver against, and `list_watches` must still show it.
    assert any(w.query == "biaryl coupling" for w in await for_owner("watch-truth"))
