"""Postgres backing for the note-proposal record (`infra/sql/027_note_proposals.sql`).

Kept separate from `chemclaw.kg.proposal` for the reason `chemclaw.durable.job_record_store` is
kept separate from `job_record`: the module the PR-gate imports carries no database dependency, so
a deployment, a test or a connector worker that runs without Postgres never pulls psycopg for a
store it will not use.

Writes are an **upsert on `(note_id, content_hash)`**, so a re-proposal of a byte-identical note
touches the existing row rather than appending — matching `GitNoteSubmitter`, which pushes nothing
when there is no diff. A *changed* note is a new version and appends, leaving any decision already
recorded against the earlier version standing: overwriting a rejection with a fresh `open` row
would erase the one thing this table exists to keep. The exceptions are the two states that are not
decisions at all — `failed` (git was never reached) and `superseded` (a newer version took the
queue slot) — either of which a re-proposal of the same bytes reopens, because in both cases the
branch a reviewer would look at now really holds those bytes (see `_UPSERT`).
"""

import json
from contextlib import AbstractAsyncContextManager

import psycopg
from psycopg.rows import DictRow, TupleRow, dict_row

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.kg.proposal import NoteProposal, ProposalState
from chemclaw.kg.submission import NoteFile

_COLUMNS = (
    "id, note_id, note_type, content_hash, content, dependencies, branch, reference, actor, "
    "session_id, correlation_id, state, submitted_at, decided_at, decided_by, reason"
)

# The mutable columns of an unchanged re-proposal: a fresh reference (the submitter may have
# returned a different one), refreshed provenance (a second chemist proposing the same note is who
# the row should now name), and a bumped `submitted_at` so the review queue orders by the most
# recent ask.
#
# `state` moves in exactly one direction: out of the two states that are **not decisions**. A
# decision is never touched — a note re-proposed unchanged after a rejection must not silently
# reopen itself, or the gate is defeatable by re-asking.
#
# `failed` is not a decision (see `ProposalState`): it says the submission never reached git, and
# the retry that finally lands pushes byte-identical content, so it collapses onto the same row.
# Leaving that row `failed` made the record assert the opposite of what happened: the branch sits
# awaiting review while `state='open'` queries skip it, `POST /proposals/{id}/decision` answers 409,
# and the merge webhook's `mark_merged` moves nothing.
#
# `superseded` is not a decision either, by the same enum's own words — it says a newer version took
# the queue slot, not that a human judged the old bytes — and it was left out of this arm, which
# made the *identical* failure reachable by an ordinary path. An agent that regenerates an earlier
# form (a miner re-running, a chemist re-asking) re-proposes v1 after v2: the row refreshes while
# staying `superseded`, and `_SUPERSEDE_OLDER` below then closes v2 because the incoming state is
# `open`. Every row superseded, nothing in the review queue, and a branch really holding v1's bytes
# that no merge can ever close. Reopening it is what makes the record match the branch, and the
# newer version yielding its slot is the same one-open-row-per-note rule stated the other way.
#
# `reason` follows `state` so a reopened row does not keep explaining itself with a git error, or a
# supersession, that no longer applies.
#
# Both `CASE`s read `note_proposals.*`, the row as it was *before* this statement — SET expressions
# are evaluated against the old row — so the two stay consistent however they are ordered.
_UPSERT = """
    INSERT INTO note_proposals
        (note_id, note_type, content_hash, content, dependencies, branch, reference, actor,
         session_id, correlation_id, state, reason)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (note_id, content_hash) DO UPDATE SET
        reference = EXCLUDED.reference,
        actor = EXCLUDED.actor,
        session_id = EXCLUDED.session_id,
        correlation_id = EXCLUDED.correlation_id,
        dependencies = EXCLUDED.dependencies,
        submitted_at = now(),
        state = CASE WHEN note_proposals.state IN ('failed', 'superseded')
                     THEN EXCLUDED.state ELSE note_proposals.state END,
        reason = CASE WHEN note_proposals.state IN ('failed', 'superseded')
                      THEN EXCLUDED.reason ELSE note_proposals.reason END
    RETURNING id, state
"""

