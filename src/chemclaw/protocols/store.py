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

# **The window this used to claim was closed by a `RETURNING` clause is closed by `_SELECT_HEAD`'s
# `FOR UPDATE`.** The comment here read "`RETURNING head_revision` rather than a second SELECT …
# reading it separately leaves a window in which an append moves the head between the two
# statements", and `RETURNING` appeared nowhere in this file: `set_status` does exactly the separate
# read the comment said it avoided. The guarantee is real and the mechanism named was not, which is
# the worse of the two failures — a reader auditing the sign-off path went looking for something
# that was not there.
_SET_STATUS = """
UPDATE experiment_protocols SET status = %(status)s, updated_at = now()
WHERE design_id = %(design_id)s
"""

#: The head revision's own `kind`, read under the same row lock as the head itself — see
#: `require_movable`. The design's *current status* needs no second statement at all: `_SELECT_HEAD`
#: already returns it, under the same `FOR UPDATE`, which is what makes the order rule read the
#: status a concurrent `append` or `set_status` cannot have moved underneath it.
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
    """A sign-off whose `expected_status` is not the design's current one.

    The complement of `RevisionConflict`, and it exists because that one does not cover this case:
    `expected_revision` is a compare-and-set on the *document*, so two people looking at revision 1
    can approve it and abandon it and both are told 204. The evidence survives either way —
    `experiment_protocol_status_events` records both moves with their actors — but nobody is told at
    the time, which is the whole point of a sign-off.

    It also catches the cheaper case: a lost response, retried, arriving after somebody else moved
    the design on.
    """


class UnknownDesign(ChemclawError):
    """A design id nothing in the store answers to."""


