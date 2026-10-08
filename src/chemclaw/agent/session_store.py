"""Durable, Postgres-backed conversation history.

`PostgresHistoryProvider` appends each turn's exchange to `session_messages` and reads it back in
insertion order, so a conversation outlives its pod. It is a read-model projection, not the
conversation's state: turn state lives in the LangGraph checkpointer. `chemclaw.api.runner` writes
the chemist's message ahead of the turn (`turn_status='running'`) and the rest of the exchange once
the answer exists, settling that status. Readers are the transcript route, the audit join, and the
bounded `recent_user_texts`.

Messages are stored as LangChain's `message_to_dict()`; this module interprets only which
serialization a row holds (`message_from_row`), because older rows hold a previous framework's
shape.

Three stores share one database because they are one session's durable state: the history,
`SessionOwnerStore` (who owns a session; also the keyset-paged listing and `delete_session`), and
`SessionTurnClaims` (which process is running a turn right now).

`get_messages` has no `LIMIT` and must not grow one: its reader is a person, and a transcript
silently missing its beginning looks like a shorter conversation. `recent_user_texts` is a separate,
bounded read for a check. The table is bounded only by age, by `durable/retention.py`, which deletes
whole pairing components (`droppable_rows`).
"""

import base64
import binascii
import logging
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from functools import cache
from typing import Any, Literal, NamedTuple, get_args

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
from chemclaw.core.authorship import UNNAMED_AGENT, Authorship
from chemclaw.core.config import settings
from chemclaw.core.db import existing_tables
from chemclaw.core.identity_context import get_current_actor, get_current_correlation_id
from chemclaw.core.metrics_bridge import degraded

log = logging.getLogger(__name__)

# Stamped into `additional_kwargs` of a message this module recovered rather than decoded, so a
# reader can tell a guessed speaker and prose from a decoded row. The degradation counter says that
# a degradation happened; this marks which row. `additional_kwargs` is LangChain's own extension
# point, so the marker costs callers nothing to ignore.
DEGRADED_RENDER = "chemclaw_degraded_render"


def is_degraded_render(message: BaseMessage) -> bool:
    """Whether this message is a recovered row rather than a decoded one.

    Public for `chemclaw.cli.explain`, which must not attribute a recovered row to a speaker it does
    not actually know.
    """
    return DEGRADED_RENDER in message.additional_kwargs


#: Where a stored message carries the correlation id of the turn that stored it, stamped on read by
#: the durable provider and on save by the in-memory one, so both answer one call.
STORED_CORRELATION_ID = "chemclaw_correlation_id"


def stored_correlation_id(message: BaseMessage) -> str | None:
    """The correlation id of the turn that stored `message`, or `None` when none was recorded.

    Public for the transcript route, where a detached client recovers its turn's answer by this id.
    `None` (not `""`) for rows written off the request path or before the column existed.
    """
    value = message.additional_kwargs.get(STORED_CORRELATION_ID)
    return str(value) if value else None


#: Where a stored message carries who wrote it (`core/authorship.py`), stamped the same way as the
#: correlation id.
STORED_AUTHORSHIP = "chemclaw_authorship"


def stored_authorship(message: BaseMessage) -> Authorship | None:
    """Who wrote `message` — the person it was written for and the agent that wrote it.

    `None` when the row records neither half. Public for the transcript route.
    """
    value = message.additional_kwargs.get(STORED_AUTHORSHIP)
    return Authorship.model_validate(value) if isinstance(value, dict) else None


#: How the turn a stored question opened has ended so far (`session_messages.turn_status`).
#:
#: Only a chemist's message written ahead of its turn carries one; every other row is `None`.
#: `running` is the write-ahead state; `done`, `failed` and `stopped` are settled by the turn's own
#: process; `interrupted` is written by a different process when the turn's claim lapsed with no
#: live owner (`PostgresHistoryProvider.mark_interrupted`).
TurnStatus = Literal["running", "done", "failed", "stopped", "interrupted"]


class InterruptedTurn(NamedTuple):
    """One turn `mark_interrupted` just marked: whose it was, and whether it is already booked."""

    correlation_id: str
    actor: str | None
    #: The turn already has a `turn_costs` row — its own process booked an outcome and then failed
    #: to settle its question. Marked so the transcript says the turn is over; not booked again.
    booked: bool


#: Where a stored message carries its turn's status, stamped as the correlation id is.
STORED_TURN_STATUS = "chemclaw_turn_status"

_TURN_STATUSES: dict[str, TurnStatus] = {status: status for status in get_args(TurnStatus)}


def turn_status_of(value: object) -> TurnStatus | None:
    """`value` as a member of the vocabulary, or `None` for anything else.

    The column has no constraint, so an unknown spelling reads as "nobody recorded".
    """
    return _TURN_STATUSES.get(value) if isinstance(value, str) else None


def stored_turn_status(message: BaseMessage) -> TurnStatus | None:
    """The status of the turn this stored question opened, or `None` for every other row."""
    return turn_status_of(message.additional_kwargs.get(STORED_TURN_STATUS))


def message_authorship(message: BaseMessage, actor: str | None) -> Authorship:
    """Who wrote a message this system is about to store, on behalf of `actor`.

    A `HumanMessage` is the chemist's own words, with no agent. Everything else is the agent's,
    recorded as `UNNAMED_AGENT`: the graph's identity does not reach the transcript writer, and the
    audit convention leaves the chemist-facing agent unnamed.
    """
    return Authorship(actor=actor, agent=None if message.type == "human" else UNNAMED_AGENT)


def _stamped(
    message: BaseMessage,
    correlation_id: str,
    authorship: Authorship | None = None,
    turn_status: str | None = None,
) -> BaseMessage:
    """`message` carrying its turn's correlation id, authorship and status, where each is known."""
    if correlation_id:
        message.additional_kwargs[STORED_CORRELATION_ID] = correlation_id
    if authorship is not None and (authorship.actor is not None or authorship.agent is not None):
        message.additional_kwargs[STORED_AUTHORSHIP] = authorship.model_dump()
    if turn_status:
        message.additional_kwargs[STORED_TURN_STATUS] = turn_status
    return message


