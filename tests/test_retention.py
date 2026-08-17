"""Retention windows bound the durable stores (gap SCH-1).

Before this, nothing in the system deleted anything: every Postgres table grew for the life of the
deployment. That is a records gap, not just a disk one — "keep for N years, then
dispose, provably" needs a disposal step.

The Postgres round-trip skips offline (like every other PG test), so these pin the *policy* the job
encodes, which is where the real risk lives: what it prunes, what it refuses to prune and why, and
that a deployment must opt in before anything is deleted.
"""

import asyncio
from typing import Any

import pytest
from langchain_core.messages import HumanMessage, message_to_dict
from psycopg.types.json import Jsonb

from chemclaw.agent.message_migration import to_langchain
from chemclaw.agent.message_pairing import droppable_rows, unmatched_result_ids
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.durable import retention
from chemclaw.durable.retention import (
    _EXPIRED_THREADS,
    _PRUNABLE,
    RetentionOutcome,
    _window_days,
    prune_expired_rows,
)
from tests.legacy_rows import legacy_call, legacy_result, legacy_text
from tests.pg import migrated_db_or_skip


def test_only_spent_operational_rows_are_prunable() -> None:
    """The prunable set is closed and small — a new table is a deliberate addition, not a sweep.

    `tool_result_blobs` is the third and is the one member that holds no *record*: it is the full
    text of what a tool returned, kept so a surface can render it, and the answers it describes
    live in `calculation_results` and `job_records`. That is what makes a plain age cutoff the
    right instrument for it and the wrong one for the three tables refused below.

    `checkpoints` is the fourth, and it was missing for a reason worth naming: the LangGraph
    checkpoint tables are created by `AsyncPostgresSaver.setup()` rather than by a migration in
    `infra/sql`, so they are absent from the schema anybody reviews. Erasure already reached them
    (`agent/leaver.py`); nothing disposed of them, so a deployment that erased no one kept every
    turn's state forever.
    """
    assert set(_PRUNABLE) == {
        "session_events",
        "session_messages",
        "tool_result_blobs",
        "checkpoints",
    }


