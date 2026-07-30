"""Durable, Postgres-backed conversation history (plan Phase F3).

`PostgresHistoryProvider` is the durable replacement for MAF's `InMemoryHistoryProvider`: instead of
keeping a session's messages in the in-process session state (which dies with the pod), it appends
each turn's stored messages to the `session_messages` table keyed by session id, and loads them
back in insertion order. So a fresh process over the same database resumes the conversation — the
"session survives a restart" requirement (F3-T1). It overrides only the two storage primitives
(`get_messages`/`save_messages`), exactly as `InMemoryHistoryProvider` does; the base
`HistoryProvider` still decides *which* messages to store per turn and runs `before_run`/
`after_run`, and compaction still layers on top.

This is the conversation layer, deliberately separate from Temporal job state (D-002) and the
calculation cache. The MAF `Message` is stored via its own `to_dict()`/`from_dict()`, so the store
never interprets message shape — a MAF change is a value change, not a schema change.

Three stores live here because they are one session's durable state and must share a database:
the message history above, `SessionOwnerStore` (who owns a session id — the fact the in-process
LRU loses on restart), and `SessionTurnClaims` (which process is running a turn on it right now —
the fact the in-process 409 guard loses at the pod boundary, D-121).

**MAF's after-run compaction cannot reach this provider, so the provider does it itself** (REV-4,
D-149). `CompactionProvider.after_run` reads `session.state[history_source_id]["messages"]` — the
place `InMemoryHistoryProvider` keeps its thread. This provider deliberately keeps nothing there,
which is the entire point of it, so that lookup finds nothing and the strategy returns having done
nothing. Under `session_store="postgres"` — the production default — the `after_strategy` half of
`chemclaw.agent.chemclaw_agent._build_compaction` is a silent no-op, and it always will be:
nothing short of
reintroducing the in-process thread would change it.

The consequence was that the rows grew for the session's whole life and every turn re-read all of
them before the model call — `_SELECT_WITH_ID` has no `LIMIT`. `save_messages` now applies the
*same* strategy to the table after storing a turn (see `_compact`), which is the promise
`_build_compaction`'s docstring was already making, kept in the one place that can keep it.

**`get_messages` is untouched, and the `LIMIT` that looks like the obvious fix stays out.** Loading
only a recent window looks safe because the read already repairs unmatched tool-call pairings — and
it is not, because that repair *writes back*. Over a windowed read a `tool_result` whose `tool_use`
merely fell outside the window is indistinguishable from a real orphan, so the repair would strip it
and commit that, destroying a pairing that was intact on disk. Worse, the repair is one-directional
(`chemclaw.agent.message_pairing`): it can heal an orphaned call and is blind to an orphaned
*result*, which
has no self-heal path at all. Compaction avoids the whole class by deleting only whole pairing
components, via `droppable_rows` (D-145). `tests/test_durable_compaction_gap.py` pins both the
absent `LIMIT` and the write-back that makes it unsafe.
"""

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from typing import Any, ClassVar

import psycopg
from agent_framework import HistoryProvider, Message
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from chemclaw.agent.history_compaction import plan_compaction
from chemclaw.agent.message_pairing import strip_call_ids, unmatched_call_ids
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.metrics_bridge import record_metric

log = logging.getLogger(__name__)

_INSERT = "INSERT INTO session_messages (session_id, message) VALUES (%s, %s)"
_SELECT = "SELECT message FROM session_messages WHERE session_id = %s ORDER BY id"
# Row ids come back too, so a repaired message can be written to the row it came from.
_SELECT_WITH_ID = "SELECT id, message FROM session_messages WHERE session_id = %s ORDER BY id"
_UPDATE_MESSAGE = "UPDATE session_messages SET message = %s WHERE id = %s"
_DELETE_IDS = "DELETE FROM session_messages WHERE session_id = %s AND id = ANY(%s)"
_MAX_ID = "SELECT MAX(id) FROM session_messages WHERE session_id = %s"
_COUNT = "SELECT count(*) FROM session_messages WHERE session_id = %s"
_DELETE_AFTER = "DELETE FROM session_messages WHERE session_id = %s AND id > %s"

