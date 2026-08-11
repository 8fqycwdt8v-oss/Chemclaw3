"""Durable, Postgres-backed conversation history (plan Phase F3).

`PostgresHistoryProvider` appends each turn's exchange to the `session_messages` table keyed by
session id and loads it back in insertion order, so a fresh process over the same database can show
a conversation that outlived its pod — the "session survives a restart" requirement (F3-T1).

**It is a read-model projection, not the conversation's state.** That is the change D-2026-08-10 §2
made and it is what everything below follows from. Under MAF this table *was* the thread: the
framework wrote it as the turn went and read it back before each model call, which made it
load-bearing, made it grow without bound, made a half-written turn a poison pill, and made three
mechanisms necessary that are now gone (a disconnect rollback, a read-time orphan repair, and a
compaction pass over the stored rows). Turn state lives in the LangGraph checkpointer now. What is
written here is written once, by `chemclaw.api.runner._record_transcript`, after the answer exists;
what reads it is `GET /sessions/{id}/messages` and the audit trail's join, both for a person.

This is the conversation layer, deliberately separate from Temporal job state (D-002) and the
calculation cache. A message is stored as LangChain's own `message_to_dict()`, so the column is a
serialization the library owns; what this module interprets is only *which* serialization a row
holds (`message_from_row`), because the table still contains rows the previous framework wrote.

Three stores live here because they are one session's durable state and must share a database:
the message history above, `SessionOwnerStore` (who owns a session id — the fact the in-process
LRU loses on restart), and `SessionTurnClaims` (which process is running a turn on it right now —
the fact the in-process 409 guard loses at the pod boundary, D-121).

**`get_messages` has no `LIMIT` and must not grow one.** That used to be a data-safety rule, because
the read repaired tool-call pairings and wrote the repair back. It is now a rendering rule: the
reader is a person reloading a conversation, and a transcript that silently omits its own beginning
does not look truncated — it looks like the conversation started later than it did.

**The table is bounded by `durable/retention.py`, by age, and by nothing else.** A compaction pass
used to shrink it too, applying the model's context-window policy (`keep_last_conversation_groups`)
to the stored rows. That was right while the rows were the model's context and wrong the moment they
stopped being: it deleted a chemist's older messages not because any policy said to keep less, but
because the model no longer needed them — a context heuristic quietly editing a GxP record. Age-
based retention is the policy statement a deployment actually makes, and it deletes only whole
pairing components (`droppable_rows`, D-145).
"""

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from typing import Any

import psycopg
from langchain_core.messages import AIMessage, BaseMessage, message_to_dict, messages_from_dict
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from chemclaw.agent.message_migration import (
    LANGCHAIN_SHAPE,
    UnconvertibleMessage,
    to_langchain,
)
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.identity_context import get_current_correlation_id

log = logging.getLogger(__name__)


def message_from_row(payload: dict[str, Any], shape: str | None) -> BaseMessage:
    """One stored row as a LangChain message, whichever shape it holds.

    Public because it has a second reader outside this module: `chemclaw.cli.explain` reconstructs
    the same conversation for the audit join, and a CLI that parsed the stored payload itself is
    exactly how a table holding two shapes acquires a reader that knows one. (It did: the CLI read
    the legacy shape only, so every row written after the M6 conversion rendered blank.) One
    function knows the shapes; everything else asks it.

    Both shapes read, and that is what the `message_shape` stamp is for (D-2026-08-10 §"why a shape
    version"): a rollout is not atomic, and `make db-migrate`'s conversion pass is resumable, so
    during it some rows are MAF and some are LangChain. An unstamped row is MAF, because every row
    written before the stamp existed has no stamp and rewriting them all to add one is exactly the
    rewrite the version exists to avoid.

    A row that will not convert degrades to its own text rather than raising. `to_langchain` is
    deliberately strict — a migration must stop on a shape nobody anticipated rather than guess —
    but this is the *read* path, and the reader is a chemist reloading a conversation. Failing the
    whole transcript because one historical row holds a content type this system no longer writes
    would lose the conversation to protect it.
    """
    if shape == LANGCHAIN_SHAPE:
        return messages_from_dict([payload])[0]
    try:
        return to_langchain(payload)
    except UnconvertibleMessage:
        log.warning("could not render a stored message; showing it as plain text", exc_info=True)
        return AIMessage(content=str(payload.get("text", "")))


