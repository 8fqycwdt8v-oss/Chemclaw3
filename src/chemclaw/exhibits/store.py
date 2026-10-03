"""Where artefacts and their revisions live — a header row and an append-only history under it.

Shaped as `protocols.store` is and for the same reasons: a `Protocol` with an in-memory and a
Postgres implementation, so the tools and routes are testable with no database while the store that
serves the front door is exercised against a real one; the header is a mutable projection and the
revisions are append-only; and **a write whose base revision is not the head is refused**
(`StaleRevision`) rather than allowed to discard the revision it did not see. That last property is
the one the artefact exists for: the agent drafts, the chemist corrects, and neither may overwrite
the other without having read it.

**Every read is scoped to a session.** An artefact belongs to one conversation, and an id from
another session answers exactly as an unknown one does — `None`, never "it exists but is not
yours" — so neither a tool nor a route can be used as an oracle for which ids exist.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import psycopg
from psycopg.rows import TupleRow

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.jsonb import json_column
from chemclaw.exhibits.models import (
    EXHIBIT_ID,
    ExhibitHeader,
    ExhibitKind,
    ExhibitLimit,
    ExhibitRevision,
    ExhibitState,
    ExhibitView,
    InvalidExhibit,
    Spec,
    StaleRevision,
    UnknownExhibit,
    new_exhibit_id,
    parse_spec,
    spec_bytes,
    spec_json,
)
from chemclaw.protocols.models import AuthorKind


@runtime_checkable
class ExhibitStore(Protocol):
    """The operations the tools, the routes and the turn note need, and nothing else."""

    async def create(
        self,
        session_id: str,
        *,
        title: str,
        spec: Spec,
        author_kind: AuthorKind,
        author: str,
        change_note: str = "",
        correlation_id: str = "",
        unverified_figures: list[str] | None = None,
        exhibit_id: str | None = None,
    ) -> ExhibitView:
        """Store revision 1 of a new artefact, refusing at the session's cap.

        `exhibit_id` is for a writer that must be idempotent across retries — a durable activity
        deriving the id from its workflow — and makes the call a create-or-return: when this
        session already holds that id, its revision 1 comes back and nothing is written. Every
        other writer leaves it `None` and gets a fresh random id.

        Raises:
            ExhibitLimit: the session already holds `exhibit_max_per_session` artefacts.
            InvalidExhibit: `exhibit_id` is malformed, or another session holds it.
        """
        ...

    async def append(
        self,
        session_id: str,
        exhibit_id: str,
        *,
        spec: Spec,
        parent_revision: int,
        author_kind: AuthorKind,
        author: str,
        change_note: str = "",
        title: str | None = None,
        correlation_id: str = "",
        unverified_figures: list[str] | None = None,
    ) -> ExhibitView:
        """Store the next revision, refusing a stale base, a spec of a different kind or a full one.

        Raises:
            UnknownExhibit: the session holds no artefact `exhibit_id`.
            StaleRevision: `parent_revision` is not the head.
            InvalidExhibit: the spec's kind is not the artefact's.
            ExhibitLimit: the artefact already holds `exhibit_max_revisions` revisions.
        """
        ...

    async def view(self, session_id: str, exhibit_id: str, revision: int = 0) -> ExhibitView | None:
        """One revision — the head for 0 — or `None` when the session holds no such thing."""
        ...

    async def headers(self, session_id: str) -> list[ExhibitHeader]:
        """The session's artefacts, most recently updated first."""
        ...

    async def states(self, session_id: str) -> list[ExhibitState]:
        """The headers again, with what the agent has seen and last wrote of each."""
        ...

    async def revisions(self, session_id: str, exhibit_id: str) -> list[ExhibitRevision] | None:
        """The history oldest first, or `None` when the session holds no such artefact."""
        ...

    async def human_edits(self, session_id: str, exhibit_id: str) -> list[tuple[Spec, Spec | None]]:
        """Each person-authored revision's spec beside its parent's (`None` for revision 1).

        One read rather than two per revision: the grounding check runs on every agent write and
        wants exactly this pair, so it is served whole.
        """
        ...

    async def mark_seen(self, session_id: str, exhibit_id: str, revision: int) -> None:
        """Record that the agent has read or been told of `revision`; never moves the mark back."""
        ...

    async def listing_for(self, actor: str, limit: int) -> list[ExhibitHeader]:
        """Artefacts across every session `actor` owns or is a member of, newest first."""
        ...


