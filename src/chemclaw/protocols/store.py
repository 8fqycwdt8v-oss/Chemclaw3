"""The revision history of every design — append-only, because an edit is the evidence.

Three tables and one rule. The header row is a mutable projection — its status, head revision and
counts move with the head, and the grant gives UPDATE on it alone. **The two beneath it are
append-only:** a change is a new row naming the row it came from, and a sign-off is a new row
naming the revision it was made on. That is what makes an expert's alteration of the first shot
observable at all, and it is what makes a concurrent edit a refusal instead of a silent overwrite —
`parent_revision` is compared against the head, so two people editing one protocol produce a
`RevisionConflict` rather than one of them losing their work without being told.

The third table is `experiment_protocol_status_events`, and it exists because the header's `status`
describes the **head**: `advanced()` retires an `approved` or `executed` status the moment a new
revision lands, correctly, and that leaves nowhere on the header row to say *which document* a
chemist signed off on. Only a deliberate move is recorded — an automatic demotion has no actor and
no reason, and the revision that caused it is already in the history.

Shaped as `ingest.eln.records` is, and for the same reason: a Protocol with an in-memory and a
Postgres implementation, so the drafting path is testable with no database while the store that
actually serves the front door is exercised against a real one.

**A design is data, not a knowledge claim, so it is a row rather than a PR-gated note.** The gate
answers "is this true"; a draft is a proposal to act and nothing about it is true yet. That is
`D-2026-08-25-an-eln-transcription-is-data-not-a-claim` arriving from the opposite side — the
transcription is ungated because there is nothing to decide, and a draft is ungated because the
decision is *running it*, which happens in a laboratory and not in a review queue. A chemist who
wants a rule out of an approved design still proposes a `playbook` or an `experiment-proposal` note
citing it, through the gate that has always been there.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.protocols.models import (
    AuthorKind,
    DesignRevision,
    DesignStatus,
    DesignSummary,
    ExperimentDesign,
    ExperimentRequest,
    ProtocolCheck,
    StatusEvent,
)

logger = logging.getLogger(__name__)

_REVISION_COLUMNS = (
    "revision, kind, author_kind, author, parent_revision, change_note, "
    "document, checks, created_at"
)

#: The same row without the document, for `history()`. A revision's `document` is kilobytes of
#: JSONB and a pydantic validation each; the history list renders seven scalars and a blocker
#: count, and touches none of it.
_HEADER_COLUMNS = (
    "revision, kind, author_kind, author, parent_revision, change_note, checks, created_at"
)

_UPSERT_DESIGN = """
INSERT INTO experiment_protocols
    (design_id, title, mode, status, project, opened_by, session_id, correlation_id,
     head_revision, arm_count, blocker_count, created_at, updated_at)
VALUES
    (%(design_id)s, %(title)s, %(mode)s, %(status)s, %(project)s, %(opened_by)s, %(session_id)s,
     %(correlation_id)s, %(revision)s, %(arm_count)s, %(blocker_count)s, now(), now())
ON CONFLICT (design_id) DO UPDATE SET
    title = EXCLUDED.title,
    mode = EXCLUDED.mode,
    -- Safe to take from the insert because the caller computed it from *this* row a moment ago,
    -- inside the same transaction, through `advanced()` — and under `FOR UPDATE`, which is what
    -- makes "a moment ago" mean anything. Without the lock a concurrent `set_status` committing in
    -- that window was overwritten by this transaction's stale value, 20 times out of 20.
    --
    -- `advanced()` makes **five** self-transitions, not the one this comment used to name:
    -- `requested` becoming `draft`, and the four demotions that retire an `approved` or `executed`
    -- status when any revision lands. Those ride through this very line, so a reader told the
    -- upsert can only promote was being told the opposite of what it does.
    status = EXCLUDED.status,
    project = EXCLUDED.project,
    head_revision = EXCLUDED.head_revision,
    arm_count = EXCLUDED.arm_count,
    blocker_count = EXCLUDED.blocker_count,
    updated_at = now()
