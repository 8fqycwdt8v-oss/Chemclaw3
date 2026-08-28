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

import psycopg
import pytest
from langchain_core.messages import HumanMessage, message_to_dict
from psycopg.types.json import Jsonb

from chemclaw.agent.checkpointer import CHECKPOINT_TABLES
from chemclaw.agent.leaver import _RETAINED as leaver_retained
from chemclaw.agent.message_migration import to_langchain
from chemclaw.agent.message_pairing import droppable_rows, unmatched_result_ids
from chemclaw.agent.scratchpad import STORE_TABLES
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.durable import retention
from chemclaw.durable.retention import (
    _ANALYZE_THREADS,
    _EXPIRED_THREADS,
    _NOT_PRUNED,
    _PRUNABLE,
    _SESSION_SCOPED_ROWS,
    RetentionOutcome,
    _window_days,
    prune_expired_rows,
)
from tests.legacy_rows import legacy_call, legacy_result, legacy_text
from tests.pg import migrated_db_or_skip

# The same reader `tests/test_schema_inventory.py` pins `infra/sql/README.md` with. Imported rather
# than re-implemented: a second regex over the migrations would be a second answer to "what tables
# exist", and the two would drift in exactly the direction that makes an exhaustiveness check pass
# while going blind.
from tests.test_schema_inventory import tables_on_disk


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

    `result_publications` is the fifth and joins on the same test as `tool_result_blobs`: it holds
    no record of its own. A *delivered* row is a receipt for a result that now lives both in
    `calculation_results` and in an external results store, so pruning it loses nothing. Its
    predicate is what keeps that true — a `pending` or `failed` row is the only record that
    something has **not** been published, and sweeping it on a clock would turn a results-store
    outage into a silent gap.

    `session_owners` is the sixth and the only member whose disposal is not about the row's own age
    at all: it is the row that makes a session reopenable, so it is pruned behind everything it
    keys and only when nothing holds a row for the session
    (`D-2026-08-27-a-session-nobody-can-reopen-is-disposable`). It is in this set because it does
    have a window — the floor under "how long may an empty session live" — and not in `_NOT_PRUNED`
    because something now bounds it.
    """
    assert set(_PRUNABLE) == {
        "session_events",
        "session_messages",
        "tool_result_blobs",
        "result_publications",
        "checkpoints",
        "session_owners",
    }


def test_the_ownership_row_is_the_last_table_the_sweep_touches() -> None:
    """Order in `_PRUNABLE` is load-bearing, so it is asserted rather than commented.

    Every session-scoped sweep in this system starts from `session_owners` — `leaver.erase_actor`
    selects session ids out of it and `session_store.delete_session` deletes one session by it — so
    an ownership row disposed of *before* the tables it keys puts their rows beyond both. The sweep
    iterates `_PRUNABLE` in insertion order, which makes the position of this entry the whole
    protection: moving it up would strand rows silently, and a comment cannot fail.
    """
    assert list(_PRUNABLE)[-1] == "session_owners"


def test_the_reachability_guard_names_every_session_scoped_erasure_table() -> None:
    """What must be gone before an ownership row may go is derived from erasure, not transcribed.

    `_SESSION_SCOPED_ROWS` is the set of tables whose rows are reachable *only* through the
    ownership row. The authoritative answer to "which tables hold one session's rows" already
    exists — `session_store._session_delete_statements()`, itself derived from `leaver._ERASE` — so
    this asserts the two agree instead of letting a table added to the erasure sweep be silently
    outlived by the row that finds it.

    Two entries differ by name for a stated reason, and both are checked here rather than trusted:
    the guard reads `tool_result_links` where the erasure deletes `tool_result_blobs` (the link is
    the session-scoped row; a blob is content-addressed and may belong to another session too), and
    `session_turns` is not a guard at all — it is swept *with* the ownership row, so it appears in
    the delete rather than in the anti-joins.
    """
    from chemclaw.agent.session_store import _session_delete_statements

    erasable = {table for table, _statement in _session_delete_statements()}
    # `tool_result_blobs` and `tool_result_links` are the same fact seen from either side: the
    # erasure deletes the blob and the link cascades behind it, because the app role has no DELETE
    # on the link table at all (`infra/sql/grants/app_privileges.sql`).
    seen_from_the_guard = {"tool_result_blobs": "tool_result_links"}
    expected = {seen_from_the_guard.get(table, table) for table in erasable} - {
        # Swept *with* the ownership row rather than guarding it — they are what is deleted.
        "session_turns",
        "session_owners",
    }
    assert set(_SESSION_SCOPED_ROWS) == expected, (
        "the reachability guard and the erasure sweep disagree about which tables hold one "
        f"session's rows: guard-only {sorted(set(_SESSION_SCOPED_ROWS) - expected)}, "
        f"erasure-only {sorted(expected - set(_SESSION_SCOPED_ROWS))}"
    )


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
    an age cutoff could quietly re-run an expensive conformer search.
    """
    assert "calculation_results" not in _PRUNABLE


