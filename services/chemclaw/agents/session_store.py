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
"""

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any, ClassVar

import psycopg
from agent_framework import HistoryProvider, Message
from psycopg.types.json import Jsonb

from agents.message_pairing import strip_unmatched_calls
from chemclaw import db
from chemclaw.config import settings

log = logging.getLogger(__name__)

_INSERT = "INSERT INTO session_messages (session_id, message) VALUES (%s, %s)"
_SELECT = "SELECT message FROM session_messages WHERE session_id = %s ORDER BY id"
# Row ids come back too, so a repaired message can be written to the row it came from.
_SELECT_WITH_ID = "SELECT id, message FROM session_messages WHERE session_id = %s ORDER BY id"
_UPDATE_MESSAGE = "UPDATE session_messages SET message = %s WHERE id = %s"
_DELETE_IDS = "DELETE FROM session_messages WHERE session_id = %s AND id = ANY(%s)"
_MAX_ID = "SELECT MAX(id) FROM session_messages WHERE session_id = %s"
_DELETE_AFTER = "DELETE FROM session_messages WHERE session_id = %s AND id > %s"

_OWNER_INSERT = (
    "INSERT INTO session_owners (session_id, owner) VALUES (%s, %s) "
    "ON CONFLICT (session_id) DO NOTHING"
)
_OWNER_SELECT = "SELECT owner FROM session_owners WHERE session_id = %s"
# Newest first: a session list is read as "what was I just working on", and the caller pages from
# the top. `owner IS NOT DISTINCT FROM %s` rather than `=` so the shared dev principal (a real NULL
# owner) matches itself instead of dropping every row to SQL's three-valued logic.
_OWNER_LIST = (
    "SELECT session_id, created_at FROM session_owners "
    "WHERE owner IS NOT DISTINCT FROM %s ORDER BY created_at DESC, session_id DESC LIMIT %s"
)


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
        self._dsn = dsn or settings.session_store_dsn or settings.postgres_dsn

    async def _connect(self) -> psycopg.AsyncConnection[Any]:
        """Open a fast-failing connection with the configured per-statement timeout."""
        return await db.connect(
            self._dsn, statement_timeout_seconds=settings.pg_statement_timeout_seconds
        )

    async def get_messages(
        self, session_id: str | None, *, state: dict[str, Any] | None = None, **kwargs: Any
    ) -> list[Message]:
        """Load a session's messages in insertion order (empty for an unknown/None session).

        Repairs the history on the way out: a function call with no matching result is dropped,
        and the stored row is corrected. See `agents.message_pairing` for why this is enforced on
        read rather than only on write — a `SIGKILL` or pod eviction between the call and its
        result runs no cleanup handler, and the orphan it leaves behind makes every later turn on
        that session fail outright. Doing it here also heals sessions already broken in the wild.
        """
        if not session_id:
            return []
        async with await self._connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT_WITH_ID, (session_id,))
                rows = await cur.fetchall()
        stored = [(int(row[0]), Message.from_dict(row[1])) for row in rows]
        repaired = strip_unmatched_calls([message for _, message in stored])
        if len(repaired) == len(stored) and all(
            new is old for new, (_, old) in zip(repaired, stored, strict=True)
        ):
            return repaired  # untouched — the overwhelmingly common path, no write at all
        await self._persist_repair(session_id, stored, repaired)
        return repaired

    async def _persist_repair(
        self, session_id: str, stored: list[tuple[int, Message]], repaired: list[Message]
    ) -> None:
        """Write back a repaired history, so the orphan is removed once rather than re-filtered.

        Best-effort: reading the conversation is the critical path, so a failure here is logged and
        swallowed — the caller still gets the clean history either way. Idempotent, so two readers
        racing on the same broken session converge on the same rows.
        """
        by_id = dict(zip([row_id for row_id, _ in stored], repaired, strict=False))
        surviving = {id(message) for message in repaired}
        deletions = [row_id for row_id, message in stored if id(message) not in surviving]
        rewrites = [
            (Jsonb(by_id[row_id].to_dict()), row_id)
            for row_id, message in stored
            if row_id in by_id and by_id[row_id] is not message
        ]
        try:
            async with await self._connect() as conn:
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
        async with await self._connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_MAX_ID, (session_id,))
                row = await cur.fetchone()
        return None if row is None or row[0] is None else int(row[0])

    async def rollback_to(self, session_id: str, watermark: int | None) -> int:
        """Delete everything stored for `session_id` after `watermark`; return how many rows went.

        The durable half of the turn rollback in `service.runner`: that only restored the
        in-process session state, which under this provider is not where the messages live — they
        are already committed, so a half-written turn survived the rollback that was supposed to
        discard it.
        """
        async with await self._connect() as conn:
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
        """Append this turn's messages to the session's durable history (no-op if none to store)."""
        if not session_id or not messages:
            return
        rows = [(session_id, Jsonb(message.to_dict())) for message in messages]
        async with await self._connect() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(_INSERT, rows)
            await conn.commit()


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
        self._dsn = dsn or settings.session_store_dsn or settings.postgres_dsn

    async def _connect(self) -> psycopg.AsyncConnection[Any]:
        """Open a fast-failing connection with the configured per-statement timeout."""
        return await db.connect(
            self._dsn, statement_timeout_seconds=settings.pg_statement_timeout_seconds
        )

    async def record(self, session_id: str, owner: str | None) -> None:
        """Record a session's owner at creation (idempotent — the first writer wins)."""
        async with await self._connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_OWNER_INSERT, (session_id, owner))
            await conn.commit()

    async def lookup(self, session_id: str) -> tuple[bool, str | None]:
        """Return `(found, owner)` for a session id — `(False, None)` when there is no such session.

        The `found` flag distinguishes an unknown session from a known one owned by the shared
        principal (a real `NULL` owner), which a bare `str | None` return could not.
        """
        async with await self._connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_OWNER_SELECT, (session_id,))
                row = await cur.fetchone()
        return (row is not None, row[0] if row is not None else None)

    async def list_for_owner(self, owner: str | None) -> list[tuple[str, datetime]]:
        """The owner's sessions as `(session_id, created_at)`, newest first, capped by config.

        This table is already the durable answer to "which sessions exist and who owns them", so
        listing reads it directly rather than adding a second registry that could disagree with the
        one `_resolve_session` authorizes against.
        """
        async with await self._connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_OWNER_LIST, (owner, settings.service_max_listed_sessions))
                rows = await cur.fetchall()
        return [(row[0], row[1]) for row in rows]