# The correlation id makes a stored message joinable to the audit rows of the turn that wrote it
# (D-2026-07-31-the-audit-chain-is-versioned).
# Without it the two halves of "what happened in this conversation" — the words and the
# tool calls — sat in tables with no key between them, so the GxP trail could show *that* a tool ran
# and never *why*.
_INSERT = (
    "INSERT INTO session_messages (session_id, message, message_shape, correlation_id) "
    "VALUES (%s, %s, %s, %s)"
)
# Row ids come back too, so a repaired message can be written to the row it came from. There is no
# id-less variant: every reader needs the id, and the one that existed was dead code that D-143's
# prose then cited as the statement the read path runs.
_SELECT_WITH_ID = (
    "SELECT id, message, message_shape FROM session_messages WHERE session_id = %s ORDER BY id"
)

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


def _session_dsn() -> str:
    """Resolve the session layer's DSN: `session_store_dsn`, else the shared `postgres_dsn`.

    One resolver for all three stores in this module, so they can never end up pointing at
    different databases — the ownership row, the turn claim and the message history are one
    session's state and must live together (D-002).

    It took a `dsn` override until the 2026-08-05 review counted the call sites: all twenty, in
    `src/` and in `tests/`, construct these classes with no arguments, so the first branch of
    `dsn or …` was unreachable in the whole tree. A parameter nothing passes is a parameter that
    documents a capability the deployment does not have.
    """
    return settings.session_store_dsn or settings.postgres_dsn


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
    async with db.connection(dsn) as conn:
        yield conn


