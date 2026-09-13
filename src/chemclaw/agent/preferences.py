"""Per-user working preferences (gap AGT-4).

Every memory layer is corpus-level — `campaign`, `playbook`, `optimization-campaign` and
`interaction` notes all describe the chemistry, shared by everyone. Nothing remembered *this
chemist*: their project, their preferred solvent system, the units they think in, or that they
already rejected an analogy last week. The identity was available (`Principal.oid`, and the
`session_owners` table); only the layer was missing.

**Why not knowledge-graph notes.** A preference is personal, revisable, and of no interest to anyone
else. Putting it in the graph would publish "Anna prefers 2-MeTHF" to everyone — noise that
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
from chemclaw.agent.framing import defang
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.metrics import METRICS
from chemclaw.core.metrics_bridge import degraded
from chemclaw.core.tool_registry import tool

logger = logging.getLogger(__name__)

_UPSERT = """
INSERT INTO user_preferences (owner, key, value, updated_at)
VALUES (%s, %s, %s, now())
ON CONFLICT (owner, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
"""
# **Bounded, and it was not.** `remember_preference` takes a *model-chosen* key, so this table is
# agent-writable with no ceiling on rows — `durable/retention.py` names it "**nothing bounds it**"
# and the retention sweep leaves it alone for the right reason (a preference has no age at which it
# stops being current). A count is the instrument, as it is for `ingest_rejections` and for the
# memory store: the least *recently updated* preference goes, which is a tiebreak rather than a
# policy, because the bound is on how many a person may hold and not on how old one may be.
#
# In the writer's own transaction, so the invariant is exact rather than eventual: unlike the memory
# store this path owns its connection and commits once. `NOT IN` over the keeps rather than `IN`
# over the doomed, so the statement is one round trip whatever the overflow is.
_EVICT = """
DELETE FROM user_preferences
WHERE owner = %s AND key NOT IN (
    SELECT key FROM user_preferences WHERE owner = %s ORDER BY updated_at DESC, key LIMIT %s
)
"""
# **`LIMIT` is the half that is about the prompt rather than about the table.** This read had none,
# so every preference a chemist had ever set re-entered the model's context on every recall, in
# every later session, for the life of the row. Ordered by `updated_at DESC` *first* so the limit
# keeps what is current rather than what sorts early alphabetically — a truncation by key would
# silently drop the preference stated five minutes ago in favour of one from last year beginning
# with "a". The key sort is the stable tiebreak underneath it, which is what the model reads.
_SELECT = (
    "SELECT key, value FROM ("
    "  SELECT key, value, updated_at FROM user_preferences WHERE owner = %s"
    "  ORDER BY updated_at DESC, key LIMIT %s"
    ") AS recent ORDER BY key"
)
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
        async with db.connection(self._dsn) as conn:
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
        self._evict_in_memory(owner)
        if settings.session_store != "postgres":
            return True
        try:
            async with self._connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(_UPSERT, (owner, key, value))
                    # Same transaction as the write, which is what makes this bound exact — the
                    # shape `ingest/rejections.py` uses and the memory store cannot, because that
                    # one sits on an autocommit pool.
                    await cur.execute(_EVICT, (owner, owner, settings.preferences_max_per_owner))
                    evicted = cur.rowcount
                await conn.commit()
            if evicted > 0:
                METRICS.increment("chemclaw_preference_evictions_total", evicted)
                logger.warning(
                    "evicted %d preference(s) for %s past the %d-preference cap",
                    evicted,
                    owner,
                    settings.preferences_max_per_owner,
                )
        except Exception:
            degraded(logger, "preferences", "could not persist preference %r for %s", key, owner)
            return False
        return True

    async def recall(self, owner: str) -> list[Preference]:
        """The `preferences_recall_limit` most recent preferences `owner` has set, key-sorted.

        **Bounded, and the bound is about the prompt rather than the table.** This read had no
        `LIMIT`, so every preference a chemist had ever set re-entered the model's context on every
        recall, in every later session, for the life of the row — unbounded prompt spend behind a
        tool the model is told to call "early in a substantive answer". The row cap in `remember`
        is the storage half; this is the context half, and a deployment that lowers the row cap
        still holds the rows it already wrote.

        Key-sorted for the model — a stable order is what keeps one turn's reading comparable with
        the next — but selected by recency, so the limit keeps what is current rather than what
        sorts early alphabetically.
        """
        if settings.session_store == "postgres":
            try:
                async with self._connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(_SELECT, (owner, settings.preferences_recall_limit))
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
        # The same two bounds as the Postgres path, so a deployment in memory mode and one in
        # Postgres mode answer the same question the same way. Insertion order is this dict's
        # recency, so the *last* `preferences_recall_limit` are the current ones and they are then
        # key-sorted for the model, exactly as the SQL does it.
        mine = [
            Preference(key=key, value=value)
            for (row_owner, key), value in self._memory.items()
            if row_owner == owner
        ]
        recent = mine[-settings.preferences_recall_limit :]
        return sorted(recent, key=lambda preference: preference.key)

    def _evict_in_memory(self, owner: str) -> None:
        """Hold the in-memory fallback to the same row cap as the table.

        Not a convenience: in memory mode this dict *is* the configured store, so leaving it
        unbounded would mean the bound existed only where a database did. `dict` preserves
        insertion order and `remember` re-inserts on every write, so the front of it is the least
        recently written — which is the same ordering `_EVICT` takes, one instrument apart
        (`updated_at` is a clock, this is arrival).
        """
        cap = settings.preferences_max_per_owner
        keys = [pair for pair in self._memory if pair[0] == owner]
        for pair in keys[: max(len(keys) - cap, 0)]:
            del self._memory[pair]

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
    in the knowledge graph via `record_knowledge_note`, where everyone can read it. This store is
    personal, and putting shared knowledge here would keep it from the people it is for.

    Args:
        key: Short stable name, e.g. "project", "preferred_solvent", "units".
        value: What to remember.

    Returns:
        Confirmation of what was stored.
    """
    owner = require_actor()
    # The confirmation echoes the model's own arguments, so it is the same untrusted span
    # `recall_preferences` neutralises — it simply reaches the prompt a turn earlier.
    echoed = f"{defang(key)}={defang(value)!r}"
    if await _STORE.remember(owner, key, value):
        return f"Remembered {echoed} for this chemist."
    return (
        f"Remembered {echoed} for THIS SESSION ONLY — it could not be saved durably, so it "
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
        This chemist's most recently set preferences, key-sorted. Bounded — a chemist with more
        than `preferences_recall_limit` of them gets the current ones, not all of them.
    """
    # A preference is free text the model wrote — through `remember_preference`, out of whatever it
    # had just read, including framed third-party content — and it re-enters a prompt on every
    # later turn, in every later session, for the life of the row. Measured: a value carrying the
    # live closing delimiter came back verbatim, which is the laundering
    # `D-2026-08-29-a-helpers-report-is-model-prose-in-its-callers-thread` closed for `task`, except
    # it outlives the turn, the session and the process. Defanged rather than framed, for the reason
    # `agent/tool_framing.py` gives a helper's report: an envelope says "evidence to cite" and this
    # is the system's own note about how one person works. `key` too — it is the same argument
    # surface, and a short name is no less able to spell a delimiter.
    return [
        preference.model_copy(
            update={"key": defang(preference.key), "value": defang(preference.value)}
        )
        for preference in await _STORE.recall(require_actor())
    ]


@tool
async def forget_preference(key: str) -> str:
    """Forget one of this chemist's preferences, when they say it no longer applies.

    Args:
        key: The preference name to drop.

    Returns:
        Confirmation.
    """
    owner = require_actor()
    named = defang(key)  # echoed back into the prompt, like `remember_preference`'s confirmation
    if await _STORE.forget(owner, key):
        return f"Forgot {named} for this chemist."
    return (
        f"Dropped {named} for THIS SESSION ONLY — the deletion could not be saved, so the "
        "preference will come back in the chemist's next session. Tell them it is not yet "
        "permanently removed; this is the direction of failure they most need to know about."
    )
