"""Offboarding: the conversation is erasable, the record of what was done is not (D-2026-08-08).

The two-tier rule in `chemclaw.agent.leaver` is a data-protection decision, so these tests assert
the *line* rather than the plumbing: that a departed person's sessions, preferences and watches go,
that the rows attributing scientific work to them stay and are counted rather than quietly ignored,
that a dry run writes nothing while still reporting real numbers, and that one person's erasure
cannot take another's data with it.

The last of those has two halves now that a writer which cannot authenticate its caller records the
claimed id as `unverified:<id>`: both spellings are the same person and must be *seen*, and a
different person whose id merely contains theirs must not be — the two tests that pin the closed set
of exact forms in `_actor_forms` against the one-line substring match that would fail the second.

Postgres-backed and skipped where no database is reachable, like every other store test here.
"""

import asyncio
import contextlib
import io
import re
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from chemclaw.agent.leaver import (
    _BEYOND_REACH,
    _ERASE,
    _RETAINED,
    _RETAINED_IN_PAYLOAD,
    ErasureError,
    _residue_columns,
    _residue_for,
    erase_actor,
    finish_erasure,
    finish_leaves,
    retention_reasons,
)
from chemclaw.agent.session_store import (
    SessionOwnerStore,
    SessionTurnClaims,
    _session_delete_statements,
)
from chemclaw.cli.erase_actor import main as erase_actor_main
from chemclaw.core.config import settings
from chemclaw.core.db import connect
from chemclaw.durable.digest import digest_channel
from tests.pg import create_checkpoint_tables, migrated_db_or_skip

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _runbook_section(text: str, heading: str) -> str:
    """One `###` section of the runbook, heading to the next heading of any level."""
    assert heading in text, f"{heading!r} is gone from the runbook"
    body = text.split(heading, 1)[1]
    return re.split(r"\n#{2,3} ", body, maxsplit=1)[0]


# The column *spellings* this system uses for a person that are not covered by the `_by` suffix
# below. Not every TEXT column — a derived set needs a vocabulary, and this is the irregular half
# of it.
#
# **The vocabulary was itself a hand-written list of every spelling, which is the defect this test
# exists to prevent, one level up.** It happened twice. `audit_anchors.reseal_by` names "who
# accepted the gap and why" (`infra/sql/032_audit_anchors.sql`) and was missing, so a live
# person-column sat in neither tier with this test green; `experiment_protocol_revisions.author`
# (073) landed in `_RETAINED` because its author happened to think of it, not because anything
# here would have failed if they had not.
#
# It happened a third time and that is why this is no longer the whole predicate: measured on
# 2026-09-06, `effects.approved_by` and `pending_requests.answered_by` were *live person-columns
# the completeness check could not see*, because neither spelling was in the list — and deleting
# `approved_by` from every tier in `leaver.py` left `tests/test_leaver.py` at 23 passed. So the
# regular half is matched by suffix (`_LIKE_A_PERSON` below) and only the spellings a suffix cannot
# reach are enumerated here. A future `signed_off_by` is then in the scan on the day the migration
# adds it, rather than on the day somebody remembers to add it here.
_ACTOR_COLUMN_NAMES = frozenset({"actor", "author", "owner", "holder"})

# The regular half: `<verb>_by` is what this schema calls the person who did something.
# `\_` because `_` is a single-character wildcard to `LIKE` and this has to match the literal.
_LIKE_A_PERSON = "%\\_by"

_ANNA = "oid-anna"
_BEN = "oid-ben"
# One person per marker test, kept off `_ANNA`/`_BEN` and off each other so the counts below are
# exact rather than "at least": no other test writes a `bo_*` row, and each of these writes only its
# own — a shared id would make one test's rows show up in the other's report.
_CARLA = "oid-carla"
_ERIK = "oid-erik"
# The trap. This id *contains* `_ERIK`, so any substring or prefix match dressed up as "see the
# unverified form too" erases this person's conversation and counts their records while offboarding
# someone else.
_ERIK_LOOKALIKE = f"{_ERIK}-2"


async def _seed(actor: str, session_id: str) -> None:
    """Give `actor` one session with a message and an event, a preference, and a watch."""
    async with await connect(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO session_owners (session_id, owner) VALUES (%s, %s) "
                "ON CONFLICT (session_id) DO UPDATE SET owner = EXCLUDED.owner",
                (session_id, actor),
            )
            await cur.execute(
                "INSERT INTO session_messages (session_id, message) VALUES (%s, %s)",
                (session_id, '{"role": "user", "content": "hello"}'),
            )
            await cur.execute(
                "INSERT INTO session_events (session_id, kind) VALUES (%s, %s)",
                (session_id, "turn_started"),
            )
            await cur.execute(
                "INSERT INTO user_preferences (owner, key, value) VALUES (%s, %s, %s) "
                "ON CONFLICT (owner, key) DO UPDATE SET value = EXCLUDED.value",
                (actor, "preferred_solvent", "2-MeTHF"),
            )
            # A real subscription row. Without one the watch deletion ran against zero rows in
            # every test while the docstrings claimed it was covered — a statement executed with
            # nothing to delete proves only that it parses.
            await cur.execute(
                "INSERT INTO subscriptions (owner, query) VALUES (%s, %s) "
                "ON CONFLICT (owner, query, coalesce(note_type, '')) DO NOTHING",
                (actor, "new suzuki reactions"),
            )
        await conn.commit()


async def _seed_campaign(campaign_id: str, actor: str) -> None:
    """Give `actor` one BO campaign and one suggestion against it, written exactly as stored.

    `actor` goes in verbatim — the caller passes either the bare id (what the durable path writes,
    from a validated Temporal memo) or `unverified:<id>` (what the synchronous MCP path writes,
    because `connectors/bo` declares `auth: mode: none` and its actor is an unauthenticated header).
    Both are the same chemist, which is the whole point of these tests.
    """
    async with await connect(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO bo_campaigns (campaign_id, objective, direction, opened_by) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (campaign_id) DO NOTHING",
                (campaign_id, "yield", "maximize", actor),
            )
            await cur.execute(
                "INSERT INTO bo_suggestions (campaign_id, actor) VALUES (%s, %s)",
                (campaign_id, actor),
            )
        await conn.commit()


