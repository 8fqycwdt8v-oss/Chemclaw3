"""Branch a session onto a new thread, so a chemist can try another line without losing this one.

A fork is a copy, not a pointer: the parent is untouched and the child is an ordinary session.
Retention prunes by thread, so shared rows would tie the child's lifetime to the parent's; and the
copy gives the child rows of its own to restamp (`_RESTAMP_NEWEST`), so its retention clock starts
now.

What the copy must include:

1. **The whole thread, not the tip.** `checkpoint_blobs` rows are shared across a thread's
   checkpoints, so copying only the newest checkpoint's rows loses older channel values (and
   resuming then raises `CheckpointValuesMissing`).
2. **The transcript.** A session with no `session_messages` rows is invisible to `GET /sessions`.
3. **The parent's profile.** A profile only narrows; the default would widen the child's tool
   surface.
4. **The artefacts, as they stand** (`exhibits.store.fork_exhibits`): each head revision becomes
   revision 1 of a new artefact in the child.

`session_events` is deliberately left behind: pending job push-backs are a queue, and copying them
would deliver each twice.

This is SQL because `BaseCheckpointSaver` has no fork verb. Every checkpoint table's primary key
leads with `thread_id`, so an `INSERT … SELECT` with the id substituted cannot collide; tables are
named from `CHECKPOINT_TABLES` so an upstream addition is noticed.
"""

import logging
import uuid

from psycopg import sql

from chemclaw.agent.checkpointer import CHECKPOINT_TABLES

# Private names imported deliberately (as `agent/checkpointer.py` and others do). `_OWNER_INSERT` is
# the statement `SessionOwnerStore.record` runs, reused rather than retyped, because the fork must
# write the ownership row on its own connection to share the copy's transaction.
from chemclaw.agent.session_store import _OWNER_INSERT, _session_connection, _session_dsn
from chemclaw.core.errors import ChemclawError
from chemclaw.exhibits.store import fork_exhibits

logger = logging.getLogger(__name__)


class SessionForkError(ChemclawError):
    """A fork could not be taken — the parent has no state, or the copy failed."""


# Rows are copied whole and the thread id is overwritten afterwards in the same transaction, so no
# column list restates upstream's schema and a new upstream column is carried along.
_COPY_THREAD = sql.SQL(
    "CREATE TEMPORARY TABLE {temp} ON COMMIT DROP AS SELECT * FROM {table} WHERE thread_id = %s"
)
_RETHREAD = sql.SQL("UPDATE {temp} SET thread_id = %s")
_INSERT_BACK = sql.SQL("INSERT INTO {table} SELECT * FROM {temp}")

# The transcript, so the fork is listed and reads back as a conversation. `id` is the table's own
# `BIGSERIAL` and is not carried over.
#
# `created_at` is shifted by one interval (`now() - max(created_at)` over the parent), so the newest
# message lands at now and the spacing is preserved; otherwise the owner listing would date the fork
# by the parent and message retention would delete its transcript.
#
# Authorship is copied, not reassigned to the forker: it is a fact about who said each message. A
# turn still `running` in the parent is recorded as `interrupted`, since nothing will ever settle
# the copy.
_COPY_MESSAGES = """
    INSERT INTO session_messages
        (session_id, message, created_at, message_shape, correlation_id, actor, agent, turn_status)
    SELECT %s, message,
           created_at + (now() - (
               SELECT max(created_at) FROM session_messages WHERE session_id = %s
           )),
           message_shape, correlation_id, actor, agent,
           CASE WHEN turn_status = 'running' THEN 'interrupted' ELSE turn_status END
    FROM session_messages WHERE session_id = %s
"""

# The stored tool results the transcript's `result_ref` handles resolve through (joined on
# `session_id`); without them the fork would show only previews. The content-addressed blob is
# shared, not copied, and the fork's links also keep it alive if the parent is deleted.
_COPY_TOOL_RESULT_LINKS = """
    INSERT INTO tool_result_links (session_id, content_hash, tool, correlation_id, created_at)
    SELECT %s, content_hash, tool, correlation_id, created_at
    FROM tool_result_links WHERE session_id = %s
    ON CONFLICT DO NOTHING
"""

# Start the fork's own retention clock. Retention expires a thread on `max(checkpoint ts)`, and
# copied checkpoints carry the parent's `ts`, so without this a fork of an old conversation would
# lose its history on the next sweep.
#
# Only the newest checkpoint's `ts` is restamped: that answers retention's question, and older
# checkpoints keep their true times. `checkpoint_id`, which LangGraph orders by, is untouched.
_RESTAMP_NEWEST = """
    UPDATE checkpoints SET checkpoint = jsonb_set(checkpoint, '{ts}', to_jsonb(now()::text))
    WHERE thread_id = %s AND checkpoint_id = (
        SELECT checkpoint_id FROM checkpoints WHERE thread_id = %s
        ORDER BY (checkpoint->>'ts')::timestamptz DESC LIMIT 1
    )
"""

# A parent is forkable only with both graph state and a transcript row; checkpoints are written from
# the first node but the transcript only after the first answer, and a fork with no messages would
# never be listed. One statement, so the answer cannot change between two round trips.
_COUNT_FORKABLE = (
    "SELECT (SELECT count(*) FROM checkpoints WHERE thread_id = %s),"
    "       (SELECT count(*) FROM session_messages WHERE session_id = %s)"
)


async def fork_session(parent_id: str, owner: str | None, profile: str | None) -> str:
    """Copy a session's whole thread onto a new id and return it.

    One transaction across every table: a fork that is listed but missing half its checkpoint blobs
    is worse than no fork.

    Args:
        parent_id: The session to branch from. Its rows are read and never written.
        owner: The principal the fork belongs to, already authorized against the parent by
        `resolve_session`; passed in so this function cannot be where an ownership check is
        forgotten.
        profile: The parent's profile, carried over because a profile only ever narrows.

    Returns:
        The new session id.

    Raises:
        SessionForkError: The parent holds no checkpoint or no transcript row yet, so a copy would
        be an unloadable or unlisted fork.
    """
    child_id = uuid.uuid4().hex
    async with _session_connection(_session_dsn()) as conn:
        async with conn.cursor() as cur:
            await cur.execute(_COUNT_FORKABLE, (parent_id, parent_id))
            row = await cur.fetchone()
            checkpoints, messages = (row[0], row[1]) if row else (0, 0)
            if not checkpoints:
                raise SessionForkError(
                    f"session {parent_id} has no saved state to fork — it has taken no turn yet"
                )
            if not messages:
                raise SessionForkError(
                    f"session {parent_id} has not answered a turn yet, so there is nothing to "
                    "branch from that would be findable — ask it something and fork the answer"
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
            await cur.execute(_COPY_MESSAGES, (child_id, parent_id, parent_id))
            await cur.execute(_COPY_TOOL_RESULT_LINKS, (child_id, parent_id))
            # The artefacts beside the conversation, head revisions only, under new ids.
            await fork_exhibits(cur, parent_id, child_id)
            # The ownership row is written inside the copy's transaction, so both commit or neither
            # does. Copied rows without an ownership row would be unreachable by `agent/leaver.py`'s
            # erasure, which scopes through `session_owners`.
            await cur.execute(_OWNER_INSERT, (child_id, owner, profile))
        await conn.commit()

    logger.info("forked session %s onto %s", parent_id, child_id)
    return child_id
