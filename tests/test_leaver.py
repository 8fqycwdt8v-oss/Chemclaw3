"""Offboarding: the conversation is erasable, the record of what was done is not.

These tests assert the data-protection line in `chemclaw.agent.leaver`: a departed person's
sessions, preferences and watches go; rows attributing scientific work to them stay and are
counted; a dry run writes nothing but reports real numbers; and one person's erasure cannot take
another's data. Both spellings of an actor (`<id>` and `unverified:<id>`) are the same person,
while an id that merely contains theirs is not. Postgres-backed; skipped without a database.
"""

import asyncio
import contextlib
import io
import math
import re
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from chemclaw.agent import leaver
from chemclaw.agent.leaver import (
    _BEYOND_REACH,
    _ERASE,
    _RETAINED,
    _RETAINED_IN_PAYLOAD,
    ErasureError,
    _residue_columns,
    _residue_for,
    _sessions_held,
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
from chemclaw.exhibits.models import parse_spec
from chemclaw.exhibits.store import PostgresExhibitStore
from tests.pg import create_checkpoint_tables, migrated_db_or_skip

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _runbook_section(text: str, heading: str) -> str:
    """One `###` section of the runbook, heading to the next heading of any level."""
    assert heading in text, f"{heading!r} is gone from the runbook"
    body = text.split(heading, 1)[1]
    return re.split(r"\n#{2,3} ", body, maxsplit=1)[0]


# Person-column spellings not covered by the `_by` suffix matched in `_LIKE_A_PERSON` below.
# The regular half is matched by suffix so a new `*_by` column is scanned the day its migration
# lands; only spellings a suffix cannot reach are enumerated here.
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

    `actor` goes in verbatim: the bare id (durable path) or `unverified:<id>` (the synchronous MCP
    path, whose actor is an unauthenticated header). Both are the same chemist.
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


async def test_a_dry_run_reports_real_counts_and_writes_nothing() -> None:
    """The number an operator signs off on is the number that will be deleted.

    The dry run executes the deletes and rolls back rather than predicting them with a second query.
    """
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


async def test_applying_removes_the_conversation() -> None:
    """Sessions, their messages and events, preferences and watches all go."""
    await migrated_db_or_skip()
    await _seed(_ANNA, "sess-apply")
    report = await erase_actor(_ANNA, apply=True)
    assert report.applied is True
    assert await _count("session_owners", "owner", _ANNA) == 0
    assert await _count("user_preferences", "owner", _ANNA) == 0
    assert await _count("session_messages", "session_id", "sess-apply") == 0
    assert await _count("session_events", "session_id", "sess-apply") == 0


async def test_one_persons_erasure_leaves_another_persons_data_alone() -> None:
    """The failure that would be discovered far too late: an over-broad WHERE clause."""
    await migrated_db_or_skip()
    await _seed(_ANNA, "sess-anna")
    await _seed(_BEN, "sess-ben")
    await erase_actor(_ANNA, apply=True)
    assert await _count("session_owners", "owner", _BEN) == 1
    assert await _count("user_preferences", "owner", _BEN) == 1
    assert await _count("session_messages", "session_id", "sess-ben") == 1


async def test_the_audit_trail_survives_an_erasure_and_is_reported() -> None:
    """The audit trail survives an erasure, and the report names and counts it.

    An attributable record that can be deleted on request is not attributable; a partial erasure
    that looks complete is worse than one that says what it kept.
    """
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


async def test_a_blank_actor_is_refused() -> None:
    """A blank actor, or a bare `unverified:`, is refused.

    Either would match every un-attributed or marked row in the deployment, not one person's data.
    """
    for blank in ("   ", "unverified:", "unverified:  "):
        try:
            await erase_actor(blank)
        except ValueError as exc:
            assert "non-empty" in str(exc)
        else:  # pragma: no cover - the refusal is the behavior under test
            raise AssertionError(f"{blank!r} must be refused before any statement runs")


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


async def test_erasing_one_person_leaves_a_shared_tool_result_readable_for_the_other() -> None:
    """A blob two sessions link is not one person's to take away.

    Deleting it would cascade the other session's link row, leaving that chemist's transcript
    pointing at an unfetchable result. Same rule as `session_store._SESSION_DELETE`.
    """
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


async def test_an_unread_digest_does_not_survive_its_owners_erasure() -> None:
    """An unread digest does not survive its owner's erasure.

    A digest lands in `digest-<oid>` with no `session_owners` row, so it is not reached through the
    ownership join every other `session_events` row uses. The row is unconsumed on purpose: nothing
    else ever drains that population.
    """
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


async def test_a_publication_naming_a_person_is_reported_rather_than_silently_kept() -> None:
    """A publication naming a person is retained and counted, not omitted.

    `result_publications.document` carries `publications[].actor`, `.session_id` and a free-text
    `.rationale` inside a payload the schema-derived check cannot see. Retained like every record: a
    publication says who asked for a result and why.
    """
    await migrated_db_or_skip()
    document = {
        "publications": [
            {"actor": _ERIK, "session_id": "s-erik-pub", "rationale": "erik asked for this"}
        ]
    }
    async with await connect(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO result_publications (sink, calc_ref, document) VALUES (%s, %s, %s)",
                ("test-sink", "calc-erik-1", Jsonb(document)),
            )
        await conn.commit()

    # Cleaned up in a `finally`: `result_publications` is not in the erase tier, so the row would
    # otherwise skew later assertions about `_ERIK`'s retained count.
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
                await cur.execute("DELETE FROM result_publications WHERE sink = %s", ("test-sink",))
            await conn.commit()


@pytest.mark.parametrize(
    "publications",
    [None, {"actor": _ERIK}, "not-a-list", 3, True],
    ids=["json-null", "object", "string", "number", "boolean"],
)
async def test_a_publication_payload_it_cannot_read_counts_zero_rather_than_ending_the_erasure(
    publications: object,
) -> None:
    """One unreadable publication payload counts zero rather than making every erasure fail.

    `jsonb_array_elements` is partial and the retained count shares the transaction with every
    DELETE, so one malformed `document` would block all erasures. Parametrised over the shapes
    Postgres refuses; `document` has no CHECK and a versioned shape, so they are reachable.
    """
    await migrated_db_or_skip()
    async with await connect(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM result_publications WHERE sink = %s", ("erasure-test",))
            await cur.execute(
                "INSERT INTO result_publications (sink, calc_ref, document) VALUES (%s, %s, %s)",
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


async def test_a_session_the_leaver_deleted_themselves_does_not_spare_their_own_blob() -> None:
    """An orphan link left by the leaver's own `delete_session` does not spare their blob.

    Pairs with `test_erasing_one_person_leaves_a_shared_tool_result_readable_for_the_other`: another
    *person* spares the blob, an orphan does not. Satisfying only one is the defect in the other
    direction.
    """
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
        "the report said zero over a blob it should have deleted, which reads as 'there were none'"
    )


async def test_every_actor_bearing_column_in_the_schema_is_accounted_for() -> None:
    """No column may name a person without this module having a position on it.

    The set is derived from the live schema: every column whose name is a person spelling must be in
    the erase or the retain tier, so a new one fails with the column named and its tier is decided
    deliberately rather than by omission.
    """
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
    # The scan must see every column this module already has a position on; a retained column it
    # misses is a spelling the vocabulary does not know.
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
    # Two further positions must be stated explicitly: a person inside a payload
    # (`_RETAINED_IN_PAYLOAD`) and a table this command can neither clear nor count
    # (`_BEYOND_REACH`).
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


async def test_a_proposal_someone_wrote_and_reviewed_is_counted_once() -> None:
    """Two person columns on one row are one retained record, not two.

    The operator reads a count of records; summing per column would inflate it.
    """
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


async def test_a_reviewers_signoff_is_retained_even_when_they_proposed_nothing() -> None:
    """The case the hand-written list got wrong: `decided_by` with an empty `actor`."""
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


async def test_a_claim_marked_unverified_is_the_same_person_and_is_counted() -> None:
    """A claim marked `unverified:<id>` is the same person and is counted.

    The synchronous MCP path records `unverified:<id>` while the durable path writes the bare id
    into the same columns; matching byte-exactly would under-count rows that still hold the
    identifier. Either spelling may be named, since an operator pastes what they read.
    """
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


async def test_erasing_one_person_spares_another_whose_id_contains_theirs() -> None:
    """Erasing one person spares another whose id contains theirs.

    A `LIKE '%' || actor || '%'` match would erase `oid-erik-2` with `oid-erik`, so matching stays
    exact equality against `_actor_forms`. The bystander keeps every row in both spellings and
    appears in nobody else's report.
    """
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


async def test_a_departed_persons_turn_lease_is_released() -> None:
    """A lease names its holder, and offboarding must not leave one held by a leaver."""
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


def test_every_retained_table_states_why() -> None:
    """A retained row an operator cannot get an explanation for is one they will delete by hand."""
    reasons = dict(retention_reasons())
    assert reasons, "no retention reasons are declared"
    assert all(reason.strip() for reason in reasons.values())
    assert "audit_events" in reasons


async def test_the_erase_statements_are_valid_sql() -> None:
    """Every erase statement executes against the real schema; the report covers every table.

    Each delete runs in a rolled-back transaction for an actor nobody has. Library-created tables
    (checkpointer, memory store) are skipped when absent and executed in
    `tests/test_message_migration.py`, but their keys stay asserted here. `tool_result_blobs` is
    reached only through its links, so it must be listed explicitly; the link rows go by cascade. A
    table silently dropping out of the report is the failure this guards.
    """
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
        "budget_usage",
        # Composed workflows are erased like preferences: a working procedure cites no evidence, so
        # it is part of the conversation, not the record.
        "composed_workflows",
        # A shared session's standing and authorship in sessions somebody else owns
        # (`D-2026-09-27-in-a-shared-session-the-sender-governs`).
        "session_members",
        "plan_authors",
        # A place in a session's line, by sender
        # (`D-2026-10-01-a-queued-message-waits-in-its-senders-request`).
        "session_turn_queue",
        # A request to a running turn on another replica, by asker
        # (`D-2026-10-04-a-running-turn-is-reached-through-postgres-from-any-replica`).
        "session_turn_remotes",
        # Artefacts follow `session_messages` (`D-2026-10-02-an-artefact-is-part-of-the-answer-not-
        # an-effect`); their revisions cascade from the header.
        "session_exhibits",
        # Uploads, by session and by uploader
        # (`D-2026-10-04-an-upload-is-session-state-not-pod-state`).
        "session_attachments",
        "session_owners",
    }


def test_the_cli_reports_a_statement_level_database_error_instead_of_raising() -> None:
    """A statement-level `psycopg.Error` prints a message instead of a traceback.

    `psycopg.Error` is neither `ValueError` nor `ConnectionError`; the likeliest operator failure is
    `InsufficientPrivilege` from missing grants. Reproduced with a search path over an empty schema,
    which raises `UndefinedTable` against a healthy, reachable database.
    """
    # Synchronous on purpose, where every other test in this file is a coroutine: the subject is
    # `erase_actor_main`, a console entry point that owns an `asyncio.run` of its own, and a nested
    # one refuses outright. So the skip check gets its own loop and the CLI gets the loop it builds.
    asyncio.run(migrated_db_or_skip())

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


async def test_an_erasure_refuses_while_a_turn_holds_one_of_the_persons_sessions() -> None:
    """An erasure refuses while a turn holds one of the person's sessions, naming the session.

    A running turn would rewrite its in-memory messages after the sweep, restoring the conversation
    under a session with no `session_owners` row, beyond every path that reaches sessions by owner.
    """
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


async def test_a_refused_erasure_gives_back_every_claim_it_took() -> None:
    """A refused erasure releases every claim it took.

    The sweep claims all of the person's sessions first, so a refusal on the last must give back the
    others or they answer 409 until the lease expires.
    """
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


async def test_a_quiet_session_is_erased_and_the_sweep_holds_no_claim_afterwards() -> None:
    """The ordinary path still erases, and the claim it took to do so does not outlive it."""
    await migrated_db_or_skip()
    await _seed(_FRAN, "sess-fran-quiet")
    report = await erase_actor(_FRAN, apply=True)
    assert report.erased["session_messages"] >= 1
    assert await _count("session_owners", "owner", _FRAN) == 0
    assert await _count("session_turns", "session_id", "sess-fran-quiet") == 0
    assert report.residue == {}, "a clean run leaves nothing behind and must say so"


async def test_a_row_that_comes_back_under_an_erased_session_is_counted_not_missed() -> None:
    """A row written under an erased session after the sweep is counted, not missed.

    A lapsed lease, a session created mid-sweep, or a deployment without durable claims all leave
    rows under a session whose ownership row is gone. Re-running the erasure cannot find them, so
    the residue count reports them.
    """
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


def test_the_residue_probe_asks_about_every_table_a_session_delete_names() -> None:
    """The residue probe is derived from the session delete, so the two cover the same tables."""
    named = {table for table, _ in _residue_columns()}
    for table, _ in _session_delete_statements():
        if table == "tool_result_blobs":
            # Content-addressed and carrying no session of its own — reached through its link,
            # which is the row this probe counts instead.
            assert "tool_result_links" in named
            continue
        assert table in named, f"{table} is deleted per session but never re-counted"


def test_the_runbook_offboarding_section_names_no_table_and_points_at_the_constants() -> None:
    """The runbook's offboarding section names no table and points at the constants instead.

    A hand-maintained list of tables goes stale as the tiers grow; the dry run already prints every
    table with its row count and retention reason, so the command's output is the maintained list.
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

    Messages and checkpoints are written separately because the two records can differ by one turn;
    a residue is not reliably both.
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
    """Erase, race, finish, prove gone: the remedy `residue` names, end to end.

    A second `erase_actor` reaches sessions through `session_owners` and so finds nothing. The
    finish deletes by session id via `session_store._session_delete_statements()`, which never reads
    `session_owners`.
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
    """A residue that is only graph state is finished too.

    The transcript and the checkpoint stream can differ by one turn, so a residue may be checkpoints
    with no message row; reporting "nothing to do" would leave the conversation recoverable.
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
    """The finish route clears only sessions that are already orphaned.

    Otherwise `--finish <session id>` would be an unscoped conversation delete skipping the
    ownership check. A session with an owner is refused by name.
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
    """Every table the finish cannot clear is named.

    `tool_result_links` is withheld DELETE by the grants so a link disappears only with its blob;
    omitting it would overclaim completeness, and listing it as remaining would read as unfinished.
    """
    leaves = dict(finish_leaves())
    assert "tool_result_links" in leaves, (
        "the finish route deletes every table a session delete names except this one, and a table "
        "it does not touch has to be in the report rather than absent from it"
    )
    for table, why in leaves.items():
        assert len(why.split()) >= 8, f"{table} is named without a reason a reader can act on"


def test_the_cli_finishes_a_residue_and_says_so_in_its_exit_code() -> None:
    """The CLI finishes a residue and says so in its exit code.

    The actor run exits `2` and prints the residual session ids; the finish run over exactly those
    ids exits `0`.
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
    """An actor and `--finish` are refused in one run.

    They scope `--apply` differently; argparse's mutually-exclusive group cannot express this once
    `actor` is optional, so the rule is checked explicitly.
    """
    with pytest.raises(SystemExit) as refused:
        erase_actor_main([_GRETA, "--finish", "sess-anything"])
    assert refused.value.code == 2, "argparse exits 2 on a usage error"

    with pytest.raises(SystemExit) as empty:
        erase_actor_main([])
    assert empty.value.code == 2


# The scale half of the guard: many sessions and a short lease. What lapses a claim is the ratio of
# hold time to lease, so a shortened lease reproduces the defect in seconds.
_HOLGER = "oid-holger"


async def _seed_many(actor: str, session_ids: list[str]) -> None:
    """Give `actor` a session apiece, in two statements rather than five per session.

    These tests need a fleet, not the preference, watch and event `_seed` writes.
    """
    async with await connect(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO session_owners (session_id, owner) "
                "SELECT s, %s FROM unnest(%s::text[]) AS s "
                "ON CONFLICT (session_id) DO UPDATE SET owner = EXCLUDED.owner",
                (actor, session_ids),
            )
            await cur.execute(
                "INSERT INTO session_messages (session_id, message) "
                'SELECT s, \'{"role": "user"}\'::jsonb FROM unnest(%s::text[]) AS s',
                (session_ids,),
            )
        await conn.commit()


async def _claims_state(session_ids: list[str]) -> tuple[int, int]:
    """`(claims this sweep still holds, claims of its own that have lapsed)`."""
    async with await connect(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FILTER (WHERE expires_at > now()), "
                "       count(*) FILTER (WHERE expires_at <= now()) "
                "FROM session_turns "
                "WHERE session_id = ANY(%s) AND left(holder, 6) = 'erase:'",
                (session_ids,),
            )
            row = await cur.fetchone()
    return (int(row[0]), int(row[1])) if row else (0, 0)


def test_a_sweep_that_outlasts_its_lease_still_holds_every_claim() -> None:
    """Every claim survives a sweep that outlasts its lease.

    A deletion can run longer than the lease, and a lapsed claim is re-takeable by any pod,
    admitting the live turn the guard exists to refuse. Driven with a short lease rather than a
    large fleet, since the ratio of hold time to lease is what matters.
    """
    sessions = [f"sess-holger-{index:03d}" for index in range(20)]

    async def _run() -> tuple[int, int, int]:
        await migrated_db_or_skip()
        await _seed_many(_HOLGER, sessions)
        patch = pytest.MonkeyPatch()
        patch.setattr(settings, "service_turn_claim_lease_seconds", 2.0)
        claims = SessionTurnClaims()
        try:
            async with _sessions_held(sessions):
                # Longer than the lease. The heartbeat refreshes three times per lease, so asserting
                # just past one lease would test machine load rather than whether claims are held.
                await asyncio.sleep(3.0)
                stolen = [
                    session_id
                    for session_id in sessions
                    if await claims.claim(session_id, "pod-2", 60.0)
                ]
                held, lapsed = await _claims_state(sessions)
            for session_id in stolen:
                await claims.release(session_id, "pod-2")
            return len(stolen), held, lapsed
        finally:
            patch.undo()

    stolen, held, lapsed = asyncio.run(_run())

    assert stolen == 0, (
        f"pod-2 claimed {stolen} of the sessions this sweep is holding; a turn can start on a "
        "session the erasure is about to delete, which is the residue the guard exists to prevent"
    )
    assert lapsed == 0, (
        f"{lapsed} of this sweep's own claims have lapsed while it holds them; nothing refreshes "
        "them, so the guard covers the first lease of an erasure and not the rest of it"
    )
    assert held == len(sessions), f"{held} of {len(sessions)} claims are still live"


async def test_the_claim_sweep_does_not_pay_a_round_trip_per_session() -> None:
    """The claim sweep does not pay a round trip per session.

    Per-session claims on a large fleet take longer than the lease before anything is deleted.
    Counted as connections borrowed, since `claim` and `release` each open one. `CLAIM_BATCH` is
    shrunk so the batching runs as several statements, proving the loop and not only the statement.
    """
    sessions = [f"sess-hilde-{index:03d}" for index in range(200)]
    batch = 50
    borrows: list[str] = []

    class _Counting(SessionTurnClaims):
        """The same claims, counting how many times the sweep goes to the database."""

        def _connection(self):  # type: ignore[no-untyped-def]
            borrows.append("borrow")
            return super()._connection()

    await migrated_db_or_skip()
    await _seed_many("oid-hilde", sessions)
    patch = pytest.MonkeyPatch()
    patch.setattr(leaver, "SessionTurnClaims", _Counting)
    patch.setattr(leaver, "CLAIM_BATCH", batch)
    try:
        async with _sessions_held(sessions):
            pass
    finally:
        patch.undo()

    # Claim and release, one statement each per batch, plus headroom for a refresh tick landing
    # inside a sweep this short.
    ceiling = 4 * math.ceil(len(sessions) / batch) + 2
    assert len(borrows) <= ceiling, (
        f"{len(borrows)} connection borrows for {len(sessions)} sessions (ceiling {ceiling}): the "
        "sweep still claims one session per round trip, so its first claims lapse before its last "
        "one is taken"
    )


def test_an_erasure_does_not_take_the_organisations_judgment() -> None:
    """An erasure does not reach the organisation skills tier.

    An org skill is the organisation's judgment that other turns depend on; a departing person's
    words in it are an administrator's content decision, not a prefix sweep. Asserted through
    `store_prefixes` against the namespace the tier writes under.
    """
    from chemclaw.agent.leaver import store_prefixes
    from chemclaw.agent.org_skills import org_skills_namespace, org_versions_namespace

    prefixes = store_prefixes(["alice-oid", "unverified:alice"])
    org = ".".join(org_skills_namespace())

    assert org not in prefixes, "an organisation skill is not one person's data to erase"
    assert not any(prefix.startswith(f"{org}.") for prefix in prefixes), prefixes
    assert not any(
        prefix.startswith(org_versions_namespace("house-workup")[0]) for prefix in prefixes
    ), "the version history went with a departing chemist"


async def test_a_members_artefact_in_someone_elses_session_is_reported_and_findable() -> None:
    """A member's artefact in another's session survives their erasure; the report says where.

    The erase tier reaches artefacts through `session_owners`, so it is out of reach by design;
    `_BEYOND_REACH` names the columns and the query it hands an operator is run here.
    """
    await migrated_db_or_skip()
    session = f"sess-xb-{uuid4().hex[:8]}"
    await _seed(_ANNA, session)
    made = await PostgresExhibitStore().create(
        session,
        title="Ben's screen",
        spec=parse_spec({"kind": "document", "markdown": "Ben wrote this."}),
        author_kind="human",
        author=_BEN,
    )
    await erase_actor(_BEN)

    reason = next(why for key, why in _BEYOND_REACH.items() if key.startswith("session_exhibits"))
    assert "created_by" in reason and "head_author" in reason
    found = re.search(r"`(SELECT [^`]+)`", reason)
    assert found is not None, "the reason must hand the operator a query"
    query = found.group(1).replace("'<id>'", "%(id)s")
    async with await connect(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, {"id": _BEN})
            rows = await cur.fetchall()
    assert (session, made.exhibit_id) in {(row[0], row[1]) for row in rows}