def test_a_campaign_is_a_record_and_is_never_pruned() -> None:
    """`bo_campaigns`/`bo_suggestions` are refused, and erasure is what settles it.

    A suggestion row snapshots the candidates, the observations they were drawn from and the
    decision space they were drawn in, and migration 031 states the invariant plainly: "the
    sequence *is* the campaign's history". Both tables are append-only, so they grow on every
    campaign ask — which is what makes the *silence* the defect and not the absence of pruning.

    The argument that decides it is already merged one module over. `agent/leaver.py`'s `_RETAINED`
    tier keeps both through a data-subject erasure request, beside `audit_events` and `job_records`
    — the two tables the guards above refuse. A retention clock may not dispose of what an erasure
    request does not, so this asserts the two facts together: a change that started pruning these
    would have to take them out of `_RETAINED` first, and that is a decision with an owner.
    """
    assert "bo_campaigns" not in _PRUNABLE
    assert "bo_suggestions" not in _PRUNABLE
    retained = {table for table, _columns, _why in leaver_retained}
    assert {"bo_campaigns", "bo_suggestions"} <= retained
    assert {"audit_events", "job_records"} <= retained


def test_every_table_in_the_schema_has_a_disposal_decision() -> None:
    """The register is exhaustive over the schema — the check the docstring's claim never had.

    **This is the actual defect `bo_campaigns` exposed.** The module docstring enumerates what the
    sweep prunes and what it refuses and reads as though that were the whole schema; measured, it
    named three refusals against thirty-three tables it does not prune, so thirty had no disposal
    decision anywhere a reader or a test could reach one. Nothing checked the list because nothing
    could: it was prose. `_NOT_PRUNED` makes it data, and this makes it true.

    Both directions, for the reason `tests/test_schema_inventory.py` gives about the same schema: a
    table with no entry is a table whose growth nobody decided, and an entry with no table is a
    decision outliving what it describes — the direction the archived storage inventory decayed in.

    The expected set is **derived, never transcribed**. `tables_on_disk` is the same reader
    `test_schema_inventory` pins the `infra/sql/README.md` inventory with, so a new migration lands
    here automatically; `CHECKPOINT_TABLES` and `STORE_TABLES` are the first-party constants naming
    what upstream's `setup()` creates outside `infra/sql`, which is exactly the set that "appears in
    no schema review" (`infra/sql/README.md`) and went undisposed for as long as it existed.

    Only the **keys** are asserted. The reason strings are judgements, and a test over them would be
    a second copy of the answer — the split `infra/sql/README.md` already draws over its own
    Disposal column.
    """
    schema = tables_on_disk() | set(CHECKPOINT_TABLES) | set(STORE_TABLES)
    accounted = set(_PRUNABLE) | set(_NOT_PRUNED)
    assert schema - accounted == set(), (
        f"tables with no disposal decision in durable/retention.py: {sorted(schema - accounted)}. "
        "Add each to _PRUNABLE (with its window) or to _NOT_PRUNED (with what bounds it instead, "
        "or that nothing does) in the same commit as the migration"
    )
    assert accounted - schema == set(), (
        "durable/retention.py records a disposal decision for tables that do not exist: "
        f"{sorted(accounted - schema)}"
    )


def test_no_table_is_both_pruned_and_refused() -> None:
    """Guard the guard: the two registers must partition, not merely cover.

    Without this, the exhaustiveness check above passes for a table listed in both — and the
    contradiction it would be papering over is the dangerous direction. `_NOT_PRUNED`'s entry is
    what a reviewer reads to conclude a table is safe from the sweep, while `_PRUNABLE` is what the
    sweep actually executes, so a table in both reads as refused and is deleted.
    """
    overlap = set(_PRUNABLE) & set(_NOT_PRUNED)
    assert not overlap, f"tables in both _PRUNABLE and _NOT_PRUNED: {sorted(overlap)}"


def test_there_are_tables_to_account_for() -> None:
    """Guard the guard: an empty schema read would make the exhaustiveness check vacuous.

    The same shape `test_schema_inventory.test_there_are_tables_to_inventory` uses, and for the
    same reason — this repository has hit the vacuous-pass failure repeatedly, most recently a
    migration reader that globbed the wrong directory and applied zero files without failing.
    """
    assert len(tables_on_disk()) > 20