def _require_kind(exhibit_id: str, kind: str, spec: Spec) -> None:
    """Refuse a revision that would change what the artefact is."""
    if spec.kind != kind:
        raise InvalidExhibit(
            f"{exhibit_id} is a {kind} artefact and the spec is a {spec.kind}; create a new "
            "artefact for a different kind"
        )


def _require_room(exhibit_id: str, head: int) -> None:
    """Refuse a revision past the per-artefact cap, naming the cap and the way on."""
    cap = settings.exhibit_max_revisions
    if head >= cap:
        raise ExhibitLimit(
            f"{exhibit_id} already holds {head} revisions, the most one artefact may ({cap}); "
            "create a new artefact to carry on from its head"
        )


@dataclass
class _Revision:
    """One stored revision in the in-memory backend."""

    view: ExhibitView
    byte_size: int


@dataclass
class _Entry:
    """One artefact in the in-memory backend: its header fields and its history."""

    session_id: str
    kind: ExhibitKind
    title: str
    created_by: str
    created_at: datetime
    agent_seen: int = 0
    revisions: list[_Revision] = field(default_factory=list)


class InMemoryExhibitStore:
    """A real backend, not a test double — what a deployment without Postgres runs on."""

    def __init__(self) -> None:
        """Start empty; process-lifetime, because a store that forgot between calls is not one."""
        self._entries: dict[str, _Entry] = {}

    async def create(
        self,
        session_id: str,
        *,
        title: str,
        spec: Spec,
        author_kind: AuthorKind,
        author: str,
        change_note: str = "",
        correlation_id: str = "",
        unverified_figures: list[str] | None = None,
        exhibit_id: str | None = None,
    ) -> ExhibitView:
        """Store revision 1 of a new artefact, refusing at the session's cap."""
        if exhibit_id is not None:
            _require_mintable(exhibit_id)
            if exhibit_id in self._entries:
                return _same_session(
                    exhibit_id, session_id, await self.view(session_id, exhibit_id, 1)
                )
        held = sum(1 for entry in self._entries.values() if entry.session_id == session_id)
        if held >= settings.exhibit_max_per_session:
            raise ExhibitLimit(_limit_message(held))
        exhibit_id = exhibit_id or new_exhibit_id()
        now = datetime.now(UTC)
        entry = _Entry(
            session_id=session_id,
            kind=spec.kind,
            title=title,
            created_by=author,
            created_at=now,
        )
        self._entries[exhibit_id] = entry
        return self._add(
            exhibit_id, entry, spec, 0, author_kind, author, change_note, unverified_figures
        )

    async def append(
        self,
        session_id: str,
        exhibit_id: str,
        *,
        spec: Spec,
        parent_revision: int,
        author_kind: AuthorKind,
        author: str,
        change_note: str = "",
        title: str | None = None,
        correlation_id: str = "",
        unverified_figures: list[str] | None = None,
    ) -> ExhibitView:
        """Store the next revision, refusing a stale base or a spec of a different kind."""
        entry = self._entry(session_id, exhibit_id)
        if entry is None:
            raise UnknownExhibit(f"no artefact {exhibit_id!r} in this session")
        head = entry.revisions[-1].view.revision
        if parent_revision != head:
            raise StaleRevision(exhibit_id, head, parent_revision)
        _require_kind(exhibit_id, entry.kind, spec)
        _require_room(exhibit_id, head)
        if title is not None:
            entry.title = title
        return self._add(
            exhibit_id, entry, spec, head, author_kind, author, change_note, unverified_figures
        )

    def _add(
        self,
        exhibit_id: str,
        entry: _Entry,
        spec: Spec,
        head: int,
        author_kind: AuthorKind,
        author: str,
        change_note: str,
        unverified_figures: list[str] | None,
    ) -> ExhibitView:
        """Append one revision to `entry` and return it as served."""
        now = datetime.now(UTC)
        revision = head + 1
        if author_kind == "agent":
            entry.agent_seen = revision
        view = ExhibitView(
            exhibit_id=exhibit_id,
            session_id=entry.session_id,
            kind=entry.kind,
            title=entry.title,
            head_revision=revision,
            head_author_kind=author_kind,
            head_author=author,
            created_by=entry.created_by,
            created_at=entry.created_at,
            updated_at=now,
            revision=revision,
            parent_revision=head,
            author_kind=author_kind,
            author=author,
            change_note=change_note,
            revision_created_at=now,
            spec=spec,
            unverified_figures=list(unverified_figures or []),
        )
        entry.revisions.append(_Revision(view=view, byte_size=spec_bytes(spec)))
        return view

    def _entry(self, session_id: str, exhibit_id: str) -> _Entry | None:
        """The artefact if this session holds it — another session's answers as an unknown id."""
        entry = self._entries.get(exhibit_id)
        return entry if entry is not None and entry.session_id == session_id else None

    async def view(self, session_id: str, exhibit_id: str, revision: int = 0) -> ExhibitView | None:
        """One revision, carrying the *current* header beside the revision's own record."""
        entry = self._entry(session_id, exhibit_id)
        if entry is None:
            return None
        head = entry.revisions[-1].view
        chosen = head if revision == 0 else _find(entry.revisions, revision)
        if chosen is None:
            return None
        return chosen.model_copy(update=_header_fields(head))

    async def headers(self, session_id: str) -> list[ExhibitHeader]:
        """The session's artefacts, most recently updated first."""
        return [state.header for state in await self.states(session_id)]

    async def states(self, session_id: str) -> list[ExhibitState]:
        """The headers with the agent's read mark and its last revision."""
        found = [
            ExhibitState(
                header=ExhibitHeader.model_validate(_header_fields(entry.revisions[-1].view)),
                agent_seen_revision=entry.agent_seen,
                last_agent_revision=max(
                    (r.view.revision for r in entry.revisions if r.view.author_kind == "agent"),
                    default=0,
                ),
            )
            for entry in self._entries.values()
            if entry.session_id == session_id
        ]
        return sorted(found, key=lambda state: state.header.updated_at, reverse=True)

    async def revisions(self, session_id: str, exhibit_id: str) -> list[ExhibitRevision] | None:
        """The history oldest first."""
        entry = self._entry(session_id, exhibit_id)
        if entry is None:
            return None
        return [
            ExhibitRevision(
                revision=stored.view.revision,
                parent_revision=stored.view.parent_revision,
                author_kind=stored.view.author_kind,
                author=stored.view.author,
                change_note=stored.view.change_note,
                created_at=stored.view.revision_created_at,
                byte_size=stored.byte_size,
            )
            for stored in entry.revisions
        ]

    async def human_edits(self, session_id: str, exhibit_id: str) -> list[tuple[Spec, Spec | None]]:
        """Each person-authored revision's spec beside its parent's."""
        entry = self._entry(session_id, exhibit_id)
        if entry is None:
            return []
        specs = {stored.view.revision: stored.view.spec for stored in entry.revisions}
        return [
            (stored.view.spec, specs.get(stored.view.parent_revision))
            for stored in entry.revisions
            if stored.view.author_kind == "human"
        ]

    async def mark_seen(self, session_id: str, exhibit_id: str, revision: int) -> None:
        """Raise the agent's read mark to `revision`; a lower one is ignored."""
        entry = self._entry(session_id, exhibit_id)
        if entry is not None:
            entry.agent_seen = max(entry.agent_seen, revision)

    async def listing_for(self, actor: str, limit: int) -> list[ExhibitHeader]:
        """Empty: this backend has no registry of who owns or joined a session.

        The precedent is `GET /sessions` under the in-memory session store, which answers `[]` for
        the same reason — reporting what this process happens to hold would answer a question about
        the deployment with an eviction-dependent guess.
        """
        return []