def chemist_words(messages: Iterable[BaseMessage]) -> list[str]:
    """Only the messages a *person* typed, as plain text, in the order they were said.

    The filter behind `core/turn_text`'s ambient: a quote is evidence only if the model cannot have
    written it. Excluded are non-`HumanMessage` rows, recovered rows (`is_degraded_render`, whose
    speaker is a guess), and non-string content (an assistant wire shape). Shared by both history
    providers.
    """
    return [
        message.content
        for message in messages
        if isinstance(message, HumanMessage)
        and isinstance(message.content, str)
        and not is_degraded_render(message)
    ]


def message_from_row(payload: dict[str, Any], shape: str | None) -> BaseMessage:
    """One stored row as a LangChain message, whichever shape it holds.

    The one function that knows the stored shapes; `chemclaw.cli.explain` calls it too, rather than
    parsing payloads itself. An unstamped row is the legacy (MAF) shape, since the conversion pass
    is resumable and rows written before the `message_shape` stamp carry none.

    On the read path a row that will not convert degrades to its own text rather than raising, so
    one bad historical row cannot fail a whole transcript. The catch is `Exception` on purpose,
    covering both shapes; because that also swallows converter bugs, the result is stamped
    `DEGRADED_RENDER` and counted.
    """
    if not isinstance(payload, dict):
        # `message` is a bare `jsonb` column, so a scalar or array is storable; every branch below
        # assumes a mapping.
        degraded(log, "session_transcript", "a stored message was not an object; rendering nothing")
        return AIMessage(content="", additional_kwargs={DEGRADED_RENDER: str(shape or "")})
    try:
        if shape == LANGCHAIN_SHAPE:
            return messages_from_dict([payload])[0]
        return to_langchain(payload)
    except Exception:
        # `degraded` rather than a bare warning: the wide catch also swallows converter bugs, and
        # the counter distinguishes one legacy row from a converter broken for everyone.
        degraded(log, "session_transcript", "could not render a stored message; showing its prose")
        # The prose comes from `contents` (the stored shape has no top-level `text`), so a reader
        # still sees what was said. Stamped as recovered: the row's structure (which tool answered,
        # under which call id) is gone.
        return _degraded_class(payload)(
            content=_stored_prose(payload), additional_kwargs={DEGRADED_RENDER: str(shape or "")}
        )


# Which speaker each stored shape's label names. MAF stamps `role`, LangChain stamps `type`; the
# vocabularies are disjoint, so one mapping reads both without knowing the shape.
_DEGRADED_CLASSES: dict[str, type[BaseMessage]] = {
    "user": HumanMessage,
    "human": HumanMessage,
    "system": SystemMessage,
}


def _degraded_class(payload: dict[str, Any]) -> type[BaseMessage]:
    """The message class a refused row should render as, taken from the speaker it names.

    The label is read rather than assumed, so a chemist's question is never rendered as agent
    speech. `AIMessage` is the default for everything else (assistant, tool, unlabelled), since the
    model's voice claims nothing about a person and a `ToolMessage` would need a `tool_call_id`.

    Args:
        payload: The stored `message` column of the row that would not convert.

    Returns:
        The `BaseMessage` subclass to render its prose as.
    """
    label = payload.get("role") or payload.get("type")
    return _DEGRADED_CLASSES.get(str(label), AIMessage)


def _stored_prose(payload: dict[str, Any]) -> str:
    """Whatever text a stored row carries, for a reader that could not convert it properly.

    Deliberately forgiving and tries both stored shapes, because a refused row is exactly one whose
    shape is in doubt.
    """
    contents = payload.get("contents")
    if isinstance(contents, list):
        parts = [part for part in contents if isinstance(part, dict)]
        prose = "".join(str(p.get("text", "")) for p in parts if p.get("type") == "text")
        if prose:
            return prose
        # A refused tool row has no text part; its words are the results, so join them.
        results = [str(p.get("result", "")) for p in parts if p.get("type") == "function_result"]
        if any(results):
            return "\n".join(r for r in results if r)
    data = payload.get("data")
    if isinstance(data, dict):
        content = data.get("content", "")
        if isinstance(content, list):
            # LangChain block content: flatten the text blocks (as `api/schemas.message_text` does)
            # rather than `str()` it, which would show the wire format, tool arguments included, as
            # the agent's words.
            blocks = [str(b.get("text", "")) for b in content if isinstance(b, dict)]
            return "".join(block for block in blocks if block)
        return str(content)
    return str(payload.get("text", ""))


# The correlation id joins a stored message to the audit rows of the turn that wrote it.
# `actor`/`agent` record who wrote the row (`core/authorship.py`), named as `audit_events` names the
# pair.
_INSERT = (
    "INSERT INTO session_messages "
    "(session_id, message, message_shape, correlation_id, actor, agent) "
    "VALUES (%s, %s, %s, %s, %s, %s)"
)
# Row ids come back so a caller can name a row (the conversion pass, an operator reading a log).
#
# Public and shared with the retention sweep, so both feed `message_from_row` the same projection.
# The authorship pair and `turn_status` ride at the end, so readers indexing the first four columns
# are unaffected.
SELECT_SESSION_ROWS = (
    "SELECT id, message, message_shape, correlation_id, actor, agent, turn_status "
    "FROM session_messages WHERE session_id = %s ORDER BY id"
)

