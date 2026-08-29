"""Branch a session onto a new thread, so a chemist can try another line without losing this one.

**What a fork is for.** A session is a thread of turns and every turn is charged against the one
before it — a campaign that has spent twenty turns establishing a substrate, a set of constraints
and a shortlist. Asking "what if the solvent were different" against that context has, until now,
been a destructive act: the new turn joins the same thread, and the thread is what the model sees
next turn. The alternatives were both bad — start a fresh session and re-establish twenty turns of
context by hand, or ask in place and accept that the branch is now part of the trunk.

**A fork is a copy, not a pointer**, and that is the decision this module makes rather than
inherits. The parent thread is not touched, not marked, and does not know it was forked; the child
is an ordinary session from the moment it exists, indistinguishable to every reader from one that
grew that way. The alternative — a child that references its parent's rows and copies on write —
buys storage and costs correctness in exactly the place this system cannot afford it: retention
prunes **by thread** (`durable/retention.py`), so a shared row is one whose lifetime is the parent's
and whose reader is the child, and the child would lose its own past when the parent aged out.

**That last clause was the argument for copying, and copying alone did not deliver it.** A copied
checkpoint carries the parent's `ts`, retention expires a thread on `max(ts)`, so the fork inherited
the parent's age and was deleted by the next sweep — losing its past at the same instant a pointer
would have, by a different route. What actually makes the fork's clock its own is `_RESTAMP_NEWEST`,
and the copy is what makes restamping *possible*: there is a row of the child's own to stamp.

**Three things a naive copy gets wrong, each measured against the schema rather than assumed:**

1. **The whole thread, not the tip.** `checkpoint_blobs` is keyed `(thread_id, checkpoint_ns,
   channel, version)` and its rows are *shared across a thread's checkpoints* — a channel written
   at version 3 and unchanged since is stored once and referenced by every later checkpoint. Copying
   only the newest checkpoint's rows therefore loses every channel value whose version predates it,
   and loses it silently: the fork resumes with holes rather than failing.
2. **The transcript, not only the checkpoint.** A session with no `session_messages` rows is
   **invisible** to `GET /sessions`, whose owner listing `LATERAL`-joins `max(created_at)` over that
   table and drops sessions with none. A fork that copied only graph state would be a session the
   chemist who asked for it could not find.
3. **The parent's profile, not the default.** A profile is attenuation-only, so restoring the
   default on a fork would *widen* the tool surface — the child of a narrowed session would be able
   to do more than its parent. `_rehydrate_session` makes the same argument for the same reason.

**And one table deliberately left behind: `session_events`.** Those are job push-backs waiting to be
consumed, and a copy would deliver each one twice — once to the parent and once to the child — for a
job that ran once. They are a *queue*, not history: `claim_unconsumed` takes them, so the correct
reading of "this session has a result waiting" is that exactly one session does. The parent launched
the job and keeps them.

**Why this is SQL rather than a LangGraph call.** There is no fork, copy or branch verb on
`BaseCheckpointSaver`; `adelete_thread` is the only thread-level operation upstream offers. What
makes the copy safe to write here anyway is that every checkpoint table's primary key *leads with*
`thread_id`, so an `INSERT … SELECT` with the id substituted can neither collide with the parent's
rows nor with another fork's. The tables are named from `CHECKPOINT_TABLES` rather than spelled
here, so a fourth table added upstream is a failure this module notices rather than one it silently
skips.
"""

import logging
import uuid

from psycopg import sql

from chemclaw.agent.checkpointer import CHECKPOINT_TABLES

# The underscore names are imported across modules deliberately and by precedent —
# `agent/checkpointer.py`, `agent/leaver.py` and `agent/plan_approval_store.py` all do the same.
# They are one module's private *spelling* of "the session database", not a private capability.
# `SessionOwnerStore` is deliberately *not* used: it opens a connection of its own, and the fork's
# ownership row has to be written on the copy's connection to share its transaction.
from chemclaw.agent.session_store import _session_connection, _session_dsn
from chemclaw.core.errors import ChemclawError