def test_every_disposal_decision_states_a_reason() -> None:
    """An entry with an empty reason is the blank this register exists to replace.

    `_NOT_PRUNED` is only worth more than a set of names because each entry says *why*; a caller
    could satisfy the exhaustiveness check above with `""` and reintroduce the silence while the
    test stayed green.
    """
    blank = [table for table, why in _NOT_PRUNED.items() if not why.strip()]
    assert not blank, f"_NOT_PRUNED entries with no stated reason: {sorted(blank)}"


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
    # The ownership row takes the conversation's window deliberately, as a floor rather than as the
    # thing that decides disposal: a session may not be forgotten sooner than the conversation in
    # it would have been, and the guards — not the clock — are what hold a row that still has rows.
    assert _window_days("session_owners") == 365


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
    `job_completed` that outlived the window was destroyed before anyone read it: a long search
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

    `_PRUNABLE` iterates `session_events`, `session_messages`, `tool_result_blobs`, `checkpoints`
    and `session_owners` in that order, and a `session_messages` failure used to propagate straight
    out of `prune_expired_rows` before `tool_result_blobs` was ever reached — so a persistent
    problem confined to one table stopped every table after it from being pruned, on every retry.
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
    and says *that* it left something, because a cap that is not reported reads as "there was
    nothing more" and a growing table would look bounded in every result this job returns. The
    figure is a probe rather than a remainder — one row is selected over the cap and no more, so it
    is 0 or 1 — because a true count is a second whole-table aggregate (`RetentionOutcome`).
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


