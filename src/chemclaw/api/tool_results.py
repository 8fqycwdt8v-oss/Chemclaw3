"""The tool-result store: where a turn's full tool output lives so a surface can fetch it.

`ToolResultEvent.preview` is 200 characters cut at whatever byte the budget lands on, and its own
docstring says it will stay that way — "never a whole evidence sweep streamed to a browser". The
consequence was that everything a tool actually *returned* reached the chemist as prose the model
wrote about it: a hazard screen's severities and citations, a charge table's rows, a solvent
ranking. The full text already existed at emit time (`api/runner_trace.py::_result_text`,
"Untruncated on purpose") and was dropped on the floor once the event was built.

This is the other half of the split the preview created. The stream keeps its budget — the event
carries a *reference*, not a payload — and a surface that decides to render one result pulls that
one result, once, through `GET /sessions/{id}/tool-results/{ref}`. Streaming the typed payload on
the event instead would re-open exactly the question the truncation closed.

**Content-addressed, so a repeat stores nothing.** The ref is the SHA-256 of the result text, which
makes an identical result from an identical call the same row — the "never compute twice" position
(D-011) applied to bytes. It also means the ref is derivable from the text alone, so nothing has to
be threaded back from the database to build the event.

**Storing must never fail a turn**, and that is the whole reason this module returns `""` rather
than raising. An empty `result_ref` is the honest statement "not stored", which is also what a
surface sees when the store is off or the result was over the cap; there is exactly one way for a
consumer to read the absence, and no path in which a database outage costs a chemist an answer.

`api` rather than `science/calc` (where the artifact store lives, D-124): an artifact is a
by-product of a *calculation* and is keyed by its calculation, while this is a by-product of a
*turn* and is keyed by the session that ran it. Sharing a store would have meant one of the two
keys pretending to be the other.
"""

import hashlib
import logging
from collections.abc import Awaitable, Callable

from pydantic import BaseModel

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.metrics_bridge import degraded

logger = logging.getLogger(__name__)

# A tool result reaches a consumer as text; the address is over its UTF-8 bytes so the same string
# addresses identically whatever encoded it.
_INSERT_BLOB = """
    INSERT INTO tool_result_blobs (content_hash, byte_size, data)
    VALUES (%s, %s, %s)
    ON CONFLICT (content_hash) DO UPDATE SET created_at = now()
"""

# `DO UPDATE SET created_at = now()` and not `DO NOTHING`, which is what the artifact store does
# with the identical statement — the difference is what each `created_at` means. There it is a
# provenance stamp beside a separate `last_access_at` that drives eviction; here it *is* the
# retention clock, so leaving it at the first write would expire a blob on the clock of a turn
# from three weeks ago while two of its links are from this morning. The bytes are still stored
# once; only the clock moves.

# **`tool` and `correlation_id` collapse to `''` on disagreement rather than taking the last
# writer's word**, and that is the one place a *label* can be wrong here. The row is keyed on
# `(session_id, content_hash)`, so two calls in one session that returned identical text are one
# row; `SET tool = EXCLUDED.tool` then relabelled that row with whichever call wrote last, and the
# fetch route served the right bytes under a different call's tool name and correlation id — with
# nothing in the payload saying so, which is exactly the near-miss pairing
# `D-2026-08-09-a-derivable-ref-is-not-a-fetchable-one` refuses on the read side ("a mispaired
# result is worse than an absent one").
#
# Guaranteed rather than hypothetical: `include_detailed_errors` is off (`agent/tool_authz.py`
# documents why), so **every** unexpected tool exception in the system returns the same byte string
# "Error: Function failed." — one blob per session for every failed call it ever makes, and the
# `correlation_id` `StoredToolResult` calls "the join a reviewer asks for" would name one
# arbitrary turn among them.
#
# An empty column is the honest answer: these bytes are not one call's, so the store names no call.
# It is a `CASE` rather than a read-modify-write because it must stay one statement on the turn
# path, and it is monotone — once a value disagrees it is `''`, and `''` disagrees with every
# subsequent write, so the collapse never silently un-collapses. `created_at` still moves, because
# it is the retention clock (above) and not a label.
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

# Joined on the link rather than read straight off the blob, and that join *is* the authorization:
# a ref names bytes, a link names whose conversation produced them, so a caller holding a ref from
# somebody else's session finds nothing. The route's `resolve_session` has already established that
# this caller owns this session; this establishes that this session produced this result.
_SELECT_RESULT = """
    SELECT l.tool, l.correlation_id, b.byte_size, b.data
    FROM tool_result_links AS l
    JOIN tool_result_blobs AS b ON b.content_hash = l.content_hash
    WHERE l.session_id = %s AND l.content_hash = %s
"""