def _find(revisions: Sequence[_Revision], revision: int) -> ExhibitView | None:
    """The stored revision numbered `revision`, if there is one."""
    return next((r.view for r in revisions if r.view.revision == revision), None)


def _header_fields(head: ExhibitView) -> dict[str, Any]:
    """The header half of a view, as the fields `ExhibitHeader` declares."""
    return {name: getattr(head, name) for name in ExhibitHeader.model_fields}


def _require_mintable(exhibit_id: str) -> None:
    """Refuse a caller-chosen id that is not in the shape a minted one takes."""
    if not EXHIBIT_ID.fullmatch(exhibit_id):
        raise InvalidExhibit(f"{exhibit_id!r} is not an artefact id (`xb-` and 16 hex digits)")


def _same_session(exhibit_id: str, session_id: str, existing: ExhibitView | None) -> ExhibitView:
    """The existing revision 1 a create-or-return hands back, or a refusal when it is not ours.

    `None` means the id is held by another session: a deterministic id colliding across sessions
    is not a retry, and handing back somebody else's artefact would be a leak.
    """
    if existing is None:
        raise InvalidExhibit(f"{exhibit_id} belongs to another conversation")
    return existing


def _limit_message(held: int) -> str:
    """The refusal a full session gets, naming the cap and the way out."""
    return (
        f"this session already holds {held} artefacts, the most one session may "
        f"({settings.exhibit_max_per_session}); revise an existing artefact instead"
    )