# The chemist's message, written ahead of the turn it opens: `_INSERT` plus `turn_status =
# 'running'`, returning its id so the turn can settle this row. The checkpointer holds the message
# from the graph's first step, so the transcript must too.
_INSERT_TURN = (
    "INSERT INTO session_messages "
    "(session_id, message, message_shape, correlation_id, actor, agent, turn_status) "
    "VALUES (%s, %s, %s, %s, %s, %s, 'running') RETURNING id"
)
# Settling a turn's question, in two forms. An answer overrides whatever the row says, including an
# `interrupted` written by another process while this one was still alive. A turn that ended without
# an answer settles only a row still `running`, so a racing teardown can never demote a settled
# turn.
_SETTLE_ANSWERED = "UPDATE session_messages SET turn_status = %s WHERE id = %s AND session_id = %s"
_SETTLE_UNANSWERED = (
    "UPDATE session_messages SET turn_status = %s "
    "WHERE id = %s AND session_id = %s AND turn_status = 'running'"
)
# A turn whose owner is gone: a question still `running` with no live claim covering it — the claim
# is absent, expired, or taken after the question was written (`claimed_at <= created_at` fails only
# for a successor, since a turn claims before writing its question and refreshes never move
# `claimed_at`).
#
# One statement, so exactly one process flips the row and gets it back from `RETURNING`, which is
# what the caller books the `interrupted` outcome from. Served by a partial index.
_MARK_INTERRUPTED = (
    "UPDATE session_messages m SET turn_status = 'interrupted' "
    "WHERE m.session_id = %s AND m.turn_status = 'running' "
    "AND NOT EXISTS (SELECT 1 FROM session_turns t WHERE t.session_id = m.session_id "
    "AND t.expires_at > now() AND t.claimed_at <= m.created_at) "
    "RETURNING m.correlation_id, m.actor, "
    # Whether the turn already booked its own outcome (its process was alive but the settle failed):
    # the question is still marked, but the outcome is not booked twice.
    "EXISTS (SELECT 1 FROM turn_costs c "
    "WHERE c.correlation_id = m.correlation_id AND m.correlation_id <> '') AS booked"
)
# The newest turn's status, for the reattach route's "what happened to the turn I was following".
_LATEST_TURN_STATUS = (
    "SELECT turn_status FROM session_messages "
    "WHERE session_id = %s AND turn_status IS NOT NULL ORDER BY id DESC LIMIT 1"
)

# The chemist's own words in one thread, newest first — the bounded read behind `core/turn_text`'s
# ambient, deliberately separate from `SELECT_SESSION_ROWS` and its no-`LIMIT` rendering rule.
#
# Filtered in SQL, because a `LIMIT` over all rows would ship whole tool results and might return no
# human row at all. `(session_id, id)` serves the scan; the JSONB predicate (which detoasts this
# session's rows) filters on top, while other sessions' rows are discarded by the cheap `session_id`
# qual.
#
# Two predicates define "a row this system recorded from a person": `message_shape` pins the
# LangChain shape, and `message_original IS NULL` excludes rows the conversion pass rewrote from the
# legacy shape, whose user role is not proof a person typed them. That exclusion rests on the
# rollback column; reclaiming it gives the exclusion up.
_SELECT_RECENT_USER_ROWS = (
    "SELECT message, message_shape FROM session_messages "
    "WHERE session_id = %s AND message_shape = %s AND message_original IS NULL "
    "AND message->>'type' = 'human' "
    "ORDER BY id DESC LIMIT %s"
)

# The per-session turn claim. One statement: `ON CONFLICT … DO UPDATE … WHERE` takes the row lock
# and fires only when the incumbent claim has expired, so an empty `RETURNING` means someone else is
# running a turn.
#
# `actor` is the turn's sender, so a replica not holding the turn can apply the stop route's rule (a
# member stops only their own turn) before asking the holder (`agent/turn_remotes.py`).
_TURN_CLAIM = (
    "INSERT INTO session_turns (session_id, holder, expires_at, actor) "
    "VALUES (%s, %s, now() + make_interval(secs => %s), %s) "
    "ON CONFLICT (session_id) DO UPDATE "
    "SET holder = EXCLUDED.holder, claimed_at = now(), expires_at = EXCLUDED.expires_at, "
    "actor = EXCLUDED.actor, admitted = false "
    "WHERE session_turns.expires_at <= now() "
    "RETURNING holder"
)
# Serialises admissions across replicas; released with the transaction, so no connection holds it
# between turns.
_ADMISSION_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended('turn_admission', 0))"
# Take one of the deployment's concurrent-turn slots (`SessionTurnClaims.admit`). Run after
# `_ADMISSION_LOCK` in the same transaction: this statement's snapshot is taken after the lock is
# granted, so it counts every admission committed before it. A claim that is no longer this
# holder's matches no row, which reads as refused. `%(fleet)s = 0` and `%(actor_cap)s = 0` switch
# their limit off.
_TURN_ADMIT = (
    "UPDATE session_turns SET admitted = true "
    "WHERE session_id = %(session)s AND holder = %(holder)s AND expires_at > now() "
    "AND (%(fleet)s = 0 OR (SELECT count(*) FROM session_turns "
    "      WHERE admitted AND expires_at > now()) < %(fleet)s) "
    "AND (%(actor_cap)s = 0 OR %(actor)s::text IS NULL OR (SELECT count(*) FROM session_turns "
    "      WHERE admitted AND expires_at > now() AND actor = %(actor)s "
    "      AND session_id <> %(session)s) < %(actor_cap)s) "
    "RETURNING session_id"
)
# How many turns a person holds on sessions other than this one: the leases they sent that have not
# lapsed, admitted or not, so a turn that is still waiting for its slot counts.
_ACTOR_TURNS = (
    "SELECT count(*) FROM session_turns "
    "WHERE actor = %s AND session_id <> %s AND expires_at > now()"
)
# Guarded by `holder` so a worker whose lease already lapsed and was taken by someone else cannot
# extend — or delete — the new owner's claim.
_TURN_REFRESH = (
    "UPDATE session_turns SET expires_at = now() + make_interval(secs => %s) "
    "WHERE session_id = %s AND holder = %s"
)
_TURN_RELEASE = "DELETE FROM session_turns WHERE session_id = %s AND holder = %s"