"""

_INSERT_REVISION = """
INSERT INTO experiment_protocol_revisions
    (design_id, revision, kind, author_kind, author, parent_revision, change_note, document,
     checks, created_at)
VALUES
    (%(design_id)s, %(revision)s, %(kind)s, %(author_kind)s, %(author)s, %(parent_revision)s,
     %(change_note)s, %(document)s, %(checks)s, now())
"""

# **`FOR UPDATE`, and it is the difference between holding a chemist's decision and losing it.**
# `append` reads the status here, recomputes it through `advanced()` and writes it back through
# `_UPSERT_DESIGN`. `core/db.py` is READ COMMITTED, so without the row lock a `set_status` that
# commits between the two is overwritten by this transaction's stale value. Measured before the
# lock: a chemist abandoning a design while an agent appended a revision lost the abandonment
# **20 times out of 20**, leaving a header reading `draft` — against `advanced()`'s own promise
# that `abandoned` is held because "a design somebody decided not to run does not come back
# because an agent wrote to it".
#
# The lock is taken on the design's own header row, which every writer of that design already
# contends for, so it serialises exactly the writers that must not interleave and nothing else.
_SELECT_HEAD = (
    "SELECT head_revision, status FROM experiment_protocols WHERE design_id = %s FOR UPDATE"
)

# `RETURNING head_revision` rather than a second SELECT: the revision the event is stamped with has
# to be the one the status was set against, and reading it separately leaves a window in which an
# append moves the head between the two statements.
_SET_STATUS = """
UPDATE experiment_protocols SET status = %(status)s, updated_at = now()
WHERE design_id = %(design_id)s
RETURNING head_revision
"""

_INSERT_STATUS_EVENT = """
INSERT INTO experiment_protocol_status_events
    (design_id, revision, status, actor, reason, created_at)
VALUES
    (%(design_id)s, %(revision)s, %(status)s, %(actor)s, %(reason)s, now())
"""

_SELECT_STATUS_EVENTS = """
SELECT status, revision, actor, reason, created_at
FROM experiment_protocol_status_events
WHERE design_id = %s
ORDER BY id DESC
"""

_SELECT_SUMMARY = """
SELECT design_id, title, mode, status, project, opened_by, head_revision, arm_count,
       blocker_count, created_at, updated_at
FROM experiment_protocols
"""


class RevisionConflict(ChemclawError):
    """A write whose `parent_revision` is not the design's head — somebody else edited it first."""


class UnknownDesign(ChemclawError):
    """A design id nothing in the store answers to."""


class UnstorableDocument(ChemclawError):
    """A design carrying bytes no text column can hold — a NUL, or a C0 control character.

    Postgres `text` and `jsonb` reject `\u0000` outright, and psycopg raises it as an untyped
    `DataError`/`UntranslatableCharacter` from inside the driver. Measured before this existed: a
    NUL anywhere in a browser-supplied design — the notes field, the title, the change note — was a
    **500** with a correlation id and nothing a caller could act on, while the in-memory backend
    accepted it, so the two backends disagreed about whether the write was possible.

    Refused here rather than sanitised, because a chemist did not type a NUL: silently stripping it
    would store a document that is not the one that was sent. `ingest.eln.sync` strips control
    characters on the *ingest* path for the opposite reason — there the bytes come from somebody
    else's database and there is no author to refuse.
    """


@runtime_checkable
class DesignStore(Protocol):
    """Where designs and their revisions live."""

    async def append(
        self,
        design_id: str,
        design: ExperimentDesign,
        checks: Sequence[ProtocolCheck],
        *,
        kind: str,
        author_kind: AuthorKind,
        author: str = "",
        parent_revision: int = 0,
        change_note: str = "",
        session_id: str = "",
        correlation_id: str = "",
        status: DesignStatus = "draft",
    ) -> DesignRevision:
        """Store the next revision, refusing when `parent_revision` is not the head."""
        ...

    async def read(self, design_id: str, revision: int | None = None) -> DesignRevision | None:
        """One revision — the head when `revision` is `None` — or `None` if unknown."""
        ...

    async def summary(self, design_id: str) -> DesignSummary | None:
        """The design's header row — its status, head revision and counts — or `None`."""
        ...

    async def history(self, design_id: str) -> list[DesignRevision]:
        """Every revision, oldest first."""
        ...

    async def listing(
        self,
        *,
        status: DesignStatus | None = None,
        project: str = "",
        session_id: str = "",
        limit: int = 50,
    ) -> list[DesignSummary]:
        """Designs, newest first."""
        ...

    async def set_status(
        self, design_id: str, status: DesignStatus, actor: str = "", reason: str = ""
    ) -> None:
        """Move a design's lifecycle status, recording who moved it, why, and from which revision.

        Raises:
            UnknownDesign: nothing in the store answers to `design_id`.
        """
        ...

    async def status_history(self, design_id: str) -> list[StatusEvent]:
        """Every recorded lifecycle move, newest first."""
        ...


