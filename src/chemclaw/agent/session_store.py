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

The owner store also answers the two questions a conversation list has beyond "which are mine":
**where does the page stop** (`page_for_owner` + `encode_session_cursor`, a keyset cursor, because
this list reorders itself as it is read) and **how does one go away**
(`SessionOwnerStore.delete_session`, the session-scoped counterpart to `leaver.erase_actor`'s
actor-scoped sweep, over the table set that module already enumerates). See
`D-2026-08-27-a-session-list-is-a-cursor-and-a-session-is-deletable`.

**`get_messages` has no `LIMIT` and must not grow one.** That used to be a data-safety rule, because
the read repaired tool-call pairings and wrote the repair back. It is now a rendering rule: the
reader is a person reloading a conversation, and a transcript that silently omits its own beginning
does not look truncated — it looks like the conversation started later than it did.

**The table is bounded by `durable/retention.py`, by age, and by nothing else.** A compaction pass
used to shrink it too, applying the model's context-window policy (`keep_last_conversation_groups`)
to the stored rows. That was right while the rows were the model's context and wrong the moment they
stopped being: it deleted a chemist's older messages not because any policy said to keep less, but
because the model no longer needed them — a context heuristic quietly editing a durable record. Age-
based retention is the policy statement a deployment actually makes, and it deletes only whole
pairing components (`droppable_rows`, D-145).
"""

import base64
import binascii
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from functools import cache
from typing import Any

import psycopg
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    message_to_dict,
    messages_from_dict,
)
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from chemclaw.agent.message_migration import (
    LANGCHAIN_SHAPE,
    to_langchain,
)
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.db import existing_tables
from chemclaw.core.identity_context import get_current_correlation_id
from chemclaw.core.metrics_bridge import degraded

log = logging.getLogger(__name__)

# Stamped into `additional_kwargs` of a message this module *recovered* rather than decoded, so a
# reader can tell the two apart. Without it the degraded path is a forgery: it returns an ordinary
# message of a guessed class carrying the row's prose, and nothing downstream — no reader, no test
# — can distinguish "this row was decoded" from "this row was not, and these are its words". That
# is not hypothetical. Deleting the `LANGCHAIN_SHAPE` branch outright sends *every* row this system
# writes through the legacy converter, which refuses it, and every transcript comes back as flat
# prose with its tool calls gone (an `AIMessage` loses `tool_calls`, a `ToolMessage` becomes an
# `AIMessage` with no `tool_call_id`) — the M6 defect this module's docstring was written about,
# reached from the inside. The counter says a degradation happened; this says *which row*, which is
# what a reader rendering that row needs.
#
# `additional_kwargs` rather than a new field or a wrapper type: it is LangChain's own extension
# point on `BaseMessage`, so the marker rides along on the ordinary object every caller already
# handles and costs nothing to ignore.
DEGRADED_RENDER = "chemclaw_degraded_render"


def is_degraded_render(message: BaseMessage) -> bool:
    """Whether this message is a recovered row rather than a decoded one.

    Public for `chemclaw.cli.explain`, which reconstructs a conversation for the audit join and
    must not present a guess as the record: a row whose prose was recovered has an *unknown*
    speaker, whatever label it happens to carry.
    """
    return DEGRADED_RENDER in message.additional_kwargs


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

    **Both branches are guarded, and the unguarded one was the common one.** Only the MAF
    conversion used to sit inside the `try`, so a `langchain` row the library refuses raised
    `ValueError: Got unexpected message type` straight through — and since M6 every row this
    system writes is a `langchain` row. The one caller of `get_messages` is
    `GET /sessions/{id}/messages`, which has no handler of its own, so a single bad row answered
    the whole transcript with a 500. `UnconvertibleMessage` is likewise not the only way a stored
    payload fails to convert: a `contents` list holding a non-dict raises `AttributeError` from
    inside the converter, past a handler that named one exception type. Which is why the catch is
    `Exception` and not a tuple — the whole promise of this branch is that *no* stored payload can
    cost a chemist their conversation, and a tuple is a list of the ways that have been seen so
    far.

    **A recovered row says so** (`DEGRADED_RENDER`, read back by `is_degraded_render`). A catch that
    wide swallows a converter *bug* as readily as one unreadable legacy row, and what it returns
    then is an ordinary message of a guessed class carrying plausible prose — indistinguishable, to
    every reader downstream, from a row that decoded. The counter says a degradation happened
    somewhere; the stamp says which message is the guess, so the audit reconstruction can decline
    to attribute it to a speaker it does not actually know.
    """
    if not isinstance(payload, dict):
        # `message` is a bare `jsonb` column — only `message_shape` is constrained — so a scalar or
        # an array is storable, and every branch below assumes a mapping. Nothing writes such a row
        # today; without this, three payload shapes still raised `AttributeError` past both callers
        # and answered the whole transcript with a 500, which is the promise this function makes.
        degraded(log, "session_transcript", "a stored message was not an object; rendering nothing")
        return AIMessage(content="", additional_kwargs={DEGRADED_RENDER: str(shape or "")})
    try:
        if shape == LANGCHAIN_SHAPE:
            return messages_from_dict([payload])[0]
        return to_langchain(payload)
    except Exception:
        # `degraded` rather than a bare warning, because the catch is deliberately wide: it also
        # swallows the shape of a *converter bug* — an `AttributeError` from a typo degrades every
        # row in every transcript into plausible prose, and a log line nobody alerts on makes "one
        # legacy row" and "the converter is broken for everyone" observationally identical. The
        # counter is what separates them.
        degraded(log, "session_transcript", "could not render a stored message; showing its prose")
        # The prose out of `contents`, not `payload["text"]` — the stored shape has no top-level
        # `text` key and never did, so the fallback rendered **every** refused row as an empty
        # bubble. That is the failure this branch exists to avoid, reached by the branch itself: a
        # reader who cannot convert a row should still see what was said in it, and a blank message
        # says the turn was silent. Refusals became commonplace when the converter started stopping
        # on parallel results and unknown content types instead of quietly dropping them.
        # Stamped as recovered, not decoded. The prose below is a best effort at what was said;
        # the structure of the row — which tool answered, under which call id — is gone, and a
        # reader that cannot see the difference will present the guess as the record.
        return _degraded_class(payload)(
            content=_stored_prose(payload), additional_kwargs={DEGRADED_RENDER: str(shape or "")}
        )