# The same three operations over a set of sessions, one statement each.
#
# `agent/leaver.py` claims every session a departing person owns before deleting any; one round trip
# per session would outrun the lease on large fleets. `unnest(%s::text[])` keeps the statement
# constant for any batch size (the cast is needed for psycopg's untyped array).
#
# The caller must not repeat a session id in one batch: `ON CONFLICT DO UPDATE` cannot touch a row
# twice in one statement, and de-duplicating here would hide that caller bug.
_TURN_CLAIM_MANY = (
    "INSERT INTO session_turns (session_id, holder, expires_at) "
    "SELECT s, %s, now() + make_interval(secs => %s) FROM unnest(%s::text[]) AS s "
    "ON CONFLICT (session_id) DO UPDATE "
    "SET holder = EXCLUDED.holder, claimed_at = now(), expires_at = EXCLUDED.expires_at, "
    "admitted = false "
    "WHERE session_turns.expires_at <= now() "
    "RETURNING session_id"
)
# `FOR UPDATE SKIP LOCKED` with `ORDER BY session_id` keeps this heartbeat from deadlocking the
# erasure transaction that deletes the same `session_turns` rows. Skipping a locked row is harmless:
# a row the erasure holds cannot be claimed by another pod either. The holder guard sits in the
# locking sub-select, where the holder can no longer change.
_TURN_REFRESH_MANY = (
    "UPDATE session_turns SET expires_at = now() + make_interval(secs => %s) "
    "WHERE session_id IN ("
    "  SELECT session_id FROM session_turns "
    "   WHERE session_id = ANY(%s::text[]) AND holder = %s "
    "   ORDER BY session_id FOR UPDATE SKIP LOCKED) "
    "RETURNING session_id"
)
_TURN_RELEASE_MANY = "DELETE FROM session_turns WHERE holder = %s AND session_id = ANY(%s::text[])"
# Which of these sessions somebody else holds now, asked only when a refresh came back short, so the
# caller warns on a real takeover and not on rows its own erase transaction locked or deleted.
_TURN_OTHER_HOLDERS = (
    "SELECT session_id FROM session_turns WHERE session_id = ANY(%s::text[]) AND holder <> %s"
)

# The one definition of the session list's sort key, used by both writers: `updated_at` is
# `max(session_messages.created_at)` for the session, correlated on the owning row so it costs one
# backwards index probe.
_NEWEST_MESSAGE = (
    "(SELECT max(m.created_at) FROM session_messages m WHERE m.session_id = o.session_id)"
)

# `updated_at` is derived rather than left NULL because `agent/session_fork.py` runs this after
# copying the transcript, and a NULL sort key would hide the fork from `GET /sessions`. For a new
# session it is NULL: nothing has been said yet.
#
# `SELECT … FROM (VALUES …)` lets the subquery name the session id without a second parameter; the
# casts give bare placeholders a type.
_OWNER_INSERT = (
    "INSERT INTO session_owners (session_id, owner, profile, updated_at) "
    "SELECT o.session_id, o.owner, o.profile, "
    f"{_NEWEST_MESSAGE} "
    "FROM (VALUES (%s::text, %s::text, %s::text)) AS o (session_id, owner, profile) "
    "ON CONFLICT (session_id) DO NOTHING"
)
# The other writer: the turn that just appended to `session_messages`, in the same transaction.
# Recomputed rather than stamped `now()`, so a writer supplying its own `created_at` (the fork) is
# summarised by the same rule.
_OWNER_TOUCH = f"UPDATE session_owners o SET updated_at = {_NEWEST_MESSAGE} WHERE o.session_id = %s"
# The profile comes back with the owner: a rehydration that lost it would widen the session's tool
# surface, since a profile only attenuates.
_OWNER_SELECT = "SELECT owner, profile FROM session_owners WHERE session_id = %s"
# A page of the owner's sessions, newest activity first.
#
# The sort key is the mirrored `updated_at` column (last message), not `created_at`, served by
# `session_owners_owner_updated_idx` as a bounded index walk; a lateral `max(created_at)` would be
# evaluated for every session the owner ever created. `created_at` is still returned for display.
#
# The `EXISTS` arm keeps the mirror out of the membership decision: `o.updated_at IS NOT NULL` is
# the index condition, and `EXISTS` drops sessions with no messages (abandoned drafts, or
# transcripts pruned by retention) at the moment their rows go. The mirror can only mis-order a
# page, never invent or hide a row.
#
# The `after` arm is a keyset cursor, not an `OFFSET`: this list reorders as it is read, so the
# row-wise `(updated_at, session_id) <` comparison names a position in a strict total order. It
# self-disables through `%s::timestamptz IS NULL`, so the first and later pages are one statement.
#
# `profile` is returned so `GET /plans/pending` can skip sessions that cannot hold a plan without a
# checkpointer read.
#
# The owner match is NULL-safe (the shared dev principal is a real NULL owner) but spelled as two
# arms rather than `IS NOT DISTINCT FROM`, which is not btree-searchable and would defeat
# `session_owners_owner_idx`. Hence the owner is bound twice.
_OWNER_LIST = (
    "SELECT o.session_id, o.created_at, o.updated_at, o.title, o.profile FROM session_owners o "
    "WHERE (o.owner = %s OR (o.owner IS NULL AND %s::text IS NULL)) "
    "  AND o.updated_at IS NOT NULL "
    "  AND (%s::timestamptz IS NULL "
    "       OR (o.updated_at, o.session_id) < (%s::timestamptz, %s::text)) "
    "  AND EXISTS (SELECT 1 FROM session_messages m WHERE m.session_id = o.session_id) "
    "ORDER BY o.updated_at DESC, o.session_id DESC LIMIT %s"
)
# First writer wins, in one statement: a title names a session after its opening question, and
# `title IS NULL` lets the turn route call this unconditionally.
_OWNER_TITLE = "UPDATE session_owners SET title = %s WHERE session_id = %s AND title IS NULL"