class InMemoryDesignStore:
    """A real backend, not a test double — the one a deployment without Postgres runs on."""

    def __init__(self) -> None:
        """Start empty; process-lifetime, because a store that forgets between calls is not one."""
        self._revisions: dict[str, list[DesignRevision]] = {}
        self._meta: dict[str, dict[str, Any]] = {}
        self._status_events: dict[str, list[StatusEvent]] = {}

    async def append(
        self,
        design_id: str,
        design: ExperimentDesign,
        checks: Sequence[ProtocolCheck],
        *,
        kind: str,
        author_kind: AuthorKind,
        author: str = "",
        parent_revision: int = 0,
        change_note: str = "",
        session_id: str = "",
        correlation_id: str = "",
        status: DesignStatus = "draft",
    ) -> DesignRevision:
        """Store the next revision, refusing when `parent_revision` is not the head."""
        require_storable(design, change_note)
        existing = self._revisions.get(design_id, [])
        head = existing[-1].revision if existing else 0
        _require_head(design_id, head, parent_revision)
        revision = DesignRevision(
            design_id=design_id,
            revision=head + 1,
            kind=kind,  # type: ignore[arg-type]
            author_kind=author_kind,
            author=author,
            parent_revision=head,
            change_note=change_note,
            design=design,
            checks=list(checks),
        )
        self._revisions.setdefault(design_id, []).append(revision)
        meta = self._meta.setdefault(
            design_id,
            {
                "status": status,
                "created_at": revision.created_at,
                "opened_by": author,
                "session_id": session_id,
                "correlation_id": correlation_id,
            },
        )
        # `session_id`, `correlation_id` and `opened_by` are set once, by the write that created
        # the design, and are deliberately absent from this update — which is what `_UPSERT_DESIGN`
        # does on the Postgres side by omitting them from its `DO UPDATE SET`. They disagreed
        # before: this store overwrote `session_id` on every append while Postgres kept the
        # creator's, so `listing(session_id=…)` returned different designs on the two backends.
        # That is not a difference a store is allowed to have — this one is "a real backend, not a
        # test double", so an answer that depends on which is configured is a wrong answer on one
        # of them.
        meta.update(
            {
                "title": design.request.title,
                "mode": design.request.mode,
                "project": design.request.project,
                "status": advanced(meta["status"], kind),
                "updated_at": revision.created_at,
                "arm_count": len(design.arms),
                "blocker_count": len(revision.blockers),
            }
        )
        return revision

    async def read(self, design_id: str, revision: int | None = None) -> DesignRevision | None:
        """One revision — the head when `revision` is `None` — or `None` if unknown."""
        rows = self._revisions.get(design_id, [])
        if not rows:
            return None
        if revision is None:
            return rows[-1]
        return next((row for row in rows if row.revision == revision), None)

    async def summary(self, design_id: str) -> DesignSummary | None:
        """The design's header row — its status, head revision and counts — or `None`."""
        meta = self._meta.get(design_id)
        if meta is None:
            return None
        return DesignSummary(
            design_id=design_id,
            title=str(meta.get("title", "")),
            mode=meta["mode"],
            status=meta["status"],
            project=str(meta.get("project", "")),
            opened_by=str(meta.get("opened_by", "")),
            head_revision=self._revisions[design_id][-1].revision,
            arms=int(meta.get("arm_count", 0)),
            blockers=int(meta.get("blocker_count", 0)),
            created_at=meta["created_at"],
            updated_at=meta["updated_at"],
        )

    async def history(self, design_id: str) -> list[DesignRevision]:
        """Every revision, oldest first."""
        return list(self._revisions.get(design_id, []))

    async def listing(
        self,
        *,
        status: DesignStatus | None = None,
        project: str = "",
        session_id: str = "",
        limit: int = 50,
    ) -> list[DesignSummary]:
        """Designs, newest first."""
        # Clamped exactly as Postgres clamps it. `[:limit]` and `max(1, min(limit, 500))` disagree
        # on `limit=0` (memory returns nothing, Postgres one row) and on a negative (memory returns
        # all but the last, Postgres one row) — a divergence in a `Protocol` method whose two
        # implementations are documented as interchangeable.
        bounded = max(1, min(limit, 500))
        summaries = [
            DesignSummary(
                design_id=design_id,
                title=str(meta.get("title", "")),
                mode=meta["mode"],
                status=meta["status"],
                project=str(meta.get("project", "")),
                opened_by=str(meta.get("opened_by", "")),
                head_revision=self._revisions[design_id][-1].revision,
                arms=int(meta.get("arm_count", 0)),
                blockers=int(meta.get("blocker_count", 0)),
                created_at=meta["created_at"],
                updated_at=meta["updated_at"],
            )
            for design_id, meta in self._meta.items()
            if (status is None or meta["status"] == status)
            and (not project or meta.get("project") == project)
            and (not session_id or meta.get("session_id") == session_id)
        ]
        return sorted(summaries, key=lambda s: s.updated_at, reverse=True)[:bounded]

    async def set_status(
        self, design_id: str, status: DesignStatus, actor: str = "", reason: str = ""
    ) -> None:
        """Move a design's lifecycle status, recording the move against the head revision."""
        if design_id not in self._meta:
            raise UnknownDesign(f"no design {design_id!r}")
        self._meta[design_id]["status"] = status
        self._meta[design_id]["updated_at"] = datetime.now(UTC)
        self._status_events.setdefault(design_id, []).append(
            StatusEvent(
                status=status,
                revision=self._revisions[design_id][-1].revision,
                actor=actor,
                reason=reason,
            )
        )

    async def status_history(self, design_id: str) -> list[StatusEvent]:
        """Every recorded lifecycle move, newest first."""
        return list(reversed(self._status_events.get(design_id, [])))


