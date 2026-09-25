"""Postgres backing for the wait: `infra/sql/076_pending_requests.sql`.

The workflow in `durable/awaiting.py` is the authority on whether a wait is still open; this table
is its projection, written by that workflow's own activities and read by the front door and the
agent. Kept separate from the workflow module for the reason `audit_store` is kept separate from
`audit`: a workflow module is imported by every worker, and a worker that runs no wait should not
pull psycopg for a store it will not use.

**Every write here is an upsert keyed on `request_id`, and every state transition is guarded.**
An activity runs at-least-once, so `open` must be replayable, and `close` must not be able to
overwrite an answer with an expiry — a reminder that fires while a person is clicking would
otherwise decide the outcome by whichever transaction commits second. The guard is in the SQL
(`WHERE state = 'waiting'`), so it holds across processes rather than in whichever worker asks.
"""

import json
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Annotated, Any

import psycopg
from psycopg.rows import TupleRow, class_row
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from chemclaw.core import db
from chemclaw.core.config import settings


def _stamp(value: Any) -> Any:
    """A timestamp column as this model's ISO string, leaving anything else to be validated.

    **A validator rather than a SQL-side cast, because the string is on the wire.** These three
    fields reach `GET /pending` and the agent's own inbox tool as `datetime.isoformat()` spells
    them; `::text` in the SELECT would have converted them in the server and spelled them
    differently, which is a change to an API response rather than to a row factory.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    return "" if value is None else value


#: A `TIMESTAMPTZ` column carried as the ISO string this seam has always exposed. `answered_at` is
#: nullable, and NULL reads as the empty string — which is what "still waiting" looks like here.
Stamp = Annotated[str, BeforeValidator(_stamp)]


class PendingRequest(BaseModel):
    """One open or settled wait, as a surface reads it.

    Read back by `class_row`, so every field name here is a column name in `_COLUMNS` and in that
    order: the two are one declaration rather than two that agreed by inspection.
    """

    #: `extra="forbid"` because this model is built by `class_row` straight out of a SELECT: with
    #: pydantic's default a column nobody added a field for is *ignored*, so the read that was
    #: supposed to catch the SELECT and the model drifting apart would silently drop it. Forbidding
    #: makes the drift an error naming the column, which is the whole reason for the row factory.
    model_config = ConfigDict(extra="forbid")

    request_id: str
    kind: str
    subject: str
    rationale: str = ""
    asked_of: str = ""
    requested_by: str = ""
    session_id: str = ""
    state: str = "waiting"
    due_at: Stamp = ""
    reminders: int = 0
    answered_at: Stamp = ""
    answered_by: str = ""
    answer: dict[str, Any] = Field(default_factory=dict)
    created_at: Stamp = ""
    #: The knowledge notes the question rests on, so the answer route can ask whether they still
    #: hold (`kg/premise.py`). Read out of the row rather than recomputed from `subject`, because a
    #: re-ask may reword the question and the premise that was *validated* at ask time is the one an
    #: answer must be checked against.
    premise_note_ids: list[str] = Field(default_factory=list)


#: The most rows one `open_requests` call will serve, however much a caller asks for. A module
#: constant rather than a `Settings` field for the reason `ingest/rejections._MAX_ROWS_PER_SOURCE`
#: is one — it is the bound that keeps an inbox read from becoming a table scan into somebody's
#: prompt, not a deployment decision. Reported as `limit_applied` rather than applied silently:
#: a caller asking for 10,000 used to get 200 and had no way to tell that from a corpus of 200.
_MAX_PAGE = 200


class OpenRequests(BaseModel):
    """One page of the inbox, **and how much of the inbox it is**.

    The bare `list[PendingRequest]` this replaced was the silence this table exists to prevent, one
    level up. Measured against a real database: 35 rows waiting, a page of 20 returned, and nothing
    in the value, in a log or in a counter said the other 15 were there. Both readers then said
    something stronger than they knew — `check_pending_requests` documents itself as "everything
    still waiting", and `GET /pending` is the inbox a raised question has to appear in or it ages
    out unanswered.

    `total_waiting` is counted over the *same* predicate in the *same* transaction as the page, so
    "20 of 35" is one consistent statement rather than two reads of a moving table.
    """

    requests: list[PendingRequest] = Field(default_factory=list)
    # Everything matching, before the page bound — the population this page is a page of.
    total_waiting: int = Field(default=0, ge=0)
    # The bound actually used, which is not the bound asked for once `_MAX_PAGE` bites.
    limit_applied: int = Field(default=_MAX_PAGE, ge=1)

    @property
    def truncated(self) -> bool:
        """Whether waiting requests exist that this page does not carry."""
        return self.total_waiting > len(self.requests)


def _connect() -> AbstractAsyncContextManager[psycopg.AsyncConnection[TupleRow]]:
    """The configured connection, with the shared statement timeout (one place, DRY)."""
    return db.connection(settings.session_store_dsn or settings.postgres_dsn)


# **Three cases, and telling them apart is the whole point of `run_id`.** A retry of the opening
# activity carries the *same* Temporal run and must update in place without disturbing a state the
# workflow may already have settled. A re-ask after a **lapsed** deadline carries a different run —
# `request_id_for` is deterministic and `ALLOW_DUPLICATE` is set precisely so a lapsed question can
# be asked again — and must reopen the row, so the new wait is visible and answerable.
#
# The third case is the one the first version of this fix got wrong. Guarding on
# `run_id <> EXCLUDED.run_id` alone admitted an **answered** row, and the reopen NULLs
# `answered_at`/`answered_by`/`answer` — so re-asking a question somebody had already answered
# destroyed their attribution and their payload. This table is in `retention._NOT_PRUNED`, justified
# there as "the attribution for an answer that released a durable workflow", and the answer route
# writes no audit event: the row is the only record there is. A row that can never be deleted must
# not be silently overwritten either.
#
# So a reopen is scoped to the terminal states in which **nobody answered**. A genuinely new ask of
# an already-answered question differs in its `subject`, which is what `request_id_for` keys on, and
# therefore gets its own row rather than overwriting somebody's answer.
#
# Guarding on `state = 'waiting'` alone — the version before either fix — did the retry case and
# silently dropped the re-ask: the row kept the old cycle's `expired` state and deadline, so the new
# wait appeared in no inbox and the answer route refused it with 409 forever while the workflow ran.
_OPEN = """
    INSERT INTO pending_requests
        (request_id, kind, subject, rationale, asked_of, requested_by, session_id,
         correlation_id, premise_note_ids, due_at, run_id)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (request_id) DO UPDATE SET
        kind = EXCLUDED.kind,
        subject = EXCLUDED.subject,
        rationale = EXCLUDED.rationale,
        asked_of = EXCLUDED.asked_of,
        due_at = EXCLUDED.due_at,
        run_id = EXCLUDED.run_id,
        -- **Refreshed, because they legitimately differ between cycles and a gate reads one of
        -- them.** `request_id_for` keys on (kind, subject, asked_of) and deliberately *not* on the
        -- requester, so a re-ask is routinely a different person in a different session. Leaving
        -- these stale meant `_may_answer`'s separation-of-duties check read the *previous* cycle's
        -- requester: bob re-launches alice's irreversible job, the approval row reopens still
        -- naming alice, and bob passes a check whose entire purpose is to refuse him.
        requested_by = EXCLUDED.requested_by,
        session_id = EXCLUDED.session_id,
        correlation_id = EXCLUDED.correlation_id,
        -- Refreshed for the same reason, and with a sharper edge: a re-ask is a *new* question that
        -- was validated against today's corpus, so keeping the previous cycle's premise would check
        -- an answer against notes this question never rested on — and, where the old cycle cited a
        -- note that has since been retired, would refuse every answer to a question whose own
        -- premise is whole.
        premise_note_ids = EXCLUDED.premise_note_ids,
        state = 'waiting',
        answered_at = NULL,
        answered_by = '',
        answer = '{}'::jsonb,
        reminders = CASE
            WHEN pending_requests.run_id = EXCLUDED.run_id THEN pending_requests.reminders ELSE 0
        END,
        reminded_at = CASE
            WHEN pending_requests.run_id = EXCLUDED.run_id THEN pending_requests.reminded_at
            ELSE NULL
        END
    WHERE pending_requests.state = 'waiting'
       OR (
            pending_requests.run_id <> EXCLUDED.run_id
            AND pending_requests.state IN ('expired', 'cancelled', 'answered')
          )
