"""Standing queries: tell me when something relevant lands (gap IDEA-1).

The system was strictly pull. It already had durable sessions, a push-back mailbox, per-user
identity and fingerprint search — every ingredient for *push* — and used none of them that way. A
chemist learned about a relevant new experiment only by asking again at the right moment, which
means the useful ones were found by luck.

A subscription is a saved query plus a watermark. The digest job (`durable/digest.py`) re-runs
each one on a cadence and reports only what has appeared since that subscriber was last told, so a
digest stays a digest rather than becoming a daily re-send of the whole corpus.

Per-*user*, not per-session, deliberately: a standing query outlives the conversation that created
it, which is the entire point of it being standing.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import TupleRow
from pydantic import BaseModel, Field

from chemclaw.agent.authz import require_actor
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.tool_registry import tool

logger = logging.getLogger(__name__)

_INSERT = """
INSERT INTO subscriptions (owner, query, note_type)
VALUES (%s, %s, %s)
ON CONFLICT (owner, query, coalesce(note_type, '')) DO NOTHING
"""
_COLUMNS = "id, owner, query, note_type, last_seen_at, last_seen_note_ids"
_SELECT_OWNER = f"SELECT {_COLUMNS} FROM subscriptions WHERE owner = %s ORDER BY id"
_SELECT_ALL = f"SELECT {_COLUMNS} FROM subscriptions ORDER BY id"
_DELETE = "DELETE FROM subscriptions WHERE owner = %s AND query = %s"

# Accumulate within the watermark's date, reset when it rolls over (DARK-7). Done in SQL rather
# than read-modify-write in Python because the date comparison and the write have to be one
# statement: two digest runs overlapping would otherwise each read the same list and the second
# would overwrite the first's additions, re-reporting exactly what this is meant to stop.
_TOUCH = """
UPDATE subscriptions
   SET last_seen_note_ids = CASE
           WHEN last_seen_at::date = now()::date
           THEN ARRAY(SELECT DISTINCT unnest(last_seen_note_ids || %s::text[]))
           ELSE %s::text[]
       END,
       last_seen_at = now()
 WHERE id = %s
"""


class Subscription(BaseModel):
    """One standing query and how far it has already reported."""

    id: int
    owner: str
    query: str
    note_type: str | None = None
    last_seen_at: datetime | None = None
    # The note ids already delivered *at `last_seen_at`'s date*. Only that date's, so the list is
    # bounded by one day of matches: anything older is already excluded by the date comparison
    # itself, and keeping it would make this grow with the corpus (DARK-7).
    last_seen_note_ids: list[str] = Field(default_factory=list)


@asynccontextmanager
async def _connection() -> AsyncIterator[psycopg.AsyncConnection[TupleRow]]:
    """Borrow a connection with the configured per-statement timeout.

    Pooled per process when the process opened a pool (`chemclaw.core.db.pooling`), so a
    request path pays no TCP+auth handshake; a dedicated connect otherwise. Either way a
    down or misconfigured database reports "Postgres unreachable at <host>" rather than a
    raw psycopg traceback, and a hung query is cancelled rather than pinning the enclosing
    activity for its whole budget.
    """
    async with db.connection(settings.postgres_dsn) as conn:
        yield conn


async def add(owner: str, query: str, note_type: str | None) -> None:
    """Save a standing query for `owner`. Idempotent — asking twice does not double-notify."""
    async with _connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_INSERT, (owner, query, note_type))
        await conn.commit()


async def for_owner(owner: str) -> list[Subscription]:
    """Every standing query `owner` has saved."""
    return await _fetch(_SELECT_OWNER, (owner,))


async def all_subscriptions() -> list[Subscription]:
    """Every standing query in the deployment — what the digest job iterates."""
    return await _fetch(_SELECT_ALL, ())


async def _fetch(sql: str, params: tuple[Any, ...]) -> list[Subscription]:
    """Run a subscription query and map the rows."""
    async with _connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            rows = await cur.fetchall()
    return [
        Subscription(
            id=r[0],
            owner=r[1],
            query=r[2],
            note_type=r[3],
            last_seen_at=r[4],
            last_seen_note_ids=list(r[5] or []),
        )
        for r in rows
    ]


async def remove(owner: str, query: str) -> None:
    """Drop a standing query — a subscription nobody can cancel becomes spam."""
    async with _connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_DELETE, (owner, query))
        await conn.commit()


async def mark_reported(subscription_id: int, note_ids: list[str]) -> None:
    """Advance a subscription's watermark and remember what it just delivered.

    Advanced *after* delivery, never before: a crash between the two must re-report rather than
    silently skip, because a duplicate digest line is a nuisance and a missed one defeats the
    feature.

    `note_ids` is what makes the watermark exact rather than merely approximate. The date
    comparison alone cannot separate "dated today and already sent" from "dated today and new",
    because a note's `valid_from` is a date and the digest runs hourly — so `>=` re-sent every
    same-day note every hour, and `>` would have dropped the ones that arrived later that day.
    Remembering the ids settles it without choosing between the two failures (DARK-7).
    """
    async with _connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_TOUCH, (note_ids, note_ids, subscription_id))
        await conn.commit()


@tool
async def watch_for(query: str, note_type: str | None = None) -> str:
    """Watch for new knowledge matching a query, and get told when it lands.

    Use this when the chemist wants to be *notified* rather than to keep asking — "let me know when
    a playbook touching PRJ-114 merges", "tell me when a campaign cites the biaryl route". The
    query is matched the same way `gather_evidence` matches, so phrase it the same way.

    **It watches the knowledge graph, not the ELN.** Since D-2026-08-25 an ingested run is a row in
    `reaction_records` rather than a note, and the digest reads merged notes — so "tell me when
    anyone runs a Suzuki on a chloro-pyridine", which this docstring used to offer as the leading
    example, is not what this does. It reports the *claims* people merge about runs (playbooks,
    campaigns, hand-written reaction notes), not the runs themselves. For those, ask
    `similar_reactions` when you want to know, which reads the index directly and answers now.

    Args:
        query: What to watch for (key terms, matched over note id, tags and body).
        note_type: Optionally narrow to one merged note type, e.g. "playbook" or "campaign".

    Returns:
        Confirmation. Saving the same watch twice is a no-op, not a second notification.
    """
    owner = require_actor()
    await add(owner, query, note_type)
    return f"Watching for {query!r}; you'll be told when something new matches."


@tool
async def list_watches() -> list[Subscription]:
    """List the standing queries this chemist has saved.

    Returns:
        Each saved watch and when it last reported.
    """
    return await for_owner(require_actor())


@tool
async def stop_watching(query: str) -> str:
    """Stop watching for a query the chemist no longer cares about.

    Args:
        query: The exact query text of the watch to remove (see `list_watches`).

    Returns:
        Confirmation.
    """
    owner = require_actor()
    await remove(owner, query)
    return f"Stopped watching for {query!r}."