class PostgresDesignStore:
    """The durable store — `experiment_protocols` plus its append-only revision table."""

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[psycopg.AsyncConnection[TupleRow]]:
        """Borrow a connection with the configured per-statement timeout."""
        async with db.connection(settings.postgres_dsn) as conn:
            yield conn

    async def append(
        self,
        design_id: str,
        design: ExperimentDesign,
        checks: Sequence[ProtocolCheck],
        *,
        kind: str,
        author_kind: AuthorKind,
        author: str = "",
        parent_revision: int = 0,
        change_note: str = "",
        session_id: str = "",
        correlation_id: str = "",
        status: DesignStatus = "draft",
    ) -> DesignRevision:
        """Store the next revision, refusing when `parent_revision` is not the head.

        **Two writers *can* both read the same head, and the primary key is what actually decides
        between them.** The first version of this docstring claimed the transaction prevented it;
        `core.db`'s connections are READ COMMITTED, so both readers see `head=1` and both build
        revision 2 — measured against a real database, with no artificial barrier. What stopped the
        second was `(design_id, revision)`, and it surfaced as a raw
        `psycopg.errors.UniqueViolation` that nothing translated: the second chemist in "two
        chemists editing one plate is the ordinary case" got a **500 with no `revision_conflict`
        code**, which is precisely the case the 409 was built for.

        So the violation is caught and re-raised as the same `RevisionConflict` a stale
        `parent_revision` raises. The two are the same fact reaching the writer by different routes
        — the revision you built on is not the head any more — and a caller that had to tell them
        apart would be a caller with two ways to do one thing.
        """
        require_storable(design, change_note)
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT_HEAD, (design_id,))
                row = await cur.fetchone()
                head = int(row[0]) if row else 0
                # `advanced()` on the create too, which is what the in-memory backend has always
                # done. The two disagreed on a design's *first* revision — memory applied the
                # transition, Postgres did not — and they agreed by accident only because the one
                # creator in `src/` passes `kind="request", status="requested"`, for which the
                # transition is the identity. A second creator would have made the header's status
                # depend on which backend a deployment configured.
                current_status: DesignStatus = advanced(row[1] if row else status, kind)
                _require_head(design_id, head, parent_revision)
                revision = DesignRevision(
                    design_id=design_id,
                    revision=head + 1,
                    kind=kind,  # type: ignore[arg-type]
                    author_kind=author_kind,
                    author=author,
                    parent_revision=head,
                    change_note=change_note,
                    design=design,
                    checks=list(checks),
                )
                await cur.execute(
                    _UPSERT_DESIGN,
                    {
                        "design_id": design_id,
                        "title": design.request.title,
                        "mode": design.request.mode,
                        "status": current_status,
                        "project": design.request.project,
                        "opened_by": author,
                        "session_id": session_id,
                        "correlation_id": correlation_id,
                        "revision": revision.revision,
                        "arm_count": len(design.arms),
                        "blocker_count": len(revision.blockers),
                    },
                )
                try:
                    await cur.execute(
                        _INSERT_REVISION,
                        {
                            "design_id": design_id,
                            "revision": revision.revision,
                            "kind": kind,
                            "author_kind": author_kind,
                            "author": author,
                            "parent_revision": head,
                            "change_note": change_note,
                            "document": Jsonb(design.model_dump(mode="json")),
                            "checks": Jsonb([c.model_dump() for c in checks]),
                        },
                    )
                except psycopg.errors.UniqueViolation as exc:
                    raise RevisionConflict(
                        f"{design_id} gained revision {revision.revision} while this write was "
                        "being prepared. Re-read the design and apply the change to the current "
                        "revision."
                    ) from exc
            await conn.commit()
        return revision

    async def read(self, design_id: str, revision: int | None = None) -> DesignRevision | None:
        """One revision — the head when `revision` is `None` — or `None` if unknown."""
        statement = (
            f"SELECT {_REVISION_COLUMNS} FROM experiment_protocol_revisions "
            "WHERE design_id = %(design_id)s "
            + ("AND revision = %(revision)s " if revision is not None else "")
            + "ORDER BY revision DESC LIMIT 1"
        )
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(statement, {"design_id": design_id, "revision": revision})
                row = await cur.fetchone()
        return _revision(design_id, row) if row else None

    async def summary(self, design_id: str) -> DesignSummary | None:
        """The design's header row — its status, head revision and counts — or `None`."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(f"{_SELECT_SUMMARY} WHERE design_id = %s", (design_id,))
                row = await cur.fetchone()
        return _summary(row) if row else None

    async def history(self, design_id: str) -> list[DesignRevision]:
        """Every revision, oldest first.

        **Reads the checks and not the documents.** `GET /protocols/{id}` renders seven scalars per
        revision and `len(blockers)`; it never touches `item.design`. Selecting `document` anyway
        cost a JSONB read and an `ExperimentDesign.model_validate` per row — measured at **39x** the
        rest of the query over 40 revisions of a 24-arm plate (90.6 ms against 2.3 ms), on the
        event loop, unbounded because the table is append-only and has no revision cap.

        The `design` on each row is therefore the empty design rather than the stored one, which is
        why this returns headers and the docstring says so: `read()` is how a caller gets a
        document, and it is the only caller that needs one.
        """
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT {_HEADER_COLUMNS} FROM experiment_protocol_revisions "
                    "WHERE design_id = %s ORDER BY revision",
                    (design_id,),
                )
                rows = await cur.fetchall()
        return [_revision_header(design_id, row) for row in rows]

    async def listing(
        self,
        *,
        status: DesignStatus | None = None,
        project: str = "",
        session_id: str = "",
        limit: int = 50,
    ) -> list[DesignSummary]:
        """Designs, newest first."""
        clauses: list[str] = []
        params: dict[str, Any] = {"limit": max(1, min(limit, 500))}
        if status is not None:
            clauses.append("status = %(status)s")
            params["status"] = status
        if project:
            clauses.append("project = %(project)s")
            params["project"] = project
        if session_id:
            clauses.append("session_id = %(session_id)s")
            params["session_id"] = session_id
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"{_SELECT_SUMMARY} {where}ORDER BY updated_at DESC LIMIT %(limit)s", params
                )
                rows = await cur.fetchall()
        return [_summary(row) for row in rows]

    async def set_status(
        self, design_id: str, status: DesignStatus, actor: str = "", reason: str = ""
    ) -> None:
        """Move a design's lifecycle status, recording the move against the head revision.

        The event row is the whole reason this is two statements in one transaction. `advanced()`
        demotes an approved or executed design back to `draft` when a revision lands on it, so the
        header cannot say which document a person signed off on — and until this table existed
        nothing could, while `advanced()`'s own docstring said `set_status` recorded it.
        """
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SET_STATUS, {"status": status, "design_id": design_id})
                row = await cur.fetchone()
                if row is None:
                    raise UnknownDesign(f"no design {design_id!r}")
                await cur.execute(
                    _INSERT_STATUS_EVENT,
                    {
                        "design_id": design_id,
                        "revision": row[0],
                        "status": status,
                        "actor": actor,
                        "reason": reason,
                    },
                )
            await conn.commit()
        logger.info(
            "protocol.status design_id=%s status=%s revision=%s actor=%s",
            design_id,
            status,
            row[0],
            actor,
        )

    async def status_history(self, design_id: str) -> list[StatusEvent]:
        """Every recorded lifecycle move, newest first."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT_STATUS_EVENTS, (design_id,))
                rows = await cur.fetchall()
        return [
            StatusEvent(
                status=row[0], revision=row[1], actor=row[2], reason=row[3], created_at=row[4]
            )
            for row in rows
        ]