def test_the_checkpoint_pass_says_that_it_left_threads_behind() -> None:
    """A capped checkpoint pass reports its tail, for the reason the conversation pass does.

    Without it, a first pass against a deployment with a large backlog returns exactly the cap as
    its deleted count and an empty `skipped` — indistinguishable from a pass that drained the table,
    while the growth this sweep exists to bound continues.

    *That* a tail exists, not how long it is: `_EXPIRED_THREADS` is asked for exactly one row over
    the cap, so `threads_deferred` is 0 or 1 by construction and the assertion below is written as
    the boolean it really is.
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


# --- The thread query has to stream `checkpoints_pkey`, in both backlog shapes ------------------

# The shape the plan assertions are measured against. Enough threads that a `HashAggregate` over
# every group is what the planner reaches for on a table with no statistics — which is the state
# `checkpoints` is in until autovacuum first analyzes it, because `AsyncPostgresSaver.setup()`
# creates it outside `infra/sql` and a first retention pass can easily arrive before that. Below
# roughly this size Postgres happens to pick a streaming plan even unanalyzed, and the defect hides.
_SCAN_THREADS = 2000
_SCAN_CHECKPOINTS_PER_THREAD = 5
# Far below the seeded thread count, so "the scan stopped early" is a difference of two orders of
# magnitude rather than a rounding one.
_SCAN_CAP = 20

# The two backlog shapes the sweep actually meets, as `live_every` values for the bulk fixture: one
# thread in `live_every` is *still in use* (its oldest checkpoints are past the cutoff, its newest
# is not), so `10` is a first pass against a table nobody has ever pruned and `1` is every pass
# after it.
#
# `_SCAN_SPARSE` is the one that matters and the one the previous version of this file did not
# have. Retention runs daily, so after the first pass the expired threads are a *minority* scattered
# anywhere in `thread_id` order — and a `LIMIT` cannot bound the scan there, because finding a
# minority means visiting everyone. Asserting a cap-shaped bound on the dense fixture alone was a
# test of one shape claiming a general property: it passed a `WITH RECURSIVE` walk that, on this
# same fixture made sparse, enumerated all 2 001 threads to answer for 21 and read 26 003 rows of a
# 10 000-row table.
_SCAN_DENSE_LIVE_EVERY = 10
_SCAN_SPARSE_LIVE_EVERY = 1


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


# The plan nodes that mean "this statement materialised the whole table before answering". Their
# absence is the property under test: with statistics, `GROUP BY thread_id ORDER BY thread_id`
# matches `checkpoints_pkey`'s leading column, so the answer is produced by streaming the index and
# the `LIMIT` terminates it — no hash table over every group, no sort of every group, no seq scan.
_MATERIALISING_NODES = ("Seq Scan", "HashAggregate", "Sort")


@pytest.mark.parametrize(
    ("live_every", "expected_threads"),
    [
        pytest.param(_SCAN_DENSE_LIVE_EVERY, _SCAN_CAP + 1, id="dense-first-pass"),
        pytest.param(_SCAN_SPARSE_LIVE_EVERY, 0, id="sparse-steady-state"),
    ],
)
def test_the_thread_query_streams_the_primary_key_in_both_backlog_shapes(
    live_every: int, expected_threads: int
) -> None:
    """One streaming pass over `checkpoints_pkey`, whether the backlog is dense or drained.

    **The property, and why it is this one.** A retention pass has to find the threads whose
    *newest* checkpoint has expired. When they are a scattered minority — every pass after the
    first, since this job runs daily — no statement can be bounded by the cap, because finding a
    minority means visiting everyone. So the honest bound is not "read few rows" but **"read no row
    twice"**: one ordered walk of the primary key, `max()` accumulated as it goes, the `LIMIT`
    stopping it as soon as the cap is full. That is what `Limit → GroupAggregate → Index Scan using
    checkpoints_pkey` does, and it is the plan `_EXPIRED_THREADS` gets once `_ANALYZE_THREADS` has
    run.

    Three assertions, and each of them is a measured failure of the `WITH RECURSIVE` loose index
    scan this statement was briefly replaced by:

    * **nothing materialises the table** — no `Seq Scan`, no `HashAggregate`, no `Sort`. This is the
      claim the rewrite was built on ("an aggregate must build every group before the `LIMIT` can
      discard one") and it is true only of a table with no statistics.
    * **no row is read more than once** — the walk read 26 003 rows of this 10 000-row fixture made
      sparse, because it pays a fresh index probe *plus* a correlated `max()` per thread. Measured
      at 200 000 threads that is 8 147 ms against this statement's 593 ms, and it is *cancelled*
      under a 2 s statement timeout where this completes in 618 ms.
    * **on a dense backlog the `LIMIT` still stops the scan early** — a first pass reads a small
      fraction of the table, which is the case the rewrite existed to serve and which this statement
      serves better (2.5 ms against 21.3 ms at 200 000 threads).

    The sparse arm is the one the previous version of this test lacked, and its absence is why a
    regression passed: the fixture pinned `live_every = 10`, making 90% of threads expired, so a
    statement that visits every thread still looked cap-bounded.
    """

    async def _run() -> tuple[dict[str, Any], list[str]]:
        await migrated_db_or_skip()
        await _create_checkpoint_tables()
        try:
            await _seed_checkpoint_threads(_SCAN_THREADS, _SCAN_CHECKPOINTS_PER_THREAD, live_every)
            async with db.connection(settings.postgres_dsn) as conn:
                # The statement the sweep itself runs one statement earlier, for the same reason:
                # this plan is only available to a planner that has statistics for `checkpoints`.
                await conn.execute(_ANALYZE_THREADS)
                await conn.commit()
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

    assert len(threads) == expected_threads, (
        f"the thread query returned {len(threads)} threads, expected {expected_threads}; the cap "
        "probe is how the pass tells a drained backlog from a capped one"
    )

    materialising = [
        node["Node Type"] for node in nodes if node["Node Type"] in _MATERIALISING_NODES
    ]
    assert materialising == [], (
        f"the thread query materialises `checkpoints` ({materialising}); its cost is then the "
        "table's size however small the cap, which is what makes a pass on a large one time out"
    )

    examined = _rows_examined(plan)
    assert examined <= seeded_rows, (
        f"the query read {examined} of {seeded_rows} seeded rows — more than one read per row "
        "means a probe per thread rather than one ordered pass, which is what times out when the "
        "expired threads are a scattered minority"
    )

    if expected_threads:
        assert examined < seeded_rows // 10, (
            f"the query read {examined} of {seeded_rows} rows to fill a cap of {_SCAN_CAP + 1} — "
            "on a dense backlog the LIMIT must still stop the scan early"
        )


def test_the_sweep_gives_the_planner_the_statistics_no_migration_can() -> None:
    """The one shape where `_EXPIRED_THREADS` plans badly, and the one statement that fixes it.

    `checkpoints` is created by `AsyncPostgresSaver.setup()`, outside `infra/sql`, so no migration
    ever analyzes it and a first retention pass can easily arrive before autovacuum does. With no
    statistics the planner has no idea `thread_id` holds thousands of distinct values, so it reaches
    for `Parallel Seq Scan → Partial HashAggregate → Sort → Finalize GroupAggregate` — measured at
    200 000 threads, that is 1 526 ms with 5.8 MB spilled to disk, against 2.5 ms for the identical
    statement once analyzed. Past the size where it exceeds `pg_statement_timeout_seconds` the
    activity is cancelled, retried by Temporal, cancelled again, and the table it exists to bound
    grows forever with a timeout as the only symptom.

    So this asserts both halves: that the hazard is real on a table nobody has analyzed, and that
    running the sweep removes it. The table is **dropped and recreated** rather than emptied,
    because `DELETE` leaves `pg_statistic` behind — an earlier test in this file would otherwise
    hand this one the very statistics it is meant to be missing.
    """

    async def _run() -> tuple[list[str], list[str]] | None:
        await migrated_db_or_skip()
        async with db.connection(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT current_schema()")
                row = await cur.fetchone()
            schema = str(row[0]) if row else "public"
            # Schema-qualified for the reason
            # `test_a_schema_with_no_checkpointer_is_skipped_rather_than_failed` spells out at
            # length: an unqualified drop resolves to `public` and takes the running deployment's
            # turn state with it.
            await conn.execute(f'DROP TABLE IF EXISTS "{schema}".checkpoints')
            await conn.commit()
        await _create_checkpoint_tables()
        async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
            await cur.execute("SELECT to_regclass(%s)", (f"{schema}.checkpoints",))
            recreated = await cur.fetchone()
        if recreated is None or recreated[0] is None:
            return None
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(settings, "retention_checkpoints_days", 30)
        monkeypatch.setattr(settings, "retention_session_messages_days", 0)
        monkeypatch.setattr(settings, "retention_session_events_days", 0)
        # A tiny cap so the sweep's own deletions barely change the table between the two plans.
        monkeypatch.setattr(settings, "retention_max_sessions_per_pass", 2)
        try:
            await _seed_checkpoint_threads(
                _SCAN_THREADS, _SCAN_CHECKPOINTS_PER_THREAD, _SCAN_DENSE_LIVE_EVERY
            )
            before = _plan_nodes(await _plan_of(_EXPIRED_THREADS, (30, _SCAN_CAP + 1)))
            await prune_expired_rows()
            after = _plan_nodes(await _plan_of(_EXPIRED_THREADS, (30, _SCAN_CAP + 1)))
            return (
                [node["Node Type"] for node in before],
                [node["Node Type"] for node in after],
            )
        finally:
            monkeypatch.undo()
            await _clear_checkpoint_tables()

    plans = asyncio.run(_run())
    if plans is None:
        pytest.skip(
            "the isolation schema's `checkpoints` could not be recreated, so the no-statistics "
            "state cannot be produced without touching tables this test does not own"
        )
    before, after = plans

    assert "Seq Scan" in before, (
        "this fixture no longer reproduces the unanalyzed plan, so the assertion below proves "
        f"nothing; nodes were {before}"
    )
    assert not set(after) & set(_MATERIALISING_NODES), (
        "the sweep ran and the thread query still materialises the whole table: `ANALYZE` is not "
        f"reaching `checkpoints`, nodes were {after}"
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

    So the query asks `max(ts) < cutoff` per thread rather than "does this thread hold an expired
    checkpoint", and this is the test that it still does. The straddling thread deliberately gets a
    lower-sorting id than the fully expired one, so an ordered scan reaches it first.
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


# --- The disposal rule, shape by shape ----------------------------------------------------------

# How far inside the window "newer than the cutoff" has to be for the assertion to be deterministic.
# The cutoff is `now()` at *query* time, so a checkpoint seeded at exactly `now() - window` is
# already fractionally older than it by the time the query runs and is correctly expired. A minute
# is far enough inside that no plausible scheduling delay between the seed and the query can flip
# it, and still small enough — against a 30-day window — to be a boundary rather than a margin.
_BOUNDARY_SECONDS = 60


async def _seed_checkpoint_at(
    thread_id: str, checkpoint_ns: str, checkpoint_id: str, *, days: int, seconds: int = 0
) -> None:
    """One checkpoint row dated `days`+`seconds` before the database's own `now()`.

    Sub-day precision, which `_seed_thread_with_ages` has no way to express, because the boundary
    case is a checkpoint a minute either side of the cutoff rather than a day.
    """
    async with db.connection(settings.postgres_dsn) as conn:
        await conn.execute(
            "INSERT INTO checkpoints "
            "(thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata) "
            "VALUES (%s, %s, %s, jsonb_build_object('v', 1, 'id', %s::text, 'ts', "
            "    to_char((now() - make_interval(days => %s::int, secs => %s::int)) "
            "            AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US+00:00')), '{}'::jsonb)",
            (thread_id, checkpoint_ns, checkpoint_id, checkpoint_id, days, seconds),
        )
        await conn.commit()


async def _seed_raw_checkpoint(thread_id: str, payload: dict[str, Any]) -> None:
    """One checkpoint row whose payload is written verbatim — including a `ts` Postgres cannot cast.

    The seeding helpers above all build a well-formed timestamp, which is exactly what the two
    malformed shapes need to avoid.
    """
    async with db.connection(settings.postgres_dsn) as conn:
        await conn.execute(
            "INSERT INTO checkpoints "
            "(thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata) "
            "VALUES (%s, '', %s, %s, '{}'::jsonb)",
            (thread_id, str(payload["id"]), Jsonb(payload)),
        )
        await conn.commit()


async def _expired_threads(days: int, cap: int) -> list[str]:
    """The threads `_EXPIRED_THREADS` names as disposable: the sweep's question, asked alone."""
    async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
        await cur.execute(_EXPIRED_THREADS, (days, cap))
        return [str(row[0]) for row in await cur.fetchall()]


def test_the_thread_query_is_the_disposal_rule_on_every_shape() -> None:
    """A thread is expired **iff its newest checkpoint is older than the cutoff** — on every shape.

    The rule is the one thing about this statement that may never change, and it has now been
    written three ways (a grouped `HAVING`, a recursive walk, and back), so it is pinned against the
    shapes a real `checkpoints` table holds rather than against whichever statement is current:

    * **an empty table** — no threads, not an error and not everything;
    * **no `ts` key at all** — `checkpoint->>'ts'` is SQL `NULL`, `max()` ignores it, and a thread
      whose timestamps are all missing is therefore never disposable. Worth pinning because the
      alternative reading, "unknown age means old", would delete live turn state;
    * **more than one `checkpoint_ns` per thread** — the unit of disposal is the *thread*, so the
      newest checkpoint in *any* namespace keeps the whole thread alive. Grouping by `thread_id`
      alone is what makes that true, and grouping by the primary key's first two columns instead
      would silently delete the default namespace of a thread whose subgraph is still running;
    * **a timestamp at the boundary** — the comparison is strict `<`, so the edge belongs to the
      expired side only once the clock has moved past it;
    * **a thread resumed inside the window** — old checkpoints, newest one recent, must survive.

    A malformed `ts` is the sixth shape and is asserted separately below, because its correct
    answer is an exception rather than a set.
    """

    async def _run() -> tuple[list[str], list[str]]:
        await migrated_db_or_skip()
        await _create_checkpoint_tables()
        await _clear_checkpoint_tables()
        try:
            empty = await _expired_threads(30, 100)

            # Every timestamp missing: never expired, however old the row is.
            await _seed_raw_checkpoint("shape-a-no-ts", {"v": 1, "id": "ckpt-0"})
            # Two namespaces, and only the non-default one is still live: the thread survives whole.
            await _seed_checkpoint_at("shape-b-two-ns", "", "ckpt-0", days=400)
            await _seed_checkpoint_at("shape-b-two-ns", "sub", "ckpt-0", days=1)
            # Two namespaces, both finished: the thread goes.
            await _seed_checkpoint_at("shape-c-two-ns-dead", "", "ckpt-0", days=400)
            await _seed_checkpoint_at("shape-c-two-ns-dead", "sub", "ckpt-0", days=380)
            # Exactly at the cutoff when it was written, so fractionally past it when asked.
            await _seed_checkpoint_at("shape-d-on-the-edge", "", "ckpt-0", days=30)
            # A minute inside the window: not expired.
            await _seed_checkpoint_at(
                "shape-e-inside-the-edge", "", "ckpt-0", days=30, seconds=-_BOUNDARY_SECONDS
            )
            # Resumed across the window: old checkpoints, recent newest.
            await _seed_checkpoint_at("shape-f-resumed", "", "ckpt-0", days=400)
            await _seed_checkpoint_at("shape-f-resumed", "", "ckpt-1", days=1)

            return empty, await _expired_threads(30, 100)
        finally:
            await _clear_checkpoint_tables()

    empty, expired = asyncio.run(_run())

    assert empty == [], f"an empty table named threads as expired: {empty}"
    assert expired == ["shape-c-two-ns-dead", "shape-d-on-the-edge"], (
        "the thread query no longer states the disposal rule: a thread is expired exactly when its "
        f"newest checkpoint, in any namespace, is older than the cutoff. Got {expired}"
    )


def test_an_uncastable_timestamp_fails_the_pass_rather_than_disposing_of_anything() -> None:
    """A `ts` Postgres cannot parse must raise, not be treated as old and not be skipped.

    Postgres has no `TRY_CAST`, so `(checkpoint->>'ts')::timestamptz` on a payload holding
    `"not-a-timestamp"` raises and the checkpoint pass fails — loudly, which is the right failure
    for a disposal job: the tables before `checkpoints` in `_PRUNABLE` have already committed their
    own deletions, and swallowing this would turn a job that *cannot run* into one reporting success
    while the table it bounds keeps growing.

    This also records where the two statements this rule has been written as differ. The grouping
    scan casts every row it reaches, so a malformed `ts` anywhere ahead of the cap fails the whole
    pass; the recursive walk cast only the threads it visited, so the same row failed a later pass
    instead. Earlier and louder is the direction a retention job wants, and this test pins it.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        await _create_checkpoint_tables()
        await _clear_checkpoint_tables()
        try:
            await _seed_raw_checkpoint("shape-g-bad-ts", {"v": 1, "id": "ckpt-0", "ts": "not-a-ts"})
            await _expired_threads(30, 100)
        finally:
            await _clear_checkpoint_tables()

    with pytest.raises(psycopg.DataError):
        asyncio.run(_run())


# --- The ownership row: disposed of behind everything it keys, never in front of it ------------
# `D-2026-08-27-a-session-nobody-can-reopen-is-disposable`. A `session_owners` row is what makes a
# session reopenable at all (`api/deps.py::_rehydrate_session` 404s an id this table does not
# hold), and it is also the row every session-scoped sweep starts from — so these pin both
# directions: what must be gone before it may go, and that it does go once nothing is left.


async def _clear_owner_fixtures() -> None:
    """Empty the tables the ownership pass reads, so its global cap sees only this test's rows.

    The same argument `_seed_expired_sessions` makes for clearing `session_messages`: the pass
    selects candidates table-wide under a `LIMIT`, so a row another test left behind lands inside
    the batch and shifts every count asserted here. The suite isolates one schema per run rather
    than per test (`tests/pg.py`), and every test below seeds immediately before it prunes.
    """
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            for table in ("session_messages", "session_events", "session_turns", "session_owners"):
                await cur.execute(f"DELETE FROM {table}")
        await conn.commit()


async def _seed_owner(session_id: str, *, age_days: int) -> None:
    """One ownership row of the given age — what a client's first keystroke leaves behind."""
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO session_owners (session_id, owner, created_at) "
                "VALUES (%s, 'oid-retention-test', now() - make_interval(days => %s))",
                (session_id, age_days),
            )
        await conn.commit()