# The per-session turn claim (D-121). One statement, so the check and the take cannot be
# interleaved by another process: `ON CONFLICT … DO UPDATE … WHERE` takes the row lock, and the
# update only fires when the incumbent claim has expired. `RETURNING` is empty exactly when a live
# claim was left alone, which is the caller's "someone else is running a turn" answer.
_TURN_CLAIM = (
    "INSERT INTO session_turns (session_id, holder, expires_at) "
    "VALUES (%s, %s, now() + make_interval(secs => %s)) "
    "ON CONFLICT (session_id) DO UPDATE "
    "SET holder = EXCLUDED.holder, claimed_at = now(), expires_at = EXCLUDED.expires_at "
    "WHERE session_turns.expires_at <= now() "
    "RETURNING holder"
)
# Guarded by `holder` so a worker whose lease already lapsed and was taken by someone else cannot
# extend — or delete — the new owner's claim.
_TURN_REFRESH = (
    "UPDATE session_turns SET expires_at = now() + make_interval(secs => %s) "
    "WHERE session_id = %s AND holder = %s"
)
_TURN_RELEASE = "DELETE FROM session_turns WHERE session_id = %s AND holder = %s"

_OWNER_INSERT = (
    "INSERT INTO session_owners (session_id, owner, profile) VALUES (%s, %s, %s) "
    "ON CONFLICT (session_id) DO NOTHING"
)
# The profile comes back with the owner because both are facts the in-process LRU loses, and a
# rehydration that restored one without the other silently widened the session's tool surface
# (REV-14 — a profile can only attenuate, so losing it is never the safe direction).
_OWNER_SELECT = "SELECT owner, profile FROM session_owners WHERE session_id = %s"
# Newest first: a session list is read as "what was I just working on", and the caller pages from
# the top. `owner IS NOT DISTINCT FROM %s` rather than `=` so the shared dev principal (a real NULL
# owner) matches itself instead of dropping every row to SQL's three-valued logic.
_OWNER_LIST = (
    "SELECT session_id, created_at FROM session_owners "
    "WHERE owner IS NOT DISTINCT FROM %s ORDER BY created_at DESC, session_id DESC LIMIT %s"
)


def _session_dsn(dsn: str | None) -> str:
    """Resolve a session-layer DSN: the caller's, else `session_store_dsn`, else `postgres_dsn`.

    One resolver for all three stores in this module, so they can never end up pointing at
    different databases — the ownership row, the turn claim and the message history are one
    session's state and must live together (D-002).
    """
    return dsn or settings.session_store_dsn or settings.postgres_dsn


@asynccontextmanager
async def _session_connection(dsn: str) -> AsyncIterator[psycopg.AsyncConnection[TupleRow]]:
    """Borrow a session-layer connection with the configured per-statement timeout.

    Pooled per process when the process opened a pool (`chemclaw.core.db.pooling`), so a request
    path
    pays no TCP+auth handshake; a dedicated connect otherwise. Either way a down or misconfigured
    database reports "Postgres unreachable at <host>" rather than a raw psycopg traceback, and a
    hung query is cancelled rather than pinning the enclosing activity for its whole budget.

    Extracted once the third store in this module needed the identical four lines.
    """
    async with db.connection(
        dsn, statement_timeout_seconds=settings.pg_statement_timeout_seconds
    ) as conn:
        yield conn