#: The statuses a new revision retires, because each is a claim about a *document* rather than
#: about the design: somebody approved these conditions, or somebody ran them. A revision replaces
#: the document, so the claim no longer describes what `GET /protocols/{id}` serves.
_RETIRED_BY_A_REVISION: frozenset[DesignStatus] = frozenset({"approved", "executed"})


def advanced(current: DesignStatus, kind: str) -> DesignStatus:
    """The status a design has after a revision of `kind` lands on it.

    A design that held only a structured ask becomes a `draft` the moment a protocol revision
    arrives — that transition was always here.

    **An `approved` or `executed` design becomes a `draft` again when a new revision lands**,
    because both are statements about a *document* and the document has changed. The first version
    held `approved`, reasoning that a re-draft must not silently un-approve — true of the word and
    false of the thing: measured, a chemist approving revision 1 at 80 °C and an agent then drafting
    revision 2 at 200 °C left a header reading `approved` over a protocol nobody had read, and every
    default read serves the head. `executed` was left behind in that fix and is the same sentence
    one word along: a header saying a design was run, over a document that was not.

    **What makes the demotion affordable is `experiment_protocol_status_events`**, and it is worth
    saying plainly that this docstring used to claim a record that did not exist — "`set_status`
    records it" was written above a `set_status` that wrote one column on the header row and logged
    a line without the revision in it. It records it now: which revision, by whom, and why.

    `abandoned` is deliberately not in that set and is held: a design somebody decided not to run
    does not come back because an agent wrote to it, and the way back is a person's `set_status`.
    """
    if current == "requested" and kind == "protocol":
        return "draft"
    return "draft" if current in _RETIRED_BY_A_REVISION else current