def test_the_audit_trail_is_never_pruned() -> None:
    """The trail is the record of who ran what, and a cleanup job may not decide to dispose of it.

    Disposal is a policy decision — which rows, how old, exported where first — and it belongs to
    whoever owns the record rather than to an age cutoff. The table must therefore be absent from
    the prunable set entirely. This guard predates the removal of the audit hash chain and outlives
    it deliberately: the chain used to be the stated reason, and without a test the removal would
    read as permission to start pruning.
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
    monkeypatch.setattr(settings, "retention_checkpoints_days", 30)
    assert _window_days("session_events") == 7
    assert _window_days("session_messages") == 365
    # Turn state is not the conversation and does not age with it: the transcript is a durable
    # record kept for years, while a checkpoint is what a suspended turn resumes from and is dead
    # weight long before that.
    assert _window_days("checkpoints") == 30


# --- D-145: an age cutoff alone cannot dispose of a conversation row ---------------------------


def _call(call_id: str) -> dict[str, Any]:
    """A stored assistant row with one tool call — MAF-shaped, because a real table holds those.

    Seeded as a *legacy* row on purpose: the pairing rule's whole job here is to protect pairs, and
    only rows written before M6's conversion contain any. A row the projection writes today is
    plain user text and an answer, with no call to strand.
    """
    return legacy_call(call_id, "predict_pka")


def _result(call_id: str) -> dict[str, Any]:
    """The stored tool row answering `call_id`, MAF-shaped for the same reason."""
    return legacy_result(call_id)


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
                        (400, _call("c1")),
                        # The answer arrived inside the window — the pair straddles the cutoff.
                        (1, _result("c1")),
                    ):
                        await cur.execute(
                            "INSERT INTO session_messages (session_id, message, created_at) "
                            "VALUES (%s, %s, now() - make_interval(days => %s))",
                            (session_id, Jsonb(message), age_days),
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
            # Converted rather than read as MAF objects: the assertion is about pairing, which is
            # a LangChain-message question now, and `to_langchain` is the same conversion the read
            # path applies to a legacy row.
            surviving = [to_langchain(row[1]) for row in rows]
            return [int(row[0]) for row in rows], unmatched_result_ids(surviving)
        finally:
            monkeypatch.undo()

    ids, stranded = asyncio.run(_run())
    assert len(ids) == 2, (
        "retention split a tool-call pairing across the cutoff; the surviving half is unusable"
    )
    assert stranded == set(), f"retention stranded {stranded} — the session is now bricked"


def test_a_session_holding_both_stored_shapes_is_pruned_rather_than_crashing() -> None:
    """The sweep reads a table mid-migration, which a rollout is in for as long as it takes.

    M6's conversion pass is resumable and a rollout is not atomic, so one session's rows can be
    part MAF and part LangChain. The reader was MAF's `Message.from_dict`, which does not merely
    mis-read a LangChain payload — it raises `TypeError: unexpected keyword argument 'data'`. So
    the activity failed, Temporal retried it to exhaustion, and retention stopped entirely for
    every session that had taken a turn since the conversion: the sessions still in use, which are
    exactly the ones a retention window is for.

    Loud in the logs and invisible in effect, which is the combination that makes it survive — the
    job reports failure, the table quietly stops shrinking.
    """

    async def _run() -> list[str]:
        await migrated_db_or_skip()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(settings, "retention_session_messages_days", 365)
        monkeypatch.setattr(settings, "retention_session_events_days", 0)
        try:
            session_id = "m13-mixed-shapes"
            async with db.connection(settings.postgres_dsn) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM session_messages WHERE session_id = %s", (session_id,)
                    )
                    for shape, message, age_days in (
                        ("maf", legacy_text("user", "an old question"), 400),
                        ("langchain", message_to_dict(HumanMessage(content="a new one")), 1),
                    ):
                        await cur.execute(
                            "INSERT INTO session_messages "
                            "(session_id, message, message_shape, created_at) "
                            "VALUES (%s, %s, %s, now() - make_interval(days => %s))",
                            (session_id, Jsonb(message), shape, age_days),
                        )
                await conn.commit()

            await prune_expired_rows()

            async with db.connection(settings.postgres_dsn) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT message_shape FROM session_messages "
                        "WHERE session_id = %s ORDER BY id",
                        (session_id,),
                    )
                    return [str(row[0]) for row in await cur.fetchall()]
        finally:
            monkeypatch.undo()

    # The expired legacy row went; the recent converted one stayed. Neither held a tool call, so
    # the pairing rule had nothing to protect and the age cutoff decided alone — which is the
    # ordinary case, and the one that used to raise before reaching any of that.
    assert asyncio.run(_run()) == ["langchain"]


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
                    for message in (_call("c9"), _result("c9")):
                        await cur.execute(
                            "INSERT INTO session_messages (session_id, message, created_at) "
                            "VALUES (%s, %s, now() - make_interval(days => 400))",
                            (session_id, Jsonb(message)),
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
    never consumed by construction, so retention quietly removed the integrity alerts.
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
            message = legacy_text("user", "old")
            for index in range(count):
                await cur.execute(
                    "INSERT INTO session_messages (session_id, message, created_at) "
                    "VALUES (%s, %s, now() - make_interval(days => 400))",
                    (f"{prefix}{index:03d}", Jsonb(message)),
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


def test_a_failed_table_does_not_starve_the_tables_after_it() -> None:
    """The outer per-table loop's own version of the fix above.

    `_PRUNABLE` iterates `session_events`, `session_messages`, `tool_result_blobs`, `checkpoints` in
    that order, and a `session_messages` failure used to propagate straight out of
    `prune_expired_rows` before `tool_result_blobs` was ever reached — so a persistent problem
    confined to one table stopped every table after it from being pruned at all, on every retry.
    Injected the same way as the test above (`droppable_rows` failing on the fourth call), but this
    one asserts on the table that comes *after* the one that fails.
    """

    async def _run() -> int:
        await migrated_db_or_skip()
        await _seed_expired_sessions(6, "sweep-order-")
        async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM tool_result_blobs WHERE content_hash = %s", ("sweep-order-test",)
            )
            await cur.execute(
                "INSERT INTO tool_result_blobs (content_hash, byte_size, data, created_at) "
                "VALUES (%s, %s, %s, now() - make_interval(days => 400))",
                ("sweep-order-test", 1, b"x"),
            )
            await conn.commit()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(settings, "retention_session_messages_days", 365)
        monkeypatch.setattr(settings, "retention_session_events_days", 0)
        monkeypatch.setattr(settings, "retention_tool_results_days", 365)
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
        finally:
            monkeypatch.undo()
        async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM tool_result_blobs WHERE content_hash = %s",
                ("sweep-order-test",),
            )
            row = await cur.fetchone()
        return int(row[0]) if row else -1

    remaining = asyncio.run(_run())
    assert remaining == 0, (
        "tool_result_blobs comes after session_messages in _PRUNABLE; a failure in the earlier "
        "table must not stop the later one from being pruned in the same pass"
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


# --- The LangGraph checkpoint tables: pruned by thread, skipped when absent --------------------


async def _create_checkpoint_tables() -> None:
    """Create the checkpointer's own tables in the test schema.

    Run here rather than through `AsyncPostgresSaver.setup()` because that opens a pool of its own
    against `settings.postgres_dsn` and would land outside the session fixture's isolation schema.
    The statements are the saver's own, so the shape under test is the shape production has.
    """
    from langgraph.checkpoint.postgres import base

    async with db.connection(settings.postgres_dsn) as conn:
        for statement in base.MIGRATIONS[1:4]:
            await conn.execute(statement)
        await conn.commit()


async def _seed_thread(thread_id: str, *, age_days: int) -> None:
    """One thread with a single checkpoint of the given age, plus its blob and write rows."""
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO checkpoints "
                "(thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata) "
                "VALUES (%s, '', 'ckpt-1', %s, '{}'::jsonb)",
                (
                    thread_id,
                    Jsonb({"v": 1, "id": "ckpt-1", "ts": f"__ts_{age_days}__"}),
                ),
            )
            # The payload's `ts` is what dates a checkpoint, and it has to be a real timestamp
            # rather than a literal — computed in SQL so the app clock and the database clock
            # cannot disagree, exactly as the sweep's own cutoff is.
            await cur.execute(
                "UPDATE checkpoints SET checkpoint = jsonb_set(checkpoint, '{ts}', "
                "to_jsonb((now() - make_interval(days => %s))::text)) WHERE thread_id = %s",
                (age_days, thread_id),
            )
            await cur.execute(
                "INSERT INTO checkpoint_blobs "
                "(thread_id, checkpoint_ns, channel, version, type, blob) "
                "VALUES (%s, '', 'messages', '1', 'msgpack', %s)",
                (thread_id, b"payload"),
            )
            await cur.execute(
                "INSERT INTO checkpoint_writes "
                "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, blob) "
                "VALUES (%s, '', 'ckpt-1', 'task-1', 0, 'messages', 'msgpack', %s)",
                (thread_id, b"payload"),
            )
        await conn.commit()


async def _thread_row_counts(thread_id: str) -> dict[str, int]:
    """How many rows each checkpoint table still holds for `thread_id`."""
    counts: dict[str, int] = {}
    async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            await cur.execute(
                f"SELECT count(*) FROM {table} WHERE thread_id = %s",
                (thread_id,),
            )
            row = await cur.fetchone()
            counts[table] = int(row[0]) if row else 0
    return counts


def test_an_expired_thread_leaves_none_of_its_three_tables_behind() -> None:
    """A thread past its window goes whole; a live one is untouched.

    All three tables, because they are one thread's state split across three keys with no foreign
    key to enforce it: a sweep that removed `checkpoints` and left the blobs behind would report
    success while the rows it was built to bound kept growing, and nothing downstream would notice —
    `checkpoint_blobs` is the one that actually holds the payload.

    The live thread is in the assertion for the reason every retention test here carries its
    counter-example: a cutoff that deletes everything is not a retention policy, and a `HAVING
    max(...)` that was wrong in the other direction would pass a test that only looked at the
    expired thread.
    """

    async def _run() -> tuple[dict[str, int], dict[str, int], RetentionOutcome]:
        await migrated_db_or_skip()
        await _create_checkpoint_tables()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(settings, "retention_checkpoints_days", 30)
        monkeypatch.setattr(settings, "retention_session_messages_days", 0)
        monkeypatch.setattr(settings, "retention_session_events_days", 0)
        try:
            await _seed_thread("retention-old-thread", age_days=90)
            await _seed_thread("retention-live-thread", age_days=1)

            outcome = await prune_expired_rows()

            return (
                await _thread_row_counts("retention-old-thread"),
                await _thread_row_counts("retention-live-thread"),
                outcome,
            )
        finally:
            monkeypatch.undo()

    expired, live, outcome = asyncio.run(_run())

    assert expired == {"checkpoints": 0, "checkpoint_blobs": 0, "checkpoint_writes": 0}, (
        f"the expired thread left rows behind: {expired}"
    )
    assert live == {"checkpoints": 1, "checkpoint_blobs": 1, "checkpoint_writes": 1}, (
        f"a thread inside its window was pruned: {live}"
    )
    assert outcome.deleted["checkpoint_blobs"] == 1, (
        f"the pass did not report what it removed per table: {outcome.deleted}"
    )


def test_the_checkpoint_pass_says_how_many_threads_it_left() -> None:
    """A capped checkpoint pass reports its tail, for the reason the conversation pass does.

    Without it, a first pass against a deployment with a large backlog returns exactly the cap as
    its deleted count and an empty `skipped` — indistinguishable from a pass that drained the table,
    while the growth this sweep exists to bound continues.
    """

    async def _run() -> tuple[RetentionOutcome, int]:
        await migrated_db_or_skip()
        await _create_checkpoint_tables()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(settings, "retention_checkpoints_days", 30)
        monkeypatch.setattr(settings, "retention_session_messages_days", 0)
        monkeypatch.setattr(settings, "retention_session_events_days", 0)
        monkeypatch.setattr(settings, "retention_max_sessions_per_pass", 2)
        try:
            for index in range(5):
                await _seed_thread(f"retention-capped-{index}", age_days=90)
            outcome = await prune_expired_rows()
            surviving = 0
            for index in range(5):
                surviving += (await _thread_row_counts(f"retention-capped-{index}"))["checkpoints"]
            return outcome, surviving
        finally:
            monkeypatch.undo()

    outcome, surviving = asyncio.run(_run())

    assert outcome.deleted["checkpoints"] == 2, (
        f"the pass worked more threads than its cap allowed: {outcome.deleted}"
    )
    assert surviving == 3, f"{surviving} of 5 seeded threads survive, expected 3"
    assert outcome.threads_deferred > 0, (
        "the pass stopped at its cap and reported nothing left, which reads as a bounded table"
    )


def test_a_schema_with_no_checkpointer_is_skipped_rather_than_failed() -> None:
    """The absent-tables case is every deployment that has never run the graph engine.

    Raising there would be worse than not pruning: `prune_expired_rows` would fail the whole
    activity, Temporal would retry it to exhaustion, and the three tables the sweep *can* handle
    would stop being pruned too — a missing checkpointer silently disabling retention for
    everything else.

    **The drop is schema-qualified, and an unqualified one destroyed the application's tables.**
    `tests/pg.py` isolates every test table behind `search_path={isolation},public`, so a bare
    `DROP TABLE IF EXISTS checkpoints` resolves to the *first* match — and this test does not create
    the tables it drops. Run after `test_an_expired_thread_leaves_none_of_its_three_tables_behind`
    there is an isolation-schema copy to hit; run alone there is not, and the statement reaches
    **`public.checkpoints`**: the running deployment's turn state, dropped by a unit test.

    That is not hypothetical. It wedged this repository's own dev stack twice on 2026-08-11–12 —
    the second time irrecoverably, because `AsyncPostgresSaver.setup()` is idempotent against
    `checkpoint_migrations` and that table survived, so every later turn died with
    `UndefinedTable: relation "checkpoints" does not exist` and nothing could repair it. A live
    concurrency sweep read 0 accepted turns at every admission cap before the cause was found.

    So the drop names `current_schema()` explicitly and can no longer reach `public`. The cost is
    that on a database where the application *has* run, `public` still shadows the now-absent
    isolation copies and the sweep correctly reports a checkpointer it can see — which is not this
    test's subject, so it skips saying so rather than failing.
    """

    async def _run() -> RetentionOutcome | None:
        await migrated_db_or_skip()
        async with db.connection(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT current_schema()")
                row = await cur.fetchone()
            schema = str(row[0]) if row else "public"
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                await conn.execute(f'DROP TABLE IF EXISTS "{schema}".{table}')
            await conn.commit()
            # Still visible means `public` holds a real checkpointer this test must not touch.
            async with conn.cursor() as cur:
                await cur.execute("SELECT to_regclass('checkpoints')")
                shadowed = await cur.fetchone()
            if shadowed is not None and shadowed[0] is not None:
                return None
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(settings, "retention_checkpoints_days", 30)
        monkeypatch.setattr(settings, "retention_session_events_days", 7)
        try:
            return await prune_expired_rows()
        finally:
            monkeypatch.undo()

    outcome = asyncio.run(_run())
    if outcome is None:
        pytest.skip(
            "a checkpointer exists in `public` (this database has run the application), so the "
            "absent-schema case cannot be produced without dropping tables this test does not own"
        )

    assert any("no checkpointer" in reason for reason in outcome.skipped), (
        f"the missing tables were not reported: {outcome.skipped}"
    )
    assert "session_events" in outcome.deleted, (
        "a missing checkpointer stopped the sweep reaching the tables it can prune"
    )


# --- The thread query's LIMIT has to bound the scan, not just the answer ------------------------

# The shape the plan assertions are measured against. Enough threads that a `HashAggregate` over
# every group is what the planner reaches for on a table with no statistics — which is the state
# `checkpoints` is in until autovacuum first analyzes it, because `AsyncPostgresSaver.setup()`
# creates it outside `infra/sql` and a first retention pass can easily arrive before that. Below
# roughly this size Postgres happens to pick a streaming plan for the old query too, and the defect
# hides.
_SCAN_THREADS = 2000
_SCAN_CHECKPOINTS_PER_THREAD = 5
# One thread in ten is *still in use*: its oldest checkpoints are past the cutoff while its newest
# is not. The walk has to skip those without reading past its `LIMIT`, so they belong in the fixture
# the plan is measured on, not only in the behavioural test.
_SCAN_LIVE_EVERY = 10
# Far below the seeded thread count, so "the scan stopped early" is a difference of two orders of
# magnitude rather than a rounding one.
_SCAN_CAP = 20


async def _seed_checkpoint_threads(threads: int, per_thread: int, live_every: int) -> None:
    """Bulk-seed `threads` threads of `per_thread` checkpoints each, in one statement.

    Every checkpoint is 400 days old except the last one of every `live_every`-th thread, which is a
    day old — so that thread holds expired checkpoints and is not itself expired. One
    `INSERT … SELECT` rather than a Python loop because the plan under test only becomes the
    pathological one at a few thousand threads, and ten thousand round trips would dominate the
    suite's runtime.

    The `ts` is written in the ISO-8601 form `create_checkpoint` actually produces
    (`2026-08-17T09:00:00.000000+00:00`), not Postgres's own `::text` rendering, so what the sweep
    parses here is what it parses in production.

    Clears all three tables first: the assertions below are about a *global* scan bound, so a thread
    another test left behind changes the number.
    """
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                await cur.execute(f"DELETE FROM {table}")
            await cur.execute(
                "INSERT INTO checkpoints "
                "(thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata) "
                "SELECT 'retention-scan-' || to_char(t, 'FM000000'), '', 'ckpt-' || c, "
                "       jsonb_build_object('v', 1, 'id', 'ckpt-' || c, 'ts', "
                "           to_char((now() - make_interval(days => "
                "               CASE WHEN t %% %s = 0 AND c = %s THEN 1 ELSE 400 END)) "
                "               AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US+00:00')), "
                "       '{}'::jsonb "
                "FROM generate_series(1, %s) t, generate_series(1, %s) c",
                (live_every, per_thread, threads, per_thread),
            )
        await conn.commit()


async def _clear_checkpoint_tables() -> None:
    """Empty all three checkpoint tables, so a bulk fixture cannot leak into another test."""
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                await cur.execute(f"DELETE FROM {table}")
        await conn.commit()


async def _plan_of(sql: str, params: tuple[object, ...]) -> dict[str, Any]:
    """The executed plan tree of `sql`, as `EXPLAIN (ANALYZE, FORMAT JSON)` reports it.

    `ANALYZE` rather than a cost-only explain because the claim under test is about what the
    statement *did* — how many rows the scan actually touched — and an estimate is exactly the thing
    that was wrong about the old query.
    """
    async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
        await cur.execute("EXPLAIN (ANALYZE, FORMAT JSON, COSTS OFF) " + sql, params)
        row = await cur.fetchone()
    assert row is not None
    plan: dict[str, Any] = row[0][0]["Plan"]
    return plan


def _plan_nodes(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Every node of a plan tree, parents before children."""
    nodes = [plan]
    for child in plan.get("Plans", []):
        nodes.extend(_plan_nodes(child))
    return nodes