"""

# **Where the answer goes so the reopen above may have `'answered'` in it** (`D-2026-09-13-an-
# answer-is-archived-so-the-question-can-be-asked-again`). Run before `_OPEN`, in the same
# transaction, so the attribution is either moved aside or the reopen does not happen: migration
# 079 excluded `answered` from the reopen because blanking somebody's answer is worse than refusing
# the ask, and the consequence was that a legitimate re-ask met a non-retryable `ApplicationError`
# in `durable/awaiting.py` and the workflow failed. With the answer archived there is nothing left
# to destroy.
#
# `run_id <> %s` is what keeps a *retry* of the opening activity from archiving its own answer: the
# retry carries the run that already owns the row, `_OPEN`'s `state = 'waiting'` arm does not apply
# to an answered row, and `_CLAIMED_BY` then tells it that it still owns it. Only a different run
# is a new cycle.
#
# `ON CONFLICT DO NOTHING` rather than an upsert: the archive is keyed on the run that *answered*,
# so a second attempt at the same archive is the same row, and the application holds no UPDATE on
# this table anyway (`infra/sql/grants/app_privileges.sql`).
_ARCHIVE_ANSWER = """
    INSERT INTO pending_request_answers
        (request_id, run_id, kind, subject, asked_of, requested_by, session_id,
         answered_at, answered_by, answer)
    SELECT request_id, run_id, kind, subject, asked_of, requested_by, session_id,
           answered_at, answered_by, answer
      FROM pending_requests
     WHERE request_id = %s
       AND state = 'answered'
       AND answered_at IS NOT NULL
       AND run_id <> %s
    ON CONFLICT (request_id, run_id) DO NOTHING