_HEADER_COLUMNS = (
    "e.exhibit_id, e.session_id, e.kind, e.title, e.head_revision, e.head_author_kind, "
    "e.head_author, e.created_by, e.created_at, e.updated_at"
)

_INSERT_HEADER = """
INSERT INTO session_exhibits
    (exhibit_id, session_id, kind, title, head_revision, head_author_kind, head_author,
     agent_seen_revision, created_by, correlation_id, created_at, updated_at)
VALUES
    (%(exhibit_id)s, %(session_id)s, %(kind)s, %(title)s, 1, %(author_kind)s, %(author)s,
     %(seen)s, %(author)s, %(correlation_id)s, now(), now())
"""

_INSERT_REVISION = """
INSERT INTO session_exhibit_revisions
    (exhibit_id, revision, parent_revision, author_kind, author, change_note, spec, byte_size,
     unverified_figures, correlation_id, created_at)
VALUES
    (%(exhibit_id)s, %(revision)s, %(parent)s, %(author_kind)s, %(author)s, %(change_note)s,
     %(spec)s, %(byte_size)s, %(unverified)s, %(correlation_id)s, now())
"""

# The header row is locked for the whole append, so two writers on one artefact serialise and the
# second reads the head the first wrote — and is refused by the `parent_revision` comparison rather
# than by the primary key. The protocol store measured the race this closes; this copies the cure.
_LOCK_HEAD = (
    "SELECT head_revision, kind FROM session_exhibits "
    "WHERE exhibit_id = %s AND session_id = %s FOR UPDATE"
)

_ADVANCE_HEADER = """
UPDATE session_exhibits SET
    head_revision = %(revision)s,
    head_author_kind = %(author_kind)s,
    head_author = %(author)s,
    title = COALESCE(%(title)s, title),
    agent_seen_revision = CASE WHEN %(author_kind)s = 'agent' THEN %(revision)s
                               ELSE agent_seen_revision END,
    updated_at = now()
WHERE exhibit_id = %(exhibit_id)s
"""

# Serialises creates within one session, so two concurrent creates at `cap - 1` cannot both pass
# the count. Keyed by session rather than table-wide: creates in different sessions never contend.
_SESSION_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended('session_exhibits:' || %s, 0))"
_COUNT = "SELECT count(*) FROM session_exhibits WHERE session_id = %s"
_HELD_BY = "SELECT session_id FROM session_exhibits WHERE exhibit_id = %s"