#: The characters no Postgres `text` or `jsonb` column can hold. NUL is the one that actually
#: arrives (it is what a truncated UTF-16 read or a fuzzing client produces); the rest of the C0
#: range is refused with it because none of them belongs in a laboratory procedure and a document
#: carrying one is not a document somebody typed.
_UNSTORABLE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def require_storable(design: ExperimentDesign, change_note: str) -> None:
    """Refuse a document Postgres cannot hold, in this process, naming what is wrong.

    Both backends call it, which is the point: without it the in-memory store accepted a NUL and
    Postgres answered **500**, so whether a write was possible depended on which backend a
    deployment had configured — the divergence `InMemoryDesignStore`'s own docstring forbids.

    Raises:
        UnstorableDocument: the design or its change note carries a NUL or a C0 control character.
    """
    # The **values**, not the JSON encoding of them: `model_dump_json` escapes a NUL as the six
    # characters `\u0000`, so a regex over the serialised form finds nothing and the guard passes
    # exactly the input it exists to refuse. Measured — the first version of this function did.
    for label, text in _strings(design.model_dump(), "the document"):
        if _UNSTORABLE.search(text):
            raise UnstorableDocument(
                f"{label} contains a control character no text column can store (NUL or C0). "
                "Remove it and send the document again."
            )
    if _UNSTORABLE.search(change_note):
        raise UnstorableDocument(
            "change_note contains a control character no text column can store (NUL or C0). "
            "Remove it and send the document again."
        )