async def _seed_message(session_id: str, *, age_days: int) -> None:
    """One self-contained conversation row for that session, of the given age."""
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO session_messages (session_id, message, created_at) "
                "VALUES (%s, %s, now() - make_interval(days => %s))",
                (session_id, Jsonb(legacy_text("user", "old")), age_days),
            )
        await conn.commit()


async def _seed_lease(session_id: str, *, expires_in_seconds: float) -> None:
    """A turn lease on that session — live when positive, an abandoned crash artifact when not."""
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO session_turns (session_id, holder, expires_at) "
                "VALUES (%s, 'worker-1', now() + make_interval(secs => %s)) "
                "ON CONFLICT (session_id) DO UPDATE SET expires_at = EXCLUDED.expires_at",
                (session_id, expires_in_seconds),
            )
        await conn.commit()


async def _rows_left(table: str) -> set[str]:
    """Which session ids that table still holds."""
    async with db.connection(settings.postgres_dsn) as conn, conn.cursor() as cur:
        await cur.execute(f"SELECT session_id FROM {table}")
        return {str(row[0]) for row in await cur.fetchall()}


async def _sweep(**windows: int) -> RetentionOutcome:
    """One retention pass with exactly these windows stated and every other one off."""
    monkeypatch = pytest.MonkeyPatch()
    for name in (
        "retention_session_events_days",
        "retention_session_messages_days",
        "retention_tool_results_days",
        "retention_result_publications_days",
        "retention_checkpoints_days",
    ):
        monkeypatch.setattr(settings, name, windows.get(name, 0))
    try:
        return await prune_expired_rows()
    finally:
        monkeypatch.undo()


