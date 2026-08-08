"""Per-user working preferences (gap AGT-4).

Every memory layer is corpus-level — `campaign`, `playbook`, `optimization-campaign` and
`interaction` notes all describe the chemistry, shared by everyone. Nothing remembered *this
chemist*: their project, their preferred solvent system, the units they think in, or that they
already rejected an analogy last week. The identity was available (`Principal.oid`, and the
`session_owners` table); only the layer was missing.

**Why not knowledge-graph notes.** A preference is personal, revisable, and of no interest to anyone
else. Routing it through the PR-gate would ask a human to review "Anna prefers 2-MeTHF" — noise that
would erode the seriousness of the gate itself (D-005). The graph holds what the *organisation*
knows; this holds how one *person* works. That separation is the whole design decision here.

The store degrades to in-memory when no database is configured, exactly as the session store does,
so dev and tests need no infrastructure and a preference is never a hard dependency of a turn.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from psycopg.rows import TupleRow
from pydantic import BaseModel

from chemclaw.agent.authz import require_actor
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.metrics_bridge import degraded
from chemclaw.core.tool_registry import tool

logger = logging.getLogger(__name__)

_UPSERT = """
INSERT INTO user_preferences (owner, key, value, updated_at)
VALUES (%s, %s, %s, now())
ON CONFLICT (owner, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
"""
_SELECT = "SELECT key, value FROM user_preferences WHERE owner = %s ORDER BY key"
_DELETE = "DELETE FROM user_preferences WHERE owner = %s AND key = %s"


class Preference(BaseModel):
    """One remembered preference."""

    key: str
    value: str


class PreferenceStore:
    """Durable per-user preferences, with an in-memory fallback for dev and tests."""

    def __init__(self, dsn: str | None = None) -> None:
        """Persist to `dsn`, or to the configured session/shared database."""
        self._dsn = dsn or settings.session_store_dsn or settings.postgres_dsn
        self._memory: dict[tuple[str, str], str] = {}

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[psycopg.AsyncConnection[TupleRow]]:
        """Borrow a connection with the configured per-statement timeout.

        Pooled per process when the process opened a pool (`chemclaw.core.db.pooling`), so a
        request path pays no TCP+auth handshake; a dedicated connect otherwise. Either way a
        down or misconfigured database reports "Postgres unreachable at <host>" rather than a
        raw psycopg traceback, and a hung query is cancelled rather than pinning the enclosing
        activity for its whole budget.
        """
        async with db.connection(
            self._dsn, statement_timeout_seconds=settings.pg_statement_timeout_seconds
        ) as conn:
            yield conn

    async def remember(self, owner: str, key: str, value: str) -> bool:
        """Set (or replace) one preference for `owner`. Idempotent by (owner, key).

        Returns whether it was stored *as durably as this deployment is configured for* — True in
        memory mode, where memory is the configured store, and True in Postgres mode only if the
        row was actually written.

        The caller needs that distinction because the failure is invisible from the outside: the
        in-memory copy is updated first and always succeeds, so the chemist's *current* session
        behaves correctly while the preference silently will not survive it. Swallowing the error
        is still right — a lost preference must degrade personalization, not fail a turn — but
        answering "Remembered for future sessions" afterwards is not.
        """
        self._memory[(owner, key)] = value
        if settings.session_store != "postgres":
            return True
        try:
            async with self._connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(_UPSERT, (owner, key, value))
                await conn.commit()
        except Exception:
            degraded(logger, "preferences", "could not persist preference %r for %s", key, owner)
            return False
        return True

    async def recall(self, owner: str) -> list[Preference]:
        """Every preference `owner` has set, key-sorted (stable for the model to read)."""
        if settings.session_store == "postgres":
            try:
                async with self._connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(_SELECT, (owner,))
                        rows = await cur.fetchall()
                return [Preference(key=row[0], value=row[1]) for row in rows]
            except Exception:
                logger.warning("could not read preferences for %s", owner, exc_info=True)
                if not any(row_owner == owner for row_owner, _key in self._memory):
                    # Falling back to memory is right when memory has something — it is this
                    # process's own view of the same preferences. But an *empty* fallback after a
                    # failed read is not "this chemist has no preferences", which is exactly how
                    # an empty list reads to the model; it is "I could not find out". A wrong
                    # answer is worse than a failed one, because the chemist then re-states
                    # preferences that also will not persist.
                    raise
        return [
            Preference(key=key, value=value)
            for (row_owner, key), value in sorted(self._memory.items())
            if row_owner == owner
        ]

    async def forget(self, owner: str, key: str) -> bool:
        """Drop one preference — a chemist must be able to take a preference back.

        Returns whether the deletion reached the configured store. The failure mode here is the
        worse direction of the two: the in-memory copy is gone, so the preference *looks* removed
        for the rest of this session and then reappears from Postgres on the next one. A chemist
        who asked for something to be forgotten and was told it was must not find it back.
        """
        self._memory.pop((owner, key), None)
        if settings.session_store != "postgres":
            return True
        try:
            async with self._connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(_DELETE, (owner, key))
                await conn.commit()
        except Exception:
            degraded(logger, "preferences", "could not delete preference %r for %s", key, owner)
            return False
        return True


# One process-wide store, like the audit sink's default: the tools below need it without threading
# an instance through the whole agent construction.
_STORE = PreferenceStore()


@tool
async def remember_preference(key: str, value: str) -> str:
    """Remember how this chemist likes to work, for future turns and future sessions.

    Use this for durable *working* preferences the chemist states — their current project, a
    preferred solvent system or base, the units they think in, a constraint they always apply
    ("no chlorinated solvents on scale"). Recall them with `recall_preferences` at the start of a
    substantive answer so advice fits how they actually work.

    Do **not** use this for chemistry knowledge: a distilled rule, a protocol, or a result belongs
    in the knowledge graph via `propose_knowledge_note`, where a human reviews it. This store is
    personal and unreviewed, and putting shared knowledge here would route it around the PR-gate.

    Args:
        key: Short stable name, e.g. "project", "preferred_solvent", "units".
        value: What to remember.

    Returns:
        Confirmation of what was stored.
    """
    owner = require_actor()
    if await _STORE.remember(owner, key, value):
        return f"Remembered {key}={value!r} for this chemist."
    return (
        f"Remembered {key}={value!r} for THIS SESSION ONLY — it could not be saved durably, so it "
        "will be gone once this session ends. Tell the chemist that, so they can restate it later "
        "rather than believing it is on file."
    )


@tool
async def recall_preferences() -> list[Preference]:
    """Recall how this chemist likes to work (their project, solvents, units, constraints).

    Call this early in a substantive answer so recommendations fit their actual practice rather
    than generic defaults. An empty list simply means nothing has been recorded yet — never invent
    a preference, and never assume one from a single past message.

    Returns:
        Every preference this chemist has set, key-sorted.
    """
    return await _STORE.recall(require_actor())


@tool
async def forget_preference(key: str) -> str:
    """Forget one of this chemist's preferences, when they say it no longer applies.

    Args:
        key: The preference name to drop.

    Returns:
        Confirmation.
    """
    owner = require_actor()
    if await _STORE.forget(owner, key):
        return f"Forgot {key} for this chemist."
    return (
        f"Dropped {key} for THIS SESSION ONLY — the deletion could not be saved, so the preference "
        "will come back in the chemist's next session. Tell them it is not yet permanently "
        "removed; this is the direction of failure they most need to know about."
    )