logger = logging.getLogger(__name__)


class SessionForkError(ChemclawError):
    """A fork could not be taken — the parent has no state, or the copy failed."""


# The columns are not listed. `INSERT INTO t SELECT %s, <every other column> FROM t` would have to
# name them, and naming them here is a second declaration of upstream's schema that nothing checks —
# the failure `manifests/README.md` forbids for a manifest and `MODULES.md` for a port. Instead the
# row is copied whole and the thread id is *overwritten* afterwards inside the same transaction,
# which needs no column list and cannot drift: a column added upstream is carried along rather than
# dropped to its default.
_COPY_THREAD = sql.SQL(
    "CREATE TEMPORARY TABLE {temp} ON COMMIT DROP AS SELECT * FROM {table} WHERE thread_id = %s"
)
_RETHREAD = sql.SQL("UPDATE {temp} SET thread_id = %s")
_INSERT_BACK = sql.SQL("INSERT INTO {table} SELECT * FROM {temp}")

# The transcript, copied so the fork is visible to the owner listing and reads back as a
# conversation. `id` is a `BIGSERIAL` and is deliberately *not* carried over — it is the table's own
# identity, not the message's, and reusing it would collide.
_COPY_MESSAGES = """
    INSERT INTO session_messages (session_id, message, created_at, message_shape, correlation_id)
    SELECT %s, message, created_at, message_shape, correlation_id
    FROM session_messages WHERE session_id = %s
"""

# The stored tool results the transcript points at. A `session_messages` row carries a `result_ref`
# handle per call; `api/tool_results.py` resolves it through *this* table, joined on `session_id`.
# Copy the transcript without the links and every one of those handles resolves to nothing, so the
# fork shows the 400-character preview where the parent shows what the tool returned — the exact
# regression `D-2026-08-09-a-preview-is-not-a-result` exists to prevent.
#
# **The blob is shared, not copied**, which is the point of a content-addressed store: the link
# names a `content_hash` that already exists, so a fork costs one small row per result rather than
# a second copy of the bytes. It also *protects* them — `session_store`'s deletion arm spares a blob
# any link still references, so the fork's links are what stop deleting the parent from destroying
# results the fork's own transcript still displays.
_COPY_TOOL_RESULT_LINKS = """
    INSERT INTO tool_result_links (session_id, content_hash, tool, correlation_id, created_at)
    SELECT %s, content_hash, tool, correlation_id, created_at
    FROM tool_result_links WHERE session_id = %s
    ON CONFLICT DO NOTHING
"""

# The fork's ownership row, written on the **same connection and inside the same transaction** as
# the copy — see `fork_session` for why that is not the tidier-looking alternative.
_RECORD_OWNER = """
    INSERT INTO session_owners (session_id, owner, profile)
    VALUES (%s, %s, %s)
    ON CONFLICT (session_id) DO NOTHING
"""

# **What makes the fork's own clock start now.** `durable/retention.py` expires a thread on
# `max((checkpoint->>'ts')::timestamptz)`, and a copied checkpoint carries the parent's `ts` — so a
# fork of a conversation last touched a year ago is already past the window the instant it is
# created, and the next sweep deletes its whole thread while `session_owners` and `session_messages`
# survive. Measured: the session still lists, still opens, still renders every turn from the
# transcript, and the next turn runs with **no history at all**, because context comes from the
# checkpointer rather than from `session_messages`.
#
# That is worth stating plainly because this module's own opening argument was wrong about it.
# Copy-rather-than-pointer was chosen so the child would not "lose its own past when the parent aged
# out" — and the copy inherited the parent's age, so it lost it at the same instant, by a different
# route.
#
# **Only the newest checkpoint is restamped**, and only its `ts`. Retention reads `max(ts)`, so one
# row answers the question it actually asks — *when was this thread last active* — and the honest
# answer for a fork is "now, it was just created". The older checkpoints keep their true creation
# times, which are facts about when the parent wrote them and are not this module's to rewrite.
# `checkpoint_id` is untouched: LangGraph orders and resolves parents by that, never by `ts`.
_RESTAMP_NEWEST = """
    UPDATE checkpoints SET checkpoint = jsonb_set(checkpoint, '{ts}', to_jsonb(now()::text))
    WHERE thread_id = %s AND checkpoint_id = (
        SELECT checkpoint_id FROM checkpoints WHERE thread_id = %s
        ORDER BY (checkpoint->>'ts')::timestamptz DESC LIMIT 1
    )
"""