def test_a_session_nobody_can_reopen_is_forgotten_and_one_still_in_use_is_not() -> None:
    """The policy in one pass: age is necessary, emptiness decides, and the lease rides along.

    Four sessions, all four cases the rule has to separate:

    - `gone` — created past the window, never a message. The abandoned draft the companion UI
      creates on the first keystroke, which nothing has ever deleted: it is invisible in the
      session list already (`_OWNER_LIST` drops a session with no messages), so the row is a
      permanent 124 bytes nobody can reach except by an id they still remember.
    - `stale-lease` — the same, plus the lease a SIGKILLed worker never released. It goes *with*
      the ownership row, because a lease naming a session nothing can find is an orphan beyond both
      `delete_session` and erasure.
    - `history` — past the window, but its conversation is not, so the row that makes that
      conversation reachable stays.
    - `draft` — created minutes ago with nothing in it yet, which is what every session looks like
      between the first keystroke and the first answer.
    """

    async def _run() -> tuple[RetentionOutcome, set[str], set[str]]:
        await migrated_db_or_skip()
        await _clear_owner_fixtures()
        for session_id in ("gone", "stale-lease", "history"):
            await _seed_owner(session_id, age_days=400)
        await _seed_owner("draft", age_days=0)
        await _seed_lease("stale-lease", expires_in_seconds=-172800)
        await _seed_message("history", age_days=10)
        outcome = await _sweep(retention_session_messages_days=365)
        return outcome, await _rows_left("session_owners"), await _rows_left("session_turns")

    outcome, owners, leases = asyncio.run(_run())
    assert owners == {"history", "draft"}, (
        "a session with a conversation, or one created inside the window, must stay reopenable"
    )
    assert leases == set(), "the lease of a forgotten session is an orphan nothing can reach"
    assert outcome.deleted["session_owners"] == 2
    assert outcome.deleted["session_turns"] == 1
    assert outcome.owners_deferred == 0