# The per-table predicate for deleting one session's rows.
#
# The table set is `chemclaw.agent.leaver._ERASE`'s (see `_session_delete_statements`), not declared
# here; only the predicate is, since some erasure tables are actor-scoped (`_ACTOR_SCOPED_ONLY`).
#
# `tool_result_blobs` is content-addressed and may be shared across sessions, so it is deleted only
# when no link from a still-existing session references it. This session's own link row survives a
# shared blob (the runtime role has no DELETE on `tool_result_links`), and the retention sweep later
# collects it with the blob. Counting only links of existing sessions keeps such orphan links from
# pinning a blob for ever, which matters because forks share their parent's links.
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
    # An artefact is part of the conversation; its revisions go by cascade, so the runtime role
    # needs no DELETE on the append-only revision table.
    "session_exhibits": "DELETE FROM session_exhibits WHERE session_id = %(session_id)s",
    # The conversation's uploaded working files (`120_session_attachments.sql`), whoever uploaded
    # them: deleting a conversation deletes what was handed to it.
    "session_attachments": "DELETE FROM session_attachments WHERE session_id = %(session_id)s",
    "session_events": "DELETE FROM session_events WHERE session_id = %(session_id)s",
    "session_turns": "DELETE FROM session_turns WHERE session_id = %(session_id)s",
    # Both also cascade from `session_owners` (`infra/sql/110_shared_sessions.sql`); named so the
    # delete says what it removed.
    "session_members": "DELETE FROM session_members WHERE session_id = %(session_id)s",
    "plan_authors": "DELETE FROM plan_authors WHERE session_id = %(session_id)s",
    "session_turn_queue": "DELETE FROM session_turn_queue WHERE session_id = %(session_id)s",
    # Requests other replicas addressed to the session's running turn, and the frames in transit to
    # them (`infra/sql/121_session_turn_remotes.sql`); the frames cascade from the request.
    "session_turn_remotes": "DELETE FROM session_turn_remotes WHERE session_id = %(session_id)s",
    "session_owners": "DELETE FROM session_owners WHERE session_id = %(session_id)s",
}

# Tables in the erasure set a session delete must leave alone: each is keyed by the person, so
# deleting one conversation would take data from all of theirs.
_ACTOR_SCOPED_ONLY: dict[str, str] = {
    "store": "an agent memory outlives the session it was written in — that is what it is for",
    "store_vectors": "the embedding half of the same memory",
    "subscriptions": "a standing query belongs to the person, not to one conversation",
    "composed_workflows": (
        "a workflow the agent wrote down outlives the conversation that asked for it — that is "
        "the whole point of composing one, and deleting one session must not take the procedure "
        "the next session is meant to run"
    ),
    "user_preferences": "a preference is the person's, and survives every session they close",
    # A spend window bounds what one person may spend; a `session_id` predicate here would let
    # deleting a conversation reset the quota.
    "budget_usage": (
        "a spend window bounds a person, not a conversation — and a session delete that reset it "
        "would be a free allowance reset available to anyone who is over budget"
    ),
    # The same argument for the request rate.
    "request_buckets": (
        "a request-rate balance bounds a person, not a conversation — and a session delete that "
        "refilled it would be a free burst available to anyone who is being limited"
    ),
}


