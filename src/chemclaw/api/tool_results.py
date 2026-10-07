"""The tool-result store: where a turn's full tool output lives so a surface can fetch it.

Events carry a preview and a ref; a surface fetches one result's full text through `GET
/sessions/{id}/tool-results/{ref}`. The ref is the SHA-256 of the text, so repeats store nothing
new. Storing never fails a turn: every failure yields `""`. Keyed by session, unlike the
calculation-keyed artifact store.
"""

import hashlib
import logging
from collections.abc import Awaitable, Callable

from pydantic import BaseModel

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.metrics_bridge import degraded

logger = logging.getLogger(__name__)

# Addressed over the text's UTF-8 bytes, so a string addresses identically whatever encoded it. `DO
# UPDATE SET created_at = now()` rather than `DO NOTHING`: here `created_at` is the retention clock,
# so a repeat write keeps the blob alive; the bytes are still stored once.
_INSERT_BLOB = """
    INSERT INTO tool_result_blobs (content_hash, byte_size, data)
    VALUES (%s, %s, %s)
    ON CONFLICT (content_hash) DO UPDATE SET created_at = now()
"""


# The row is keyed on `(session_id, content_hash)`, so identical text from two calls is one row.
# `tool` and `correlation_id` collapse to `''` on disagreement rather than taking the last writer's
# label, which would serve the bytes under the wrong call (every unexpected tool error returns the
# same text, so this is common). A `CASE` keeps it one statement, and it is monotone: once `''`,
# always `''`. `created_at` still moves (retention clock).
_UPSERT_LINK = """
    INSERT INTO tool_result_links (session_id, content_hash, tool, correlation_id)
    VALUES (%s, %s, %s, %s)
    ON CONFLICT (session_id, content_hash) DO UPDATE SET
        tool = CASE
            WHEN tool_result_links.tool = EXCLUDED.tool THEN EXCLUDED.tool ELSE ''
        END,
        correlation_id = CASE
            WHEN tool_result_links.correlation_id = EXCLUDED.correlation_id
            THEN EXCLUDED.correlation_id ELSE ''
        END,
        created_at = now()
"""

# The join on the link is the authorization: a ref from another session's result finds nothing.
# `resolve_session` established the caller owns the session; this establishes the session produced
# the result.
_SELECT_RESULT = """
    SELECT l.tool, l.correlation_id, b.byte_size, b.data
    FROM tool_result_links AS l
    JOIN tool_result_blobs AS b ON b.content_hash = l.content_hash
    WHERE l.session_id = %s AND l.content_hash = %s
"""

# Which of a session's results are fetchable now, asked once per transcript read. Only links are
# read: `ON DELETE CASCADE` means a link cannot outlive its blob. Answered by an index-only scan of
# the primary key `(session_id, content_hash)`.
_SELECT_SESSION_REFS = "SELECT content_hash FROM tool_result_links WHERE session_id = %s"


class StoredToolResult(BaseModel):
    """One stored tool result, as the fetch route returns it.

    `text` is exactly what the tool returned — the same string the answer verifier scores against,
    not the preview. It is deliberately typed as text rather than parsed JSON: a tool result is
    whatever the framework handed back, and a store that promised JSON would have to fail or lie
    about the ones that are not.

    `correlation_id` rides along so a fetched result joins the audit trail and the logs of the turn
    that produced it, which is the join a reviewer asks for and the one a ref alone cannot make.

    **Both `tool` and `correlation_id` are empty when the store cannot name one call**, and a
    reader must treat an empty one as "unknown" rather than as a value. A ref names *bytes*: two
    calls in one session that returned identical text share one blob and one link row by design
    (D-011 applied to bytes), and there is then no single tool or turn the row belongs to. The
    write collapses a disagreeing column to `''` rather than overwriting it with the newest call's
    (see `_UPSERT_LINK`), because a label that is right most of the time is the failure mode this
    whole surface is built to avoid — and "Error: Function failed." makes it a certainty, not an
    edge case. What is never ambiguous is `text`: those are the bytes the ref addresses, and every
    call that produced them produced exactly these.
    """

    ref: str
    tool: str
    correlation_id: str
    byte_size: int
    text: str


# `(tool, text) -> ref`, empty when nothing was stored; a closure, so the trace never knows about
# sessions.
ResultSink = Callable[[str, str], Awaitable[str]]