_SELECT_VIEW = f"""
SELECT {_HEADER_COLUMNS}, r.revision, r.parent_revision, r.author_kind, r.author, r.change_note,
       r.created_at, r.spec, r.unverified_figures
FROM session_exhibits e
JOIN session_exhibit_revisions r ON r.exhibit_id = e.exhibit_id
WHERE e.exhibit_id = %(exhibit_id)s AND e.session_id = %(session_id)s
  AND r.revision = CASE WHEN %(revision)s = 0 THEN e.head_revision ELSE %(revision)s END
"""

_SELECT_STATES = f"""
SELECT {_HEADER_COLUMNS}, e.agent_seen_revision,
       COALESCE((SELECT max(r.revision) FROM session_exhibit_revisions r
                 WHERE r.exhibit_id = e.exhibit_id AND r.author_kind = 'agent'), 0)
FROM session_exhibits e
WHERE e.session_id = %s
ORDER BY e.updated_at DESC, e.exhibit_id
"""

_SELECT_REVISIONS = """
SELECT r.revision, r.parent_revision, r.author_kind, r.author, r.change_note, r.created_at,
       r.byte_size
FROM session_exhibit_revisions r
JOIN session_exhibits e ON e.exhibit_id = r.exhibit_id
WHERE r.exhibit_id = %s AND e.session_id = %s
ORDER BY r.revision
"""

_SELECT_HUMAN_EDITS = """
SELECT r.spec, p.spec
FROM session_exhibit_revisions r
JOIN session_exhibits e ON e.exhibit_id = r.exhibit_id
LEFT JOIN session_exhibit_revisions p
       ON p.exhibit_id = r.exhibit_id AND p.revision = r.parent_revision
WHERE r.exhibit_id = %s AND e.session_id = %s AND r.author_kind = 'human'
ORDER BY r.revision
"""

_MARK_SEEN = """
UPDATE session_exhibits SET agent_seen_revision = GREATEST(agent_seen_revision, %s)
WHERE exhibit_id = %s AND session_id = %s
"""

# Owner or member, the same two arms `resolve_session` authorizes a session read by. `IS NOT
# DISTINCT FROM` so a dev deployment's NULL owner matches a NULL caller, as `_OWNER_LIST` does.
_SELECT_FOR_ACTOR = f"""
SELECT {_HEADER_COLUMNS}
FROM session_exhibits e
WHERE e.session_id IN (
    SELECT session_id FROM session_owners WHERE owner IS NOT DISTINCT FROM %(actor)s
    UNION
    SELECT session_id FROM session_members WHERE actor = %(actor)s
)
ORDER BY e.updated_at DESC, e.exhibit_id
LIMIT %(limit)s
"""