@cache
def _session_delete_statements() -> tuple[tuple[str, str], ...]:
    """The per-table DELETEs for one session, in the order an actor's erasure uses.

    Derived from `leaver._ERASE` so "which tables hold a session's data" has one answer. Every table
    there is either session-scoped (`_SESSION_DELETE`, or the checkpointer's `thread_id`) or
    actor-scoped (`_ACTOR_SCOPED_ONLY`); an unclassified table raises. The order puts the ownership
    row last, since it is the only way to find the session again. The imports are deferred because
    `leaver` and `checkpointer` import this module.

    Returns:
        `(table, statement)` pairs, each statement taking one `session_id` parameter.

    Raises:
        RuntimeError: the erasure sweep names a table this delete has not classified.
    """
    from chemclaw.agent.checkpointer import checkpoint_thread_delete_statements
    from chemclaw.agent.leaver import _ERASE

    scoped = dict(_SESSION_DELETE)
    # A thread id is a session id. The checkpointer supplies these statements because their order is
    # what stops a concurrent turn keeping a checkpoint whose payload has gone.
    scoped.update(dict(checkpoint_thread_delete_statements("thread_id = %(session_id)s")))
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

    The cursor is the sort key (last activity, session id), so it names a place in the ordering and
    stays valid as rows move or the page size changes. Base64url so clients do not construct one;
    not signed, because every page is re-scoped to the caller's own sessions and a forged cursor
    only moves the forger within their own list.

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
        ValueError: the token is not one this service minted. One error type for every malformation,
        since the caller's answer is the same.
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

    One resolver for all three stores, so one session's ownership, turn claim and history always
    share a database.
    """
    return settings.session_store_dsn or settings.postgres_dsn


@asynccontextmanager
async def _session_connection(dsn: str) -> AsyncIterator[psycopg.AsyncConnection[TupleRow]]:
    """Borrow a session-layer connection with the configured per-statement timeout.

    Pooled when the process opened a pool (`chemclaw.core.db.pooling`), a dedicated connect
    otherwise. A down database reports "Postgres unreachable at <host>", and a hung query is
    cancelled.
    """
    async with db.connection(dsn) as conn:
        yield conn


class PostgresHistoryProvider:
    """Persists a session's transcript to Postgres, and reads it back for a person."""

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

        A plain read with no repair: the graph builds its thread from the checkpointer, and the only
        caller is the transcript route, which renders for a person.
        """
        if not session_id:
            return []
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(SELECT_SESSION_ROWS, (session_id,))
                rows = await cur.fetchall()
        return [
            _stamped(
                message_from_row(row[1], row[2]),
                str(row[3] or ""),
                Authorship(actor=row[4], agent=row[5]),
                row[6],
            )
            for row in rows
        ]

    async def recent_user_texts(
        self,
        session_id: str | None,
        *,
        limit: int,
        state: dict[str, Any] | None = None,
    ) -> list[str]:
        """The chemist's own last `limit` messages in this thread, oldest first.

        Unlike `get_messages`, bounded: it serves `core/turn_text`'s ambient once per turn on the
        hot path.

        Args:
            session_id: The thread. Unknown or `None` returns nothing, which every reader treats as
            "no chemist spoke" rather than as a waiver.
            limit: How many of the chemist's messages to return, newest kept.
            state: Ignored; present so both providers answer the same call.

        Returns:
            Their messages oldest first, so the caller can append the turn in flight in the order it
            was said.
        """
        if not session_id or limit <= 0:
            return []
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT_RECENT_USER_ROWS, (session_id, LANGCHAIN_SHAPE, limit))
                rows = await cur.fetchall()
        # Reversed because the query orders newest-first to make the `LIMIT` mean "the most recent
        # ones"; a conversation reads the other way.
        return chemist_words(message_from_row(row[0], row[1]) for row in reversed(rows))

    async def save_messages(
        self,
        session_id: str | None,
        messages: Sequence[BaseMessage],
        *,
        state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Append this turn's messages to the session's durable history (no-op if none to store).

        One transaction, which also updates the session list's sort key (`_OWNER_TOUCH`), so the
        exchange and the mirror commit together and `chemclaw.api.runner` needs no rollback.
        Bounding the table is `durable/retention.py`'s job, not this append's.
        """
        if not session_id or not messages:
            return
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(_INSERT, _rows(session_id, messages))
                await cur.execute(_OWNER_TOUCH, (session_id,))
            await conn.commit()

    async def begin_turn(
        self,
        session_id: str | None,
        message: BaseMessage,
        *,
        state: dict[str, Any] | None = None,
    ) -> int | None:
        """Write the chemist's message ahead of its turn, `running`; return the row to settle.

        The checkpointer holds the message from the graph's first step, so writing it here keeps the
        transcript in step, and how the turn ended becomes a column rather than an absence. The sort
        key moves in the same transaction. `None` for no session, which the caller reads as "settle
        nothing, write the exchange whole".
        """
        if not session_id:
            return None
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_INSERT_TURN, _rows(session_id, [message])[0])
                row = await cur.fetchone()
                await cur.execute(_OWNER_TOUCH, (session_id,))
            await conn.commit()
        return int(row[0]) if row is not None else None

    async def finish_turn(
        self,
        session_id: str | None,
        turn: int,
        messages: Sequence[BaseMessage],
        status: TurnStatus,
        *,
        state: dict[str, Any] | None = None,
    ) -> None:
        """Append the rest of a turn's exchange and settle its question's status, in one commit.

        `messages` is everything after the question and is empty for a turn that ended without an
        answer. `done` overrides the row's status; anything else settles only a row still `running`
        (see `_SETTLE_ANSWERED`).
        """
        if not session_id:
            return
        settle = _SETTLE_ANSWERED if status == "done" else _SETTLE_UNANSWERED
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                if messages:
                    await cur.executemany(_INSERT, _rows(session_id, messages))
                await cur.execute(settle, (status, turn, session_id))
                if messages:
                    await cur.execute(_OWNER_TOUCH, (session_id,))
            await conn.commit()

    async def mark_interrupted(
        self, session_id: str | None, *, state: dict[str, Any] | None = None
    ) -> list[InterruptedTurn]:
        """Mark this session's turns whose owner is gone `interrupted`; return the ones marked now.

        Each turn is returned by exactly one call across every process (`_MARK_INTERRUPTED`), so its
        outcome is booked once; `booked` says whether the turn's own process already booked it.
        Called by whoever touches the session next.
        """
        if not session_id:
            return []
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_MARK_INTERRUPTED, (session_id,))
                rows = await cur.fetchall()
            await conn.commit()
        return [InterruptedTurn(str(row[0] or ""), row[1], bool(row[2])) for row in rows]

    async def latest_turn_status(
        self, session_id: str | None, *, state: dict[str, Any] | None = None
    ) -> TurnStatus | None:
        """How the session's newest written-ahead turn stands, or `None` when it has none."""
        if not session_id:
            return None
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_LATEST_TURN_STATUS, (session_id,))
                row = await cur.fetchone()
        return turn_status_of(row[0]) if row is not None else None


def _rows(session_id: str, messages: Sequence[BaseMessage]) -> list[tuple[Any, ...]]:
    """The `_INSERT` parameters for these messages, stamped with their turn's id and authorship.

    One turn's messages share its correlation id (empty off the request path) and its person.
    """
    correlation_id = get_current_correlation_id() or ""
    actor = get_current_actor()
    rows: list[tuple[Any, ...]] = []
    for message in messages:
        authorship = message_authorship(message, actor)
        rows.append(
            (
                session_id,
                Jsonb(message_to_dict(message)),
                LANGCHAIN_SHAPE,
                correlation_id,
                authorship.actor,
                authorship.agent,
            )
        )
    return rows


