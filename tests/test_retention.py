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

from chemclaw.agent.message_pairing import droppable_rows, unmatched_result_ids
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.durable import retention
from chemclaw.durable.retention import (
    _PRUNABLE,
    RetentionOutcome,
    _window_days,
    prune_expired_rows,
)
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


def test_an_undelivered_push_back_event_survives_the_window() -> None:
    """Age alone does not make a mailbox row disposable — only *delivery* does.

    The module docstring justifies pruning `session_events` with "a **consumed** push-back mailbox
    row is spent", and the sweep was a bare age cutoff with no `consumed_at` predicate. So a
    `job_completed` that outlived the window was destroyed before anyone read it: a QM or HPC run
    longer than the retention window — exactly the jobs this channel exists for — lost its
    completion, the session waited on it forever, and the harness "awaiting job" todo never
    flipped. It also deleted the `system-audit-integrity` and `system-eval-drift` alerts, which are
    never consumed by construction, so retention quietly removed the tamper evidence.
    """

    async def _run() -> tuple[int, int]:
        await migrated_db_or_skip()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(settings, "retention_session_events_days", 7)
        monkeypatch.setattr(settings, "retention_session_messages_days", 0)
        try:
            unread, read = "retention-unread", "retention-read"
            async with db.connection(settings.postgres_dsn) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM session_events WHERE session_id = ANY(%s)", ([unread, read],)
                    )
                    # Both far older than the window; one was delivered, the other never was.
                    await cur.execute(
                        "INSERT INTO session_events (session_id, kind, payload, created_at) "
                        "VALUES (%s, 'job_completed', %s, now() - make_interval(days => 90))",
                        (unread, Jsonb({"job_id": "qm-long-run"})),
                    )
                    await cur.execute(
                        "INSERT INTO session_events "
                        "(session_id, kind, payload, created_at, consumed_at) VALUES "
                        "(%s, 'job_completed', %s, now() - make_interval(days => 90), now())",
                        (read, Jsonb({"job_id": "already-delivered"})),
                    )
                await conn.commit()

            await prune_expired_rows()

            async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
                await cur.execute(
                    "SELECT count(*) FROM session_events WHERE session_id = %s", (unread,)
                )
                kept = await cur.fetchone()
                await cur.execute(
                    "SELECT count(*) FROM session_events WHERE session_id = %s", (read,)
                )
                gone = await cur.fetchone()
            return (int(kept[0]) if kept else -1, int(gone[0]) if gone else -1)
        finally:
            monkeypatch.undo()

    surviving_unread, surviving_read = asyncio.run(_run())
    assert surviving_unread == 1, "an undelivered push-back event was deleted by age alone"
    assert surviving_read == 0, "a delivered event past the window should still be pruned"


async def _seed_expired_sessions(count: int, prefix: str) -> str:
    """Insert `count` fully-expired single-message sessions; return the SQL LIKE prefix to match.

    One self-contained message per session (no tool pairing), so every row is disposable and the
    only thing under test is how the sweep commits and how much of the backlog it takes.

    Clears the **whole** table first, not just this prefix. Both callers assert on a count the
    sweep produces from a global `SELECT DISTINCT ... LIMIT`, so a row another test left behind
    lands inside the batch and shifts the number — the suite isolates one schema per run rather
    than per test (`tests/pg.py`; BACKLOG LIVE-6). Safe because every test in this file seeds
    immediately before it prunes.
    """
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM session_messages")
            message = Message(role="user", contents=[Content(type="text", text="old")])
            for index in range(count):
                await cur.execute(
                    "INSERT INTO session_messages (session_id, message, created_at) "
                    "VALUES (%s, %s, now() - make_interval(days => 400))",
                    (f"{prefix}{index:03d}", Jsonb(message.to_dict())),
                )
        await conn.commit()
    return f"{prefix}%"


async def _remaining(like: str) -> int:
    """How many seeded conversation rows are still stored."""
    async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM session_messages WHERE session_id LIKE %s", (like,))
        row = await cur.fetchone()
    return int(row[0]) if row else -1


def test_a_failure_part_way_through_keeps_what_the_sweep_already_removed() -> None:
    """The per-table commit's own argument, one level down, where it had not been made.

    `prune_expired_rows` commits each table separately so one table's failure cannot roll back
    another's — and then handed `session_messages` to a loop over every expired session whose
    single `commit()` came after the loop. A failure on the last session discarded every deletion
    before it, and the pass reported nothing removed while the table went on growing. That is the
    same "a sweep that says it removed rows it then rolled back" failure the table-level fix was
    written against (D-2026-08-05-a-sweep-that-commits-once).

    Injected at the pairing closure rather than at the database, so the failure lands mid-loop
    exactly where a statement timeout or a dropped connection would.
    """

    async def _run() -> int:
        await migrated_db_or_skip()
        like = await _seed_expired_sessions(6, "sweep-commit-")
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(settings, "retention_session_messages_days", 365)
        monkeypatch.setattr(settings, "retention_session_events_days", 0)
        calls = {"n": 0}
        real = droppable_rows

        def _fail_on_the_fourth(rows: object, expired: object) -> object:
            calls["n"] += 1
            if calls["n"] == 4:
                raise RuntimeError("statement timeout part way through the sweep")
            return real(rows, expired)  # type: ignore[arg-type]

        monkeypatch.setattr(retention, "droppable_rows", _fail_on_the_fourth)
        try:
            with pytest.raises(RuntimeError):
                await prune_expired_rows()
            return await _remaining(like)
        finally:
            monkeypatch.undo()

    remaining = asyncio.run(_run())
    assert remaining == 3, (
        f"{remaining} of 6 seeded rows survive; the three sessions pruned before the failure "
        "should have been committed, not rolled back with it"
    )


def test_one_pass_works_a_bounded_batch_and_reports_the_rest() -> None:
    """An unbounded first pass spends an attempt and commits only what it reached.

    The conversation prune costs three round trips per session and cannot be one `DELETE` (D-145),
    so a deployment enabling retention over a long backlog faces every session it has ever had
    inside one activity's `retention_timeout_seconds`. Capped, each pass makes bounded progress —
    and says how much it left, because a cap that is not reported reads as "there was nothing
    more" and a growing table would look bounded in every result this job returns.
    """

    async def _run() -> tuple[RetentionOutcome, int]:
        await migrated_db_or_skip()
        like = await _seed_expired_sessions(5, "sweep-batch-")
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(settings, "retention_session_messages_days", 365)
        monkeypatch.setattr(settings, "retention_session_events_days", 0)
        monkeypatch.setattr(settings, "retention_max_sessions_per_pass", 2)
        try:
            first = await prune_expired_rows()
            return first, await _remaining(like)
        finally:
            monkeypatch.undo()

    outcome, remaining = asyncio.run(_run())
    assert outcome.deleted["session_messages"] == 2
    assert remaining == 3, "the pass worked more sessions than its cap allowed"
    assert outcome.sessions_deferred > 0, (
        "the pass stopped at its cap and reported nothing left, which reads as a bounded table"
    )