class UnstorableDocument(ChemclawError):
    """A write this store will not take, that the caller can fix — the 422 of this module.

    Two families, and the docstring named only the first for as long as the second existed:

    **Bytes no text column can hold** — a NUL, or a C0 control character, or an unpaired UTF-16
    surrogate. Postgres `text` and `jsonb` reject `\u0000` outright, and psycopg raises it as an
    untyped `DataError`/`UntranslatableCharacter` from inside the driver. Measured before this
    existed: a NUL anywhere in a browser-supplied design — the notes field, the title, the change
    note — was a **500** with a correlation id and nothing a caller could act on, while the
    in-memory backend accepted it, so the two backends disagreed about whether the write was
    possible. Refused rather than sanitised, because a chemist did not type a NUL: silently
    stripping it would store a document that is not the one that was sent. `ingest.eln.sync` strips
    control characters on the *ingest* path for the opposite reason — there the bytes come from
    somebody else's database and there is no author to refuse.

    **A status the design cannot support** — `require_movable`, which refuses `approved` or
    `executed` on a design holding only the structured ask, `requested` on one holding a procedure,
    and any move the lifecycle table does not permit from where the design currently is. Same
    exception because it is the same answer to the caller: the request as sent cannot be stored, the
    reason is in the message, and the fix is theirs. Both reach the routes as a 422.
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
    ) -> list[DesignSummary]:
        """Designs, newest first."""
        ...

    async def set_status(
        self,
        design_id: str,
        status: DesignStatus,
        *,
        expected_revision: int,
        expected_status: DesignStatus | None = None,
        actor: str = "",
        reason: str = "",
    ) -> None:
        """Move a design's lifecycle status, recording who moved it, why, and from which revision.

        `expected_revision` is the revision the person was *looking at*, and a move against anything
        else is refused. Required and keyword-only for the reason `RevisionIn.parent_revision` is
        both: a sign-off that did not say what it signed off on is exactly the one that ends up
        attributed to a document nobody read, and defaulting it to the head "for convenience" would
        remove the control rather than provide one.

        The move itself is checked against where the design is — see `require_movable` for the
        table and for why every `X -> X` is permitted.

        Raises:
            UnknownDesign: nothing in the store answers to `design_id`.
            RevisionConflict: a revision landed between the read and this move.
            UnstorableDocument: the design cannot hold this status, or cannot reach it from the one
                it has.
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
        self,
        design_id: str,
        status: DesignStatus,
        *,
        expected_revision: int,
        expected_status: DesignStatus | None = None,
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
        # The design's current status, from the same dict the move is about to write — the
        # in-memory counterpart of the header column Postgres reads under its row lock. Nothing
        # yields between this read and the write, so there is no window to close here.
        current: DesignStatus = self._meta[design_id]["status"]
        require_current_status(current, expected_status)
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

        **That is history, and the paragraph above is kept because it explains the handler rather
        than because it still describes the race.** `_SELECT_HEAD` took `FOR UPDATE` afterwards, for
        a different defect (a `set_status` losing to a concurrent append), and the lock serialises
        these two writers as a side effect: the loser now waits, reads the moved head, and is
        refused by the `parent_revision` comparison before it ever reaches the INSERT. Measured over
        5x100 concurrent pairs, the primary key decided **none** of them, and replacing this handler
        with a raised `AssertionError` leaves the whole suite green — no test reaches it.

        It stays anyway, as a backstop rather than a control anybody relies on: it is one `except`
        clause on a live statement, it costs nothing, and it is what keeps a future writer that
        skips the lock from serving a 500 where a 409 belongs. What is *not* claimed is that
        anything proves it works.
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
        """Every revision, oldest first — documents included, as the Protocol says.

        **A `document`-free variant was tried here and reverted, and the reason is worth keeping.**
        The route renders seven scalars and `len(blockers)` and never touches `item.design`, so
        selecting the document looked like pure waste: it measured 4x on a 24-arm plate and 39x on
        a 384-arm one, and the property worth having is better than either ratio — the header-only
        read is *flat* in document size, O(revisions) rather than O(bytes).

        It was reverted because of what it did to the value it returned. Filling `design` with a
        placeholder made this method answer differently on the two backends — the one thing
        `InMemoryDesignStore`'s docstring forbids — and the placeholder is an ordinary
        `ExperimentDesign` that nothing refuses, so a caller that read it and appended it wrote
        `title="(not read)"` into the header row `GET /protocols` renders. Measured, both.

        The optimisation is sound and belongs behind its own name, returning a type with no
        `design` field at all, so a caller that wants a document cannot silently get a fiction.
        That is a `docs/planning/BACKLOG.md` row rather than a second method with one caller.
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
        self,
        design_id: str,
        status: DesignStatus,
        *,
        expected_revision: int,
        expected_status: DesignStatus | None = None,
        actor: str = "",
        reason: str = "",
    ) -> None:
        """Move a design's lifecycle status, recording the move against the revision it names.

        The event row is the whole reason this is several statements in one transaction.
        `advanced()` demotes an approved or executed design back to `draft` when a revision lands on
        it, so the header cannot say which document a person signed off on — and until this table
        existed nothing could, while `advanced()`'s own docstring said `set_status` recorded it.

        **The head is read under `FOR UPDATE` and compared, which is what makes the recorded
        revision the one the approver saw.** Without it this stamped whatever `head_revision` had
        become by the time the UPDATE ran, so a chemist who opened revision 1, thought about it and
        clicked Approve after a colleague saved revision 2 signed a document they had never read —
        and the status-event table, whose whole purpose is to say *which* document was signed, said
        revision 2 with their name beside it.

        **And this one needs no race at all**, which is why it is worth more than the concurrency
        bugs beside it: the sign-off is wrong on plain latency, from reading a design, thinking
        about it, and clicking. Measured that way it is **100 of 100** across five runs of twenty,
        and 0 of 100 with the comparison — where the same scenario driven as a true `gather` race
        reproduced 0 of 100, because the two statements serialise on the pool. A defect that needs
        no interleaving is not a rare one.

        It is the identical control `append(parent_revision=…)` already is one statement below, and
        the `FOR UPDATE` is doing a second job besides: it serialises a status move against a
        concurrent `append`, which is the interleaving the deterministic case does not need.
        """
        require_storable(None, design_id=design_id, actor=actor, reason=reason)
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT_HEAD, (design_id,))
                head_row = await cur.fetchone()
                if head_row is None:
                    raise UnknownDesign(f"no design {design_id!r}")
                head = int(head_row[0])
                current: DesignStatus = head_row[1]
                if expected_revision != head:
                    raise RevisionConflict(
                        f"revision {expected_revision} is not the head ({head}); "
                        "re-read the design before signing off on it"
                    )
                # The kind of the revision this move is stamped against, and the reason that is
                # not a race: `_SELECT_HEAD` locks the *header* row, and every `append` takes that
                # same lock before writing, so no revision can land between the two reads. The
                # revisions table is append-only, so the row this finds cannot change either.
                require_current_status(current, expected_status)
                await cur.execute(_SELECT_HEAD_KIND, (design_id, head))
                kind_row = await cur.fetchone()
                # Anything that is not provably `protocol` is treated as `request`, so a header
                # naming a head revision whose row this read does not find fails *closed* rather
                # than waving an `executed` through on a document nobody can see.
                require_movable(
                    current,
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

        **One transaction is not on its own enough, and that is the whole subtlety here.**
        `core/db.py` is READ COMMITTED, which takes a *new snapshot per statement*, so four
        statements inside one transaction tear exactly as four transactions do. `REPEATABLE READ`
        gives the whole block one snapshot, which is what "the history comes back in the same call"
        was always supposed to mean. Nothing here writes, and a read-only transaction cannot take a
        serialization failure, so there is no retry to write.

        **The revision clause is `read`'s, spelled the same way, because `or 0` is not `is None`.**
        This selected on `(%s = 0 OR revision = %s)` over `revision or 0`, so `page(design_id, 0)`
        answered with the **head** here and `None` on the in-memory store — a backend divergence in
        the one method written to remove one, and reachable from a client that sends `revision=0`.
        There is no revision 0: `DesignRevision.revision` is `ge=1` and `parent_revision=0` is the
        word for "nothing", so `None` is the honest answer on both.
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


def revision_kind(design: ExperimentDesign) -> RevisionKind:
    """The word for what this revision *is*, read off the document rather than taken on trust.

    It used to be an argument, and the three callers derived it separately — which is the shape
    `has_protocol` exists to prevent and which failed exactly where a duplicated predicate always
    does, on the path where the two inputs come apart. `structure_experiment_request` deliberately
    carries a drafted procedure forward when a chemist corrects the ask, and it stamped
    `kind="request"` regardless. `require_movable` reads this column to decide whether a design has
    a procedure to approve, so a corrected ask made a fully drafted plate **permanently
    un-approvable and un-executable**: every arm, step and charge line present, and the store
    refusing the sign-off with "this design holds only the structured ask". The document was right
    and only the word for it was wrong, so nothing on the page hinted at the contradiction.

    Deriving it here is what makes the column true by construction: `kind` is `has_protocol` as it
    stood when the revision was written, which is what `advanced` and `require_movable` have always
    said they were reading. There is no caller that legitimately wants the other word — a document
    holding a procedure is a protocol whatever the tool that stored it was called.
    """
    return "protocol" if design.has_protocol else "request"


#: The statuses that assert something about a *procedure*. A design holding only a structured ask
#: has no procedure, so neither word can be true of it.
_NEEDS_A_PROTOCOL: frozenset[DesignStatus] = frozenset({"approved", "executed"})

#: Where a design may go from where it is. Data rather than a chain of `if`s, because this is a
#: product decision about a laboratory workflow and the next person to change it should be editing
#: one row rather than reading a branch — and because a table can be driven as a matrix, which is
#: how `tests/test_protocol_store.py` checks all twenty-five pairs on both backends.
#:
#: Every edge, and why it is here:
#:
#: * `requested -> draft` — the ask becomes a procedure. It is also the one promotion `advanced()`
#:   makes on its own when a protocol revision lands, so refusing it here would let a write do what
#:   a person may not.
#: * `requested -> abandoned` — an ask nobody will act on. `abandoned` is kept precisely because a
#:   design deliberately not run is evidence too.
#: * `draft -> approved` — the sign-off, and the only door into `approved`.
#: * `approved -> executed` — it was run. The only door into `executed`, which is the point of the
#:   whole table: **`draft -> executed` is absent**, so a design cannot be recorded as run without a
#:   human having said yes to it first. That is not a paperwork rule — `approved` is the moment a
#:   person takes responsibility for conditions somebody is about to put on a bench.
#: * `approved -> draft` — a sign-off withdrawn without editing the document. Without this edge the
#:   only way to un-approve is to write a revision, which changes the protocol in order to change
#:   the header, and `advanced()` already does that half automatically.
#: * `executed -> abandoned` — the only move out of `executed`. A run happened; no later word
#:   un-runs it, and `abandoned` here means "this line of work stops", not "it was never done".
#: * `abandoned -> draft` — the one way back, and it must exist here because `advanced()` refuses to
#:   do it on a write: "a design somebody decided not to run does not come back because an agent
#:   wrote to it, and the way back is a person's `set_status`". This is that sentence's other half.
#: * **`abandoned -> executed` is absent**, which is the move this table was written for: measured
#:   on both backends, a retired design went straight to `executed`, asserting that an experiment
#:   somebody had decided not to run had been run.
#:
#: Anything not listed is refused. `requested` is reachable from nothing but itself, because a
#: design that has held a procedure cannot truthfully say it holds only an ask — `require_movable`
#: refuses that on the document rather than on the order, one function below.
_LEGAL_MOVES: dict[DesignStatus, frozenset[DesignStatus]] = {
    "requested": frozenset({"draft", "abandoned"}),
    "draft": frozenset({"approved", "abandoned"}),
    "approved": frozenset({"executed", "draft", "abandoned"}),
    "executed": frozenset({"abandoned"}),
    "abandoned": frozenset({"draft"}),
}


def require_current_status(current: DesignStatus, expected: DesignStatus | None) -> None:
    """Refuse a sign-off written against a status the design has since left.

    One function so the two backends cannot disagree, and checked *before* `require_movable` because
    "somebody moved this while you were reading" is the more actionable answer: the caller's next
    step is to re-read and decide again, not to pick a different target status.

    `None` means the caller did not say what it saw, and is allowed — every in-tree caller other
    than the front door moves a design it has just created, and making them read a status back would
    be ceremony rather than a control. The route requires it, which is where the concurrent humans
    are.

    Raises:
        StatusConflict: the design is no longer in the status the caller signed off against.
    """
    if expected is not None and expected != current:
        raise StatusConflict(
            f"the design is {current!r}, not the {expected!r} you signed off against; "
            "re-read it before deciding again"
        )


def require_movable(current: DesignStatus, status: DesignStatus, head_kind: RevisionKind) -> None:
    """Refuse a lifecycle move the design cannot support, naming why.

    Nothing tied a status to the document it is a statement about, so `set_status("executed")` on a
    design that holds only the structured ask was accepted on both backends: a lab record saying an
    experiment was *run*, written against a document with no charge table, no procedure and no arms.
    `executed` and `approved` are the two words that assert something about a procedure, and a
    `request` revision has none to assert it of. The head's `kind` is what decides it, because that
    column is `has_protocol` as it stood when the revision was written — so the two backends read
    one fact rather than each deriving it, and Postgres does not have to load the document to
    answer.

    **`requested` is the same rule read backwards, and it was missing.** It is the one status that
    asserts the *absence* of a procedure — `DesignStatus` defines it as "holds only a structured
    ask" — so a `protocol` head contradicts it exactly as a `request` head contradicts `executed`.
    Nothing refused it: measured on both backends, an executed design moved to `requested` and
    stayed there with a fully drafted protocol as head, so `GET /protocols?status=requested` listed
    it among the intakes and `?status=executed` did not. Written as an `if` rather than as a second
    frozenset beside `_NEEDS_A_PROTOCOL` because it has exactly one member and always will: the
    other four statuses are claims about a document or about a decision, and only this one is a
    claim that no document exists yet.

    **The transition *order* is the third rule and it arrived last**, because it is the one that
    cannot be derived from the document: it needs the design's *current* status, which this function
    was not given. Nothing forbade `abandoned → executed` or `draft → executed`, measured accepted
    on both backends — a design retired because the starting material decomposes, marked run; a
    protocol nobody had signed off, marked run. `_LEGAL_MOVES` above is the decided table, and
    each of its edges says there why it exists.

    **Every self-transition is legal**, which the table does not spell out because it is one rule
    about a repeat rather than five decisions about the lifecycle. `Chemclaw3_ui`'s sign-off panel
    renders a *Mark X* button for all five statuses regardless of where the design is, so
    `approved → approved` is one click away by construction — and a move whose response is lost is
    reported to the chemist as "The status was not recorded" when it may well have been, so pressing
    again is the ordinary recovery and it arrives here as `X → X`. Refusing it would turn a button
    the client itself offers into a 422 on the one screen where the honest answer is "it already
    worked". This tree relies on it too: the test that proves both SQL `CHECK (status IN (...))`
    constraints accept every member of the Literal drives `draft → draft` and `requested →
    requested` to do it. The move is not swallowed — the caller still gets its 204 and
    `experiment_protocol_status_events` still gains a row, because a repeat is somebody acting a
    second time and that table is the record of who moved a design and why.

    **The document rules run first, deliberately.** Where both refuse — `requested` on a protocol
    head, say — the more actionable message wins: "there is no procedure to approve" tells a chemist
    what to do next, and "you cannot go from here to there" does not.

    The table is indexed on `current` unconditionally, so a sixth `DesignStatus` added without a row
    here fails on the first move rather than being silently movable anywhere.

    The complementary guard — refusing a sign-off that would silently overwrite a *different*
    person's sign-off at the same revision — is `require_current_status`, which runs before this
    one. It is separate because the two answer different questions: this table says whether the move
    is legal at all, and that one says whether the design is still where the person thought it was.
    `expected_revision` covers neither, being a compare-and-set on the *document*.

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