def owner_permits(owner: str | None, actor: str | None) -> bool:
    """Whether a stored owner lets `actor` reach the row — the one ownership rule.

    Shared by every caller that resolves ownership (the HTTP session gate, evidence packs, protocol
    designs) so no surface is looser than another. `owner` is whichever column records who opened
    the row.

    With `entra_required` off there is no real actor, so an owner-less row degrades open. Once
    identity is enforced, an owner-less row (a dev-mode leftover) belongs to nobody. `None` and `""`
    are treated alike.

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
    """Durable session-ownership registry, so a restarted front door can reattach a client.

    The front door's live-session LRU dies with the pod; this row records who owns each session id,
    so on a cache miss the owner is looked up to authorize a reattach over the durable history. One
    identity row per session, separate from the append-only history.
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

        `found` distinguishes an unknown session from one owned by the shared principal (a real
        `NULL` owner).
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

        Called every turn; one conditional `UPDATE` rather than a read-then-write, which would cost
        two round trips and could race.
        """
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_OWNER_TITLE, (title, session_id))
            await conn.commit()

    async def list_for_owner(
        self, owner: str | None
    ) -> list[tuple[str, datetime, datetime, str | None, str | None]]:
        """The owner's newest page of sessions — `page_for_owner` from the top.

        `(session_id, created_at, updated_at, title, profile)` per row. The shape the front door's
        `SessionOwners` protocol declares; it delegates so the first page and later pages share one
        statement.
        """
        return await self.page_for_owner(owner)

    async def page_for_owner(
        self, owner: str | None, *, after: str | None = None
    ) -> list[tuple[str, datetime, datetime, str | None, str | None]]:
        """One page as `(session_id, created_at, updated_at, title, profile)`.

        Newest first, at most `service_max_listed_sessions` rows, resuming strictly after the row
        `after` names; `_OWNER_LIST` explains the order, which sessions appear, and the keyset
        resume. `profile` `None` means the default profile. A tuple, matching `lookup`; each row's
        cursor is derivable with `encode_session_cursor`.

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
                    (owner, owner, stamp, stamp, last_id, settings.service_max_listed_sessions),
                )
                rows = await cur.fetchall()
        return [(row[0], row[1], row[2], row[3], row[4]) for row in rows]

    async def delete_session(self, session_id: str) -> dict[str, int]:
        """Delete one conversation and everything keyed by it, in one transaction.

        The session-scoped counterpart to `chemclaw.agent.leaver.erase_actor`, over the same table
        set. One transaction, because an ownership row deleted without the rows it keys would leave
        data nothing can find again. Actor-scoped rows outlive the conversation
        (`_ACTOR_SCOPED_ONLY`). Missing tables (e.g. the checkpointer's, before the graph ever ran)
        are skipped, as the erasure sweep skips them.

        Args:
            session_id: The conversation to delete.

        Returns:
            `{table: rows deleted}`, one key per table in the sweep, zero where a table held nothing
            or does not exist.
        """
        statements = _session_delete_statements()
        removed: dict[str, int] = {}
        for attempt in range(settings.pg_deadlock_retries + 1):
            try:
                removed = await self._delete_session_once(session_id, statements)
                break
            except (psycopg.errors.DeadlockDetected, psycopg.errors.SerializationFailure):
                if attempt == settings.pg_deadlock_retries:
                    raise
                log.warning(
                    "deleting session %s was aborted as a deadlock victim (attempt %d); retrying",
                    session_id,
                    attempt + 1,
                )
        log.info(
            "deleted session %s: %d row(s) across %d table(s)",
            session_id,
            sum(removed.values()),
            len([table for table, count in removed.items() if count]),
        )
        return removed

    async def _delete_session_once(
        self, session_id: str, statements: tuple[tuple[str, str], ...]
    ) -> dict[str, int]:
        """One attempt at the delete transaction — the body `delete_session` retries.

        A deadlock abort rolls the whole transaction back, so the retry unit is the transaction.
        """
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
        return removed