class PostgresExhibitStore:
    """The durable store — `session_exhibits` and its append-only revision table (115)."""

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[psycopg.AsyncConnection[TupleRow]]:
        """Borrow a connection on the *session layer's* database.

        `session_store_dsn`, else `postgres_dsn` — the resolver `agent.session_store` uses — because
        an artefact is session state: `delete_session` removes it inside the same transaction as
        the transcript, which only works if both live in one database.
        """
        async with db.connection(settings.session_store_dsn or settings.postgres_dsn) as conn:
            yield conn

    async def create(
        self,
        session_id: str,
        *,
        title: str,
        spec: Spec,
        author_kind: AuthorKind,
        author: str,
        change_note: str = "",
        correlation_id: str = "",
        unverified_figures: list[str] | None = None,
        exhibit_id: str | None = None,
    ) -> ExhibitView:
        """Store revision 1 of a new artefact, refusing at the session's cap.

        A caller-chosen id is looked up first; a retry that raced the attempt it repeats past that
        look-up lands on the header's primary key instead, and the backstop below turns that into
        the same create-or-return answer.
        """
        if exhibit_id is not None:
            _require_mintable(exhibit_id)
            async with self._connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(_HELD_BY, (exhibit_id,))
                    held_by = await cur.fetchone()
            if held_by is not None:
                return _same_session(
                    exhibit_id, session_id, await self.view(session_id, exhibit_id, 1)
                )
        chosen = exhibit_id is not None
        exhibit_id = exhibit_id or new_exhibit_id()
        raced = False
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SESSION_LOCK, (session_id,))
                await cur.execute(_COUNT, (session_id,))
                counted = await cur.fetchone()
                held = int(counted[0]) if counted else 0
                if held >= settings.exhibit_max_per_session:
                    raise ExhibitLimit(_limit_message(held))
                try:
                    await cur.execute(
                        _INSERT_HEADER,
                        {
                            "exhibit_id": exhibit_id,
                            "session_id": session_id,
                            "kind": spec.kind,
                            "title": title,
                            "author_kind": author_kind,
                            "author": author,
                            "seen": 1 if author_kind == "agent" else 0,
                            "correlation_id": correlation_id,
                        },
                    )
                except psycopg.errors.UniqueViolation:
                    if not chosen:
                        raise
                    raced = True
                if not raced:
                    await self._insert_revision(
                        cur,
                        exhibit_id,
                        spec,
                        1,
                        author_kind,
                        author,
                        change_note,
                        correlation_id,
                        unverified_figures,
                    )
            if raced:
                await conn.rollback()
            else:
                await conn.commit()
        if raced:
            return _same_session(exhibit_id, session_id, await self.view(session_id, exhibit_id, 1))
        return await self._require_view(session_id, exhibit_id, 1)

    async def append(
        self,
        session_id: str,
        exhibit_id: str,
        *,
        spec: Spec,
        parent_revision: int,
        author_kind: AuthorKind,
        author: str,
        change_note: str = "",
        title: str | None = None,
        correlation_id: str = "",
        unverified_figures: list[str] | None = None,
    ) -> ExhibitView:
        """Store the next revision under the header's row lock."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_LOCK_HEAD, (exhibit_id, session_id))
                row = await cur.fetchone()
                if row is None:
                    raise UnknownExhibit(f"no artefact {exhibit_id!r} in this session")
                head, kind = int(row[0]), str(row[1])
                if parent_revision != head:
                    raise StaleRevision(exhibit_id, head, parent_revision)
                _require_kind(exhibit_id, kind, spec)
                _require_room(exhibit_id, head)
                revision = head + 1
                try:
                    await self._insert_revision(
                        cur,
                        exhibit_id,
                        spec,
                        revision,
                        author_kind,
                        author,
                        change_note,
                        correlation_id,
                        unverified_figures,
                    )
                except psycopg.errors.UniqueViolation as exc:
                    # The backstop for a writer that skipped the lock: the same fact as a stale
                    # base, reaching the writer by another route.
                    raise StaleRevision(exhibit_id, revision, parent_revision) from exc
                await cur.execute(
                    _ADVANCE_HEADER,
                    {
                        "exhibit_id": exhibit_id,
                        "revision": revision,
                        "author_kind": author_kind,
                        "author": author,
                        "title": title,
                    },
                )
            await conn.commit()
        return await self._require_view(session_id, exhibit_id, revision)

    @staticmethod
    async def _insert_revision(
        cur: psycopg.AsyncCursor[TupleRow],
        exhibit_id: str,
        spec: Spec,
        revision: int,
        author_kind: AuthorKind,
        author: str,
        change_note: str,
        correlation_id: str,
        unverified_figures: list[str] | None,
    ) -> None:
        """Write one revision row — the one statement both writes share."""
        await cur.execute(
            _INSERT_REVISION,
            {
                "exhibit_id": exhibit_id,
                "revision": revision,
                "parent": revision - 1,
                "author_kind": author_kind,
                "author": author,
                "change_note": change_note,
                "spec": json_column(spec_json(spec)),
                "byte_size": spec_bytes(spec),
                "unverified": None
                if unverified_figures is None
                else json_column(unverified_figures),
                "correlation_id": correlation_id,
            },
        )

    async def _require_view(self, session_id: str, exhibit_id: str, revision: int) -> ExhibitView:
        """The revision just written, read back as every other read serves it."""
        view = await self.view(session_id, exhibit_id, revision)
        if view is None:  # pragma: no cover - the row was committed by the caller a moment ago
            raise UnknownExhibit(f"{exhibit_id} revision {revision} vanished after it was written")
        return view

    async def view(self, session_id: str, exhibit_id: str, revision: int = 0) -> ExhibitView | None:
        """One revision with the current header, in one statement so the two cannot tear."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    _SELECT_VIEW,
                    {"exhibit_id": exhibit_id, "session_id": session_id, "revision": revision},
                )
                row = await cur.fetchone()
        if row is None:
            return None
        header = _header(row)
        return ExhibitView(
            **header.model_dump(),
            revision=row[10],
            parent_revision=row[11],
            author_kind=row[12],
            author=row[13],
            change_note=row[14],
            revision_created_at=row[15],
            spec=parse_spec(row[16]),
            unverified_figures=list(row[17] or []),
        )

    async def headers(self, session_id: str) -> list[ExhibitHeader]:
        """The session's artefacts, most recently updated first."""
        return [state.header for state in await self.states(session_id)]

    async def states(self, session_id: str) -> list[ExhibitState]:
        """The headers with the agent's read mark and its last revision, in one statement."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT_STATES, (session_id,))
                rows = await cur.fetchall()
        return [
            ExhibitState(
                header=_header(row), agent_seen_revision=row[10], last_agent_revision=row[11]
            )
            for row in rows
        ]

    async def revisions(self, session_id: str, exhibit_id: str) -> list[ExhibitRevision] | None:
        """The history oldest first; an artefact always has revision 1, so empty means unknown."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT_REVISIONS, (exhibit_id, session_id))
                rows = await cur.fetchall()
        if not rows:
            return None
        return [
            ExhibitRevision(
                revision=row[0],
                parent_revision=row[1],
                author_kind=row[2],
                author=row[3],
                change_note=row[4],
                created_at=row[5],
                byte_size=row[6],
            )
            for row in rows
        ]

    async def human_edits(self, session_id: str, exhibit_id: str) -> list[tuple[Spec, Spec | None]]:
        """Each person-authored revision's spec beside its parent's, in one statement."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT_HUMAN_EDITS, (exhibit_id, session_id))
                rows = await cur.fetchall()
        return [
            (parse_spec(row[0]), None if row[1] is None else parse_spec(row[1])) for row in rows
        ]

    async def mark_seen(self, session_id: str, exhibit_id: str, revision: int) -> None:
        """Raise the agent's read mark to `revision`; `GREATEST` keeps it from moving back."""
        async with self._connection() as conn:
            await conn.execute(_MARK_SEEN, (revision, exhibit_id, session_id))
            await conn.commit()

    async def listing_for(self, actor: str, limit: int) -> list[ExhibitHeader]:
        """Artefacts across the sessions `actor` owns or was let into, newest first."""
        async with self._connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_SELECT_FOR_ACTOR, {"actor": actor, "limit": limit})
                rows = await cur.fetchall()
        return [_header(row) for row in rows]