class PostgresHistoryProvider:
    """Persists a session's transcript to Postgres, and reads it back for a person.

    A plain class since M13. It subclassed MAF's `HistoryProvider` while the framework asked a
    provider for the thread it was about to send a model; nothing asks now — the graph reads its
    checkpointer — so the base class contributed a `source_id` and a set of hooks with no callers.
    What is left is the two storage primitives it always overrode.
    """

    def __init__(self) -> None:
        """Configure the provider against the session-store database."""
        self._dsn = _session_dsn()

    def _connection(self) -> AbstractAsyncContextManager[psycopg.AsyncConnection[TupleRow]]:
        """Borrow a connection on this provider's database (see `_session_connection`)."""
        return _session_connection(self._dsn)

    async def get_messages(
        self, session_id: str | None, *, state: dict[str, Any] | None = None, **kwargs: Any
    ) -> list[BaseMessage]:
        """Load a session's messages in insertion order (empty for an unknown/None session).

        A plain read, and the absence of the repair that used to sit here is the point. That repair
        dropped a function call no result answered, and wrote the correction back, because the
        thread it returned was fed straight to the model and an unmatched `tool_use` makes every
        later turn on the session fail outright — a `SIGKILL` between the call and its result
        leaves one behind and runs no cleanup handler. Both halves of that are gone: the graph
        builds its thread from the checkpointer, never from here, and the only caller left is the
        transcript route, which renders for a person. New rows cannot even acquire an orphan, since
        the projection writes the user's message and the answer as plain text (D-2026-08-10 §2).
        """
        if not session_id:
            return []
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT_WITH_ID, (session_id,))
                rows = await cur.fetchall()
        return [message_from_row(row[1], row[2]) for row in rows]

    async def save_messages(
        self,
        session_id: str | None,
        messages: Sequence[BaseMessage],
        *,
        state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Append this turn's messages to the session's durable history (no-op if none to store).

        One statement in one transaction, and nothing follows it. The turn's exchange lands whole or
        not at all, which is what lets `chemclaw.api.runner` carry no rollback: there is no window
        in which half of it is committed. Bounding the table is `durable/retention.py`'s job, on its
        own schedule, and deliberately not this call's — an append on the answer path must not also
        be deciding what to delete.
        """
        if not session_id or not messages:
            return
        # Read once for the whole batch: these messages are one turn's work, so they share its
        # correlation id. Empty off the request path (the CLI, tests), where there is no turn.
        correlation_id = get_current_correlation_id() or ""
        rows = [
            (session_id, Jsonb(message_to_dict(message)), LANGCHAIN_SHAPE, correlation_id)
            for message in messages
        ]
        async with self._connection() as conn:
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

    def __init__(self) -> None:
        """Bind to the session-store database (falling back to the shared `postgres_dsn`)."""
        self._dsn = _session_dsn()

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
    set needed a process restart. The cost is the standard lease property, stated in
    `core/config/service.py` beside the lease setting itself: exclusion holds as long as the holder
    is scheduled often enough to refresh. (This sentence used to cite `chemclaw.api.app`, which
    never said it — the 2026-08-05 review grepped for the claim and found it in the config and in
    D-121, not there.)
    """

    def __init__(self) -> None:
        """Bind to the session-store database (falling back to the shared `postgres_dsn`)."""
        self._dsn = _session_dsn()

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

    async def refresh(self, session_id: str, holder: str, lease_seconds: float) -> bool:
        """Push this holder's claim out by another lease; False if it is no longer ours.

        A no-op when the claim is gone or now belongs to someone else: that means this worker was
        already declared dead, and re-taking the slot behind the live holder's back is exactly
        the interleaving the guard exists to prevent.

        **It returns whether the claim survived, and that return value is the point.** The no-op
        was correct and silent: `rowcount` was discarded, so a holder whose lease had been taken
        over could not tell, and `api/state.py::_hold_turn_claim` reacts only to *exceptions* — of
        which a silent takeover raises none. Its own warning ("another worker may start a turn on
        this session") was therefore unreachable in exactly the scenario it describes. Found by
        the 2026-08-05 review.
        """
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_TURN_REFRESH, (lease_seconds, session_id, holder))
                still_ours = cur.rowcount == 1
            await conn.commit()
        return still_ours

    async def release(self, session_id: str, holder: str) -> None:
        """Give the slot back at the end of the turn (idempotent; only this holder's row goes)."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_TURN_RELEASE, (session_id, holder))
            await conn.commit()


class InMemoryHistoryProvider:
    """The dev/test transcript store: the same two primitives, over the session's own state.

    Keeps the thread in `session.state` — the dict `TurnSession` carries — which is why both
    primitives take `state`. That is not incidental: it is the whole difference from the Postgres
    provider, which deliberately keeps nothing there, and it is why a memory-backed session's
    transcript dies with the pod.

    First-party since M13, replacing MAF's provider of the same name. Twelve lines rather than an
    import because the framework's version carried a thread the model was sent, a compaction seam
    and a set of run hooks — none of which has a caller now that the graph reads its checkpointer.
    """

    _KEY = "chemclaw_transcript"

    async def get_messages(
        self, session_id: str | None, *, state: dict[str, Any] | None = None, **kwargs: Any
    ) -> list[BaseMessage]:
        """This session's stored transcript, or empty when it has none."""
        if state is None:
            return []
        stored = state.get(self._KEY) or []
        return list(stored)

    async def save_messages(
        self,
        session_id: str | None,
        messages: Sequence[BaseMessage],
        *,
        state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Append this turn's exchange to the session's state (no-op without one)."""
        if state is None or not messages:
            return
        state.setdefault(self._KEY, []).extend(messages)