_COUNT_CHECKPOINTS = "SELECT count(*) FROM checkpoints WHERE thread_id = %s"


async def fork_session(parent_id: str, owner: str | None, profile: str | None) -> str:
    """Copy a session's whole thread onto a new id and return it.

    The copy is **one transaction across every table**, for the reason `durable/retention.py` gives
    for its own: a fork visible in the owner listing but missing half its checkpoint blobs is worse
    than no fork at all, because the chemist finds it and it does not work.

    Args:
        parent_id: The session to branch from. Its rows are read and never written.
        owner: The principal the fork belongs to — the caller's, already authorized against the
            parent by `resolve_session`. Passed in rather than looked up here so that this function
            cannot be the place an ownership check is forgotten.
        profile: The parent's profile, carried over because a profile only ever narrows.

    Returns:
        The new session id.

    Raises:
        SessionForkError: The parent holds no checkpoint at all, so there is nothing to branch
            from. Refused rather than silently producing an empty session, which would look like a
            fork that worked and behave like a session that had never been used.
    """
    child_id = uuid.uuid4().hex
    async with _session_connection(_session_dsn()) as conn:
        async with conn.cursor() as cur:
            await cur.execute(_COUNT_CHECKPOINTS, (parent_id,))
            row = await cur.fetchone()
            if not row or not row[0]:
                raise SessionForkError(
                    f"session {parent_id} has no saved state to fork — it has taken no turn yet"
                )
            for index, table in enumerate(CHECKPOINT_TABLES):
                # A temp name per table, and per statement rather than reused, because
                # `ON COMMIT DROP` keeps them alive for the whole transaction.
                temp = sql.Identifier(f"fork_{index}")
                target = sql.Identifier(table)
                await cur.execute(_COPY_THREAD.format(temp=temp, table=target), (parent_id,))
                await cur.execute(_RETHREAD.format(temp=temp), (child_id,))
                await cur.execute(_INSERT_BACK.format(table=target, temp=temp))
            await cur.execute(_RESTAMP_NEWEST, (child_id, child_id))
            await cur.execute(_COPY_MESSAGES, (child_id, parent_id))
            await cur.execute(_COPY_TOOL_RESULT_LINKS, (child_id, parent_id))
            # **The ownership row is written here, inside the same transaction**, and the first
            # version of this module wrote it afterwards on a separate round trip. The reasoning
            # then was about ordering — "written first, a failed copy would leave a session that
            # lists and cannot load" — and it compared the two orders without noticing that one
            # transaction removes the choice: both rows commit or neither does.
            #
            # What the separate write actually cost is worse than a session that cannot load. If it
            # failed — a statement timeout, a pool exhausted, the pod killed between the two — the
            # copied checkpoints and the copied transcript stayed, keyed by a `session_id` that
            # `session_owners` never learned. `agent/leaver.py` scopes erasure through
            # `SELECT session_id FROM session_owners WHERE owner = ANY(...)`, so those rows are
            # **structurally unreachable** by the one sweep that must never miss anything:
            # measured, a chemist's transcript survived their own erasure while the report said the
            # erasure was complete. `durable/retention.py` states the invariant this violates —
            # an ownership row absent while session-scoped rows remain "puts it beyond *erasure*".
            await cur.execute(_RECORD_OWNER, (child_id, owner, profile))
        await conn.commit()

    logger.info("forked session %s onto %s", parent_id, child_id)
    return child_id