async def _count(table: str, column: str, value: str) -> int:
    """How many rows of `table` carry `value` in `column`."""
    async with await connect(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(f"SELECT count(*) FROM {table} WHERE {column} = %s", (value,))
            row = await cur.fetchone()
    return int(row[0]) if row else 0


def test_a_dry_run_reports_real_counts_and_writes_nothing() -> None:
    """The number an operator signs off on is the number that will be deleted.

    The dry run really executes the deletes and rolls back, rather than running a second counting
    query that hopes to predict them — a preview computed a different way from the thing it
    previews is a preview of something else.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _seed(_ANNA, "sess-dry")
        report = await erase_actor(_ANNA)
        assert report.applied is False
        assert report.erased["session_messages"] >= 1
        assert report.erased["user_preferences"] >= 1
        assert report.erased_total >= 3
        # Nothing was committed.
        assert await _count("session_owners", "owner", _ANNA) >= 1
        assert await _count("user_preferences", "owner", _ANNA) >= 1

    asyncio.run(_run())


def test_applying_removes_the_conversation() -> None:
    """Sessions, their messages and events, preferences and watches all go."""

    async def _run() -> None:
        await migrated_db_or_skip()
        await _seed(_ANNA, "sess-apply")
        report = await erase_actor(_ANNA, apply=True)
        assert report.applied is True
        assert await _count("session_owners", "owner", _ANNA) == 0
        assert await _count("user_preferences", "owner", _ANNA) == 0
        assert await _count("session_messages", "session_id", "sess-apply") == 0
        assert await _count("session_events", "session_id", "sess-apply") == 0

    asyncio.run(_run())


def test_one_persons_erasure_leaves_another_persons_data_alone() -> None:
    """The failure that would be discovered far too late: an over-broad WHERE clause."""

    async def _run() -> None:
        await migrated_db_or_skip()
        await _seed(_ANNA, "sess-anna")
        await _seed(_BEN, "sess-ben")
        await erase_actor(_ANNA, apply=True)
        assert await _count("session_owners", "owner", _BEN) == 1
        assert await _count("user_preferences", "owner", _BEN) == 1
        assert await _count("session_messages", "session_id", "sess-ben") == 1

    asyncio.run(_run())


def test_the_audit_trail_survives_an_erasure_and_is_reported() -> None:
    """The retained half of the rule, and the half a caller must not be able to miss.

    An attributable record that can be deleted on request is not an attributable record, and for a
    tool call that changed nothing durable the trail is the only place it is recorded at all. So the
    row stays — and the report *names it and counts it*, because a partial erasure that looks
    complete is worse than one that refuses out loud.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _seed(_ANNA, "sess-audit")
        async with await connect(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO audit_events "
                    "(correlation_id, actor, tool, arguments, outcome, detail, latency_ms) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    ("conv-leaver", _ANNA, "predict_pka", "{}", "ok", "", 1.0),
                )
            await conn.commit()

        report = await erase_actor(_ANNA, apply=True)
        assert report.retained["audit_events"] >= 1
        assert report.retained_total >= 1
        assert await _count("audit_events", "actor", _ANNA) >= 1

    asyncio.run(_run())


def test_a_blank_actor_is_refused() -> None:
    """A blank id matches every un-attributed row of a dev deployment, not one person's data.

    `unverified:` on its own is the same refusal wearing a disguise: it is a non-empty string, but
    the id behind the marker is blank, and matching it would sweep every row any writer ever marked
    — everyone's, from a single stray paste.
    """

    async def _run() -> None:
        for blank in ("   ", "unverified:", "unverified:  "):
            try:
                await erase_actor(blank)
            except ValueError as exc:
                assert "non-empty" in str(exc)
            else:  # pragma: no cover - the refusal is the behavior under test
                raise AssertionError(f"{blank!r} must be refused before any statement runs")

    asyncio.run(_run())


async def _seed_shared_blob(hash_: str, sessions: tuple[str, ...]) -> None:
    """One stored tool result that several sessions link — the shape dedup produces every day."""
    async with await connect(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO tool_result_blobs (content_hash, byte_size, data) "
                "VALUES (%s, %s, %s) ON CONFLICT (content_hash) DO NOTHING",
                (hash_, 5, b"hello"),
            )
            for session_id in sessions:
                await cur.execute(
                    "INSERT INTO tool_result_links (session_id, content_hash, tool) "
                    "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (session_id, hash_, "gather_evidence"),
                )
        await conn.commit()


def test_erasing_one_person_leaves_a_shared_tool_result_readable_for_the_other() -> None:
    """A blob two sessions link is not one person's to take away.

    **Measured before the fix**, against a live database: two sessions link one blob, erasing the
    first owner deleted the blob, and `ON DELETE CASCADE` took the *second* session's link row with
    it — so a chemist who erased nobody found their own transcript pointing at a result the surface
    could no longer fetch. `session_store._SESSION_DELETE` has had the "unless another session links
    it" arm since the single-session delete was written; this is the same rule reaching the same
    table through the other door.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        shared = "sha-shared-blob"
        await _seed(_ANNA, "s-anna-shared")
        await _seed(_BEN, "s-ben-shared")
        await _seed_shared_blob(shared, ("s-anna-shared", "s-ben-shared"))

        await erase_actor(_ANNA, apply=True)

        async with await connect(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT count(*) FROM tool_result_blobs WHERE content_hash = %s", (shared,)
                )
                blobs = (await cur.fetchone() or (0,))[0]
                await cur.execute(
                    "SELECT count(*) FROM tool_result_links WHERE content_hash = %s "
                    "AND session_id = %s",
                    (shared, "s-ben-shared"),
                )
                bens_link = (await cur.fetchone() or (0,))[0]

        assert blobs == 1, "erasing one reader deleted a tool result another session still links"
        assert bens_link == 1, (
            "the surviving session's link row was cascaded away with the blob — that session's "
            "transcript now points at a result nothing can fetch"
        )

    asyncio.run(_run())


def test_an_unread_digest_does_not_survive_its_owners_erasure() -> None:
    """The mailbox is a session id no ownership row backs, so the reachability join never saw it.

    A digest lands in `digest-<oid>` (`durable/digest.digest_channel`), deliberately without a
    `session_owners` row. Every other `session_events` row is reached through that table, so before
    this an erasure removed the person's standing queries and left the digests those queries had
    already produced — reporting `session_events: 0`, which reads as complete. The row here is
    unconsumed on purpose: that is the population nothing else in the system ever drains.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _seed(_CARLA, "s-carla-digest")
        async with await connect(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO session_events (session_id, kind) VALUES (%s, %s)",
                    (digest_channel(_CARLA), "digest"),
                )
            await conn.commit()

        report = await erase_actor(_CARLA, apply=True)

        async with await connect(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT count(*) FROM session_events WHERE session_id = %s",
                    (digest_channel(_CARLA),),
                )
                left = (await cur.fetchone() or (0,))[0]

        assert left == 0, "the departed person's unread digests survived their erasure"
        assert report.erased["session_events"] >= 2, (
            "the report did not count the mailbox row it deleted; a count that omits a table's "
            "rows is the same false completeness by another route"
        )

    asyncio.run(_run())


def test_a_publication_naming_a_person_is_reported_rather_than_silently_kept() -> None:
    """The one actor this schema holds inside a payload is counted, not omitted.

    `result_publications.document` carries `publications[].actor`, `.session_id` and a free-text
    `.rationale`. The column is called `document`, so the schema-derived check above could never see
    it and the two-tier report did not mention the table at all — an erasure that looked complete
    over a row holding the person's id and their own words. It is retained rather than erased, by
    the same line as every other record: a publication says who asked for a result and why.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        document = {
            "publications": [
                {"actor": _ERIK, "session_id": "s-erik-pub", "rationale": "erik asked for this"}
            ]
        }
        async with await connect(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO result_publications (sink, calc_ref, document) "
                    "VALUES (%s, %s, %s)",
                    ("test-sink", "calc-erik-1", Jsonb(document)),
                )
            await conn.commit()

        # Cleaned up in a `finally`, because `result_publications` is not in the erase tier and so
        # nothing in this run removes it: without this the row outlived the test and the next
        # assertion about `_ERIK`'s retained count in this file saw it. A fixture that survives its
        # own test is a fixture the next test is measuring.
        try:
            report = await erase_actor(_ERIK)

            assert report.retained.get("result_publications") == 1, (
                "a publication naming this person was neither erased nor reported as retained"
            )
            assert dict(retention_reasons())["result_publications"], (
                "the retained tier must say why a row stays; this one had no reason to print"
            )
            # The bystander check every count in this file carries: an id that merely *contains*
            # another must not be counted as it.
            lookalike = await erase_actor(_ERIK_LOOKALIKE)
            assert lookalike.retained.get("result_publications") == 0
        finally:
            async with await connect(settings.postgres_dsn) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM result_publications WHERE sink = %s", ("test-sink",)
                    )
                await conn.commit()

    asyncio.run(_run())


@pytest.mark.parametrize(
    "publications",
    [None, {"actor": _ERIK}, "not-a-list", 3, True],
    ids=["json-null", "object", "string", "number", "boolean"],
)
def test_a_publication_payload_it_cannot_read_counts_zero_rather_than_ending_the_erasure(
    publications: object,
) -> None:
    """One unreadable row must not make erasure impossible for the whole deployment.

    `jsonb_array_elements` is a partial function and the retained count runs in the same
    transaction as every DELETE, so before the `jsonb_typeof` guard a single
    `{"publications": null}` row — in a table this command does not even erase — turned every
    actor's erasure into `ErasureError: cannot extract elements from a scalar`, permanently, with
    no operator workaround short of editing that row by hand.

    Parametrized over the shapes Postgres refuses, because "it does not raise on the one I thought
    of" is what the first version of this predicate could already claim. `document` is
    `JSONB NOT NULL` with no CHECK and the table carries a `schema_version` precisely because the
    record shape is expected to change, so these are reachable rather than hypothetical.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        async with await connect(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM result_publications WHERE sink = %s", ("erasure-test",)
                )
                await cur.execute(
                    "INSERT INTO result_publications (sink, calc_ref, document) "
                    "VALUES (%s, %s, %s)",
                    ("erasure-test", "calc-shape", Jsonb({"publications": publications})),
                )
            await conn.commit()
        try:
            report = await erase_actor(_ERIK)
            assert report.retained.get("result_publications") == 0, (
                "a payload this predicate cannot read must count zero, not match"
            )
        finally:
            async with await connect(settings.postgres_dsn) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM result_publications WHERE sink = %s", ("erasure-test",)
                    )
                await conn.commit()

    asyncio.run(_run())