# Which speaker each stored shape's label names. MAF stamps `role`, LangChain's `message_to_dict`
# stamps `type`; the two vocabularies are disjoint, so one mapping reads both without having to
# know which shape a refused row holds — which is exactly what is in doubt when this is consulted.
_DEGRADED_CLASSES: dict[str, type[BaseMessage]] = {
    "user": HumanMessage,
    "human": HumanMessage,
    "system": SystemMessage,
}


def _degraded_class(payload: dict[str, Any]) -> type[BaseMessage]:
    """The message class a refused row should render as, taken from the speaker it names.

    **The fallback returned `AIMessage` unconditionally, which put words in the agent's mouth.** A
    chemist's own question rendered as agent speech — attributed, in the transcript, to the system
    that answered it — which is a worse failure than a blank bubble because nothing about it looks
    wrong. The row says who spoke even when it cannot say what a `ToolMessage` answers, so the
    label is read rather than assumed.

    `AIMessage` stays the default for everything else — the assistant's own `role`, a `tool` row
    (a `ToolMessage` needs a `tool_call_id` this row may not carry), and a payload with no label
    at all — because the model's voice is the one attribution that claims nothing about a person.

    Args:
        payload: The stored `message` column of the row that would not convert.

    Returns:
        The `BaseMessage` subclass to render its prose as.
    """
    label = payload.get("role") or payload.get("type")
    return _DEGRADED_CLASSES.get(str(label), AIMessage)


