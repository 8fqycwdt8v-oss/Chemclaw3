"""The revision history of every design: append-only, because an edit is the evidence.

The header row is a mutable projection (status, head revision, counts) and is the only table
granted UPDATE. Revisions and status events are append-only: a change is a new row naming its
parent, so an expert's alteration is observable, and `parent_revision` is compared against the
head so a concurrent edit is a `RevisionConflict`, never a silent overwrite.
`experiment_protocol_status_events` records which revision each deliberate status move was made
against, since the header's status describes only the head.

A Protocol with in-memory and Postgres implementations that must answer identically. A design is
data, not a knowledge claim, so it is a row rather than a note; a rule drawn from an approved
design is written as a note citing it through `kg/record.py`.
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
from pydantic import BaseModel, ConfigDict, Field

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.protocols.models import (
    AuthorKind,
    DesignRevision,
    DesignStatus,
    DesignSummary,
    ExperimentDesign,
    ProtocolCheck,
    RevisionKind,
    StatusEvent,
)

logger = logging.getLogger(__name__)

_REVISION_COLUMNS = (
    "revision, kind, author_kind, author, parent_revision, change_note, "
    "document, checks, created_at"
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

# `FOR UPDATE`: `append` reads the status, recomputes it via `advanced()` and writes it back, and
# under READ COMMITTED a concurrent `set_status` would otherwise be overwritten with a stale value.
# The lock is on the design's own header row, so it serialises exactly the writers that must not
# interleave.
_SELECT_HEAD = (
    "SELECT head_revision, status FROM experiment_protocols WHERE design_id = %s FOR UPDATE"
)

# The head moving between the read and this update is prevented by `_SELECT_HEAD`'s `FOR UPDATE`.
_SET_STATUS = """
UPDATE experiment_protocols SET status = %(status)s, updated_at = now()
WHERE design_id = %(design_id)s
"""

#: The head revision's own `kind`, read under the same row lock as the head itself — see
#: `require_movable`.
_SELECT_HEAD_KIND = (
    "SELECT kind FROM experiment_protocol_revisions WHERE design_id = %s AND revision = %s"
)

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


#: The most summaries one `listing` call serves; both backends report the clamp so a caller can
#: tell it from a site with exactly that many designs.
_MAX_LISTING = 500


class DesignIndex(BaseModel):
    """One page of the design listing, **and how many designs that page is a page of**.

    The bare `list[DesignSummary]` this replaced is the same silence `GET /sessions` was fixed for
    in the same API package — "it always bounded the answer, and nothing said so" — and the sibling
    listing was left with it. Driven on both backends: 60 designs stored, `listing(limit=20)`
    returned 20, and `find_experiment_protocols`/`GET /protocols` each answered "the stored
    experiment designs" over a third of them with nothing anywhere to say otherwise.

    `total` counts the same filters in the same transaction as the page, so "20 of 60" is one
    statement about one snapshot of a table two chemists may be appending to.
    """

    designs: list[DesignSummary] = Field(default_factory=list)
    # Everything matching the same filters, before the page bound.
    total: int = Field(default=0, ge=0)
    # The bound actually used, which is not the bound asked for once `_MAX_LISTING` bites.
    limit_applied: int = Field(default=_MAX_LISTING, ge=1)

    model_config = ConfigDict(frozen=True, extra="forbid")

    @property
    def truncated(self) -> bool:
        """Whether matching designs exist that this page does not carry."""
        return self.total > len(self.designs)


class DesignPage(BaseModel):
    """One design as `GET /protocols/{design_id}` serves it, read as a single consistent snapshot.

    **The four halves used to be four transactions, and they tore.** The route's own docstring says
    the history comes back in the same call because "asking for them separately makes the two
    answers race whenever somebody else is editing" — and the store then answered `read`, `summary`,
    `history` and `status_history` from four separate `_connection()` blocks. Measured against a
    real database with one concurrent `append`, the response was internally inconsistent in **92 to
    100 of every 100** reads: revision 1's document under a header saying head revision 2, with
    revision 2 listed in the history beside it. A client then picks its `parent_revision` from a
    response whose three halves disagree.

    It was also a backend divergence of the kind `InMemoryDesignStore` exists not to have: the
    in-memory store never yields between its four reads, so it tore 0/100, and every route test
    proved a consistency the deployment did not have.
    """

    revision: DesignRevision
    summary: DesignSummary | None
    history: list[DesignRevision] = Field(default_factory=list)
    status_history: list[StatusEvent] = Field(default_factory=list)

    model_config = ConfigDict(frozen=True, extra="forbid")


class RevisionConflict(ChemclawError):
    """A write whose `parent_revision` is not the design's head — somebody else edited it first."""