def _rows_examined(plan: dict[str, Any]) -> int:
    """How many rows the plan's scan nodes actually read, filtered-out rows included.

    `Actual Rows` alone would undercount: a `Seq Scan` that discards everything reports zero rows
    out while having read the whole table, which is precisely the cost this fix is about.
    """
    return sum(
        (node["Actual Rows"] + node.get("Rows Removed by Filter", 0)) * node["Actual Loops"]
        for node in _plan_nodes(plan)
        if node["Node Type"].endswith("Scan")
    )


def test_the_thread_query_lets_the_limit_bound_the_scan() -> None:
    """The defect: the old `LIMIT` bounded the deletes and not the work that produced them.

    `SELECT thread_id FROM checkpoints GROUP BY thread_id HAVING max(...) < cutoff ORDER BY
    thread_id LIMIT n` plans, on a real table, as `Seq Scan → HashAggregate → Sort → Limit`: every
    row read and every group built before the `LIMIT` can discard one. Work grows with the table and
    not with the cap, so past the size where it exceeds `pg_statement_timeout_seconds` the activity
    is cancelled, retried by Temporal, cancelled again, and the table it exists to bound grows
    forever with a timeout as the only symptom.

    Three properties, each of which the old statement fails:

    * **nothing sequentially scans `checkpoints`** — every access is through `checkpoints_pkey`, so
      the cost of a pass is a number of index probes rather than a fraction of the table;
    * **the recursion stops at the cap** — the walk enumerates barely more threads than it was asked
      for, out of a table holding two orders of magnitude more;
    * **the rows actually read are a small fraction of the table**, which is the plain statement of
      "the `LIMIT` bounded the scan".

    Asserted on measured row counts and on the absence of a sequential scan rather than on an exact
    plan shape, because the point is that this statement has only one plan available to it — which
    is itself the fix. `DISTINCT` was tried first and rejected for failing exactly here: measured,
    it plans `Limit → Unique → Index Scan` on a table with statistics and
    `Seq Scan → HashAggregate → Sort → Limit` on the same table without them.
    """

    async def _run() -> tuple[dict[str, Any], list[str]]:
        await migrated_db_or_skip()
        await _create_checkpoint_tables()
        try:
            await _seed_checkpoint_threads(
                _SCAN_THREADS, _SCAN_CHECKPOINTS_PER_THREAD, _SCAN_LIVE_EVERY
            )
            plan = await _plan_of(_EXPIRED_THREADS, (30, _SCAN_CAP + 1))
            async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
                await cur.execute(_EXPIRED_THREADS, (30, _SCAN_CAP + 1))
                threads = [str(row[0]) for row in await cur.fetchall()]
            return plan, threads
        finally:
            await _clear_checkpoint_tables()

    plan, threads = asyncio.run(_run())
    seeded_rows = _SCAN_THREADS * _SCAN_CHECKPOINTS_PER_THREAD
    nodes = _plan_nodes(plan)

    assert len(threads) == _SCAN_CAP + 1, (
        "the thread query must return one over the cap, so the pass can tell a drained backlog "
        f"from a capped one; got {len(threads)}"
    )

    sequential = [node["Node Type"] for node in nodes if node["Node Type"] == "Seq Scan"]
    assert sequential == [], (
        "the thread query sequentially scans `checkpoints`; its cost is then the table's size, "
        "which is what makes a first pass on a large one time out for ever"
    )

    walked = [node["Actual Rows"] for node in nodes if node["Node Type"] == "Recursive Union"]
    assert walked and max(walked) <= _SCAN_CAP + 3, (
        f"the walk enumerated {walked} threads of {_SCAN_THREADS} to answer for {_SCAN_CAP + 1}; "
        "the LIMIT is bounding the answer, not the scan"
    )

    examined = _rows_examined(plan)
    assert examined < seeded_rows // 10, (
        f"the query read {examined} of {seeded_rows} rows for {_SCAN_CAP + 1} threads — "
        "the LIMIT is bounding the answer, not the scan"
    )


