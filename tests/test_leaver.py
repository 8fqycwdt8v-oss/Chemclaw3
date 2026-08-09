"""Offboarding: the conversation is erasable, the GxP record is not (D-2026-08-08).

The two-tier rule in `chemclaw.agent.leaver` is a data-protection decision, so these tests assert
the *line* rather than the plumbing: that a departed person's sessions, preferences and watches go,
that the rows attributing scientific work to them stay and are counted rather than quietly ignored,
that a dry run writes nothing while still reporting real numbers, and that one person's erasure
cannot take another's data with it.

Postgres-backed and skipped where no database is reachable, like every other store test here.
"""

import asyncio
import contextlib
import io

from chemclaw.agent.leaver import _ERASE, _RETAINED, erase_actor, retention_reasons
from chemclaw.cli.erase_actor import main as erase_actor_main
from chemclaw.core.config import settings
from chemclaw.core.db import connect
from tests.pg import migrated_db_or_skip

# The column names this system uses for a person. Not every TEXT column — a derived set needs a
# vocabulary, and this is it, drawn from the six spellings the schema actually uses.
_ACTOR_COLUMN_NAMES = frozenset(
    {"actor", "owner", "holder", "requested_by", "decided_by", "opened_by"}
)

_ANNA = "oid-anna"
_BEN = "oid-ben"


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
    """The GxP half of the rule, and the half a caller must not be able to miss.

    An attributable record that can be deleted on request is not an attributable record, and
    `audit_events` additionally carries a hash chain whose proof spans the rows either side of any
    deletion. So the row stays — and the report *names it and counts it*, because a partial erasure
    that looks complete is worse than one that refuses out loud.
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
    """A blank id matches every un-attributed row of a dev deployment, not one person's data."""

    async def _run() -> None:
        try:
            await erase_actor("   ")
        except ValueError as exc:
            assert "non-empty" in str(exc)
        else:  # pragma: no cover - the refusal is the behavior under test
            raise AssertionError("a blank actor must be refused before any statement runs")

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
                    "WHERE table_schema = current_schema() AND column_name = ANY(%s) "
                    "ORDER BY table_name, column_name",
                    (sorted(_ACTOR_COLUMN_NAMES),),
                )
                found = {(t, c) for t, c in await cur.fetchall()}

        retained = {(table, col) for table, cols, _ in _RETAINED for col in cols}
        # The erase tier is matched by table: its statements reach rows through `session_owners`
        # rather than always naming the actor column directly, so the column-level assertion that
        # fits the retain tier would be wrong here.
        erased_tables = {table for table, _ in _ERASE}
        unaccounted = sorted(
            (t, c) for t, c in found if (t, c) not in retained and t not in erased_tables
        )
        assert not unaccounted, (
            f"these columns name a person and belong to neither tier: {unaccounted}. "
            "Add each to `_ERASE` (the conversation) or `_RETAINED` (the record) in "
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
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        report = await erase_actor("oid-nobody-at-all")
        assert report.erased_total == 0
        assert set(report.erased) == {
            "session_messages",
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