# Both filters are self-disabling through the `%s = ''` arm, so "any state, any proposer" needs no
# second statement — the shape `job_record_store._SEARCH` established. `before_id` is the cursor:
# ids are monotonic from a `BIGSERIAL`, so "older than the last row I saw" is one comparison and
# cannot skip or repeat a row the way an offset can when rows are inserted mid-page.
_SELECT_MANY = f"""
    SELECT {_COLUMNS} FROM note_proposals
    WHERE (%s = '' OR state = %s)
      AND (%s = '' OR actor = %s)
      AND (%s = 0 OR id < %s)
    ORDER BY id DESC
    LIMIT %s
"""

_SELECT_ONE = f"SELECT {_COLUMNS} FROM note_proposals WHERE id = %s"
# The decision standing against one exact version, asked before a submission reaches git. Ordered
# and limited because `(note_id, content_hash)` is not unique across decided rows in every history
# this table can hold; newest wins, which is the decision that is actually standing.
_SELECT_DECIDED_VERSION = f"""
SELECT {_COLUMNS} FROM note_proposals
WHERE note_id = %s AND content_hash = %s AND state IN ('merged', 'rejected')
ORDER BY id DESC LIMIT 1
"""

# A freshly-upserted version closes the note's previous open versions (migration 058 says why).
# Scoped by id rather than content hash so the statement is correct on the refresh path too — the
# row the upsert just returned must never supersede itself.
_SUPERSEDE_OLDER = """
    UPDATE note_proposals
       SET state = 'superseded',
           reason = 'superseded by a newer proposed version of this note'
     WHERE note_id = %s AND state = 'open' AND id <> %s
"""

# `state = 'open'` in the predicate is the concurrency control, not a courtesy: two reviewers
# deciding at once means the second `UPDATE` matches no row and returns nothing, so the caller
# learns the decision was already taken instead of overwriting it.
_DECIDE = f"""
    UPDATE note_proposals
       SET state = %s, decided_at = now(), decided_by = %s, reason = %s
     WHERE id = %s AND state = 'open'
    RETURNING {_COLUMNS}
"""

# The webhook's half. Scoped to open rows so a duplicate delivery — which webhooks routinely are —
# is a no-op rather than a second decision that restamps `decided_at`.
_MARK_MERGED = """
    UPDATE note_proposals
       SET state = 'merged', decided_at = now(), decided_by = %s
     WHERE note_id = ANY(%s) AND state = 'open'
"""


def _connect() -> AbstractAsyncContextManager[psycopg.AsyncConnection[TupleRow]]:
    """The configured connection, with the shared statement timeout (one place, DRY)."""
    return db.connection(settings.postgres_dsn)


def _rows(conn: psycopg.AsyncConnection[TupleRow]) -> psycopg.AsyncCursor[DictRow]:
    """A cursor whose rows are keyed by column name, for the length of one statement.

    Scoped to the cursor rather than set on the connection, which would follow it back into the
    shared pool and change what the next borrower reads.

    Worth the extra line because `_proposal` used to index `row[0]..row[14]` against a string
    `_COLUMNS` constant: reordering that string — or inserting a column into the middle of it,
    which the `dependencies` addition does — silently swapped every same-typed field after the
    insertion point. `_proposal`'s docstring claimed pydantic would catch that. It cannot: `branch`
    and `reference` are both `str`, and a swap between them is a valid model.
    """
    return conn.cursor(row_factory=dict_row)


def _dependencies_json(proposal: NoteProposal) -> str:
    """The supporting files as the JSONB the column stores."""
    return json.dumps([file.model_dump() for file in proposal.dependencies])


def _proposal(row: DictRow) -> NoteProposal:
    """Build a `NoteProposal` from a row keyed by column name."""
    return NoteProposal(
        id=row["id"],
        note_id=row["note_id"],
        note_type=row["note_type"],
        content=row["content"],
        dependencies=tuple(NoteFile(**file) for file in row["dependencies"]),
        branch=row["branch"],
        reference=row["reference"],
        actor=row["actor"],
        session_id=row["session_id"],
        correlation_id=row["correlation_id"],
        state=ProposalState(row["state"]),
        submitted_at=row["submitted_at"],
        decided_at=row["decided_at"],
        decided_by=row["decided_by"],
        reason=row["reason"],
    )


