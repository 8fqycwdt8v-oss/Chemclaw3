"""Retention windows bound the durable stores (gap SCH-1).

Before this, nothing in the system deleted anything: every Postgres table grew for the life of the
deployment. For a GxP system that is a records gap, not just a disk one — "keep for N years, then
dispose, provably" needs a disposal step.

The Postgres round-trip skips offline (like every other PG test), so these pin the *policy* the job
encodes, which is where the real risk lives: what it prunes, what it refuses to prune and why, and
that a deployment must opt in before anything is deleted.
"""

import asyncio

import pytest
from agent_framework import Content, Message
from psycopg.types.json import Jsonb

from chemclaw.agent.message_pairing import unmatched_result_ids
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.durable.retention import _PRUNABLE, _window_days, prune_expired_rows
from tests.pg import migrated_db_or_skip


def test_only_spent_operational_rows_are_prunable() -> None:
    """The prunable set is closed and small — a new table is a deliberate addition, not a sweep."""
    assert set(_PRUNABLE) == {"session_events", "session_messages"}


def test_the_hash_chained_audit_trail_is_never_pruned() -> None:
    """Deleting from a hash chain is indistinguishable from tampering — the thing it detects.

    Safe disposal needs archive-then-reseal (export the prefix, verify it, record an out-of-band
    genesis anchor), which is a GxP design decision for an ADR, not something a cleanup job should
    quietly do. The table must therefore be absent from the prunable set entirely.
    """
    assert "audit_events" not in _PRUNABLE


def test_the_calculation_cache_is_never_pruned_by_age() -> None:
    """Evicting a cached result silently converts a cache hit into a recomputation (D-011).

    That is a cost policy question (LRU by access, or by compute cost), not a retention clock —
    an age cutoff could quietly re-run an expensive HPC job.
    """
    assert "calculation_results" not in _PRUNABLE


def test_retention_is_off_until_a_policy_is_stated(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deployment must choose its window; inheriting a deletion default from code is wrong."""
    assert settings.retention_session_events_days == 0
    assert settings.retention_session_messages_days == 0
    assert settings.retention_enabled is False
    for table in _PRUNABLE:
        assert _window_days(table) == 0, f"{table} would be pruned on an unstated policy"


def test_a_stated_window_is_read_per_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each table carries its own window — a mailbox row and a conversation age differently."""
    monkeypatch.setattr(settings, "retention_session_events_days", 7)
    monkeypatch.setattr(settings, "retention_session_messages_days", 365)
    assert _window_days("session_events") == 7
    assert _window_days("session_messages") == 365


# --- D-145: an age cutoff alone cannot dispose of a conversation row ---------------------------


def _call(call_id: str) -> Content:
    return Content.from_function_call(call_id=call_id, name="predict_pka", arguments={})


def _result(call_id: str) -> Content:
    return Content.from_function_result(call_id=call_id, result="ok")


def test_a_pair_straddling_the_cutoff_survives_intact() -> None:
    """The defect, against a real database: an expiring call whose result is not expiring stays.

    This is what the old single `DELETE ... WHERE created_at < cutoff` got wrong. It deleted the
    call and left the result — and a stranded `tool_result` is the one failure the read-repair
    cannot heal (`unmatched_result_ids`), so the session was bricked permanently by a cleanup job.

    Rows are dated explicitly rather than by waiting, because the whole point is a pair whose two
    halves fall on opposite sides of the window.
    """

    async def _run() -> tuple[list[int], set[str]]:
        await migrated_db_or_skip()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(settings, "retention_session_messages_days", 365)
        monkeypatch.setattr(settings, "retention_session_events_days", 0)
        try:
            session_id = "d145-straddle"
            async with db.connection(settings.postgres_dsn) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM session_messages WHERE session_id = %s", (session_id,)
                    )
                    for age_days, message in (
                        (400, Message(role="assistant", contents=[_call("c1")])),
                        # The answer arrived inside the window — the pair straddles the cutoff.
                        (1, Message(role="tool", contents=[_result("c1")])),
                    ):
                        await cur.execute(
                            "INSERT INTO session_messages (session_id, message, created_at) "
                            "VALUES (%s, %s, now() - make_interval(days => %s))",
                            (session_id, Jsonb(message.to_dict()), age_days),
                        )
                await conn.commit()

            await prune_expired_rows()

            async with db.connection(settings.postgres_dsn) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT id, message FROM session_messages "
                        "WHERE session_id = %s ORDER BY id",
                        (session_id,),
                    )
                    rows = await cur.fetchall()
            surviving = [Message.from_dict(row[1]) for row in rows]
            return [int(row[0]) for row in rows], unmatched_result_ids(surviving)
        finally:
            monkeypatch.undo()

    ids, stranded = asyncio.run(_run())
    assert len(ids) == 2, (
        "retention split a tool-call pairing across the cutoff; the surviving half is unusable"
    )
    assert stranded == set(), f"retention stranded {stranded} — the session is now bricked"


def test_an_expired_pair_is_removed_whole() -> None:
    """The closure must not become a refusal to prune anything: both halves expired, both go."""

    async def _run() -> int:
        await migrated_db_or_skip()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(settings, "retention_session_messages_days", 365)
        monkeypatch.setattr(settings, "retention_session_events_days", 0)
        try:
            session_id = "d145-both-expired"
            async with db.connection(settings.postgres_dsn) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM session_messages WHERE session_id = %s", (session_id,)
                    )
                    for message in (
                        Message(role="assistant", contents=[_call("c9")]),
                        Message(role="tool", contents=[_result("c9")]),
                    ):
                        await cur.execute(
                            "INSERT INTO session_messages (session_id, message, created_at) "
                            "VALUES (%s, %s, now() - make_interval(days => 400))",
                            (session_id, Jsonb(message.to_dict())),
                        )
                await conn.commit()

            await prune_expired_rows()

            async with db.connection(settings.postgres_dsn) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT count(*) FROM session_messages WHERE session_id = %s", (session_id,)
                    )
                    row = await cur.fetchone()
            return int(row[0]) if row is not None else -1
        finally:
            monkeypatch.undo()

    assert asyncio.run(_run()) == 0