def content_address(text: str) -> str:
    """The ref for a result: the SHA-256 hex digest of its UTF-8 bytes.

    Pure, so a result can be named before or without writing it.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def store_tool_result(*, session_id: str, correlation_id: str, tool: str, text: str) -> str:
    """Store one tool result for `session_id` and return its ref.

    The blob by content address, then the link that makes it reachable from the session. Raises on a
    database failure; `session_sink` is where failures are swallowed.
    """
    ref = content_address(text)
    payload = text.encode("utf-8")
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(_INSERT_BLOB, (ref, len(payload), payload))
            await cur.execute(_UPSERT_LINK, (session_id, ref, tool, correlation_id))
        await conn.commit()
    return ref


async def load_tool_result(session_id: str, ref: str) -> StoredToolResult | None:
    """The stored result `ref` names *within* `session_id`, or `None` when there is none.

    One answer for never stored, swept, or another conversation's, so an unauthorized caller learns
    nothing; the route makes it one 404.
    """
    async with db.connection(settings.postgres_dsn) as conn:
        async with conn.cursor() as cur:
            await cur.execute(_SELECT_RESULT, (session_id, ref))
            row = await cur.fetchone()
    if row is None:
        return None
    tool, correlation_id, byte_size, data = row
    return StoredToolResult(
        ref=ref,
        tool=tool,
        correlation_id=correlation_id,
        byte_size=int(byte_size),
        # psycopg may hand BYTEA back as a memoryview.
        text=bytes(data).decode("utf-8"),
    )


async def fetchable_refs(session_id: str) -> frozenset[str]:
    """Every ref `session_id` can currently fetch — what the transcript needs to advertise one.

    The content address pairs a call with its blob but cannot say the blob still exists (store off,
    over cap, failed write, swept), so this set makes a ref checked. A failure yields an empty set
    via `degraded()`; skipped when `stream_max_result_bytes` is 0.
    """
    if settings.stream_max_result_bytes <= 0:
        return frozenset()
    try:
        async with db.connection(settings.postgres_dsn) as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT_SESSION_REFS, (session_id,))
                rows = await cur.fetchall()
    except Exception as exc:
        degraded(
            logger,
            "tool_result_store",
            "could not list the stored results of session %s (%s); its transcript will carry no "
            "result refs and a client will fall back to the truncated result text",
            session_id,
            exc,
            level=logging.WARNING,
            exc_info=False,
        )
        return frozenset()
    return frozenset(str(row[0]) for row in rows)


def session_sink(session_id: str, correlation_id: str) -> ResultSink:
    """A `ResultSink` that stores this turn's results against this session.

    A failed write returns `""`, as an over-cap result does: no rendering is worth failing a turn.
    Reported through `degraded()` at WARNING without a traceback — per tool call, so the counter is
    what matters, and an unreachable database is already loud elsewhere.
    """

    async def _put(tool: str, text: str) -> str:
        try:
            return await store_tool_result(
                session_id=session_id, correlation_id=correlation_id, tool=tool, text=text
            )
        except Exception as exc:
            degraded(
                logger,
                "tool_result_store",
                "could not store the result of tool %s for session %s (%s); its trace event will "
                "carry no result_ref and the full result is not fetchable",
                tool,
                session_id,
                exc,
                level=logging.WARNING,
                exc_info=False,
            )
            return ""

    return _put


async def stored_within_cap(sink: ResultSink | None, tool: str, text: str) -> str:
    """Store `text` and return the ref a surface fetches it by, or `""` when it was not stored.

    The one place the size cap is applied, for both writers (the trace and `full_result_sink`). An
    over-cap result is refused and logged, never trimmed: a truncated JSON payload would render as
    complete with data missing. Measured in bytes, since the cap protects a `BYTEA` column. `""`
    covers every way of not storing, and none fails the turn.
    """
    if sink is None or settings.stream_max_result_bytes <= 0:
        return ""
    size = len(text.encode("utf-8"))
    if size > settings.stream_max_result_bytes:
        logger.warning(
            "tool %s returned %d bytes, over the %d-byte store cap; its trace event carries no "
            "result_ref for these bytes and they are not fetchable",
            tool,
            size,
            settings.stream_max_result_bytes,
        )
        return ""
    return await sink(tool, text)


def full_result_sink(session_id: str, correlation_id: str) -> ResultSink:
    """Where a cut result keeps its full text: this session's store, under the store's own cap.

    The consumer is the chemist, never the model: the cut in `agent/tool_result_size.py` stores the
    full text here and stamps the ref, so `GET /sessions/{id}/tool-results/{ref}` opens what the
    tool returned. Same tables, authorization, retention and erasure as any stored result. Installed
    by `api/runner._turn_ambient` via `set_full_result_sink`, since the layering forbids `agent ->
    api`.
    """
    put = session_sink(session_id, correlation_id)

    async def _put(tool: str, text: str) -> str:
        return await stored_within_cap(put, tool, text)

    return _put
