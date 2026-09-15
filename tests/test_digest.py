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
    """The other half, and the reason `>` is not the fix.

    A note whose `valid_from` is today but which reached the tree after this morning's digest must
    still be delivered — dropping it is the failure the ordering elsewhere in that module goes out
    of its way to avoid.
    """
    arrived_later = _note("reaction-2", date(2026, 7, 31))

    assert _is_new(arrived_later, _subscription(_TODAY, ["reaction-1"])) is True


def test_an_undated_note_is_told_once_and_then_not_again() -> None:
    """This asserted "silence is the worse answer" and the code delivered the other failure.

    The branch returned `True` unconditionally, and the id memory cannot help because it is scoped
    to the watermark's date and resets when that rolls over — so an undated note re-qualified on
    **every** run, forever. Measured on the shipped corpus, 32 of 39 notes carry no `valid_from`,
    so a subscriber's hourly digest was mostly the same notes over and over, which is the exact
    promise `agent/subscriptions.py` makes (DARK-7) being broken by the branch written to keep it.

    What `None` means settles it rather than a preference between two failures: `Note.is_current`
    reads it as *open-ended*, true for as long as anyone has known, so such a note did not become
    knowledge after a subscriber was last told. A subscriber who has never been told anything still
    hears it once — that is the first arm below, and it is the whole of "silence is the worse
    answer" that survives.
    """
    undated = _note("playbook-1", None)

    assert _is_new(undated, _subscription(None)) is True
    assert _is_new(undated, _subscription(_TODAY)) is False
    assert _is_new(_note("playbook-1", date(2026, 7, 31)), _subscription(None)) is True


def test_a_distilled_playbook_carries_the_day_it_was_minted() -> None:
    """The other half of the same fix, and the reason the half above can be strict.

    A playbook is the one note type nobody writes on a day — a miner concludes it — so it shipped
    with no `valid_from` and therefore, under the rule above, would reach only a subscriber who had
    never been told anything. `minted_on` is the honest statement that it became knowledge when the
    corpus first supported it, which is the day the miner ran.
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
            assert client.get("/digests").json() == [
                {"query": "q", "note_ids": ["n-1"], "disputed": [], "headlines": {}}
            ]

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


# --- the corpus disagreeing with itself ----------------------------------------------------------


def test_a_new_note_that_contradicts_an_existing_one_is_marked_in_the_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`kg/conflicts.py` has always known this and only a reader who asked was ever told.

    `retrieval.retrievers._conflict_index` flags a disputed note at *retrieval* time, so a chemist
    who happens to query is warned and a chemist watching the subject is not — while the corpus
    starting to disagree with itself on their standing query is the one thing in a digest that
    changes what they should do next.

    Driven over a real corpus with a real `contradicts` relation, not a stubbed index: the claim is
    that the sweep and the digest agree about the same notes.
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
    # The body is *exactly* the list. Asserting the absence of the word "disputed" was the first
    # form of this and it did not fire: the appended notice says "disagree with something already
    # in the graph", so driving the mutation that appends unconditionally left it green. A guard
    # against an extra line has to be about the line count, not about a word somebody chose.
    assert _digest_body(item.note_ids, item.disputed) == "- reaction-thermolysin-9"


def test_the_body_marks_a_dispute_in_place_and_says_how_many() -> None:
    """A reader's question is "what is new"; a dispute is a property of an entry in that list.

    The count is the half a reader acts on — `kg/conflicts.py`'s own rule is that a silent
    truncation reads as completeness, and "two of these nine" is what makes the marks countable
    without re-reading the list.
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


def test_the_route_carries_the_dispute_flag_the_job_computed() -> None:
    """`disputed` reached the mailbox and stopped at the API model, on the only default-config path.

    `collect_digests` has computed which matches the corpus now disagrees with since
    `D-2026-08-27`, and writes them into the payload. Both outbound delivery channels rendered
    them. `api/routes/streams.Digest` had no such field and `_digest` never read the key — and
    `CHEMCLAW_DELIVERY_CHANNELS` is empty in every shipped deployment, so the flag existed, was
    computed on every run, and reached nobody.

    That is the asymmetry `DigestItem`'s own docstring names as the reason the field exists — "a
    chemist who happens to ask is told, and a chemist watching the subject is not" — reproduced one
    layer down. Driven here against a row written the way the job writes one, rather than against
    the model, because a model assertion would have been satisfied by the field being *declared*.
    """

    async def _run() -> None:
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

    asyncio.run(_run())


def test_a_digest_names_what_it_found_and_not_only_its_id() -> None:
    """A digest built off a real corpus carries a sentence per match, not just a handle.

    Driven through `_match_corpus` against notes on disk rather than by calling `Note.headline`,
    because the defect was never in the deriving — there was nothing to derive from, and every
    surface printed `playbook-<hash>`. What has to hold is that the *job* carries it.

    The id stays beside the headline everywhere it is rendered: it is what a reader passes to
    `GET /notes/{id}` and what the watermark works in.
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


def test_a_watch_says_so_when_nothing_will_evaluate_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """Off, `watch_for` used to answer "you'll be told" and no schedule existed to tell anyone.

    The two halves are separate failures and this pins the second. `digest_enabled` now defaults
    **on**, so the shipped deployment evaluates a watch — but a deployment may still turn it off,
    and `durable/schedules.py` then creates no `digest` Schedule at all. Nothing else in the tree
    reads that setting, so with it off a chemist's watch was written, confirmed in the first
    person, and never looked at again.

    Asserted in both directions, because "says so when off" is satisfied by a tool that always
    warns — which would be a different defect, telling every chemist on every deployment that their
    watch does not work.
    """

    async def _run() -> None:
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

    asyncio.run(_run())