def test_a_session_the_leaver_deleted_themselves_does_not_spare_their_own_blob() -> None:
    """An orphan link is not another person, and treating it as one left the leaver's data behind.

    `delete_session` deliberately leaves the link row when its blob is shared. The first version of
    the erasure arm asked only whether a link *outside* the leaver's sessions existed, so that
    orphan — the leaver's own — spared the blob. Measured: a chemist tidies up one of their own
    sessions, later asks to be erased, and their untruncated tool output survives while the report
    prints `tool_result_blobs: 0`, which reads as "there were none".

    The pairing with `test_erasing_one_person_leaves_a_shared_tool_result_readable_for_the_other` is
    the whole point: that one proves another *person* still spares the blob, this one proves an
    orphan does not. A fix that satisfies only one of the two is the defect in the other direction.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        shared = "sha-self-orphan"
        await _seed(_CARLA, "s-carla-keep")
        await _seed(_CARLA, "s-carla-drop")
        await _seed_shared_blob(shared, ("s-carla-keep", "s-carla-drop"))

        await SessionOwnerStore().delete_session("s-carla-drop")
        report = await erase_actor(_CARLA, apply=True)

        async with await connect(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT count(*) FROM tool_result_blobs WHERE content_hash = %s", (shared,)
                )
                blobs = (await cur.fetchone() or (0,))[0]
                await cur.execute(
                    "SELECT count(*) FROM tool_result_links WHERE content_hash = %s", (shared,)
                )
                links = (await cur.fetchone() or (0,))[0]

        assert blobs == 0, "the leaver's own orphaned link spared their own stored tool result"
        assert links == 0, "the link rows should have cascaded away with the blob"
        assert report.erased["tool_result_blobs"] == 1, (
            "the report said zero over a blob it should have deleted, which reads as 'there were "
            "none'"
        )

    asyncio.run(_run())


def test_every_actor_bearing_column_in_the_schema_is_accounted_for() -> None:
    """No column may name a person without this module having a position on it.

    **The test the hand-written list needed.** The first version of `_RETAINED` enumerated six
    columns from memory and missed two — `note_proposals.decided_by` and `bo_campaigns.opened_by` —
    so a departing PR-gate reviewer was told zero `note_proposals` rows mentioned them while the
    column recording every sign-off they gave still did. A list of columns checked against nothing
    is a list that drifts the moment a migration adds one.

    So the set is derived from the live schema instead: every column whose name is one this system
    uses for a person must appear in the erase tier or the retain tier. A new one is then a failing
    test with the column named, and the author has to decide which tier it belongs to — which is the
    decision, and it should never be made by omission.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        async with await connect(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND (column_name = ANY(%s) OR column_name LIKE %s) "
                    "ORDER BY table_name, column_name",
                    (sorted(_ACTOR_COLUMN_NAMES), _LIKE_A_PERSON),
                )
                found = {(t, c) for t, c in await cur.fetchall()}

        retained = {(table, col) for table, cols, _ in _RETAINED for col in cols}
        # The scan has to be able to *see* every column this module already has a position on,
        # or the completeness check below is a completeness check over whatever the predicate
        # happens to match. A retained column the scan misses is a spelling the vocabulary does
        # not know, and the next column with that spelling would be accounted for by nobody.
        invisible = sorted(retained - found)
        assert not invisible, (
            f"the scan does not match {invisible}, which `_RETAINED` already names as person "
            "columns — so a *new* column spelled that way would sit in no tier with this test "
            "green. Add the spelling to `_ACTOR_COLUMN_NAMES`."
        )
        # The erase tier is matched by table: its statements reach rows through `session_owners`
        # rather than always naming the actor column directly, so the column-level assertion that
        # fits the retain tier would be wrong here.
        erased_tables = {table for table, _ in _ERASE}
        # Two further answers, both of which have to be *given* rather than assumed: a table whose
        # person sits inside a payload (`_RETAINED_IN_PAYLOAD`, where the column is `document` and
        # the vocabulary above can never match it), and one this command can neither clear nor
        # count (`_BEYOND_REACH`). Both are accounted-for positions; neither is silence.
        payload_tables = {table for table, *_ in _RETAINED_IN_PAYLOAD}
        # The declared column has to exist, or the predicate over it matches nothing in silence —
        # which is exactly how this tier's table came to be missing in the first place.
        async with await connect(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                for table, column, _predicate, _why in _RETAINED_IN_PAYLOAD:
                    await cur.execute(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_schema = current_schema() AND table_name = %s "
                        "AND column_name = %s",
                        (table, column),
                    )
                    assert (await cur.fetchone() or (0,))[0] == 1, (
                        f"{table}.{column} is declared as where a person sits in a payload and "
                        "does not exist; the predicate over it would match nothing, silently"
                    )
        accounted_tables = erased_tables | payload_tables | set(_BEYOND_REACH)
        unaccounted = sorted(
            (t, c) for t, c in found if (t, c) not in retained and t not in accounted_tables
        )
        assert not unaccounted, (
            f"these columns name a person and belong to no tier: {unaccounted}. "
            "Add each to `_ERASE` (the conversation), `_RETAINED` (the record) or "
            "`_BEYOND_REACH` (out of this command's reach, with the reason) in "
            "chemclaw.agent.leaver — deciding by omission is what this test exists to prevent"
        )

    asyncio.run(_run())


def test_a_proposal_someone_wrote_and_reviewed_is_counted_once() -> None:
    """Two columns of one row are one retained record, not two.

    `note_proposals` names a person twice, and the count an operator reads is a count of *records*
    they still appear in. Summing per column would inflate exactly the table whose retention is
    hardest to explain.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        async with await connect(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO note_proposals "
                    "(note_id, note_type, content_hash, content, branch, actor, decided_by) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    ("note-lv-1", "reaction", "h1", "body", "b1", _ANNA, _ANNA),
                )
            await conn.commit()

        report = await erase_actor(_ANNA)
        assert report.retained["note_proposals"] == 1

    asyncio.run(_run())


def test_a_reviewers_signoff_is_retained_even_when_they_proposed_nothing() -> None:
    """The case the hand-written list got wrong: `decided_by` with an empty `actor`."""

    async def _run() -> None:
        await migrated_db_or_skip()
        async with await connect(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO note_proposals "
                    "(note_id, note_type, content_hash, content, branch, actor, decided_by) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    ("note-lv-2", "reaction", "h2", "body", "b2", "someone-else", _BEN),
                )
            await conn.commit()

        report = await erase_actor(_BEN)
        assert report.retained["note_proposals"] >= 1, (
            "a reviewer's sign-off must be reported as retained, not silently missed"
        )

    asyncio.run(_run())


def test_a_claim_marked_unverified_is_the_same_person_and_is_counted() -> None:
    """The regression: one chemist, two spellings, and a report that saw only one of them.

    `connectors/bo` declares `auth: mode: none`, so its synchronous MCP path cannot authenticate the
    caller and records the claimed actor as `unverified:<id>` — while the durable path, reading a
    validated principal off the run's memo, writes the bare id into the *same* two columns. Erasure
    matched actor columns byte-exactly, so an offboarding report for `oid-carla` counted the durable
    rows and silently missed the inline ones: an under-count of rows that still hold that person's
    identifier, which is precisely the number this command exists to state correctly.

    Either spelling may be named, because an operator pastes what they read out of the column.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _seed_campaign("camp-carla-durable", _CARLA)
        await _seed_campaign("camp-carla-inline", f"unverified:{_CARLA}")

        report = await erase_actor(_CARLA)
        assert report.retained["bo_campaigns"] == 2, (
            "a campaign opened under an unverified claim of this person's id still names them"
        )
        assert report.retained["bo_suggestions"] == 2

        marked = await erase_actor(f"unverified:{_CARLA}")
        assert marked.retained == report.retained, "the two spellings name one person"

    asyncio.run(_run())


def test_erasing_one_person_spares_another_whose_id_contains_theirs() -> None:
    """The dangerous way to have fixed the above, caught before it can be shipped.

    `LIKE '%' || actor || '%'` sees the `unverified:` form in one line — and also sees `oid-erik-2`
    when erasing `oid-erik`, deleting a working chemist's conversation and attributing their
    campaigns to the leaver. So the match stays exact equality against the closed set of spellings
    `_actor_forms` enumerates, and this test is what says so: the bystander keeps every row, in both
    of *their* spellings, and appears in nobody else's report.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _seed(_ERIK, "sess-erik")
        await _seed_campaign("camp-erik-inline", f"unverified:{_ERIK}")
        await _seed(_ERIK_LOOKALIKE, "sess-erik-lookalike")
        await _seed_campaign("camp-lookalike-durable", _ERIK_LOOKALIKE)
        await _seed_campaign("camp-lookalike-inline", f"unverified:{_ERIK_LOOKALIKE}")

        report = await erase_actor(_ERIK, apply=True)
        assert report.retained["bo_campaigns"] == 1, "only the leaver's own campaign is theirs"
        assert report.retained["bo_suggestions"] == 1

        assert await _count("session_owners", "owner", _ERIK_LOOKALIKE) == 1
        assert await _count("user_preferences", "owner", _ERIK_LOOKALIKE) == 1
        assert await _count("subscriptions", "owner", _ERIK_LOOKALIKE) == 1
        assert await _count("session_messages", "session_id", "sess-erik-lookalike") == 1
        assert await _count("bo_campaigns", "opened_by", _ERIK_LOOKALIKE) == 1
        assert await _count("bo_campaigns", "opened_by", f"unverified:{_ERIK_LOOKALIKE}") == 1

    asyncio.run(_run())


def test_a_departed_persons_turn_lease_is_released() -> None:
    """A lease names its holder, and offboarding must not leave one held by a leaver."""

    async def _run() -> None:
        await migrated_db_or_skip()
        async with await connect(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO session_turns (session_id, holder, expires_at) "
                    "VALUES (%s, %s, now() + interval '1 hour') "
                    "ON CONFLICT (session_id) DO UPDATE SET holder = EXCLUDED.holder",
                    ("sess-not-theirs", _ANNA),
                )
            await conn.commit()

        await erase_actor(_ANNA, apply=True)
        assert await _count("session_turns", "holder", _ANNA) == 0

    asyncio.run(_run())


def test_every_retained_table_states_why() -> None:
    """A retained row an operator cannot get an explanation for is one they will delete by hand."""
    reasons = dict(retention_reasons())
    assert reasons, "no retention reasons are declared"
    assert all(reason.strip() for reason in reasons.values())
    assert "audit_events" in reasons


def test_the_erase_statements_are_valid_sql() -> None:
    """Parse every statement against the real schema, so a typo'd column fails here.

    Runs each delete inside a rolled-back transaction on an actor nobody has: the statements must
    be *executable*, which a string never proves on its own.

    **The library-created tables are reported but not necessarily parsed here.** The checkpointer's
    three come from `AsyncPostgresSaver.setup()` and the memory store's two from
    `AsyncPostgresStore.setup()`, rather than from a migration, so `erase_actor` skips — and reports
    zero for — any that this schema does not have. Their statements are executed against real tables
    in `tests/test_message_migration.py`, which is where a typo in one would fail. The keys stay
    asserted here because a table silently dropping out of the report is the failure this test is
    for.

    **`tool_result_blobs` was the shape this test could not see.** It is reached only through
    `tool_result_links.session_id`, and the completeness check below derives its expectations from
    columns whose *name* identifies a person — so a table holding the full untruncated text of
    everything a chemist's tools returned was invisible to the derivation, and the erasure report
    said nothing about it. A partial erasure that looks complete is the one outcome this module says
    it must never produce. The link rows are not listed because they are not deleted here: the
    cascade removes them, which is what lets the grant keep withholding DELETE on that table.

    **`store` and `store_vectors` were added by the arrival they exist to catch.** The scratchpad
    gave a turn durable memories under an actor-keyed namespace, `agent/leaver.py` grew the two
    statements that erase them, and this assertion went red on the *addition* — which is the same
    alarm working in the useful direction. Growing the set is the deliberate act the test forces;
    the failure it is really guarding against is the silent shrink, because a table that stops being
    reported is a departing person's data nobody knows is still there.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        report = await erase_actor("oid-nobody-at-all")
        assert report.erased_total == 0
        assert set(report.erased) == {
            "session_messages",
            "tool_result_blobs",
            "checkpoints",
            "checkpoint_blobs",
            "checkpoint_writes",
            "store",
            "store_vectors",
            "session_events",
            "session_turns",
            "subscriptions",
            "user_preferences",
            "session_owners",
        }

    asyncio.run(_run())


def test_the_cli_reports_a_statement_level_database_error_instead_of_raising() -> None:
    """A `psycopg.Error` that is not a connection failure must still print, not traceback.

    **Two earlier versions of this test were worthless, in different ways.** The first asserted
    `issubclass(psycopg.OperationalError, Exception)` — true of every exception, and it passed with
    the CLI's error handling deleted. The second drove the CLI at an unreachable port, which
    `chemclaw.core.db` already translates into `ConnectionError`, so it passed against the narrow
    `except (ValueError, ConnectionError)` it was written to condemn.

    The gap is a *statement-level* error: `psycopg.Error` is neither a `ValueError` nor a
    `ConnectionError`, so `InsufficientPrivilege` — what a deployment gets when `make db-grants`
    has not been re-applied for this command's own `DELETE ON session_owners` — escaped as a raw
    traceback. That is the single likeliest failure the first operator to run this will hit.
    Reproduced here by pointing the search path at a schema with no tables, which raises
    `UndefinedTable` from the same family, against a database that is reachable and healthy.
    """

    async def _run() -> None:
        await migrated_db_or_skip()

    asyncio.run(_run())

    original = settings.postgres_dsn
    separator = "&" if "?" in original else "?"
    settings.postgres_dsn = f"{original}{separator}options=-c%20search_path%3Dno_such_schema"
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr):
            code = erase_actor_main(["oid-anyone"])
    finally:
        settings.postgres_dsn = original
    assert code == 1, "a statement-level database error must be reported, not raised"
    assert "erasure failed" in stderr.getvalue()