class StatusConflict(ChemclawError):
    """A lifecycle move from a status the design no longer holds: somebody else moved it first.

    A sibling of `RevisionConflict`, not a subclass: that one says the document moved under you,
    this one says the decision did. The routes map them to two codes in one 409, and a subclass
    would make the code depend on `except` order.
    """


class UnknownDesign(ChemclawError):
    """A design id nothing in the store answers to."""


class UnstorableDocument(ChemclawError):
    """A write this store will not take, that the caller can fix: the 422 of this module.

    Two families:

    - **Bytes no text column can hold**: a NUL, a C0 control character, or an unpaired UTF-16
      surrogate. Refused rather than stripped (the author did not type them, and stripping would
      store a different document), and checked in-process so both backends agree.
    - **A status the design cannot support**: see `require_movable`.
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
    ) -> DesignIndex:
        """One page of designs, newest first, with how many matched the same filters."""
        ...

    async def set_status(
        self,
        design_id: str,
        status: DesignStatus,
        *,
        expected_revision: int,
        expected_status: DesignStatus,
        actor: str = "",
        reason: str = "",
    ) -> None:
        """Move a design's lifecycle status, recording who moved it, why, and from which revision.

        Two compare-and-sets, because a design has two things somebody else can move:
        `expected_revision` (the document they were looking at) and `expected_status` (the decision
        they saw). Both are required keyword-only: a defaulted control is no control.

        Raises:
            UnknownDesign: nothing in the store answers to `design_id`.
            RevisionConflict: a revision landed between the read and this move.
            StatusConflict: somebody else moved the status between the read and this move.
        """
        ...

    async def status_history(self, design_id: str) -> list[StatusEvent]:
        """Every recorded lifecycle move, newest first."""
        ...

    async def page(self, design_id: str, revision: int | None = None) -> DesignPage | None:
        """The revision, the header, the history and the sign-offs as one consistent snapshot."""
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
        author_kind: AuthorKind,
        author: str = "",
        parent_revision: int = 0,
        change_note: str = "",
        session_id: str = "",
        correlation_id: str = "",
        status: DesignStatus = "draft",
    ) -> DesignRevision:
        """Store the next revision, refusing when `parent_revision` is not the head."""
        require_storable(
            design,
            change_note=change_note,
            author=author,
            design_id=design_id,
            session_id=session_id,
            correlation_id=correlation_id,
        )
        kind = revision_kind(design)
        existing = self._revisions.get(design_id, [])
        head = existing[-1].revision if existing else 0
        _require_head(design_id, head, parent_revision)
        revision = DesignRevision(
            design_id=design_id,
            revision=head + 1,
            kind=kind,
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
        # `session_id`, `correlation_id` and `opened_by` belong to the creating write and are not
        # updated, matching `_UPSERT_DESIGN`'s `DO UPDATE SET`, so `listing(session_id=...)` agrees
        # across backends.
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
    ) -> DesignIndex:
        """One page of designs, newest first, with how many matched the same filters."""
        # Clamped exactly as Postgres clamps it, including `limit <= 0`.
        bounded = max(1, min(limit, _MAX_LISTING))
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
        ordered = sorted(summaries, key=lambda s: s.updated_at, reverse=True)
        return DesignIndex(designs=ordered[:bounded], total=len(ordered), limit_applied=bounded)

    async def set_status(
        self,
        design_id: str,
        status: DesignStatus,
        *,
        expected_revision: int,
        expected_status: DesignStatus,
        actor: str = "",
        reason: str = "",
    ) -> None:
        """Move a design's lifecycle status, recording the move against the revision it names."""
        require_storable(None, design_id=design_id, actor=actor, reason=reason)
        if design_id not in self._meta:
            raise UnknownDesign(f"no design {design_id!r}")
        head_revision = self._revisions[design_id][-1]
        head = head_revision.revision
        if expected_revision != head:
            raise RevisionConflict(
                f"revision {expected_revision} is not the head ({head}); "
                "re-read the design before signing off on it"
            )
        current: DesignStatus = self._meta[design_id]["status"]
        require_unmoved(expected_status, current)
        require_movable(current, status, head_revision.kind)
        self._meta[design_id]["status"] = status
        self._meta[design_id]["updated_at"] = datetime.now(UTC)
        self._status_events.setdefault(design_id, []).append(
            StatusEvent(
                status=status,
                revision=head,
                actor=actor,
                reason=reason,
            )
        )

    async def status_history(self, design_id: str) -> list[StatusEvent]:
        """Every recorded lifecycle move, newest first."""
        return list(reversed(self._status_events.get(design_id, [])))

    async def page(self, design_id: str, revision: int | None = None) -> DesignPage | None:
        """Consistent by construction: nothing between these four reads yields to another task."""
        stored = await self.read(design_id, revision)
        if stored is None:
            return None
        return DesignPage(
            revision=stored,
            summary=await self.summary(design_id),
            history=await self.history(design_id),
            status_history=await self.status_history(design_id),
        )


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
        author_kind: AuthorKind,
        author: str = "",
        parent_revision: int = 0,
        change_note: str = "",
        session_id: str = "",
        correlation_id: str = "",
        status: DesignStatus = "draft",
    ) -> DesignRevision:
        """Store the next revision, refusing when `parent_revision` is not the head.

        `_SELECT_HEAD`'s `FOR UPDATE` serialises concurrent appends, so the loser reads the moved
        head and is refused by the `parent_revision` comparison. The `(design_id, revision)`
        primary-key violation is still translated to the same `RevisionConflict` as a backstop, so a
        future writer that skips the lock yields a 409, not a 500; no test currently reaches it.
        """
        require_storable(
            design,
            change_note=change_note,
            author=author,
            design_id=design_id,
            session_id=session_id,
            correlation_id=correlation_id,
        )
        kind = revision_kind(design)
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT_HEAD, (design_id,))
                row = await cur.fetchone()
                head = int(row[0]) if row else 0
                # `advanced()` on the create too, as the in-memory backend does, so a design's first
                # status does not depend on the backend.
                current_status: DesignStatus = advanced(row[1] if row else status, kind)
                _require_head(design_id, head, parent_revision)
                revision = DesignRevision(
                    design_id=design_id,
                    revision=head + 1,
                    kind=kind,
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
        """Every revision, oldest first, documents included, as the Protocol says.

        A header-only variant must return a type with no `design` field rather than a placeholder
        document, which callers could mistake for real and append.
        """
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT {_REVISION_COLUMNS} FROM experiment_protocol_revisions "
                    "WHERE design_id = %s ORDER BY revision",
                    (design_id,),
                )
                rows = await cur.fetchall()
        return [_revision(design_id, row) for row in rows]

    async def listing(
        self,
        *,
        status: DesignStatus | None = None,
        project: str = "",
        session_id: str = "",
        limit: int = 50,
    ) -> DesignIndex:
        """One page of designs, newest first, with how many matched the same filters.

        The count runs in the same transaction as the page so "20 of 60" describes one table state.
        """
        clauses: list[str] = []
        bounded = max(1, min(limit, _MAX_LISTING))
        params: dict[str, Any] = {"limit": bounded}
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
                await cur.execute(
                    f"SELECT count(*) FROM experiment_protocols {where}",
                    {key: value for key, value in params.items() if key != "limit"},
                )
                counted = await cur.fetchone()
        return DesignIndex(
            designs=[_summary(row) for row in rows],
            total=int(counted[0]) if counted else len(rows),
            limit_applied=bounded,
        )

    async def set_status(
        self,
        design_id: str,
        status: DesignStatus,
        *,
        expected_revision: int,
        expected_status: DesignStatus,
        actor: str = "",
        reason: str = "",
    ) -> None:
        """Move a design's lifecycle status, recording the move against the revision it names.

        The header, the head check and the status-event row are written in one transaction. The head
        is read under `FOR UPDATE` and compared with `expected_revision`, so the recorded revision
        is the one the approver saw (a colleague saving a new revision while they read is caught
        without any race). The status read under the same lock is compared with `expected_status`,
        so of two people deciding from one status exactly one write lands and the other gets
        `StatusConflict`. Which one wins is the lock queue's choice, not this code's.
        """
        require_storable(None, design_id=design_id, actor=actor, reason=reason)
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT_HEAD, (design_id,))
                head_row = await cur.fetchone()
                if head_row is None:
                    raise UnknownDesign(f"no design {design_id!r}")
                head = int(head_row[0])
                if expected_revision != head:
                    raise RevisionConflict(
                        f"revision {expected_revision} is not the head ({head}); "
                        "re-read the design before signing off on it"
                    )
                require_unmoved(expected_status, head_row[1])
                # Not a race: `_SELECT_HEAD` locks the header row, which every `append` also takes,
                # and the revisions table is append-only.
                await cur.execute(_SELECT_HEAD_KIND, (design_id, head))
                kind_row = await cur.fetchone()
                # Anything not provably `protocol` is treated as `request`, so a missing head row
                # fails closed.
                require_movable(
                    head_row[1],
                    status,
                    "protocol" if kind_row and kind_row[0] == "protocol" else "request",
                )
                await cur.execute(_SET_STATUS, {"status": status, "design_id": design_id})
                await cur.execute(
                    _INSERT_STATUS_EVENT,
                    {
                        "design_id": design_id,
                        "revision": head,
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
            head,
            actor,
        )

    async def status_history(self, design_id: str) -> list[StatusEvent]:
        """Every recorded lifecycle move, newest first."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT_STATUS_EVENTS, (design_id,))
                rows = await cur.fetchall()
        return [_status_event(row) for row in rows]

    async def page(self, design_id: str, revision: int | None = None) -> DesignPage | None:
        """All four reads in one transaction, at an isolation level that makes that mean something.

        READ COMMITTED takes a new snapshot per statement, so the block runs at `REPEATABLE READ`
        for one snapshot; it is read-only, so no serialization retry is needed. The revision clause
        matches `read`'s: `revision=0` is not "the head" and returns `None` on both backends.
        """
        clause = "AND revision = %(revision)s " if revision is not None else ""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                await cur.execute(
                    f"SELECT {_REVISION_COLUMNS} FROM experiment_protocol_revisions "
                    "WHERE design_id = %(design_id)s " + clause + "ORDER BY revision DESC LIMIT 1",
                    {"design_id": design_id, "revision": revision},
                )
                head_row = await cur.fetchone()
                if head_row is None:
                    return None
                stored = _revision(design_id, head_row)

                await cur.execute(f"{_SELECT_SUMMARY} WHERE design_id = %s", (design_id,))
                summary_row = await cur.fetchone()

                await cur.execute(
                    f"SELECT {_REVISION_COLUMNS} FROM experiment_protocol_revisions "
                    "WHERE design_id = %s ORDER BY revision",
                    (design_id,),
                )
                history_rows = await cur.fetchall()

                await cur.execute(_SELECT_STATUS_EVENTS, (design_id,))
                event_rows = await cur.fetchall()
        return DesignPage(
            revision=stored,
            summary=_summary(summary_row) if summary_row else None,
            history=[_revision(design_id, row) for row in history_rows],
            status_history=[_status_event(row) for row in event_rows],
        )


def _status_event(row: Any) -> StatusEvent:
    """One `experiment_protocol_status_events` row as its model."""
    return StatusEvent(
        status=row[0], revision=row[1], actor=row[2], reason=row[3], created_at=row[4]
    )


#: The statuses a new revision retires, because each is a claim about a *document* rather than
#: about the design: somebody approved these conditions, or somebody ran them. A revision replaces
#: the document, so the claim no longer describes what `GET /protocols/{id}` serves.
_RETIRED_BY_A_REVISION: frozenset[DesignStatus] = frozenset({"approved", "executed"})


def advanced(current: DesignStatus, kind: RevisionKind) -> DesignStatus:
    """The status a design has after a revision of `kind` lands on it.

    A `requested` design becomes `draft` when a protocol revision arrives. An `approved` or
    `executed` design is demoted to `draft` because both describe a document that has now changed;
    `experiment_protocol_status_events` keeps which revision was signed. `abandoned` is held: a
    design somebody decided not to run comes back only through a person's `set_status`.
    """
    if current == "requested" and kind == "protocol":
        return "draft"
    return "draft" if current in _RETIRED_BY_A_REVISION else current


def revision_kind(design: ExperimentDesign) -> RevisionKind:
    """The word for what this revision *is*, read off the document rather than taken on trust.

    `kind` is `has_protocol` at write time, so the column `advanced` and `require_movable` read is
    true by construction (a corrected ask that carries a drafted procedure forward is a protocol).
    """
    return "protocol" if design.has_protocol else "request"


#: The statuses that assert something about a *procedure*. A design holding only a structured ask
#: has no procedure, so neither word can be true of it.
_NEEDS_A_PROTOCOL: frozenset[DesignStatus] = frozenset({"approved", "executed"})


#: Which lifecycle move each status permits, as data rather than a chain of `if`s.
#:
#: Every `X -> X` is permitted on top of this table, so an idempotent retry is not a 422.
#: `draft -> executed` is absent (running without sign-off); `abandoned -> draft` is present
#: (reviving a retired design is a person's act).
_LEGAL_MOVES: dict[DesignStatus, frozenset[DesignStatus]] = {
    "requested": frozenset({"draft", "abandoned"}),
    "draft": frozenset({"approved", "abandoned"}),
    "approved": frozenset({"executed", "draft", "abandoned"}),
    "executed": frozenset({"abandoned"}),
    "abandoned": frozenset({"draft"}),
}


def require_movable(current: DesignStatus, status: DesignStatus, head_kind: RevisionKind) -> None:
    """Refuse a lifecycle move the design cannot support, naming why.

    Three rules, the document rules first because their message is more actionable:

    - `approved` and `executed` assert something about a procedure, so a `request` head refuses
      them; `requested` asserts there is none, so a `protocol` head refuses it. The head's `kind`
      decides, so Postgres need not load the document.
    - The transition order comes from `_LEGAL_MOVES`, indexed unconditionally so a new status
      without a row fails on its first move.
    - Every self-transition is exempt from the table (but not from the document rules): a repeat
      by somebody who has read the design, such as a co-signature, is recorded as a new event.

    `require_unmoved` runs first and checks the design is still where the caller thought it was.

    Raises:
        UnstorableDocument: the design cannot hold this status.
    """
    if status in _NEEDS_A_PROTOCOL and head_kind != "protocol":
        raise UnstorableDocument(
            f"this design holds only the structured ask, so it cannot be {status!r}: there is no "
            "procedure to approve or to have run. Draft the protocol first."
        )
    if status == "requested" and head_kind == "protocol":
        raise UnstorableDocument(
            "this design holds a procedure, so it cannot go back to 'requested', which means it "
            "holds only the structured ask. Abandon it, or draft over it — a revision moves a "
            "design's status by itself."
        )
    legal = _LEGAL_MOVES[current]
    if status != current and status not in legal:
        raise UnstorableDocument(
            f"a design that is {current!r} cannot be moved to {status!r}: from {current!r} the "
            f"moves are {sorted(legal | {current})}."
        )


def require_unmoved(expected: DesignStatus, actual: DesignStatus) -> None:
    """Refuse a lifecycle move made from a status the design no longer holds.

    The status counterpart to `expected_revision`'s document compare-and-set, shared by both
    backends. A no-op move naming the status actually held passes. A retry that has not re-read
    names the pre-move status and is refused.

    Raises:
        StatusConflict: somebody else moved the status between the caller's read and this move.
    """
    if expected != actual:
        raise StatusConflict(
            f"this design is {actual!r}, not {expected!r} as you saw it; somebody else moved it. "
            "Re-read the design before signing off on it"
        )


#: The characters no Postgres `text` or `jsonb` column can hold: the C0 controls (NUL above all)
#: and unpaired UTF-16 surrogates, which `json.loads` produces from a `"\ud800"` escape and
#: pydantic does not refuse on an unconstrained string. Refusing them in-process keeps both
#: backends agreeing.
_UNSTORABLE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff]")


def require_storable(design: ExperimentDesign | None, **text: str) -> None:
    """Refuse anything Postgres cannot hold, in this process, naming what is wrong.

    Both backends call it so the in-memory store refuses exactly what Postgres would. Covers the
    document and every caller-supplied string, including `set_status`'s browser-supplied `reason`.

    Raises:
        UnstorableDocument: something carries a NUL, a C0 control character, or an unpaired UTF-16
            surrogate.
    """
    if design is not None:
        for label, value in _strings(design.model_dump(), "the document"):
            _require_clean(label, value)
    for label, value in text.items():
        _require_clean(label, value)


def _require_clean(label: str, value: str) -> None:
    """Refuse one string, naming where it came from."""
    if _UNSTORABLE.search(value):
        raise UnstorableDocument(
            f"{label} contains a character no text column can store (a NUL, a C0 control "
            "character, or an unpaired UTF-16 surrogate). Remove it and send it again."
        )


def _strings(value: Any, path: str) -> Iterator[tuple[str, str]]:
    """Every string in a dumped model, with the path that reaches it."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            # Keys as well as values: `ProtocolArm.levels` keys are unconstrained.
            yield f"{path}.<key>", key
            yield from _strings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{path}[{index}]")


def _require_head(design_id: str, head: int, parent_revision: int) -> None:
    """Refuse a write derived from anything but the current head.

    `parent_revision=0` means "create" and is refused once the design exists: it is never a
    shortcut for "the head, whatever it is".
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
    """The store this deployment uses: Postgres where sessions are durable, memory otherwise.

    The in-memory instance is module-level: it is a real backend, and one that forgot every design
    between calls would be worse than none.
    """
    if settings.session_store == "postgres":
        return PostgresDesignStore()
    return _IN_MEMORY