class PostgresProposalStore:
    """The durable `ProposalStore`: one short-lived connection per call (KISS, the house choice)."""

    async def upsert(self, proposal: NoteProposal) -> int:
        """Insert the proposal (or refresh an unchanged re-proposal); return the row id."""
        async with _connect() as conn:
            cursor = _rows(conn)
            await cursor.execute(
                _UPSERT,
                (
                    proposal.note_id,
                    proposal.note_type,
                    proposal.content_hash,
                    proposal.content,
                    _dependencies_json(proposal),
                    proposal.branch,
                    proposal.reference,
                    proposal.actor,
                    proposal.session_id,
                    proposal.correlation_id,
                    proposal.state.value,
                    proposal.reason,
                ),
            )
            row = await cursor.fetchone()
            # Only a version this statement actually left **open** closes its predecessors — a
            # `failed` record must not push an older, genuinely reviewable version out of the
            # queue.
            #
            # The guard reads the *resulting* state rather than the requested one, and the
            # difference is a measured defect: re-proposing the bytes of a *rejected* version
            # arrives with `state='open'`, the `CASE` above correctly refuses to reopen it — and
            # this then closed the note's live open version anyway, leaving the note with a
            # rejected row, a superseded row and nothing awaiting review. `RETURNING state` is what
            # makes the two halves agree about what just happened, and it is also what makes the
            # reopened-`superseded` path sweep without a second condition to keep in step.
            if row is not None and row["state"] == ProposalState.OPEN.value:
                await cursor.execute(_SUPERSEDE_OLDER, (proposal.note_id, int(row["id"])))
            await conn.commit()
        return int(row["id"]) if row is not None else 0

    async def read(self, proposal_id: int) -> NoteProposal | None:
        """One proposal in full, or None when there is no such row."""
        async with _connect() as conn:
            cursor = _rows(conn)
            await cursor.execute(_SELECT_ONE, (proposal_id,))
            row = await cursor.fetchone()
        return _proposal(row) if row is not None else None

    async def listing(
        self, state: ProposalState | None, actor: str, limit: int, before_id: int | None
    ) -> list[NoteProposal]:
        """Proposals newest-first, filtered by state and proposer, paged by `before_id`."""
        wanted = state.value if state is not None else ""
        cursor_id = before_id or 0
        async with _connect() as conn:
            cursor = _rows(conn)
            await cursor.execute(
                _SELECT_MANY, (wanted, wanted, actor, actor, cursor_id, cursor_id, limit)
            )
            rows = await cursor.fetchall()
        return [_proposal(row) for row in rows]

    async def decide(
        self, proposal_id: int, state: ProposalState, decided_by: str, reason: str
    ) -> NoteProposal | None:
        """Record a decision on an open proposal; None when absent or already decided."""
        async with _connect() as conn:
            cursor = _rows(conn)
            await cursor.execute(_DECIDE, (state.value, decided_by, reason, proposal_id))
            row = await cursor.fetchone()
            await conn.commit()
        return _proposal(row) if row is not None else None

    async def mark_merged(self, note_ids: list[str], decided_by: str) -> int:
        """Close every open proposal for the named notes as merged; return how many moved."""
        async with _connect() as conn:
            cursor = await conn.execute(_MARK_MERGED, (decided_by, note_ids))
            moved = cursor.rowcount
            await conn.commit()
        return int(moved)

    async def decided_version(self, note_id: str, content_hash: str) -> NoteProposal | None:
        """The decision standing against these exact bytes, newest first, or None."""
        async with _connect() as conn:
            cursor = _rows(conn)
            await cursor.execute(_SELECT_DECIDED_VERSION, (note_id, content_hash))
            row = await cursor.fetchone()
        return _proposal(row) if row is not None else None