async def _seed_thread_with_ages(thread_id: str, ages: tuple[int, ...]) -> None:
    """One thread holding a checkpoint at each of `ages` (in days), plus its blob and write rows.

    The multi-checkpoint counterpart of `_seed_thread`: a thread whose *oldest* checkpoints have
    expired while its newest has not cannot be expressed with one row, and it is the only shape that
    separates "has an expired checkpoint" from "is finished with".
    """
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            for index, age_days in enumerate(ages):
                await cur.execute(
                    "INSERT INTO checkpoints "
                    "(thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata) "
                    "VALUES (%s, '', %s, jsonb_build_object('v', 1, 'id', %s::text, 'ts', "
                    "    to_char((now() - make_interval(days => %s::int)) AT TIME ZONE 'UTC', "
                    "            'YYYY-MM-DD\"T\"HH24:MI:SS.US+00:00')), '{}'::jsonb)",
                    (thread_id, f"ckpt-{index}", f"ckpt-{index}", age_days),
                )
            await cur.execute(
                "INSERT INTO checkpoint_blobs "
                "(thread_id, checkpoint_ns, channel, version, type, blob) "
                "VALUES (%s, '', 'messages', '1', 'msgpack', %s)",
                (thread_id, b"payload"),
            )
            await cur.execute(
                "INSERT INTO checkpoint_writes "
                "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, blob) "
                "VALUES (%s, '', 'ckpt-0', 'task-1', 0, 'messages', 'msgpack', %s)",
                (thread_id, b"payload"),
            )
        await conn.commit()