def _stored_prose(payload: dict[str, Any]) -> str:
    """Whatever text a stored row carries, for a reader that could not convert it properly.

    Deliberately forgiving where `to_langchain` is strict: this runs *after* a refusal, and its job
    is that a chemist reloading a conversation still reads the words. Both stored shapes are tried
    because a row that fails conversion is exactly the row whose shape is in doubt.
    """
    contents = payload.get("contents")
    if isinstance(contents, list):
        parts = [part for part in contents if isinstance(part, dict)]
        prose = "".join(str(p.get("text", "")) for p in parts if p.get("type") == "text")
        if prose:
            return prose
        # A refused *tool* row carries no text part at all — its words are the results. Joining
        # them is what makes the commonest refusal (a row answering parallel calls) render as the
        # answers it holds rather than as an empty bubble.
        results = [str(p.get("result", "")) for p in parts if p.get("type") == "function_result"]
        if any(results):
            return "\n".join(r for r in results if r)
    data = payload.get("data")
    if isinstance(data, dict):
        content = data.get("content", "")
        if isinstance(content, list):
            # A LangChain assistant message carries block content, so `str()` of it is a Python
            # repr of the wire format — including a tool call's `input` arguments — presented in
            # the transcript as the agent's own words. This branch was unreachable while the
            # `langchain` shape returned before the `try`; widening the guard made it live, so it
            # has to flatten the way `api/schemas.message_text` already does.
            blocks = [str(b.get("text", "")) for b in content if isinstance(b, dict)]
            return "".join(block for block in blocks if block)
        return str(content)
    return str(payload.get("text", ""))


