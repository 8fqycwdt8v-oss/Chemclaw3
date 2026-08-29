"""The revision history of every design — append-only, because an edit is the evidence.

Two tables and one rule: **a revision is never updated.** A change is a new row naming the row it
came from. That is what makes an expert's alteration of the first shot observable at all, and it is
what makes a concurrent edit a refusal instead of a silent overwrite — `parent_revision` is
compared against the head, so two people editing one protocol produce a `RevisionConflict` rather
than one of them losing their work without being told.

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
from collections.abc import AsyncIterator, Sequence
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
    ProtocolCheck,
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
    -- inside the same transaction, through `advanced()`. It is the one transition a write makes on
    -- its own — `requested` becoming `draft` — and every other status move is `set_status`.
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

_SELECT_HEAD = "SELECT head_revision, status FROM experiment_protocols WHERE design_id = %s"

_SELECT_SUMMARY = """
SELECT design_id, title, mode, status, project, opened_by, head_revision, arm_count,
       blocker_count, created_at, updated_at
FROM experiment_protocols
"""


class RevisionConflict(ChemclawError):
    """A write whose `parent_revision` is not the design's head — somebody else edited it first."""


class UnknownDesign(ChemclawError):
    """A design id nothing in the store answers to."""


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

    async def set_status(self, design_id: str, status: DesignStatus, actor: str = "") -> None:
        """Move a design's lifecycle status."""
        ...


class InMemoryDesignStore:
    """A real backend, not a test double — the one a deployment without Postgres runs on."""

    def __init__(self) -> None:
        """Start empty; process-lifetime, because a store that forgets between calls is not one."""
        self._revisions: dict[str, list[DesignRevision]] = {}
        self._meta: dict[str, dict[str, Any]] = {}

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
        return sorted(summaries, key=lambda s: s.updated_at, reverse=True)[:limit]

    async def set_status(self, design_id: str, status: DesignStatus, actor: str = "") -> None:
        """Move a design's lifecycle status."""
        if design_id not in self._meta:
            raise UnknownDesign(f"no design {design_id!r}")
        self._meta[design_id]["status"] = status
        self._meta[design_id]["updated_at"] = datetime.now(UTC)


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
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT_HEAD, (design_id,))
                row = await cur.fetchone()
                head = int(row[0]) if row else 0
                current_status: DesignStatus = advanced(row[1], kind) if row else status
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
        """Every revision, oldest first."""
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

    async def set_status(self, design_id: str, status: DesignStatus, actor: str = "") -> None:
        """Move a design's lifecycle status."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE experiment_protocols SET status = %s, updated_at = now() "
                    "WHERE design_id = %s",
                    (status, design_id),
                )
                if cur.rowcount == 0:
                    raise UnknownDesign(f"no design {design_id!r}")
            await conn.commit()
        logger.info("protocol.status design_id=%s status=%s actor=%s", design_id, status, actor)


def advanced(current: DesignStatus, kind: str) -> DesignStatus:
    """The status a design has after a revision of `kind` lands on it.

    Two automatic transitions, and the second is a correction. A design that held only a structured
    ask becomes a `draft` the moment a protocol revision arrives — that one was always here.

    **An `approved` design becomes a `draft` again when a new revision lands**, because an approval
    is a statement about a *document* and the document has changed. The first version held the
    status instead, reasoning that a re-draft must not silently un-approve — which is true of the
    word and false of the thing: measured, a chemist approving revision 1 at 80 °C and an agent then
    drafting revision 2 at 200 °C left a header reading `approved` over a protocol nobody had read,
    and `GET /protocols/{id}` serves the head. That is the one path in this tier to somebody running
    conditions no one signed off. Which revision *was* approved stays recoverable: `set_status`
    records it, and the revision history is append-only.

    `abandoned` is deliberately not in that shape and is held: a design somebody decided not to run
    does not come back because an agent wrote to it, and the way back is a person's `set_status`.
    """
    if current == "requested" and kind == "protocol":
        return "draft"
    return "draft" if current == "approved" else current


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