"""

# **There is no verdict to read any more, and that is what archiving the answer bought.**
#
# `open_request` used to return whether this run held the row, and `_CLAIMED_BY` was the read behind
# it: `_OPEN` is a guarded upsert, so "wrote nothing" is one of its ordinary outcomes, and the one
# case that mattered was a re-ask meeting an **answered** row — refused, because reopening blanked
# an attribution nothing can delete. The workflow was told, and raised a non-retryable
# `ApplicationError`, because no number of attempts changes whose answer is in that row.
#
# Since `_ARCHIVE_ANSWER` moves the answer aside
# (`D-2026-09-13-an-answer-is-archived-so-the-question-can-be-asked-again`) every terminal state is
# reopenable by a different run, so that refusal has **no reachable input**: driven over all five
# shapes — first ask, retry by the owning run, re-ask after `answered`, re-ask after `expired`, and
# a caller with no run id — the verdict was `True` in every one. A guard whose condition is provably
# false reads as a control and is not one, which is the `reject_widening` shape this repository
# deleted rather than kept alive by a test that calls it directly.
#
# The invariant is not lost, because an invariant is not a function: `tests/test_pending_store.py`
# drives all five shapes and asserts what each one does to the row and to the archive. A future
# narrowing of `_OPEN`'s `WHERE` — which has been rewritten three times, in 076, 079 and 096 — goes
# red on the behaviour rather than on a verdict nobody reads.

# `answered_at` only where somebody answered. It was stamped unconditionally, so an `expired` or
# `cancelled` row carried a timestamp with an empty `answered_by` — a column saying "somebody
# answered at some point" about a question nobody answered, surfaced to the agent and the front door
# that way. `076`'s `pending_requests_answer_is_attributed` constraint exists to stop exactly that
# claim and only fires on `state = 'answered'`; this walked around it from the other side.
_SETTLE = """
    UPDATE pending_requests
    SET state = %s,
        answered_at = CASE WHEN %s = 'answered' THEN now() ELSE NULL END,
        answered_by = %s,
        answer = %s
    WHERE request_id = %s AND state = 'waiting'