class SessionTurnClaims:
    """One turn at a time per session, across every process, as a leased row.

    Two concurrent turns on one session would interleave their messages, and the front door runs
    several replicas, so the guard must be shared. A lease, not a lock: an advisory or row lock
    would pin a pooled connection for the whole turn, while each operation here is one short
    statement. The claim is refreshed while the turn runs and deleted when it ends, so a killed
    worker stops blocking after one lease. Exclusion holds as long as the holder refreshes in time
    (see the lease setting in `core/config/service.py`).
    """

    def __init__(self) -> None:
        """Bind to the session-store database (falling back to the shared `postgres_dsn`)."""
        self._dsn = _session_dsn()

    def _connection(self) -> AbstractAsyncContextManager[psycopg.AsyncConnection[TupleRow]]:
        """Borrow a connection on this store's database (see `_session_connection`)."""
        return _session_connection(self._dsn)

    async def claim(
        self, session_id: str, holder: str, lease_seconds: float, *, actor: str | None = None
    ) -> bool:
        """Take the session's turn slot for `lease_seconds`; False if someone else holds it.

        One statement, so no process observes a gap between check and take. `actor` records who sent
        the turn for replicas that do not hold it (`agent/turn_remotes.py`).
        """
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_TURN_CLAIM, (session_id, holder, lease_seconds, actor))
                taken = await cur.fetchone() is not None
            await conn.commit()
        return taken

    async def admit(
        self, session_id: str, holder: str, *, fleet_cap: int, actor: str | None, actor_cap: int
    ) -> bool:
        """Take one of the deployment's concurrent-turn slots for this holder's live claim.

        Atomic across replicas: the count and the take happen in one transaction under an advisory
        lock, so two replicas cannot both see the last slot free. `fleet_cap` bounds the turns
        admitted at once anywhere (0: unbounded); `actor_cap` bounds the admitted turns of `actor`
        on other sessions (0: unbounded). A slot lives as long as the claim: it is freed by the
        turn's release, or by the claim's lease lapsing when its pod dies. False when a limit is
        reached or the claim is no longer this holder's.
        """
        params = {
            "session": session_id,
            "holder": holder,
            "fleet": fleet_cap,
            "actor": actor,
            "actor_cap": actor_cap,
        }
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_ADMISSION_LOCK)
                await cur.execute(_TURN_ADMIT, params)
                taken = await cur.fetchone() is not None
            await conn.commit()
        return taken

    async def actor_turns(self, actor: str, besides: str) -> int:
        """How many live turn claims `actor` holds on sessions other than `besides`, anywhere."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_ACTOR_TURNS, (actor, besides))
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def refresh(self, session_id: str, holder: str, lease_seconds: float) -> bool:
        """Push this holder's claim out by another lease; False if it is no longer ours.

        A no-op when the claim is gone or taken over (this worker was declared dead), since
        re-taking it would cause the interleaving the guard prevents. The return value lets the
        holder notice a silent takeover.
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

    async def claim_many(
        self, session_ids: Sequence[str], holder: str, lease_seconds: float
    ) -> set[str]:
        """Take the turn slot of every one of these sessions at once; return the ones taken.

        The set-shaped `claim`, for `agent/leaver.py`'s erasure. Sessions not returned are held by
        someone else; this neither waits for them nor releases the ones it took.

        Args:
            session_ids: The sessions to claim. Must not repeat one (see `_TURN_CLAIM_MANY`).
            holder: Who is claiming, for the release and refresh guards.
            lease_seconds: How long the claims last without a refresh.

        Returns:
            The subset that is now held by `holder`.
        """
        if not session_ids:
            return set()
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_TURN_CLAIM_MANY, (holder, lease_seconds, list(session_ids)))
                taken = {str(row[0]) for row in await cur.fetchall()}
            await conn.commit()
        return taken

    async def refresh_many(
        self, session_ids: Sequence[str], holder: str, lease_seconds: float
    ) -> set[str]:
        """Push this holder's claims out by another lease; return the ones still ours.

        A session absent from the result was taken over, deleted, or locked by a concurrent
        transaction and skipped (see `_TURN_REFRESH_MANY`).
        """
        if not session_ids:
            return set()
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_TURN_REFRESH_MANY, (lease_seconds, list(session_ids), holder))
                still_ours = {str(row[0]) for row in await cur.fetchall()}
            await conn.commit()
        return still_ours

    async def release_many(self, session_ids: Sequence[str], holder: str) -> None:
        """Give a whole batch of slots back (idempotent; only this holder's rows go)."""
        if not session_ids:
            return
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_TURN_RELEASE_MANY, (holder, list(session_ids)))
            await conn.commit()

    async def other_holders(self, session_ids: Sequence[str], holder: str) -> set[str]:
        """Which of these sessions are claimed by somebody other than `holder`, right now."""
        if not session_ids:
            return set()
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_TURN_OTHER_HOLDERS, (list(session_ids), holder))
                return {str(row[0]) for row in await cur.fetchall()}


class InMemoryHistoryProvider:
    """The dev/test transcript store: the same primitives, over the session's own state.

    Keeps the thread in `session.state`, which is why the primitives take `state` and why a
    memory-backed transcript dies with the pod.
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

    async def recent_user_texts(
        self,
        session_id: str | None,
        *,
        limit: int,
        state: dict[str, Any] | None = None,
    ) -> list[str]:
        """The chemist's own last `limit` messages in this session's state, oldest first.

        Same filter, order and bound as the durable provider, so both stores answer a
        `basis="stated"` quote alike.
        """
        if state is None or limit <= 0:
            return []
        return chemist_words(state.get(self._KEY) or [])[-limit:]

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
        # Stamped copies, as the durable provider stamps rows on read; the stamp belongs to the
        # stored transcript, not to the turn's own message objects.
        state.setdefault(self._KEY, []).extend(_copies(messages))

    async def begin_turn(
        self,
        session_id: str | None,
        message: BaseMessage,
        *,
        state: dict[str, Any] | None = None,
    ) -> int | None:
        """The durable provider's write-ahead, over the thread kept in `state`; its index back."""
        if state is None:
            return None
        stored = state.setdefault(self._KEY, [])
        stored.extend(_copies([message], "running"))
        return len(stored) - 1

    async def finish_turn(
        self,
        session_id: str | None,
        turn: int,
        messages: Sequence[BaseMessage],
        status: TurnStatus,
        *,
        state: dict[str, Any] | None = None,
    ) -> None:
        """Settle the question at `turn` and append the rest, by the durable provider's rule.

        Settled only while it is still a written-ahead question: a teardown rollback
        (`api/runner._roll_back_unfinished`) may have restored `state` to before it was written.
        """
        if state is None:
            return
        stored = state.setdefault(self._KEY, [])
        if 0 <= turn < len(stored):
            current = stored_turn_status(stored[turn])
            if current == "running" or (status == "done" and current is not None):
                stored[turn].additional_kwargs[STORED_TURN_STATUS] = status
        stored.extend(_copies(messages))

    async def mark_interrupted(
        self, session_id: str | None, *, state: dict[str, Any] | None = None
    ) -> list[InterruptedTurn]:
        """Nothing, ever: an in-memory transcript dies with the process whose turn it would mark."""
        return []

    async def latest_turn_status(
        self, session_id: str | None, *, state: dict[str, Any] | None = None
    ) -> TurnStatus | None:
        """The newest written-ahead question's status in `state`, or `None`."""
        for message in reversed((state or {}).get(self._KEY) or []):
            status = stored_turn_status(message)
            if status is not None:
                return status
        return None


def _copies(messages: Sequence[BaseMessage], turn_status: str | None = None) -> list[BaseMessage]:
    """Stamped copies of `messages`, as the durable provider stamps its rows on read."""
    correlation_id = get_current_correlation_id() or ""
    actor = get_current_actor()
    return [
        _stamped(
            message.model_copy(update={"additional_kwargs": dict(message.additional_kwargs)}),
            correlation_id,
            message_authorship(message, actor),
            turn_status,
        )
        for message in messages
    ]
