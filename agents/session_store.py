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

from collections.abc import Sequence
from datetime import datetime
from typing import Any, ClassVar

import psycopg
from agent_framework import HistoryProvider, Message
from psycopg.types.json import Jsonb

from chemclaw import db
from chemclaw.config import settings

_INSERT = "INSERT INTO session_messages (session_id, message) VALUES (%s, %s)"
_SELECT = "SELECT message FROM session_messages WHERE session_id = %s ORDER BY id"

_OWNER_INSERT = (
    "INSERT INTO session_owners (session_id, owner) VALUES (%s, %s) "
    "ON CONFLICT (session_id) DO NOTHING"
)
_OWNER_SELECT = "SELECT owner FROM session_owners WHERE session_id = %s"

# The caller's sessions, newest first, each carrying its earliest stored message so the caller can
# label it.
#
# The label is derived from that message rather than kept in a column, so listing needs no
# migration and no second representation of the conversation that could drift from the messages
# themselves. The message is returned as its raw `Message.to_dict()` blob and decoded in Python via
# `Message.from_dict(...).text`, exactly as the history provider does — the store deliberately does
# not reach into MAF's message shape from SQL, which would turn a MAF version bump into a broken
# query instead of a value change.
#
# `IS NOT DISTINCT FROM` rather than `=`, so the dev path's NULL owner matches NULL — `=` never
# does, which would make the listing silently empty exactly where `entra_required` is off.
_OWNER_LIST = """
    SELECT o.session_id,
           o.created_at,
           (SELECT m.message
              FROM session_messages m
             WHERE m.session_id = o.session_id
             ORDER BY m.id
             LIMIT 1) AS first_message
      FROM session_owners o
     WHERE o.owner IS NOT DISTINCT FROM %s
     ORDER BY o.created_at DESC
     LIMIT %s
"""


# How much of the opening message becomes a session's label in a listing.
_TITLE_CHARS = 80


def _title_of(blob: Any) -> str:
    """Label a session from its opening message blob (empty when there is nothing usable).

    Decoded through `Message.from_dict` rather than by indexing the JSON, so the store keeps its
    "never interpret MAF's message shape" property: a MAF change stays a value change.
    """
    if not blob:
        return ""
    try:
        text = Message.from_dict(blob).text or ""
    except Exception:
        return ""
    text = " ".join(text.split())
    return text[:_TITLE_CHARS]


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
        """Load a session's messages in insertion order (empty for an unknown/None session)."""
        if not session_id:
            return []
        async with await self._connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT, (session_id,))
                rows = await cur.fetchall()
        return [Message.from_dict(row[0]) for row in rows]

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

    async def list_for_owner(
        self, owner: str | None, *, limit: int
    ) -> list[tuple[str, datetime, str]]:
        """The owner's sessions, newest first, as `(session_id, created_at, title)` triples.

        The front door had no way to enumerate a chemist's conversations: a session id was
        returned once at creation and never listed again, so a client that lost it — a new
        browser, a cleared cache — could only start over, orphaning durable history that was
        sitting right there. Scoped to the caller for the same reason `_resolve_session` is:
        a session is a conversation, and conversations are not shared.

        The title is the opening message's text, truncated; empty when the session has no stored
        messages yet (created but never used) or when the blob cannot be decoded — a listing is a
        convenience, and no label is worth failing it over.

        Bounded by `limit` because `session_owners` grows without a retention policy and an
        unbounded listing would degrade quietly as a deployment ages.
        """
        async with await self._connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_OWNER_LIST, (owner, limit))
                rows = await cur.fetchall()
        return [(row[0], row[1], _title_of(row[2])) for row in rows]