def test_a_live_turn_lease_protects_a_session_that_is_otherwise_disposable() -> None:
    """A turn writes its transcript at the end, so mid-turn the lease is the only thing saying so.

    A session resumed from an old, empty ownership row genuinely holds no rows anywhere while its
    turn is running — the transcript is written by `api/runner._record_transcript` after the answer
    exists — so without this guard the sweep would delete the ownership row of a conversation in
    progress and leave a transcript nothing can find. The second half is the other direction and is
    what keeps the rule narrow: once that same lease has expired it is a crash artifact, which
    every other reader of the table already treats as dead, and it is collected.
    """

    async def _run() -> tuple[set[str], set[str], set[str]]:
        await migrated_db_or_skip()
        await _clear_owner_fixtures()
        await _seed_owner("mid-turn", age_days=400)
        await _seed_lease("mid-turn", expires_in_seconds=600)
        await _sweep(retention_session_messages_days=365)
        during = await _rows_left("session_owners")
        await _seed_lease("mid-turn", expires_in_seconds=-1)
        await _sweep(retention_session_messages_days=365)
        return during, await _rows_left("session_owners"), await _rows_left("session_turns")

    during, after, leases = asyncio.run(_run())
    assert during == {"mid-turn"}, "the sweep deleted the ownership row of a running turn"
    assert after == set(), "an expired lease is a crash artifact and must not hold the row forever"
    assert leases == set()