def _strings(value: Any, path: str) -> Iterator[tuple[str, str]]:
    """Every string in a dumped model, with the path that reaches it."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{path}[{index}]")


def _require_head(design_id: str, head: int, parent_revision: int) -> None:
    """Refuse a write derived from anything but the current head.

    `parent_revision=0` means "I am creating this design" and is refused once it exists — an edit
    that forgot to name its parent is exactly the write that would silently discard somebody's
    revision, so it is not treated as a shortcut for "the head, whatever it is".
    """
    if parent_revision != head:
        raise RevisionConflict(
            f"{design_id} is at revision {head}; this write is derived from {parent_revision}. "
            "Re-read the design and apply the change to the current revision."
        )


def _summary(row: tuple[Any, ...]) -> DesignSummary:
    """Build a `DesignSummary` from a `_SELECT_SUMMARY` row."""
    return DesignSummary(
        design_id=row[0],
        title=row[1],
        mode=row[2],
        status=row[3],
        project=row[4],
        opened_by=row[5],
        head_revision=row[6],
        arms=row[7],
        blockers=row[8],
        created_at=row[9],
        updated_at=row[10],
    )


def _revision_header(design_id: str, row: tuple[Any, ...]) -> DesignRevision:
    """Build a `DesignRevision` from a `_HEADER_COLUMNS` row, with no document in it.

    `design` is the empty design rather than the stored one — see `history()`. Every reader of this
    shape wants the scalars and `blockers`; the one reader that wants a document calls `read()`.
    """
    return DesignRevision(
        design_id=design_id,
        revision=row[0],
        kind=row[1],
        author_kind=row[2],
        author=row[3],
        parent_revision=row[4],
        change_note=row[5],
        design=ExperimentDesign(request=ExperimentRequest(title="(not read)", goal="(not read)")),
        checks=[ProtocolCheck.model_validate(c) for c in row[6]],
        created_at=row[7],
    )


def _revision(design_id: str, row: tuple[Any, ...]) -> DesignRevision:
    """Build a `DesignRevision` from a `_REVISION_COLUMNS` row."""
    return DesignRevision(
        design_id=design_id,
        revision=row[0],
        kind=row[1],
        author_kind=row[2],
        author=row[3],
        parent_revision=row[4],
        change_note=row[5],
        design=ExperimentDesign.model_validate(row[6]),
        checks=[ProtocolCheck.model_validate(c) for c in row[7]],
        created_at=row[8],
    )


_IN_MEMORY = InMemoryDesignStore()


def default_design_store() -> DesignStore:
    """The store this deployment uses — Postgres where sessions are durable, memory otherwise.

    The same switch the audit sink, the job record, the checkpointer and the campaign store read.
    The in-memory instance is module-level rather than per-call: it is a real backend for a
    deployment without Postgres, and one that forgot every design between two calls would be worse
    than none at all.
    """
    if settings.session_store == "postgres":
        return PostgresDesignStore()
    return _IN_MEMORY