def test_a_thread_whose_oldest_checkpoints_expired_but_is_still_in_use_is_not_deleted() -> None:
    """The bound had to be bought without changing what "expired" means.

    The cheap way to bound the scan is to ask a per-row question — "which threads hold a checkpoint
    older than the cutoff" — and take the first `n` answers. That set includes every conversation
    resumed across the window, old checkpoints plus recent ones, and deleting on it would destroy
    the turn state of exactly the threads still in daily use, silently, because nothing reads a
    checkpoint until someone resumes the conversation and finds no state.

    So the walk asks `max(ts) < cutoff` of each thread it visits rather than "does this thread hold
    an expired checkpoint", and this is the test that it still does. The straddling thread
    deliberately gets a lower-sorting id than the fully expired one, so the walk reaches it first.
    """

    async def _run() -> tuple[dict[str, int], dict[str, int], RetentionOutcome]:
        await migrated_db_or_skip()
        await _create_checkpoint_tables()
        await _clear_checkpoint_tables()
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(settings, "retention_checkpoints_days", 30)
        monkeypatch.setattr(settings, "retention_session_messages_days", 0)
        monkeypatch.setattr(settings, "retention_session_events_days", 0)
        try:
            # Sorts first, so a pass that trusted the candidate query would delete it first.
            await _seed_thread_with_ages("retention-a-resumed", (400, 380, 1))
            await _seed_thread_with_ages("retention-b-finished", (400, 380))

            outcome = await prune_expired_rows()

            return (
                await _thread_row_counts("retention-a-resumed"),
                await _thread_row_counts("retention-b-finished"),
                outcome,
            )
        finally:
            monkeypatch.undo()
            await _clear_checkpoint_tables()

    resumed, finished, outcome = asyncio.run(_run())

    assert resumed == {"checkpoints": 3, "checkpoint_blobs": 1, "checkpoint_writes": 1}, (
        "a conversation resumed inside the window was pruned because its *older* checkpoints had "
        f"expired; its turn state is now gone: {resumed}"
    )
    assert finished == {"checkpoints": 0, "checkpoint_blobs": 0, "checkpoint_writes": 0}, (
        f"the finished thread was not disposed of: {finished}"
    )
    assert outcome.deleted["checkpoints"] == 2, (
        f"exactly the finished thread's two checkpoints should have gone: {outcome.deleted}"
    )