class PostgresHistoryProvider(HistoryProvider):
    """A `HistoryProvider` that persists a session's messages to Postgres (durable, resumable)."""

    DEFAULT_SOURCE_ID: ClassVar[str] = "postgres_history"

    def __init__(self, source_id: str | None = None, *, dsn: str | None = None) -> None:
        """Configure the provider.

        Args:
            source_id: This provider's id (used by compaction to find its stored history). Defaults
                to `DEFAULT_SOURCE_ID`.
            dsn: Database to persist to. Defaults to `session_store_dsn`, falling back to the shared
                `postgres_dsn` when that is empty (one database in the simple deployment).
        """
        super().__init__(source_id=source_id or self.DEFAULT_SOURCE_ID)
        self._dsn = _session_dsn(dsn)

    def _connection(self) -> AbstractAsyncContextManager[psycopg.AsyncConnection[TupleRow]]:
        """Borrow a connection on this provider's database (see `_session_connection`)."""
        return _session_connection(self._dsn)

    async def get_messages(
        self, session_id: str | None, *, state: dict[str, Any] | None = None, **kwargs: Any
    ) -> list[Message]:
        """Load a session's messages in insertion order (empty for an unknown/None session).

        Repairs the history on the way out: a function call with no matching result is dropped,
        and the stored row is corrected. See `chemclaw.agent.message_pairing` for why this is
        enforced on
        read rather than only on write — a `SIGKILL` or pod eviction between the call and its
        result runs no cleanup handler, and the orphan it leaves behind makes every later turn on
        that session fail outright. Doing it here also heals sessions already broken in the wild.
        """
        if not session_id:
            return []
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT_WITH_ID, (session_id,))
                rows = await cur.fetchall()
        stored = [(int(row[0]), Message.from_dict(row[1])) for row in rows]
        orphans = unmatched_call_ids([message for _, message in stored])
        if not orphans:
            return [message for _, message in stored]  # the common path: no rewrite, no write
        # Decided per row rather than by diffing two lists: once a message is dropped entirely the
        # lists no longer line up, and a positional pairing would rewrite the wrong row's message.
        kept: list[Message] = []
        deletions: list[int] = []
        rewrites: list[tuple[Jsonb, int]] = []
        for row_id, message in stored:
            repaired = strip_call_ids(message, orphans)
            if repaired is None:
                deletions.append(row_id)
                continue
            kept.append(repaired)
            if repaired is not message:  # identity, so only genuinely changed rows are written
                rewrites.append((Jsonb(repaired.to_dict()), row_id))
        await self._persist_repair(session_id, deletions, rewrites)
        return kept

    async def _persist_repair(
        self, session_id: str, deletions: list[int], rewrites: list[tuple[Jsonb, int]]
    ) -> None:
        """Write back a repaired history, so the orphan is removed once rather than re-filtered.

        Best-effort: reading the conversation is the critical path, so a failure here is logged and
        swallowed — the caller still gets the clean history either way. Idempotent, so two readers
        racing on the same broken session converge on the same rows.
        """
        try:
            async with self._connection() as conn:
                async with conn.cursor() as cur:
                    if deletions:
                        await cur.execute(_DELETE_IDS, (session_id, deletions))
                    if rewrites:
                        await cur.executemany(_UPDATE_MESSAGE, rewrites)
                await conn.commit()
        except (psycopg.Error, ConnectionError):
            log.warning(
                "could not persist history repair for session %s; "
                "the unmatched tool call was filtered for this turn but remains stored",
                session_id,
                exc_info=True,
            )
        else:
            log.warning(
                "repaired session %s: removed %d unmatched tool call(s) from durable history",
                session_id,
                len(deletions) + len(rewrites),
            )

    async def latest_message_id(self, session_id: str) -> int | None:
        """Return the highest stored row id for `session_id`, or `None` when it has no history.

        The pre-turn watermark for `rollback_to`.
        """
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_MAX_ID, (session_id,))
                row = await cur.fetchone()
        return None if row is None or row[0] is None else int(row[0])

    async def rollback_to(self, session_id: str, watermark: int | None) -> int:
        """Delete everything stored for `session_id` after `watermark`; return how many rows went.

        The durable half of the turn rollback in `chemclaw.api.runner`: that only restored the
        in-process session state, which under this provider is not where the messages live — they
        are already committed, so a half-written turn survived the rollback that was supposed to
        discard it.
        """
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_DELETE_AFTER, (session_id, watermark or 0))
                deleted = cur.rowcount
            await conn.commit()
        return max(deleted, 0)

    async def save_messages(
        self,
        session_id: str | None,
        messages: Sequence[Message],
        *,
        state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Append this turn's messages to the session's durable history (no-op if none to store).

        Then compact, if the deployment has asked for it. The append commits on its own first, and
        the compaction pass runs in a second transaction whose failure is logged and swallowed —
        exactly the split `_persist_repair` makes, and for the same reason: storing the turn is the
        critical path and disposing of old rows is not. This keeps the append's contract byte-for-
        byte what it was.
        """
        if not session_id or not messages:
            return
        rows = [(session_id, Jsonb(message.to_dict())) for message in messages]
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                # The watermark before the insert: everything above it is this turn's own work and
                # must survive the pass regardless of what the strategy says (see `_compact`).
                await cur.execute(_MAX_ID, (session_id,))
                row = await cur.fetchone()
                watermark = 0 if row is None or row[0] is None else int(row[0])
                await cur.executemany(_INSERT, rows)
            await conn.commit()
        if settings.agent_durable_compaction_enabled:
            await self._compact(session_id, watermark)

    async def _compact(self, session_id: str, watermark: int) -> None:
        """Apply the context compaction policy to the *stored* history (D-149).

        MAF's `CompactionProvider.after_run` cannot do this: it reads
        `session.state[source_id]["messages"]`, the slot `InMemoryHistoryProvider` writes and this
        provider deliberately does not. So the rows grew forever and every turn re-read all of them.
        This runs the identical strategy — `chemclaw.agent.chemclaw_agent.compaction_strategy`,
        the same one
        that bounds the model's context — against the table.

        `watermark` protects the turn just written. The composed strategy's fallback can exclude
        *every* message when a single payload is oversized, and a turn that deleted the rows it had
        just stored would lose the conversation it was recording.

        Best-effort by construction: a failure here leaves a larger history, which is the state the
        system was in before this existed. It must never cost the turn its messages.
        """
        # Imported at call time: `agents.chemclaw_agent` reaches the connector registry and the
        # whole tool surface, and the storage layer must not pull that in at import (the workers
        # import this module without ever building an agent).
        from chemclaw.agent.chemclaw_agent import compaction_strategy

        try:
            async with self._connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(_COUNT, (session_id,))
                    counted = await cur.fetchone()
                    if (
                        counted is None
                        or int(counted[0]) < settings.agent_durable_compaction_min_rows
                    ):
                        return
                    await cur.execute(_SELECT_WITH_ID, (session_id,))
                    stored = [
                        (int(row[0]), Message.from_dict(row[1])) for row in await cur.fetchall()
                    ]
                strategy, _ = compaction_strategy()
                plan = await plan_compaction(
                    stored,
                    strategy=strategy,
                    protected={row_id for row_id, _ in stored if row_id > watermark},
                )
                if plan.is_empty():
                    return
                async with conn.cursor() as cur:
                    for row_id, message in plan.rewrites:
                        await cur.execute(_UPDATE_MESSAGE, (Jsonb(message.to_dict()), row_id))
                    if plan.deletes:
                        await cur.execute(_DELETE_IDS, (session_id, sorted(plan.deletes)))
                await conn.commit()
        except Exception:
            log.warning("could not compact stored history for %s", session_id, exc_info=True)
            return
        record_metric(
            lambda m: m.increment("chemclaw_history_rows_compacted_total", float(len(plan.deletes)))
        )
        log.info(
            "compacted session %s: %d row(s) removed, %d collapsed to a summary",
            session_id,
            len(plan.deletes),
            len(plan.rewrites),
        )


class SessionOwnerStore:
    """Durable session-ownership registry, so a restarted front door can reattach a client (F3).

    The front door holds live `AgentSession` handles in an in-process LRU that a pod restart wipes;
    without a durable record of *who owns which session id*, a returning client's id is unknown
    after a restart and it is forced onto a brand-new session — orphaning its durable history
    (`session_messages`) and any unconsumed job push-back (`session_events`). This is that record:
    `create_session` writes `(session_id, owner)` once, and on a cache miss the front door looks
    the owner up to authorize a reattach before rebuilding the live handle over its durable history.

    One identity row per session, deliberately separate from the append-only message history — it
    carries the single security-relevant fact (the owner) the in-memory LRU lost. The DSN resolves
    exactly as the history provider's, so both durable-session tables live in one database (D-002).
    """

    def __init__(self, *, dsn: str | None = None) -> None:
        """Bind to the session-store database (falling back to the shared `postgres_dsn`)."""
        self._dsn = _session_dsn(dsn)

    def _connection(self) -> AbstractAsyncContextManager[psycopg.AsyncConnection[TupleRow]]:
        """Borrow a connection on this store's database (see `_session_connection`)."""
        return _session_connection(self._dsn)

    async def record(self, session_id: str, owner: str | None, profile: str | None = None) -> None:
        """Record a session's owner and profile at creation (idempotent — first writer wins)."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_OWNER_INSERT, (session_id, owner, profile))
            await conn.commit()

    async def lookup(self, session_id: str) -> tuple[bool, str | None, str | None]:
        """Return `(found, owner, profile)` — `(False, None, None)` when there is no such session.

        The `found` flag distinguishes an unknown session from a known one owned by the shared
        principal (a real `NULL` owner), which a bare `str | None` return could not.
        """
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_OWNER_SELECT, (session_id,))
                row = await cur.fetchone()
        if row is None:
            return (False, None, None)
        return (True, row[0], row[1])

    async def list_for_owner(self, owner: str | None) -> list[tuple[str, datetime]]:
        """The owner's sessions as `(session_id, created_at)`, newest first, capped by config.

        This table is already the durable answer to "which sessions exist and who owns them", so
        listing reads it directly rather than adding a second registry that could disagree with the
        one `_resolve_session` authorizes against.
        """
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_OWNER_LIST, (owner, settings.service_max_listed_sessions))
                rows = await cur.fetchall()
        return [(row[0], row[1]) for row in rows]


class SessionTurnClaims:
    """One turn at a time per session, across every process, as a leased row (D-121).

    The front door refuses a second concurrent turn on a session with a 409, because two turns
    driving `agent.run` against the same conversation thread interleave their messages into one
    history. That guard was a `set` in one process's memory, and the shipped chart runs the front
    door at two replicas — so two turns on one session landing on different pods were both
    admitted, and raising `service_uvicorn_workers` would add the same hazard inside a pod. This
    is the same guard at the width the deployment actually has.

    A **lease**, not a lock, and that is the whole design. A Postgres advisory lock (or
    `SELECT … FOR UPDATE`) lives on a connection or a transaction, so holding one for a turn means
    pinning a pooled connection for minutes — re-creating the connection starvation that made a
    bounded pool start raising in the first place. Each of the three operations here is one short
    statement that borrows a connection and gives it straight back.

    The claim is taken under `expires_at`, refreshed while the turn runs, and deleted when it
    ends. A worker that is SIGKILLed mid-turn therefore stops blocking its session after one
    lease, where a lock held by a dead connection waits for the server to notice and an in-memory
    set needed a process restart. The cost is the standard lease property, stated plainly in
    `chemclaw.api.app`: exclusion holds as long as the holder is scheduled often enough to refresh.
    """

    def __init__(self, *, dsn: str | None = None) -> None:
        """Bind to the session-store database (falling back to the shared `postgres_dsn`)."""
        self._dsn = _session_dsn(dsn)

    def _connection(self) -> AbstractAsyncContextManager[psycopg.AsyncConnection[TupleRow]]:
        """Borrow a connection on this store's database (see `_session_connection`)."""
        return _session_connection(self._dsn)

    async def claim(self, session_id: str, holder: str, lease_seconds: float) -> bool:
        """Take the session's turn slot for `lease_seconds`; False if someone else holds it.

        One statement, so no other process can observe the gap between the check and the take —
        the same atomicity the in-process `set` got for free from having no `await` between its
        membership test and its `add`.
        """
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_TURN_CLAIM, (session_id, holder, lease_seconds))
                taken = await cur.fetchone() is not None
            await conn.commit()
        return taken

    async def refresh(self, session_id: str, holder: str, lease_seconds: float) -> None:
        """Push this holder's claim out by another lease, so a long turn is not stolen from.

        A no-op when the claim is gone or now belongs to someone else: that means this worker was
        already declared dead, and re-taking the slot behind the live holder's back is exactly
        the interleaving the guard exists to prevent.
        """
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_TURN_REFRESH, (lease_seconds, session_id, holder))
            await conn.commit()

    async def release(self, session_id: str, holder: str) -> None:
        """Give the slot back at the end of the turn (idempotent; only this holder's row goes)."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_TURN_RELEASE, (session_id, holder))
            await conn.commit()