# Which of a session's results are fetchable *right now* — the transcript's question, asked once
# per reload rather than once per tool call. Only the links are read: `ON DELETE CASCADE` (042)
# means a link cannot outlive its blob, so the link's existence is the blob's existence, and
# joining `tool_result_blobs` would re-derive a fact the foreign key already guarantees.
#
# Served by the table's **primary key**, `(session_id, content_hash)`: the predicate is its leading
# column and the projection is its second, so the whole answer is inside the index. Measured on
# 20,000 links over 200 sessions — `Index Only Scan using tool_result_links_pkey`, `Heap Fetches:
# 0`. Not by `tool_result_links_session_idx`, which this comment used to name: that index is
# `(session_id, created_at DESC)` and carries no `content_hash`, so it cannot answer this query
# without going to the heap. It exists for a recency-ordered read of a session's links, which is a
# different question and not one this module asks.
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


# `(tool, text) -> ref`, empty when nothing was stored. The trace holds one of these rather than a
# session id and a store, because the trace's whole design is that it knows nothing about sessions
# (see its module docstring) — a closure the runner builds keeps that true.
ResultSink = Callable[[str, str], Awaitable[str]]


def content_address(text: str) -> str:
    """The ref for a result: the SHA-256 hex digest of its UTF-8 bytes.

    Pure, so the producer can name a result before — or without — writing it, and two producers
    that never meet agree on the name.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def store_tool_result(*, session_id: str, correlation_id: str, tool: str, text: str) -> str:
    """Store one tool result for `session_id` and return its ref.

    Two statements, like the artifact store's write: the blob by content address (a no-op on the
    bytes when the same result is already there) and the link that makes it reachable from the
    session. Raises on a database failure — the swallowing belongs to `session_sink`, so a caller
    that genuinely wants to know a write failed (a test, an operator script) can still find out.
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

    One answer for three different misses — never stored, swept by retention, or belongs to a
    different conversation — because distinguishing them would tell an unauthorized caller that a
    ref exists somewhere. The route turns it into one 404.
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

    **Why the transcript needs this at all, when the ref is derivable from the bytes.** A stored
    tool call is paired to its blob by *content address*: the transcript holds the result text, and
    the SHA-256 of that text is the address the store wrote it under. That pairing is exact by
    construction — no timestamp window, no "the nearest link row with the same tool", nothing that
    is right most of the time — and it is why `tool_result_links` is not consulted as a *join key*.
    What the address cannot say is whether those bytes are still there: `result_ref` on the live
    event means "stored", and a transcript that computed an address for every past result would
    hand a client refs for results the store never took (the store is off, the result was over the
    cap, the write failed) and for results retention has since swept. This turns the derivable
    address into a checked one, so an empty `TranscriptToolCall.result_ref` keeps the one meaning
    it has on the stream: not fetchable.

    Returned frozen because it is a lookup table the projection only reads, and it crosses from a
    route into a pure function that must not be able to change it.

    **A failure is an empty set, not an error**, for the reason `session_sink` swallows its write:
    a fetchable full result is a rendering, and a transcript a chemist is reloading must not fail
    because the blob store is unreachable — they still get every message, every tool call and the
    400-character result. The swallow goes through `degraded()` under the same subsystem name as
    the write side, because from an operator's seat "the tool-result store is not answering" is one
    condition whether it was noticed writing or reading.

    Skipped entirely when `stream_max_result_bytes` is 0, which is the store's documented off
    switch: nothing was written, so there is nothing to ask about and no reason to open a
    connection to ask.
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

    The failure policy lives here rather than in the trace or the store: a tool result reaching a
    browser is a rendering, and no rendering is worth failing a turn over. A write that raises is
    answered with `""` — the same value an over-cap result gets, so a consumer has one thing to
    check and "not stored" has one meaning.

    Through `degraded()` rather than a bare `logger.warning`, which is this repository's rule for a
    deliberate swallow: the log line names one lost write, and the counter is what makes a run of
    them visible to an operator. It matters more here than at most `degraded` sites because the
    write is per *tool call* rather than per turn — a development CLI pointed at no database will
    produce one line per call, and the aggregate is the thing worth alerting on.

    `level=WARNING` rather than the ERROR default, and `exc_info=False`: what is lost is a
    rendering, which is the "cosmetic" case the helper documents for the lower level, and an
    unreachable database is already loud on paths that do matter (`ConnectionError` classifies as
    `storage_unavailable` and fails the turn there). A stack trace per tool call would bury both.
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