# --- A sweep and a live turn are two writers, and only one of them was guarded. ---------------


_FRAN = "oid-fran"


async def _claim(session_id: str, holder: str) -> bool:
    """Take the session's durable turn claim the way a running turn does."""
    return await SessionTurnClaims().claim(session_id, holder, 60.0)


def test_an_erasure_refuses_while_a_turn_holds_one_of_the_persons_sessions() -> None:
    """The guard both single-session paths had and the fleet-wide sweep did not.

    Measured before this guard existed, against a real graph on a real checkpointer: the sweep
    reported 15 checkpoints, 2 messages and the ownership row erased; the still-running turn then
    rewrote its whole in-memory message list back onto the thread; and the conversation from
    *before* the erasure was in the database again in full — under a session id whose
    `session_owners` row was gone, which puts it beyond `delete_session`, beyond the retention
    sweep and beyond a second `erase_actor`, all three of which reach a session through that row.
    A second erasure erased nothing and printed zeros, which reads as "there were none".

    So the run is refused, and the refusal names the session an operator has to deal with.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _seed(_FRAN, "sess-fran-live")
        assert await _claim("sess-fran-live", "some-other-worker")
        try:
            with pytest.raises(ErasureError) as caught:
                await erase_actor(_FRAN, apply=True)
            assert "sess-fran-live" in str(caught.value)
            # And it refused *before* deleting anything, rather than part-way through.
            assert await _count("session_owners", "owner", _FRAN) == 1
            assert await _count("session_messages", "session_id", "sess-fran-live") == 1
        finally:
            await SessionTurnClaims().release("sess-fran-live", "some-other-worker")

    asyncio.run(_run())


def test_a_refused_erasure_gives_back_every_claim_it_took() -> None:
    """A refusal must leave the fleet exactly as it found it, or it locks out the sessions it read.

    The sweep claims every one of the person's sessions before it touches a table, so a refusal on
    the last one has already taken the others. Releasing them is what keeps a refused erasure from
    costing a chemist a whole lease of 409s on conversations that were never busy.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _seed(_FRAN, "sess-fran-a")
        await _seed(_FRAN, "sess-fran-b")
        assert await _claim("sess-fran-b", "some-other-worker")
        try:
            with pytest.raises(ErasureError):
                await erase_actor(_FRAN, apply=True)
            # The quiet session is claimable again by somebody else, so the erasure kept nothing.
            assert await _claim("sess-fran-a", "a-later-turn")
            await SessionTurnClaims().release("sess-fran-a", "a-later-turn")
        finally:
            await SessionTurnClaims().release("sess-fran-b", "some-other-worker")

    asyncio.run(_run())