def test_a_conversation_pruned_this_pass_lets_its_session_be_forgotten_in_the_same_pass() -> None:
    """The ordering hazard, pinned: `session_owners` is last in `_PRUNABLE` deliberately.

    Retention prunes `session_messages` by age, so a session whose history goes in this pass is
    empty by the time the ownership pass runs — and is disposed of in the same sweep. That is the
    intended outcome rather than an accident of ordering: what is left at that point is a shell the
    session list does not show and a resumed transcript would render blank, and keeping it would be
    exactly the unbounded growth this policy closes. The direction that would be wrong is the other
    one — the ownership row going *first*, which would put the conversation beyond erasure.
    """

    async def _run() -> tuple[RetentionOutcome, set[str], int]:
        await migrated_db_or_skip()
        await _clear_owner_fixtures()
        await _seed_owner("expiring", age_days=400)
        await _seed_message("expiring", age_days=400)
        outcome = await _sweep(retention_session_messages_days=365)
        return outcome, await _rows_left("session_owners"), await _remaining("expiring")

    outcome, owners, messages = asyncio.run(_run())
    assert messages == 0
    assert owners == set()
    assert outcome.deleted["session_messages"] == 1
    assert outcome.deleted["session_owners"] == 1


def test_graph_state_left_behind_keeps_the_ownership_row_that_finds_it() -> None:
    """The reachability guard against the table that is not in `infra/sql` at all.

    The checkpointer keys a turn's state by `thread_id`, which is the session id, and its window is
    separate — so a deployment that states a conversation window and no checkpoint window keeps
    graph state for sessions whose transcripts are gone. Deleting the ownership row there would put
    that state beyond `leaver.erase_actor`, which reaches it only by selecting session ids out of
    `session_owners`. With both windows stated the same pass removes the thread first and the
    ownership row after it, which is the ordering `_PRUNABLE` encodes.
    """

    async def _run() -> tuple[set[str], set[str]]:
        await migrated_db_or_skip()
        await _create_checkpoint_tables()
        await _clear_checkpoint_tables()
        await _clear_owner_fixtures()
        await _seed_owner("thread-left", age_days=400)
        await _seed_thread("thread-left", age_days=400)
        try:
            await _sweep(retention_session_messages_days=365)
            with_state = await _rows_left("session_owners")
            await _sweep(retention_session_messages_days=365, retention_checkpoints_days=30)
            return with_state, await _rows_left("session_owners")
        finally:
            await _clear_checkpoint_tables()

    with_state, after = asyncio.run(_run())
    assert with_state == {"thread-left"}, (
        "an ownership row was deleted while the checkpointer still held the session's turn state, "
        "which is the only way erasure can reach it"
    )
    assert after == set(), "once the thread is gone the session is a shell and may be forgotten"


def test_the_ownership_pass_works_a_bounded_batch_and_reports_the_rest() -> None:
    """The cap and its probe, for the reason the other two passes carry them.

    A first pass against a deployment that has never pruned faces every abandoned draft it has ever
    created. Capped, each pass commits a bounded amount; reported, an operator can tell a drained
    backlog from a pass that stopped at its limit — a cap that is not reported makes a still-growing
    table look bounded in every result this job returns.
    """

    async def _run() -> tuple[RetentionOutcome, set[str]]:
        await migrated_db_or_skip()
        await _clear_owner_fixtures()
        for index in range(3):
            await _seed_owner(f"capped-{index}", age_days=400)
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(settings, "retention_max_sessions_per_pass", 2)
        try:
            outcome = await _sweep(retention_session_messages_days=365)
        finally:
            monkeypatch.undo()
        return outcome, await _rows_left("session_owners")

    outcome, owners = asyncio.run(_run())
    assert outcome.deleted["session_owners"] == 2, "the pass worked more rows than its cap allowed"
    assert len(owners) == 1
    assert outcome.owners_deferred == 1, (
        "the pass stopped at its cap and reported nothing left, which reads as a bounded table"
    )