# The correlation id makes a stored message joinable to the audit rows of the turn that wrote it
# (D-2026-07-31-the-audit-chain-is-versioned).
# Without it the two halves of "what happened in this conversation" — the words and the
# tool calls — sat in tables with no key between them, so the trail could show *that* a tool ran
# and never *why*.
_INSERT = (
    "INSERT INTO session_messages (session_id, message, message_shape, correlation_id) "
    "VALUES (%s, %s, %s, %s)"
)
# Row ids come back too. The repair that used to write a fixed message back to its own row is gone
# (D-2026-08-10 §2), so what the id serves now is the caller that needs to name a row — the
# conversion pass stamping it, and an operator reading a refusal's row number out of a log.
#
# **Public, and shared with the retention sweep.** `message_shape` is in the projection because the
# pairing rule reads it, and the sweep and the transcript reader must decide "which serialization
# is this" the same way — `message_from_row` is already the one function allowed to decide that
# (`D-2026-08-11-what-the-removal-found`), so the SELECT that feeds it is single too. It used to be
# written twice, byte-identically, and the destructive copy was the one living furthest from this
# rule.
SELECT_SESSION_ROWS = (
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
#
# "Newest" is the last message now, not the row's `created_at`, which is when the session was
# *started*. The two diverge exactly where it matters: a session opened last Tuesday and abandoned
# sorted above one used an hour ago, so the top of the list was the least likely thing to be wanted.
# `created_at` still comes back, because when a conversation began is worth showing; it just no
# longer decides the order.
#
# The lateral is also the filter, deliberately rather than as a trick. `max()` with no GROUP BY
# always returns a row — NULL when there is nothing to aggregate — so `ON m.updated_at IS NOT NULL`
# drops precisely the sessions that have never had a turn. Those exist in bulk: the companion UI
# creates the session on the first keystroke to save a round-trip on the first message, so every
# abandoned draft leaves an ownership row behind. Listing them handed a caller a column of empty
# conversations it could not tell apart from ones whose transcript had failed to load — both are an
# empty array from outside. One join answers "what was the last activity" and "was there any".
#
# **The `after` arm is the cursor, and it is a keyset rather than an offset.** The ceiling
# (`service_max_listed_sessions`) used to be the end of the list: a chemist with more sessions than
# it could never reach the older ones, from any client, because nothing said where the page
# stopped. `OFFSET` is the obvious fix and is wrong here — this list *reorders itself as it is
# read*, since a session moves to the top the moment its owner speaks in it, so an offset page
# boundary skips the rows that moved down and repeats the ones that moved up. Comparing against the
# sort key instead cannot: `(m.updated_at, o.session_id) < (%s, %s)` names a position in the
# ordering rather than a count of rows before it. The pair is compared row-wise, so the session id
# breaks the tie two sessions whose last message shares a timestamp would otherwise be ordered by
# arbitrarily — a strict total order is what makes "everything after this row" unambiguous.
#
# The arm is self-disabling through `%s::timestamptz IS NULL`, the shape
# `kg/proposal_store._SELECT_MANY` established, so the first page and a resumed one are the same
# statement rather than two that can drift. The casts are not decoration: psycopg sends an untyped
# NULL, and Postgres cannot infer the type of a parameter that only ever appears beside another
# parameter.
#
# `profile` rides along because it is the one thing about a session that says whether it can be
# holding an undecided plan at all: the todo list only exists under a harness-enabled profile
# (`agent/langgraph_agent`), so `GET /plans/pending` skips a session on this column instead of
# paying a serialized checkpointer read to find nothing. It is already on the row, and one listing
# both surfaces read is one listing they cannot disagree about — a second query filtered on
# `profile` would be a second answer to "which sessions does this person have".
_OWNER_LIST = (
    "SELECT o.session_id, o.created_at, m.updated_at, o.title, o.profile FROM session_owners o "
    "JOIN LATERAL ("
    "  SELECT max(created_at) AS updated_at FROM session_messages WHERE session_id = o.session_id"
    ") m ON m.updated_at IS NOT NULL "
    "WHERE o.owner IS NOT DISTINCT FROM %s "
    "  AND (%s::timestamptz IS NULL "
    "       OR (m.updated_at, o.session_id) < (%s::timestamptz, %s::text)) "
    "ORDER BY m.updated_at DESC, o.session_id DESC LIMIT %s"
)
# First writer wins, in one statement and without a read first. A title is derived from a session's
# opening question, so every later turn would otherwise overwrite it; `title IS NULL` is what lets
# the turn route call this unconditionally and stay correct. Naming a conversation after how it
# started rather than where it drifted to is what makes a sidebar scannable.
_OWNER_TITLE = "UPDATE session_owners SET title = %s WHERE session_id = %s AND title IS NULL"

# What one session's rows are, table by table, when the session itself is what is being deleted.
#
# **The table *set* is not declared here — it is `chemclaw.agent.leaver._ERASE`'s** (see
# `_session_delete_statements`). Only the predicate is: erasure reaches a session through its
# owner, this reaches one session by name, and the two questions have different answers for four
# of the twelve tables (`_ACTOR_SCOPED_ONLY`). Writing the set out a second time is how the two
# drift, and this repository has the receipt for that failure mode — `tool_result_links` was
# invisible to the erasure check for months because a *derived* completeness test could not see a
# table whose columns name no person.
#
# `tool_result_blobs` is the one statement that is not a bare `session_id = ...`, and both halves
# of it are load-bearing. The blob is content-addressed, so two sessions that ran the same tool
# over the same arguments share one row; the `NOT EXISTS` arm is what keeps deleting *this*
# conversation from unlinking *another* one's stored result (a cascade takes the link rows with the
# blob, so an unconditional delete would remove a row belonging to a session nobody asked to
# delete). The consequence is stated rather than hidden: this session's own link row survives when
# its bytes are shared, because `infra/sql/grants/app_privileges.sql` withholds DELETE on
# `tool_result_links` on purpose — a link may only disappear behind its blob. What is left is a row
# naming a session id that no longer resolves to anything, and `durable/retention.py`'s age sweep
# collects it with the blob.
#
# **The `NOT EXISTS` arm counts only links whose session still exists**, and without that clause
# those surviving orphan rows blocked the blob for ever. Two sessions sharing bytes, both deleted:
# the first delete spares the blob (the second session still links it) and leaves an orphan link;
# the second delete then finds *that* row and spares the blob again. Nothing owns it, nothing can
# reach it, and the only collector left is `retention_tool_results_days`, which ships at 0.
#
# Coincidental sharing made this rare — two sessions had to run one tool over identical arguments
# and get byte-identical output. `agent/session_fork.py` made it certain: a fork copies the
# parent's links by design, so *every* forked conversation left its parent's results unreclaimable.
# That is what surfaced it; the defect is older than the fork and the fix is not fork-specific.
_SESSION_DELETE: dict[str, str] = {
    "tool_result_blobs": (
        "DELETE FROM tool_result_blobs b WHERE EXISTS ("
        "  SELECT 1 FROM tool_result_links l"
        "   WHERE l.content_hash = b.content_hash AND l.session_id = %(session_id)s"
        ") AND NOT EXISTS ("
        "  SELECT 1 FROM tool_result_links l"
        "   WHERE l.content_hash = b.content_hash AND l.session_id <> %(session_id)s"
        "     AND EXISTS (SELECT 1 FROM session_owners o WHERE o.session_id = l.session_id))"
    ),
    "session_messages": "DELETE FROM session_messages WHERE session_id = %(session_id)s",
    "session_events": "DELETE FROM session_events WHERE session_id = %(session_id)s",
    "session_turns": "DELETE FROM session_turns WHERE session_id = %(session_id)s",
    "session_owners": "DELETE FROM session_owners WHERE session_id = %(session_id)s",
}

# The tables in the erasure set that a *session* delete must leave alone, and why. Each one is
# keyed by the person rather than by the conversation, so deleting one conversation would take data
# from every other one the same chemist has.
_ACTOR_SCOPED_ONLY: dict[str, str] = {
    "store": "an agent memory outlives the session it was written in — that is what it is for",
    "store_vectors": "the embedding half of the same memory",
    "subscriptions": "a standing query belongs to the person, not to one conversation",
    "user_preferences": "a preference is the person's, and survives every session they close",
}


@cache
def _session_delete_statements() -> tuple[tuple[str, str], ...]:
    """The per-table DELETEs for one session, in the order an actor's erasure uses.

    **Derived from `leaver._ERASE` rather than listed again**, because the question "which tables
    hold a session's data" already has one answer in this codebase and a second copy of it is a
    copy that goes stale in silence. Every table there is either session-scoped (a predicate in
    `_SESSION_DELETE`, or the checkpointer's `thread_id`, which *is* the session id) or deliberately
    actor-scoped (`_ACTOR_SCOPED_ONLY`); a table that is neither raises here, so the next writer to
    add one to the erasure sweep is told that this delete has no opinion about it yet, rather than
    finding out from a session whose rows outlived it.

    The order is `_ERASE`'s too, for `_ERASE`'s reason: everything keyed by the session goes before
    the ownership row that is the only way to find the session again.

    Both imports are deferred, and have to be: `leaver` and `checkpointer` each import this module
    at import time, so naming either of them at module scope here is an import cycle that fails on
    whichever one is loaded first.

    Returns:
        `(table, statement)` pairs, each statement taking one `session_id` parameter.

    Raises:
        RuntimeError: the erasure sweep names a table this delete has not classified.
    """
    from chemclaw.agent.checkpointer import CHECKPOINT_TABLES
    from chemclaw.agent.leaver import _ERASE

    scoped = dict(_SESSION_DELETE)
    for table in CHECKPOINT_TABLES:
        # The checkpointer keys graph state by `thread_id`, and a thread id is a session id.
        scoped[table] = f"DELETE FROM {table} WHERE thread_id = %(session_id)s"
    unclassified = [
        table for table, _ in _ERASE if table not in scoped and table not in _ACTOR_SCOPED_ONLY
    ]
    if unclassified:
        raise RuntimeError(
            f"chemclaw.agent.leaver erases {unclassified} and chemclaw.agent.session_store does "
            "not say whether deleting one session should: add a predicate to _SESSION_DELETE or "
            "a reason to _ACTOR_SCOPED_ONLY"
        )
    return tuple((table, scoped[table]) for table, _ in _ERASE if table in scoped)


def encode_session_cursor(updated_at: datetime, session_id: str) -> str:
    """This row's position in the session listing, as one opaque token.

    The cursor is the *sort key* — the row's last activity and its session id — and nothing else,
    which is what makes it stable: it names a place in the ordering rather than a page number, so
    it keeps meaning the same thing when rows are added, removed, or reordered by a chemist
    speaking in an old conversation, and it survives a change to the page size. The row it was
    minted from need not still exist.

    Base64url of the two fields, because a caller must not be able to *read* it and conclude
    anything: an id-plus-timestamp pair spelled in the clear invites a client to construct one, and
    a constructed cursor is a client that breaks the day the ordering gains a third component.
    It is deliberately **not** signed. A cursor is not a capability — every page is re-scoped to
    the caller's own sessions by `owner IS NOT DISTINCT FROM`, so the worst a forged one can do is
    move the forger around their own list.

    Args:
        updated_at: The row's last-activity timestamp, exactly as the listing ordered by it.
        session_id: The row's session id, the tiebreak within one timestamp.

    Returns:
        A URL-safe token to hand back as `after`.
    """
    raw = f"{updated_at.isoformat()}|{session_id}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_session_cursor(cursor: str) -> tuple[datetime, str]:
    """The `(updated_at, session_id)` position a cursor names.

    Raises:
        ValueError: the token is not one this service minted — bad base64, bad UTF-8, a missing
            separator, or a timestamp `datetime.fromisoformat` refuses. One error type for all of
            them, because the caller's answer is the same in every case and the difference is not
            something a client should be told.
    """
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("not a session cursor") from exc
    stamp, separator, session_id = raw.partition("|")
    if not separator or not session_id:
        raise ValueError("not a session cursor")
    try:
        return (datetime.fromisoformat(stamp), session_id)
    except ValueError as exc:
        raise ValueError("not a session cursor") from exc


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
                await cur.execute(SELECT_SESSION_ROWS, (session_id,))
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


def owner_permits(owner: str | None, actor: str | None) -> bool:
    """Whether a stored owner lets `actor` reach the row — the one ownership rule.

    **One definition, because there are now three callers and they must not drift.** The HTTP layer
    resolves ownership for `/sessions/{id}/…` (`api/deps._owner_authorizes`, which delegates here
    and which `chemclaw/api/routes/protocols.py` reaches for a design as well); the agent resolves
    it for a tool handed an explicit session id
    (`agent/evidence_tools.assemble_evidence_pack`); and it resolves it again for a tool handed an
    explicit `design_id` (`agent/protocol_design_tools._require_writable`). A second copy of this
    predicate is how one surface ends up stricter than the other, and the loose one is the one that
    matters.

    The subject is no longer only a session, and the docstring said "two" and named the session pair
    for a release after the third caller landed. `owner` is whatever column records who opened the
    row — `session_owners.owner` or `experiment_protocols.opened_by` — and the rule below reads
    neither table, which is what lets it be one rule.

    The dev/enforced split is deliberate and is `_is_reviewer`'s, applied to ownership: with
    `entra_required` off there is no real actor, so an owner-less row degrades open exactly as
    every other route does. Once identity is enforced a *recorded* absence of an owner is no longer
    "everyone's" — enforcement never mints an owner-less row, so one surviving into it is a
    leftover from a dev-mode write, and treating it as anyone's would hand it to every
    authenticated principal instead of to nobody. `owner` is falsy for both `None` and `""`, so a
    row written without one and one holding the empty-string sentinel are refused alike.

    Args:
        owner: The session's recorded owner, or `None`/`""` when it has none.
        actor: The reader's Entra object id, or `None`/`""` when there is no authenticated actor.

    Returns:
        Whether the read is permitted.
    """
    if not owner:
        return not settings.entra_required
    return bool(actor) and owner == actor


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

    async def set_title_if_absent(self, session_id: str, title: str) -> None:
        """Name a session after its opening question, once (see `_OWNER_TITLE`).

        Called on every turn and expected to match nothing after the first, which is why it is one
        conditional `UPDATE` on the primary key rather than a read followed by a write: the second
        shape costs two round-trips to discover it has nothing to do, and can lose a race between
        them. Against a turn that is about to spend seconds in a model, one indexed no-op write does
        not register.
        """
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_OWNER_TITLE, (title, session_id))
            await conn.commit()

    async def list_for_owner(
        self, owner: str | None
    ) -> list[tuple[str, datetime, datetime, str | None, str | None]]:
        """The owner's newest page of sessions — `page_for_owner` from the top.

        `(session_id, created_at, updated_at, title, profile)` per row.

        Kept as its own name because it is the shape the front door's `SessionOwners` protocol
        declares and the only one a registry that cannot resume a listing has to implement. It
        holds no query of its own: a first page that was assembled by a second statement is a first
        page that can order its rows differently from every page after it.
        """
        return await self.page_for_owner(owner)

    async def page_for_owner(
        self, owner: str | None, *, after: str | None = None
    ) -> list[tuple[str, datetime, datetime, str | None, str | None]]:
        """One page as `(session_id, created_at, updated_at, title, profile)`.

        Newest first, at most `service_max_listed_sessions` rows, resuming strictly after the row
        `after` names — see `_OWNER_LIST` for why the order is `updated_at` rather than
        `created_at`, why a session with no messages is not listed at all, and why the resume is a
        keyset comparison rather than an `OFFSET`.

        This table is already the durable answer to "which sessions exist and who owns them", so
        listing reads it directly rather than adding a second registry that could disagree with the
        one `_resolve_session` authorizes against. `updated_at` is derived from `session_messages`
        rather than mirrored onto a column here, because the turn that would have to maintain a
        mirror already writes the row the derivation reads — a second write per turn is a second
        thing that can fall out of step.

        `profile` is the fifth field rather than a second query — see `_OWNER_LIST` for what reads
        it. `None` is a real value there and means the session runs the default profile, which is
        exactly what `agent.profiles.get_profile(None)` resolves.

        A tuple rather than a record type, matching `lookup` above: this module is below the API
        layer that consumes it, so a shared shape would have to live somewhere neither of them owns.
        The cursor for each row is derivable from that tuple (`encode_session_cursor`), so the page
        carries no field the caller has to be told how to combine.

        Args:
            owner: The principal whose sessions to list; `None` is the shared dev principal's real
                SQL NULL and matches itself.
            after: A cursor from `encode_session_cursor`, or None for the newest page.

        Returns:
            At most one page, newest activity first.

        Raises:
            ValueError: `after` is not a cursor this service minted.
        """
        stamp, last_id = decode_session_cursor(after) if after else (None, None)
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    _OWNER_LIST,
                    (owner, stamp, stamp, last_id, settings.service_max_listed_sessions),
                )
                rows = await cur.fetchall()
        return [(row[0], row[1], row[2], row[3], row[4]) for row in rows]

    async def delete_session(self, session_id: str) -> dict[str, int]:
        """Delete one conversation and everything keyed by it, in one transaction.

        The counterpart to `chemclaw.agent.leaver.erase_actor`, at the other scope: erasure answers
        "someone left", this answers "I do not want this conversation any more". It runs the same
        table set for the same reason — an ownership row deleted without the rows it keys leaves
        messages, events and graph state that *nothing can reach and nothing can find again*, since
        every session-scoped sweep in this system starts from `session_owners`.

        **The transaction is what makes that true rather than intended.** Twelve statements that
        commit one at a time can be interrupted after any of them, and the interruption that
        matters is the one that has already deleted the ownership row.

        A method on this store, not a free function: the ownership row is the key every other table
        here is reached by, and this store is the thing that owns it. It deletes only what this
        session's id names — an actor-scoped row (a memory, a preference, a subscription) belongs
        to the person and outlives their conversations (`_ACTOR_SCOPED_ONLY`).

        Missing tables are skipped rather than raising, exactly as the erasure sweep skips them:
        the checkpointer's three are created by `AsyncPostgresSaver.setup()` and not by a
        migration, so a deployment that has never run the graph does not have them, and deleting a
        conversation must not be the one operation such a deployment cannot perform.

        Args:
            session_id: The conversation to delete.

        Returns:
            `{table: rows deleted}`, one key per table in the sweep — zero where a table held
            nothing or does not exist here, so an operator comparing two runs sees the same keys.
        """
        statements = _session_delete_statements()
        removed: dict[str, int] = {}
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                present = await existing_tables(cur, {table for table, _ in statements})
                for table, statement in statements:
                    if table not in present:
                        removed[table] = 0
                        continue
                    await cur.execute(statement, {"session_id": session_id})
                    removed[table] = cur.rowcount if cur.rowcount > 0 else 0
            await conn.commit()
        log.info(
            "deleted session %s: %d row(s) across %d table(s)",
            session_id,
            sum(removed.values()),
            len([table for table, count in removed.items() if count]),
        )
        return removed


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