def test_a_quiet_session_is_erased_and_the_sweep_holds_no_claim_afterwards() -> None:
    """The ordinary path still erases, and the claim it took to do so does not outlive it."""

    async def _run() -> None:
        await migrated_db_or_skip()
        await _seed(_FRAN, "sess-fran-quiet")
        report = await erase_actor(_FRAN, apply=True)
        assert report.erased["session_messages"] >= 1
        assert await _count("session_owners", "owner", _FRAN) == 0
        assert await _count("session_turns", "session_id", "sess-fran-quiet") == 0
        assert report.residue == {}, "a clean run leaves nothing behind and must say so"

    asyncio.run(_run())


def test_a_row_that_comes_back_under_an_erased_session_is_counted_not_missed() -> None:
    """The half the claims cannot cover: a write this sweep could not have refused.

    A lease that lapsed under a sweep wider than one lease, a session created between the
    enumeration and the commit, or a deployment where nothing takes a durable claim at all
    (`api/state._default_turn_claims` returns `None` off the Postgres session store) all land the
    same way — a row under a session id whose ownership row is gone. Re-running the erasure is not
    the remedy, because that is exactly what cannot find it, so the count is the remedy: it turns
    an unreachable residue into a report that says so.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _seed(_FRAN, "sess-fran-residue")
        await erase_actor(_FRAN, apply=True)
        # What a turn that outlived the sweep leaves behind: a row keyed by the session, with no
        # ownership row to find it by.
        async with await connect(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO session_messages (session_id, message) VALUES (%s, %s)",
                    ("sess-fran-residue", '{"role": "assistant", "content": "after the sweep"}'),
                )
            await conn.commit()
        residue, holding = await _residue_for(["sess-fran-residue"])
        assert residue.get("session_messages") == 1, residue
        assert holding == ["sess-fran-residue"], (
            "the probe counted the residue and did not say which session it is under; the count "
            "alone names no remedy, because `session_owners` no longer answers that question"
        )

    asyncio.run(_run())


def test_the_residue_probe_asks_about_every_table_a_session_delete_names() -> None:
    """The probe is derived from the delete, so a table added to one is asked about by the other.

    A hand-written second list of "where a session's rows live" is the failure this whole check
    exists to catch, one indirection out: it would go stale in silence and the residue count would
    return zeros for the table that actually came back.
    """
    named = {table for table, _ in _residue_columns()}
    for table, _ in _session_delete_statements():
        if table == "tool_result_blobs":
            # Content-addressed and carrying no session of its own — reached through its link,
            # which is the row this probe counts instead.
            assert "tool_result_links" in named
            continue
        assert table in named, f"{table} is deleted per session but never re-counted"


def test_the_runbook_offboarding_section_names_no_table_and_points_at_the_constants() -> None:
    """The section a data-protection request is answered from may not carry a list of tables.

    It carried one, and it was half a list: "Per-actor rows live in nine tables" over six retained
    tables named, against `_ERASE` 12 + `_RETAINED` 12 + `_RETAINED_IN_PAYLOAD` 1 = 25 tables, 13
    of them retained. "Nine" is reconstructible as `_ERASE` before the checkpointer and store
    tables joined it, so it was true once and the tier grew under it in silence — while the seven
    omitted retained tables (`effects`, `pending_requests`, the three `experiment_protocol_*`,
    `bo_campaigns`, `result_publications`) each name a person. A DPO enumerating the retained tier
    from that paragraph reported six tables and was wrong about seven more, including the one whose
    data has already left for a store this system cannot erase from.

    So the assertion is the cheap direction this repository keeps choosing (`api/routes/README.md`,
    `deploy/README.md`'s expensive-actions section): the section names **no** table, states no
    count of tables, and names the three constants instead — the dry run already prints every table
    with its row count and its retention reason, so the maintained list is the command's output.
    """
    text = (_REPO_ROOT / "docs" / "guides" / "runbook.md").read_text(encoding="utf-8")
    section = _runbook_section(text, "### Offboard: erase their data")

    tables = (
        {table for table, _ in _ERASE}
        | {table for table, _, _ in _RETAINED}
        | {table for table, _, _, _ in _RETAINED_IN_PAYLOAD}
    )
    # `audit_events` is exempt: the section cites the grant that withholds DELETE from it, which is
    # a statement about a privilege rather than an enumeration of the tier.
    named = sorted(t for t in tables - {"audit_events"} if f"`{t}`" in section)
    assert not named, (
        f"the offboarding section names {named}; a list of tables here goes stale under the tier "
        "it describes — name `_ERASE`/`_RETAINED`/`_RETAINED_IN_PAYLOAD` and the dry run instead"
    )
    for constant in ("`_ERASE`", "`_RETAINED`", "`_RETAINED_IN_PAYLOAD`"):
        assert constant in section, f"the offboarding section no longer points at {constant}"
    counted = re.search(
        r"\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\d+)\s+tables\b",
        section,
    )
    assert counted is None, (
        f"the offboarding section states a table count ({counted.group(0)!r}); the tiers are "
        "counted by the dry run, not by this document"
    )


# --- Finishing an erasure a live turn interrupted -----------------------------------------------

_GRETA = "oid-greta"


async def _write_racing_rows(session_id: str, *, messages: int = 0, checkpoints: int = 0) -> None:
    """What a turn that outlived the sweep leaves behind, on its own committed connection.

    Two shapes rather than one, and separately, because the two records of a conversation can
    disagree by exactly one turn (`D-2026-09-06-an-erasure-that-races-a-live-turn-is-not-an-
    erasure`: a turn cancelled between the graph run and the transcript write leaves
    `checkpoints: 8, session_messages: 0`). A residue is therefore not reliably both, and a finish
    that only worked when both were present would fail on the commoner half.
    """
    async with await connect(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            for index in range(messages):
                await cur.execute(
                    "INSERT INTO session_messages (session_id, message) VALUES (%s, %s)",
                    (session_id, Jsonb({"role": "assistant", "content": f"after {index}"})),
                )
            for index in range(checkpoints):
                await cur.execute(
                    "INSERT INTO checkpoints "
                    "(thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata) "
                    "VALUES (%s, '', %s, %s, '{}'::jsonb)",
                    (
                        session_id,
                        f"ckpt-{index}",
                        Jsonb({"v": 1, "id": f"ckpt-{index}", "ts": "2026-09-07T00:00:00+00:00"}),
                    ),
                )
        await conn.commit()


def test_an_erasure_a_live_turn_interrupted_is_finished_by_session_id() -> None:
    """The remedy `residue` names, end to end: erase, race, finish, prove it is gone.

    **Reproduced before it was fixed, with the numbers.** A session is erased; a turn commits two
    messages afterwards; `_residue_for` sees them. A second `erase_actor` for the same person then
    reports **zeros in every table** — it reaches the session through `session_owners`, which the
    first run deleted — and the rows are still there. That is the state
    `D-2026-09-06-an-erasure-that-races-a-live-turn-is-not-an-erasure` closed on, and its last line
    is "It stays open".

    What closes it is that the residue was never out of the runtime role's reach; the *query* that
    finds it was. `finish_erasure` deletes by session id through
    `session_store._session_delete_statements()` — the same statements `delete_session` already
    runs, none of which reads `session_owners`.

    Watched failing with `_delete_orphaned_sessions` replaced by one that filters its targets
    through `session_owners` first, which is what every session-scoped route in this system did
    before this one: `removed_total == 0` and the residue still standing.
    """

    async def _run() -> tuple[dict[str, int], list[str], dict[str, int], int, bool, dict[str, int]]:
        await migrated_db_or_skip()
        await _seed(_GRETA, "sess-greta-residue")
        await erase_actor(_GRETA, apply=True)
        await _write_racing_rows("sess-greta-residue", messages=2)
        residue, sessions = await _residue_for(["sess-greta-residue"])
        rerun = await erase_actor(_GRETA, apply=True)
        finished = await finish_erasure(sessions, apply=True)
        after, _ = await _residue_for(["sess-greta-residue"])
        return residue, sessions, rerun.erased, finished.removed_total, finished.finished, after

    residue, sessions, rerun, removed, finished, after = asyncio.run(_run())

    assert residue.get("session_messages") == 2, residue
    assert sessions == ["sess-greta-residue"], (
        f"the residue named {sessions}; the session ids are what the remedy takes"
    )
    assert not any(rerun.values()), (
        f"re-running the actor erasure reached rows it should not be able to find: {rerun}. If "
        "this ever passes, the premise of the finish route has changed and it should be revisited."
    )
    assert removed >= 2, f"the finish removed {removed} rows and the residue was two messages"
    assert finished, "the finish reported itself unfinished"
    assert after == {}, f"rows survived the finish: {after}"


def test_a_residue_that_is_only_graph_state_is_finished_too() -> None:
    """A residue is not reliably both records of the conversation, and the finish must not need it.

    The transcript and the checkpoint stream can differ by exactly one turn — measured on a real
    `run_turn` cancelled between the graph run and the transcript write: `checkpoints: 8,
    session_messages: 0`, the model seeing the exchange and the chemist seeing neither. So the
    residue a racing turn leaves may be graph state with no message row at all, and a finish that
    reported "nothing to do" there would leave the conversation itself recoverable from the
    checkpointer while claiming the erasure was complete.
    """

    async def _run() -> tuple[dict[str, int], int, dict[str, int]]:
        await migrated_db_or_skip()
        await create_checkpoint_tables()
        await _seed(_GRETA, "sess-greta-graph")
        await erase_actor(_GRETA, apply=True)
        await _write_racing_rows("sess-greta-graph", checkpoints=3)
        residue, sessions = await _residue_for(["sess-greta-graph"])
        finished = await finish_erasure(sessions, apply=True)
        after, _ = await _residue_for(["sess-greta-graph"])
        return residue, finished.removed.get("checkpoints", 0), after

    residue, removed, after = asyncio.run(_run())

    assert residue == {"checkpoints": 3}, (
        f"the probe read {residue}; a residue with no transcript row is the commoner half of the "
        "divergence and has to be visible on its own"
    )
    assert removed == 3, f"the finish removed {removed} checkpoint rows of three"
    assert after == {}, f"graph state survived the finish: {after}"


def test_finishing_refuses_a_session_that_still_has_an_owner() -> None:
    """The safety property of the finish route: it can only clear what is already orphaned.

    Without this, `--finish <any session id>` would be an unscoped conversation delete that skips
    the ownership check every other path in this system makes — a bigger hole than the one this
    route closes. A session that still has an ownership row is reachable by its owner and by the
    actor erasure, so this refuses it by name rather than deleting it.
    """

    async def _run() -> tuple[dict[str, str], int, int]:
        await migrated_db_or_skip()
        await _seed(_BEN, "sess-ben-owned")
        report = await finish_erasure(["sess-ben-owned"], apply=True)
        return (
            report.refused,
            report.removed_total,
            await _count("session_messages", "session_id", "sess-ben-owned"),
        )

    refused, removed, still_there = asyncio.run(_run())

    assert "sess-ben-owned" in refused, f"an owned session was not refused: {refused}"
    assert removed == 0, f"the finish deleted {removed} rows of a session that is still owned"
    assert still_there >= 1, "an owned session's messages were deleted by the finish route"


def test_the_finish_says_what_it_leaves_behind() -> None:
    """Every table this route cannot clear is named, for the reason the erasure names its own.

    `tool_result_links` is the one, and by design: `infra/sql/grants/app_privileges.sql` withholds
    DELETE on it so a link can only disappear behind the content-addressed blob it points at. A
    finish that silently omitted it would be claiming a completeness it has not got, and one that
    reported it as *remaining* would read as unfinished for ever.
    """
    leaves = dict(finish_leaves())
    assert "tool_result_links" in leaves, (
        "the finish route deletes every table a session delete names except this one, and a table "
        "it does not touch has to be in the report rather than absent from it"
    )
    for table, why in leaves.items():
        assert len(why.split()) >= 8, f"{table} is named without a reason a reader can act on"


def test_the_cli_finishes_a_residue_and_says_so_in_its_exit_code() -> None:
    """The remedy is one command away from the failure that names it, and it is scriptable.

    The actor form exits `2` when it leaves a residue, which is what tells an operator's script the
    erasure did not finish. Before the finish route that exit code named a condition with no next
    command — the report said "an operator with owner rights has to remove these rows" and gave
    them neither the session ids nor a way to do it. So the pair is asserted together: the actor run
    exits `2` and prints the session ids, and the finish run over exactly those ids exits `0`.
    """

    async def _seed_residue() -> str:
        await migrated_db_or_skip()
        await _seed(_GRETA, "sess-greta-cli")
        await erase_actor(_GRETA, apply=True)
        await _write_racing_rows("sess-greta-cli", messages=1)
        return "sess-greta-cli"

    session_id = asyncio.run(_seed_residue())

    reported = io.StringIO()
    with contextlib.redirect_stdout(reported):
        # A second actor run, which is what an operator would try first: it finds the residue it
        # left behind, because the probe reads by session id even though the sweep cannot.
        first_code = erase_actor_main([_GRETA, "--apply"])
    finish = io.StringIO()
    with contextlib.redirect_stdout(finish):
        second_code = erase_actor_main(["--finish", session_id, "--apply"])
    left = asyncio.run(_count("session_messages", "session_id", session_id))

    assert first_code in (0, 2), first_code
    assert second_code == 0, f"the finish reported itself unfinished: {finish.getvalue()}"
    assert left == 0, f"{left} row(s) survived the finish"
    assert "--finish" in reported.getvalue() or first_code == 0, (
        "an actor run that reports a residue must print the command that clears it, not only the "
        f"counts: {reported.getvalue()}"
    )


def test_the_cli_refuses_an_actor_and_a_finish_in_one_run() -> None:
    """Two halves of one operation, never both at once.

    They target different things — a person, and a set of orphaned session ids — and a run that
    accepted both would have to guess which scoping the `--apply` belongs to. `argparse`'s
    mutually-exclusive group cannot express this once `actor` is optional (it means "at most one"),
    so the rule is checked explicitly and this is what holds it.
    """
    with pytest.raises(SystemExit) as refused:
        erase_actor_main([_GRETA, "--finish", "sess-anything"])
    assert refused.value.code == 2, "argparse exits 2 on a usage error"

    with pytest.raises(SystemExit) as empty:
        erase_actor_main([])
    assert empty.value.code == 2
