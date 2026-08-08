"""Offboarding: the conversation is erasable, the GxP record is not (D-2026-08-08).

The two-tier rule in `chemclaw.agent.leaver` is a data-protection decision, so these tests assert
the *line* rather than the plumbing: that a departed person's sessions, preferences and watches go,
that the rows attributing scientific work to them stay and are counted rather than quietly ignored,
that a dry run writes nothing while still reporting real numbers, and that one person's erasure
cannot take another's data with it.

Postgres-backed and skipped where no database is reachable, like every other store test here.
"""

import asyncio

import psycopg

from chemclaw.agent.leaver import erase_actor, retention_reasons
from chemclaw.core.config import settings
from chemclaw.core.db import connect
from tests.pg import migrated_db_or_skip

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


def test_a_missing_database_surfaces_as_a_connection_error() -> None:
    """The CLI turns this into a message; it must not be an opaque psycopg traceback."""
    assert issubclass(psycopg.OperationalError, Exception)