def _header(row: Sequence[Any]) -> ExhibitHeader:
    """An `ExhibitHeader` from the first ten columns of a `_HEADER_COLUMNS` row."""
    return ExhibitHeader(
        exhibit_id=row[0],
        session_id=row[1],
        kind=row[2],
        title=row[3],
        head_revision=row[4],
        head_author_kind=row[5],
        head_author=row[6],
        created_by=row[7],
        created_at=row[8],
        updated_at=row[9],
    )


# A fork's artefacts: each head revision as revision 1 of a new artefact in the child, named after
# the revision it came from. `unnest` pairs every parent id with the id minted for it, so the two
# statements copy exactly the same set and a header never exists without its revision.
#
# **Shifted, not copied, in time** — `session_fork._COPY_MESSAGES`' argument one table over:
# `retention_session_exhibits_days` ages an artefact by `updated_at`, so a verbatim copy of a
# year-old conversation's artefacts would be swept with the fork's first retention pass. One
# interval, so the newest lands at now and the listing keeps its order. `created_at` stays the
# parent's: when the artefact was started is a fact the fork has no business rewriting.
#
# The read mark carries over as "seen" only if the agent had seen the head it copies, so a chemist's
# edit the parent's agent was never told of is told to the fork's.
_FORK_SHIFT = "now() - (SELECT max(updated_at) FROM session_exhibits WHERE session_id = %(parent)s)"
_FORK_HEADERS = f"""
INSERT INTO session_exhibits
    (exhibit_id, session_id, kind, title, head_revision, head_author_kind, head_author,
     agent_seen_revision, created_by, correlation_id, created_at, updated_at)
SELECT m.new_id, %(child)s, e.kind, e.title, 1, r.author_kind, r.author,
       CASE WHEN e.agent_seen_revision >= e.head_revision THEN 1 ELSE 0 END,
       e.created_by, e.correlation_id, e.created_at, e.updated_at + ({_FORK_SHIFT})
FROM unnest(%(old)s::text[], %(new)s::text[]) AS m(old_id, new_id)
JOIN session_exhibits e ON e.exhibit_id = m.old_id AND e.session_id = %(parent)s
JOIN session_exhibit_revisions r ON r.exhibit_id = e.exhibit_id AND r.revision = e.head_revision
"""
_FORK_REVISIONS = f"""
INSERT INTO session_exhibit_revisions
    (exhibit_id, revision, parent_revision, author_kind, author, change_note, spec, byte_size,
     unverified_figures, correlation_id, created_at)
SELECT m.new_id, 1, 0, r.author_kind, r.author,
       'forked from ' || e.exhibit_id || ' r' || e.head_revision, r.spec, r.byte_size,
       r.unverified_figures, r.correlation_id, e.updated_at + ({_FORK_SHIFT})
FROM unnest(%(old)s::text[], %(new)s::text[]) AS m(old_id, new_id)
JOIN session_exhibits e ON e.exhibit_id = m.old_id AND e.session_id = %(parent)s
JOIN session_exhibit_revisions r ON r.exhibit_id = e.exhibit_id AND r.revision = e.head_revision
"""
_PARENT_IDS = "SELECT exhibit_id FROM session_exhibits WHERE session_id = %s ORDER BY exhibit_id"