"""

# **A running total, not an increment, because an activity is at-least-once.** `reminders =
# reminders + 1` under a 5-attempt retry policy counts one escalation twice whenever an execution
# commits and its completion report is then lost — a worker that dies, a broker that misses the
# response, or simply an attempt that overruns its own `start_to_close_timeout` after the UPDATE.
# Established on a real broker: one escalation, two attempts, `reminders = 2` against the
# workflow's own `self._reminders` of 1 — and that column is what an inbox shows and what
# `AwaitOutcome.reminders` is compared against. `GREATEST` makes the redelivered attempt a no-op
# and keeps the two counters one number, because the value written is the workflow's replay-stable
# total rather than a delta.
_REMIND = """
    UPDATE pending_requests
    SET reminders = GREATEST(reminders, %s), reminded_at = now()
    WHERE request_id = %s AND state = 'waiting'
"""

_COLUMNS = (
    "request_id, kind, subject, rationale, asked_of, requested_by, session_id, state, "
    "due_at, reminders, answered_at, answered_by, answer, created_at, premise_note_ids"
)


async def open_request(
    *,
    request_id: str,
    kind: str,
    subject: str,
    rationale: str,
    asked_of: str,
    requested_by: str,
    session_id: str,
    correlation_id: str,
    due_at: datetime,
    premise_note_ids: list[str] | None = None,
    run_id: str = "",
) -> None:
    """Record a wait as open — archiving the previous cycle's answer when there is one.

    Idempotent within one Temporal run, and **reopening across runs** — see `_OPEN` for why those
    are different cases and what it cost to treat them as one. `run_id` defaults to empty so a
    caller with no run to name (a test, a backfill) keeps the old within-run behaviour.

    **An answered cycle is archived first, which is what lets the reopen include `'answered'`**
    (`D-2026-09-13-an-answer-is-archived-so-the-question-can-be-asked-again`). `_ARCHIVE_ANSWER` and
    `_OPEN` are two statements in one transaction, in that order, so the attribution is either moved
    aside or the reopen does not happen — there is no ordering in which an answer is blanked. The
    archive is a no-op for every other case: a first ask, a retry by the owning run, a reopen of an
    expired or cancelled cycle.

    **Returns nothing, and the verdict it used to return is deleted** — see the comment above
    `_ARCHIVE_ANSWER` for why no input can refuse an open any more.
    """
    async with _connect() as conn:
        await conn.execute(_ARCHIVE_ANSWER, (request_id, run_id))
        await conn.execute(
            _OPEN,
            (
                request_id,
                kind,
                subject,
                rationale,
                asked_of,
                requested_by,
                session_id,
                correlation_id,
                list(premise_note_ids or []),
                due_at,
                run_id,
            ),
        )


async def settle_request(
    request_id: str, *, state: str, answered_by: str, answer: dict[str, Any]
) -> bool:
    """Move a waiting request to `answered`, `expired` or `cancelled`.

    Returns whether this call was the one that settled it. `False` means somebody else got there
    first, which is not an error: an expiry racing a person's click is the ordinary case, and the
    guard is what makes the first writer win rather than the last.
    """
    async with _connect() as conn:
        cursor = await conn.execute(
            _SETTLE, (state, state, answered_by, json.dumps(answer), request_id)
        )
        return cursor.rowcount == 1


# **Guarded on the run, not only on the state.** `_OPEN` reopens a request id under a new run and
# rewrites `run_id`, so a sweep that read an old run's row and then found that run dead must not
# settle the *new* run's question. The run it examined is the only one it may speak for.
_SETTLE_ORPHAN = """
    UPDATE pending_requests
    SET state = 'cancelled', answered_at = NULL, answered_by = '', answer = %s
    WHERE request_id = %s AND run_id = %s AND state = 'waiting'
"""

_ORPHAN_CANDIDATES = """
    SELECT request_id, run_id FROM pending_requests
    WHERE state = 'waiting' AND created_at < now() - make_interval(secs => %s)
    ORDER BY created_at
    LIMIT %s