#: The characters no Postgres `text` or `jsonb` column can hold. NUL is the one that actually
#: arrives (it is what a truncated UTF-16 read or a fuzzing client produces); the rest of the C0
#: range is refused with it because none of them belongs in a laboratory procedure and a document
#: carrying one is not a document somebody typed.
#:
#: **Unpaired UTF-16 surrogates are here for the same reason and were missed.** Starlette parses a
#: request body with stdlib `json.loads`, which turns `"\ud800"` into a lone surrogate, and pydantic
#: only refuses one on a `str` field carrying a constraint — so any unconstrained string in a design
#: (`setpoints.atmosphere`, `solvent`, `waste`, an arm's `note`, a level's value) reached the driver
#: and blew up there. Measured on the real app: `POST /protocols/{id}/revisions` answered **500** on
#: Postgres and **200** in memory, which is exactly the backend divergence this guard exists to
#: prevent. It is not even counted as a database failure — `UnicodeEncodeError` is not a
#: `psycopg.Error`, so `_failure_kind` returns `None` and the metric never moves.
_UNSTORABLE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff]")


def require_storable(design: ExperimentDesign | None, **text: str) -> None:
    """Refuse anything Postgres cannot hold, in this process, naming what is wrong.

    Both backends call it, which is the point: without it the in-memory store accepted a NUL and
    Postgres answered **500**, so whether a write was possible depended on which backend a
    deployment had configured — the divergence `InMemoryDesignStore`'s own docstring forbids.

    **Every caller-supplied string, not the document and the change note only.** The first version
    covered two of eight and left six diverging: `author`, `session_id`, `correlation_id`,
    `design_id`, `actor` and — browser-supplied, and therefore the one that mattered —
    `set_status`'s `reason`, which `Chemclaw3_ui` collects from a chemist and which answered a raw
    **500** one route over from the docstring saying that failure was fixed.

    Raises:
        UnstorableDocument: something carries a NUL, a C0 control character, or an unpaired UTF-16
            surrogate — the three families no `text` or `jsonb` column can hold.
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
            # The key as well as the value: `ProtocolArm.levels` is `dict[str, str]` with no
            # constraint on its keys, so a NUL in a factor name reached `jsonb` and raised
            # `UntranslatableCharacter` — the exact exception `UnstorableDocument` names.
            yield f"{path}.<key>", key
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