async def fork_exhibits(cur: psycopg.AsyncCursor[TupleRow], parent_id: str, child_id: str) -> int:
    """Copy `parent_id`'s artefacts into `child_id` on the caller's transaction; return how many.

    Head revision only, as revision 1 of a new id with `change_note` "forked from <xid> r<n>": a
    fork is a branch of the conversation, and the artefact's history is the parent's record of how
    the parent got there — the child starts from where it stands, and the note says from where. The
    revision's own author is kept (who wrote the words), as `session_fork` keeps each message's.

    On a cursor rather than a connection of its own because the fork is one transaction across
    every table it copies (`agent/session_fork.fork_session`), and an artefact copy that could
    commit without the transcript, or fail after it, is the half-fork that module refuses.
    """
    await cur.execute(_PARENT_IDS, (parent_id,))
    old = [str(row[0]) for row in await cur.fetchall()]
    if not old:
        return 0
    names = {"parent": parent_id, "child": child_id, "old": old}
    names["new"] = [new_exhibit_id() for _ in old]
    await cur.execute(_FORK_HEADERS, names)
    await cur.execute(_FORK_REVISIONS, names)
    return len(old)


_IN_MEMORY = InMemoryExhibitStore()


def default_exhibit_store() -> ExhibitStore:
    """The store this deployment uses — Postgres where sessions are durable, memory otherwise.

    The switch the session store, the design store and the audit sink read. Module-level in memory
    for the reason `default_design_store` gives: a store that forgot between two calls is not one.
    """
    if settings.session_store == "postgres":
        return PostgresExhibitStore()
    return _IN_MEMORY