"""


async def waiting_rows(*, older_than_seconds: float, limit: int) -> list[tuple[str, str]]:
    """`(request_id, run_id)` of the oldest waiting rows, for the orphan sweep."""
    async with _connect() as conn:
        cursor = await conn.execute(_ORPHAN_CANDIDATES, (older_than_seconds, limit))
        return [(str(row[0]), str(row[1] or "")) for row in await cursor.fetchall()]


async def settle_orphan(request_id: str, run_id: str, reason: str) -> bool:
    """Cancel a waiting row whose run is gone, if that run still owns it (`_SETTLE_ORPHAN`)."""
    async with _connect() as conn:
        cursor = await conn.execute(
            _SETTLE_ORPHAN, (json.dumps({"reason": reason}), request_id, run_id)
        )
        return cursor.rowcount == 1


async def record_reminder(request_id: str, count: int) -> None:
    """Record how many escalations a still-open request has had — see `_REMIND` for why a total.

    Args:
        request_id: The wait being chased.
        count: The asking workflow's own running escalation count, which is replay-stable.
    """
    async with _connect() as conn:
        await conn.execute(_REMIND, (count, request_id))


async def get_request(request_id: str) -> PendingRequest | None:
    """One request by id, whatever state it is in.

    Raises:
        pydantic.ValidationError: `_COLUMNS` and `PendingRequest` have stopped describing the same
            row. `answer` and `premise_note_ids` are `NOT NULL DEFAULT` in the table (076), so the
            `or {}` / `or []` the positional builder carried had no reachable cause and is not
            restated here; a NULL in either would be a schema this code should refuse rather than
            paper over.
    """
    async with _connect() as conn:
        async with conn.cursor(row_factory=class_row(PendingRequest)) as cur:
            await cur.execute(
                f"SELECT {_COLUMNS} FROM pending_requests WHERE request_id = %s", (request_id,)
            )
            return await cur.fetchone()


async def open_requests(
    *, asked_of: str = "", identities: Sequence[str] = (), limit: int = 50
) -> OpenRequests:
    """One page of what is still waiting, soonest deadline first, **and how many there are**.

    `asked_of` narrows to what is routed to one actor **or to nobody in particular**: an unrouted
    request is waiting on whoever is entitled, so hiding it from a named query would make the
    common case invisible. Routing is advisory either way — the answer route is the control.

    `identities` is the rest of the caller's routing surface — their user principal name and the
    entitlements they hold — because **routing to a team is the case this was built for and was the
    one it could not answer.** `request_external_input` documents `asked_of` as "an actor id or a
    team entitlement", and `_may_answer` honours both, but this read matched the object id alone: a
    request routed to `qc-team` was answerable by the QC team and appeared in **nobody's** inbox, so
    it sat invisible until it expired. Passing only `asked_of` keeps the old behaviour for callers
    that have no role set to offer.

    **The count is read in the same transaction as the page**, not because two statements would be
    slow but because they would disagree: this table is written by expiry timers and by a browser,
    so "20 of 35" assembled from two connections can report a total smaller than the page it
    describes. See `OpenRequests` for why a page that cannot say it is a page is the defect.
    """
    where = "WHERE state = 'waiting'"
    params: list[Any] = []
    routes = [route for route in (asked_of, *identities) if route]
    if routes:
        where += " AND (asked_of = ANY(%s) OR asked_of = '')"
        params.append(routes)
    page = max(1, min(limit, _MAX_PAGE))
    async with _connect() as conn:
        # Two cursors on one connection, which is still one transaction — a row factory is a
        # property of the cursor, and the count is a bare scalar rather than a `PendingRequest`.
        # The docstring's "same transaction" claim is about the connection and is unaffected.
        async with conn.cursor(row_factory=class_row(PendingRequest)) as cur:
            await cur.execute(
                f"SELECT {_COLUMNS} FROM pending_requests {where} ORDER BY due_at LIMIT %s",
                (*params, page),
            )
            rows = await cur.fetchall()
        async with conn.cursor() as counter:
            await counter.execute(f"SELECT count(*) FROM pending_requests {where}", tuple(params))
            counted = await counter.fetchone()
    return OpenRequests(
        requests=rows,
        total_waiting=int(counted[0]) if counted else len(rows),
        limit_applied=page,
    )
