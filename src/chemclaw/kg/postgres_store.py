"""The note store over `kg_notes`, `kg_edges` and `kg_note_revisions`.

`PostgresNoteStore` answers every `NoteStore` and `CorpusState` question from the tables of
`infra/sql/128_kg_notes.sql` and `129_kg_note_search.sql` with indexed statements: `related` and
`current_successor` look edges up, `neighborhood` is a recursive CTE bounded by its hop count, and
`search` selects on the stored lowercased haystack. Nothing is cached, so a replica sees a write at
once and holds no copy of the corpus. `put` writes a `NoteUnit` in one transaction.

Invariants: every statement names the store's tenant; the write order is the unit's (dependencies,
subject, retirements); a revision row is inserted, never changed; identical content writes no
revision; a note id is ordered bytewise (`COLLATE "C"`), as Python orders it.
"""

import logging
from collections import defaultdict
from collections.abc import AsyncIterator, Collection, Hashable, Sequence
from contextlib import asynccontextmanager
from datetime import date
from typing import Any, NamedTuple

import networkx as nx
import psycopg
from psycopg.rows import TupleRow
from pydantic import ValidationError

from chemclaw.core.config import settings
from chemclaw.core.db import connection
from chemclaw.core.identity_context import get_current_actor
from chemclaw.core.jsonb import json_column
from chemclaw.core.logging import log_event
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.kg.note import Note, Relation
from chemclaw.kg.rows import (
    DEFAULT_TENANT,
    canonical_note,
    edge_columns,
    note_columns,
    note_from_columns,
)
from chemclaw.kg.search import term_coverage
from chemclaw.kg.store import (
    Edge,
    HumanNoteProtected,
    NoteRevisionConflict,
    NoteUnit,
    PutOutcome,
    SearchResult,
)

log = logging.getLogger(__name__)

_NOTE_COLUMNS = "id, type, frontmatter, body, created_by, valid_from, valid_to"
_NOTE_FIELDS = tuple(name.strip() for name in _NOTE_COLUMNS.split(","))

# Note ids are ASCII slugs, and Python sorts them by code point; the database's own collation would
# order `a-b` and `a_b` differently.
_BYTEWISE = 'COLLATE "C"'

_SELECT_NOTE = f"SELECT {_NOTE_COLUMNS} FROM kg_notes WHERE tenant_id = %s AND id = %s"
_SELECT_NOTES = f"SELECT {_NOTE_COLUMNS} FROM kg_notes WHERE tenant_id = %s ORDER BY id {_BYTEWISE}"
_SELECT_SOME = (
    f"SELECT {_NOTE_COLUMNS} FROM kg_notes WHERE tenant_id = %s AND id = ANY(%s) "
    f"ORDER BY id {_BYTEWISE}"
)
_SELECT_FOR_CACHE = (
    f"SELECT {_NOTE_COLUMNS}, revision, updated_at FROM kg_notes WHERE tenant_id = %s "
    f"ORDER BY id {_BYTEWISE}"
)
_SIGNATURE = "SELECT count(*), coalesce(sum(revision), 0) FROM kg_notes WHERE tenant_id = %s"
_ARRIVALS = "SELECT id, (created_at AT TIME ZONE 'UTC')::date FROM kg_notes WHERE tenant_id = %s"
_FINGERPRINTS = "SELECT id, content_sha FROM kg_notes WHERE tenant_id = %s"

# One row: whether the note is defined, and the targets of its edges through one relation, current
# on the date when one is given. `edge.validity @> NULL` is never true, hence the explicit branch.
_RELATED = """
SELECT e.dst
  FROM kg_notes n
  LEFT JOIN kg_edges e
    ON e.tenant_id = n.tenant_id AND e.src = n.id AND e.rel = %(rel)s
   AND (%(as_of)s::date IS NULL OR e.validity @> %(as_of)s::date)
 WHERE n.tenant_id = %(tenant)s AND n.id = %(id)s
"""

# A node is a defined note or a link target some note names.
_IS_A_NODE = """
SELECT EXISTS (SELECT 1 FROM kg_notes WHERE tenant_id = %(tenant)s AND id = %(id)s)
    OR EXISTS (SELECT 1 FROM kg_edges WHERE tenant_id = %(tenant)s AND dst = %(id)s)
"""

# Breadth-first over edges followed both ways. `UNION` drops a (node, depth) pair already reached,
# which ends a cycle; `depth < hops` ends everything else. The lateral union keeps the recursive
# term a single reference to `walk` and lets each arm use its own index (primary key for out-edges,
# `kg_edges_dst_idx` for in-edges).
_NEIGHBORHOOD = """
WITH RECURSIVE walk(node, depth) AS (
    SELECT %(id)s::text, 0
  UNION
    SELECT next.node, walk.depth + 1
      FROM walk
     CROSS JOIN LATERAL (
         SELECT e.dst AS node FROM kg_edges e
          WHERE e.tenant_id = %(tenant)s AND e.src = walk.node
         UNION ALL
         SELECT e.src FROM kg_edges e
          WHERE e.tenant_id = %(tenant)s AND e.dst = walk.node
     ) next
     WHERE walk.depth < %(hops)s
)
SELECT DISTINCT node FROM walk WHERE node <> %(id)s
"""

_EDGES_OF = f"""
SELECT src, dst, rel, valid_from, valid_to, confidence
  FROM kg_edges
 WHERE tenant_id = %(tenant)s AND (src = %(id)s OR dst = %(id)s)
 ORDER BY (src <> %(id)s), src {_BYTEWISE}, dst {_BYTEWISE}, rel {_BYTEWISE}
"""

_ALL_EDGES = f"""
SELECT src, dst, rel, valid_from, valid_to, confidence
  FROM kg_edges
 WHERE tenant_id = %s
 ORDER BY src {_BYTEWISE}, dst {_BYTEWISE}, rel {_BYTEWISE}
"""

# For each node of a frontier, the ids a supersede link names as its replacement from either end,
# with the note each one is, if any: `kg.graph.current_successor`'s `_replaced_by`, one level at a
# time. A link is followed whatever its window.
_REPLACEMENTS = f"""
SELECT f.node, c.cand, {", ".join(f"n.{name}" for name in _NOTE_FIELDS)}
  FROM unnest(%(frontier)s::text[]) AS f(node)
 CROSS JOIN LATERAL (
     SELECT e.dst AS cand FROM kg_edges e
      WHERE e.tenant_id = %(tenant)s AND e.src = f.node AND e.rel = 'superseded-by'
     UNION
     SELECT e.src FROM kg_edges e
      WHERE e.tenant_id = %(tenant)s AND e.dst = f.node AND e.rel = 'supersedes'
 ) c
  LEFT JOIN kg_notes n ON n.tenant_id = %(tenant)s AND n.id = c.cand
 ORDER BY f.node {_BYTEWISE}, c.cand {_BYTEWISE}
"""

# How many notes were current on the date, and those of them that contain any term. One statement,
# so the two agree; `LEFT JOIN ... ON true` keeps the count when nothing matches.
_SEARCH = f"""
SELECT counted.n, {", ".join(f"hit.{name}" for name in _NOTE_FIELDS)}
  FROM (SELECT count(*) AS n FROM kg_notes
         WHERE tenant_id = %(tenant)s AND validity @> %(as_of)s::date) counted
  LEFT JOIN (
      SELECT {_NOTE_COLUMNS} FROM kg_notes
       WHERE tenant_id = %(tenant)s AND validity @> %(as_of)s::date AND ({{any_term}})
  ) hit ON true
 ORDER BY hit.id {_BYTEWISE}
"""

_LOCK_IDS = (
    "SELECT pg_advisory_xact_lock(hashtextextended(%(tenant)s::text || ':' || k, 0)) "
    "FROM unnest(%(ids)s::text[]) AS k"
)
_SELECT_HELD = (
    "SELECT id, revision, content_sha, created_by FROM kg_notes "
    "WHERE tenant_id = %s AND id = ANY(%s)"
)
_INSERT_NOTE = (
    "INSERT INTO kg_notes (tenant_id, id, type, frontmatter, body, created_by, valid_from, "
    "valid_to, revision, content_sha, search_text) "
    "VALUES (%(tenant)s, %(id)s, %(type)s, %(frontmatter)s, %(body)s, %(created_by)s, "
    "%(valid_from)s, %(valid_to)s, %(revision)s, %(content_sha)s, %(search_text)s) "
    "ON CONFLICT (tenant_id, id) DO NOTHING"
)
_UPDATE_NOTE = (
    "UPDATE kg_notes SET type = %(type)s, frontmatter = %(frontmatter)s, body = %(body)s, "
    "created_by = %(created_by)s, valid_from = %(valid_from)s, valid_to = %(valid_to)s, "
    "revision = %(revision)s, content_sha = %(content_sha)s, search_text = %(search_text)s, "
    "updated_at = now() "
    "WHERE tenant_id = %(tenant)s AND id = %(id)s AND revision = %(held)s"
)
_INSERT_REVISION = (
    "INSERT INTO kg_note_revisions (tenant_id, note_id, revision, type, frontmatter, body, "
    "created_by, valid_from, valid_to, content_sha, recorded_by) "
    "VALUES (%(tenant)s, %(id)s, %(revision)s, %(type)s, %(frontmatter)s, %(body)s, "
    "%(created_by)s, %(valid_from)s, %(valid_to)s, %(content_sha)s, %(recorded_by)s)"
)
_DELETE_EDGES = "DELETE FROM kg_edges WHERE tenant_id = %s AND src = %s"
_INSERT_EDGE = (
    "INSERT INTO kg_edges (tenant_id, src, rel, dst, valid_from, valid_to, confidence) "
    "VALUES (%(tenant)s, %(src)s, %(rel)s, %(dst)s, %(valid_from)s, %(valid_to)s, %(confidence)s)"
)


class _Held(NamedTuple):
    """What the store holds for a note a write is about to touch."""

    revision: int
    content_sha: str
    created_by: str


def _like(term: str) -> str:
    """The `LIKE` pattern for "contains `term`", with the pattern's own characters escaped."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _storable(term: str) -> bool:
    """Whether the database can be asked about `term`: no NUL, and encodable as UTF-8.

    No note holds either, so a term that is not storable matches nothing, as it would in a file.
    """
    if "\x00" in term:
        return False
    try:
        term.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _note_or_none(row: Sequence[Any]) -> Note | None:
    """The note a row holds, or `None` (logged and counted) when it fails the schema.

    A reader never raises on one bad row, as with a bad file: `kg-validate` is where it is fixed.
    """
    values = dict(zip(_NOTE_FIELDS, row, strict=False))
    try:
        return note_from_columns(values)
    except ValidationError as exc:
        log.warning("skipping unparseable note %s: %s", values.get("id"), exc)
        record_metric(lambda m: m.increment("chemclaw_notes_unparseable_total"))
        return None


def _relation(row: Sequence[Any]) -> Relation:
    """The `Relation` an edge row `(src, dst, rel, valid_from, valid_to, confidence)` holds."""
    return Relation(rel=row[2], to=row[1], valid_from=row[3], valid_to=row[4], confidence=row[5])


def _grouped(rows: Sequence[Sequence[Any]]) -> list[Edge]:
    """Edge rows as one `Edge` per `(src, dst)`, in row order, with its relations."""
    by_link: dict[tuple[str, str], list[Relation]] = defaultdict(list)
    for row in rows:
        by_link[(row[0], row[1])].append(_relation(row))
    return [Edge(src, dst, tuple(found)) for (src, dst), found in by_link.items()]


def _admit_subject(unit: NoteUnit, note: Note, current: _Held | None) -> None:
    """Refuse a subject that would replace a human's note, or was written from a stale read."""
    if current is not None and current.created_by == "human" and note.created_by == "agent":
        raise HumanNoteProtected(
            f"refusing to write note {note.id!r}: it is authored by a human, and an agent "
            "write may not replace, retire or take the id of curated knowledge. Record a new "
            "note that contradicts or supersedes it instead."
        )
    actual = 0 if current is None else current.revision
    if unit.expected_revision is not None and unit.expected_revision != actual:
        raise NoteRevisionConflict(note.id, unit.expected_revision, actual)


class PostgresNoteStore:
    """`NoteStore` and `CorpusState` over the notes tables, plus `put`, for one tenant.

    Holds no connection and no copy of a note: each call borrows from the process's pool through
    `core.db.connection` on `settings.postgres_dsn`, so constructing one costs nothing and two of
    them agree.
    """

    def __init__(self, tenant_id: str = DEFAULT_TENANT) -> None:
        """Serve the notes of `tenant_id`."""
        self._tenant = tenant_id

    @asynccontextmanager
    async def _read(
        self, *, consistent: bool = False
    ) -> AsyncIterator[psycopg.AsyncConnection[TupleRow]]:
        """A connection for reads; `consistent` makes every statement see one snapshot.

        READ COMMITTED takes a snapshot per statement, which is right for one statement and wrong
        for a read that spans several. The rollback ends any transaction the pool's first-borrow
        bookkeeping opened, because the isolation level can only be set before a query.
        """
        async with connection(settings.postgres_dsn, operation="kg_notes_read") as conn:
            if consistent:
                await conn.rollback()
                await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            yield conn

    # --- NoteStore -------------------------------------------------------------------------

    async def get(self, note_id: str) -> Note | None:
        """The note `note_id` names; a cited-but-undefined id is `None`."""
        async with self._read() as conn:
            cursor = await conn.execute(_SELECT_NOTE, (self._tenant, note_id))
            row = await cursor.fetchone()
        return None if row is None else _note_or_none(row)

    async def get_many(self, note_ids: Collection[str]) -> dict[str, Note | None]:
        """`get` for each id from one statement, so the answers agree with each other."""
        wanted = list(dict.fromkeys(note_ids))
        async with self._read() as conn:
            cursor = await conn.execute(_SELECT_SOME, (self._tenant, wanted))
            rows = await cursor.fetchall()
        found = {row[0]: _note_or_none(row) for row in rows}
        return {note_id: found.get(note_id) for note_id in note_ids}

    async def notes(self) -> list[Note]:
        """Every note that passes the schema, in id order."""
        async with self._read() as conn:
            cursor = await conn.execute(_SELECT_NOTES, (self._tenant,))
            rows = await cursor.fetchall()
        return [note for row in rows if (note := _note_or_none(row)) is not None]

    async def related(self, note_id: str, rel: str, as_of: date | None = None) -> list[str]:
        """The sorted ids `note_id` points at through `rel`; `as_of` applies each edge's window."""
        params = {"tenant": self._tenant, "id": note_id, "rel": rel, "as_of": as_of}
        async with self._read() as conn:
            cursor = await conn.execute(_RELATED, params)
            rows = await cursor.fetchall()
        if not rows:
            raise KeyError(f"unknown note id: {note_id!r}")
        return sorted({row[0] for row in rows if row[0] is not None})

    async def neighborhood(self, note_id: str, hops: int = 1) -> set[str]:
        """Node ids within `hops` of `note_id`, following links both ways, `note_id` excluded."""
        params = {"tenant": self._tenant, "id": note_id, "hops": hops}
        async with self._read(consistent=True) as conn:
            cursor = await conn.execute(_IS_A_NODE, params)
            known = await cursor.fetchone()
            if known is None or not known[0]:
                raise KeyError(f"unknown note id: {note_id!r}")
            if hops < 1:
                return set()
            cursor = await conn.execute(_NEIGHBORHOOD, params)
            return {row[0] for row in await cursor.fetchall()}

    async def edges(self, note_id: str) -> list[Edge]:
        """Every link out of `note_id` and then every link into it; a self-link once."""
        async with self._read() as conn:
            cursor = await conn.execute(_EDGES_OF, {"tenant": self._tenant, "id": note_id})
            rows = await cursor.fetchall()
        return _grouped(rows)

    async def current_successor(self, note_id: str, as_of: date) -> Note | None:
        """The first note current on `as_of` that a chain of supersede links leads to."""
        return (await self.current_successors([note_id], as_of))[note_id]

    async def current_successors(
        self, note_ids: Collection[str], as_of: date
    ) -> dict[str, Note | None]:
        """`current_successor` for each id, one statement per level of the chains for all of them.

        Each id is walked breadth-first in id order, so its answer is the one
        `kg.graph.current_successor` gives; a cycle ends a walk and an id is never its own answer.
        The statements share one snapshot, and their number is the longest chain, not the ids.
        """
        answers: dict[str, Note | None] = dict.fromkeys(note_ids)
        seen = {origin: {origin} for origin in answers}
        frontier = {origin: [origin] for origin in answers}
        async with self._read(consistent=True) as conn:
            while frontier:
                nodes = sorted({node for walk in frontier.values() for node in walk})
                cursor = await conn.execute(
                    _REPLACEMENTS, {"tenant": self._tenant, "frontier": nodes}
                )
                level: dict[str, dict[str, Note | None]] = {}
                for row in await cursor.fetchall():
                    level.setdefault(row[0], {})[row[1]] = (
                        None if row[2] is None else _note_or_none(row[2:])
                    )
                following: dict[str, list[str]] = {}
                for origin, walk in frontier.items():
                    step = self._step(level, walk, seen[origin], as_of)
                    if isinstance(step, Note):
                        answers[origin] = step
                    elif step:
                        following[origin] = step
                frontier = following
        return answers

    @staticmethod
    def _step(
        level: dict[str, dict[str, Note | None]], walk: list[str], seen: set[str], as_of: date
    ) -> Note | list[str]:
        """One level of one walk: the first current replacement, else the nodes to expand next."""
        following: list[str] = []
        for current in walk:
            for candidate, note in level.get(current, {}).items():
                if candidate in seen:
                    continue
                seen.add(candidate)
                if note is not None and note.is_current(as_of):
                    return note
                following.append(candidate)
        return following

    async def search(self, terms: Sequence[str], as_of: date) -> SearchResult:
        """The notes current on `as_of` that contain at least one of `terms`, in id order.

        The database selects the candidates by substring on the stored haystack; coverage is then
        counted by `kg.search.term_coverage`, the one definition, so a note is a hit here exactly
        when it is in the git store.
        """
        patterns = [_like(term) for term in terms if _storable(term)]
        params: dict[str, Any] = {"tenant": self._tenant, "as_of": as_of}
        clause = " OR ".join(f"search_text LIKE %(p{i})s" for i in range(len(patterns))) or "false"
        params.update({f"p{i}": pattern for i, pattern in enumerate(patterns)})
        async with self._read() as conn:
            cursor = await conn.execute(_SEARCH.format(any_term=clause), params)
            rows = await cursor.fetchall()
        matches: list[tuple[int, Note]] = []
        for row in rows:
            if row[1] is None:
                continue
            note = _note_or_none(row[1:])
            if note is not None and (coverage := term_coverage(note, terms)):
                matches.append((coverage, note))
        return SearchResult(searched=int(rows[0][0]), matches=matches)

    async def graph(self) -> nx.DiGraph:
        """The whole note graph, built from both tables in one snapshot, frozen."""
        async with self._read(consistent=True) as conn:
            notes = await conn.execute(_SELECT_NOTES, (self._tenant,))
            note_rows = await notes.fetchall()
            edges = await conn.execute(_ALL_EDGES, (self._tenant,))
            edge_rows = await edges.fetchall()
        graph: nx.DiGraph = nx.DiGraph()
        for row in note_rows:
            if (note := _note_or_none(row)) is not None:
                graph.add_node(note.id, note=note)
        for edge in _grouped(edge_rows):
            graph.add_edge(edge.src, edge.dst, relations=edge.relations)
        return nx.freeze(graph)

    # --- CorpusState -----------------------------------------------------------------------

    async def snapshot(self) -> tuple[Hashable | None, list[Note]]:
        """The notes and a token that changes whenever any of them does, from one statement.

        The token is the note count, the sum of the revisions and the newest `updated_at`. The sum
        only grows, so an edit moves it even if two writers' clocks disagree.
        """
        async with self._read() as conn:
            cursor = await conn.execute(_SELECT_FOR_CACHE, (self._tenant,))
            rows = await cursor.fetchall()
        newest = max((row[8] for row in rows), default=None)
        token = (len(rows), sum(row[7] for row in rows), newest)
        return token, [note for row in rows if (note := _note_or_none(row)) is not None]

    async def signature(self) -> tuple[int, int]:
        """`(note count, sum of revisions)`: cheap, and every add or edit moves one of them."""
        async with self._read() as conn:
            cursor = await conn.execute(_SIGNATURE, (self._tenant,))
            row = await cursor.fetchone()
        return (0, 0) if row is None else (int(row[0]), int(row[1]))

    async def arrivals(self) -> dict[str, date]:
        """Note id to the UTC date the note first reached the store."""
        async with self._read() as conn:
            cursor = await conn.execute(_ARRIVALS, (self._tenant,))
            return {row[0]: row[1] for row in await cursor.fetchall()}

    async def fingerprints(self) -> dict[str, str]:
        """Note id to `content_sha`, the digest of its canonical render."""
        async with self._read() as conn:
            cursor = await conn.execute(_FINGERPRINTS, (self._tenant,))
            return {row[0]: row[1] for row in await cursor.fetchall()}

    async def unparseable(self, note_ids: Collection[str]) -> list[str]:
        """The ids among `note_ids` whose row fails the schema, in id order."""
        if not note_ids:
            return []
        async with self._read() as conn:
            cursor = await conn.execute(_SELECT_SOME, (self._tenant, list(note_ids)))
            rows = await cursor.fetchall()
        return [row[0] for row in rows if _note_or_none(row) is None]

    async def revision(self) -> int | None:
        """`None`: one database has no replica that is behind another."""
        return None

    async def refresh(self, *, reparse: bool = False) -> None:
        """Nothing to forget: no read is cached."""

    # --- Writing ---------------------------------------------------------------------------

    async def put(self, unit: NoteUnit) -> PutOutcome:
        """Write `unit` in one transaction: dependencies, then the subject, then retirements.

        Each note is stored as its redacted canonical render reads back, with a new revision row.
        A dependency that exists is left alone. A subject or retirement whose content is identical
        to what is stored writes nothing, so a repeat is a no-op. A retirement of a human's note is
        left alone and logged, and the subject still lands. The subject is refused
        (`HumanNoteProtected`) when it is agent-authored and a human's note holds the id, and
        `expected_revision` that differs from the stored one raises `NoteRevisionConflict`. Either
        raises before any row changes: the transaction rolls back whole.

        Writers of the same ids queue on a transaction-scoped advisory lock taken in id order, so
        two units that share a note neither interleave nor deadlock.
        """
        planned = [(role, canonical_note(note)) for role, note in unit.ordered()]
        ids = sorted({note.id for _, note in planned})
        dependencies = {note.id for role, note in planned if role == "dependency"}
        recorded_by = get_current_actor()
        revisions: dict[str, int] = {}
        written: list[str] = []
        async with connection(settings.postgres_dsn, operation="kg_notes_write") as conn:
            # A dependency that exists is never written, so it needs no lock: taking one would make
            # every writer that names a shared compound queue behind the others.
            lock = ids
            if unit.dependencies:
                cursor = await conn.execute(_SELECT_HELD, (self._tenant, ids))
                present = {row[0] for row in await cursor.fetchall()}
                lock = [i for i in ids if i not in present or i not in dependencies]
            await conn.execute(_LOCK_IDS, {"tenant": self._tenant, "ids": lock})
            cursor = await conn.execute(_SELECT_HELD, (self._tenant, ids))
            held = {row[0]: _Held(row[1], row[2], row[3]) for row in await cursor.fetchall()}
            for role, note in planned:
                current = held.get(note.id)
                if current is not None:
                    revisions[note.id] = current.revision
                row = note_columns(note)
                if role == "dependency" and current is not None:
                    continue
                if role == "subject":
                    _admit_subject(unit, note, current)
                if role == "retirement" and current is not None and current.created_by == "human":
                    log_event(
                        log,
                        "kg.write.amendment_left_alone",
                        "left %s alone: it is a human's note, so it is not retired in place — the "
                        "note recorded alongside it still lands and marks it as contradicted",
                        note.id,
                        level=logging.WARNING,
                        note_id=note.id,
                    )
                    continue
                if current is not None and current.content_sha == row["content_sha"]:
                    continue
                revision = 1 if current is None else current.revision + 1
                await self._write(conn, note, row, current, revision, recorded_by)
                revisions[note.id] = revision
                written.append(note.id)
        return PutOutcome(subject=unit.subject.id, revisions=revisions, written=tuple(written))

    async def _write(
        self,
        conn: psycopg.AsyncConnection[TupleRow],
        note: Note,
        row: dict[str, Any],
        current: _Held | None,
        revision: int,
        recorded_by: str | None,
    ) -> None:
        """Insert or update the note's row, append its revision, and re-derive its edges."""
        values = {
            "tenant": self._tenant,
            **row,
            "frontmatter": json_column(row["frontmatter"]),
            "revision": revision,
            "held": 0 if current is None else current.revision,
        }
        cursor = await conn.execute(_UPDATE_NOTE if current else _INSERT_NOTE, values)
        if cursor.rowcount != 1:
            # A writer that did not take the advisory lock got in between it and this statement.
            latest = await (await conn.execute(_SELECT_HELD, (self._tenant, [note.id]))).fetchone()
            raise NoteRevisionConflict(note.id, values["held"], 0 if latest is None else latest[1])
        await conn.execute(_INSERT_REVISION, {**values, "recorded_by": recorded_by})
        if current is not None:
            await conn.execute(_DELETE_EDGES, (self._tenant, note.id))
        edges = edge_columns(note)
        if edges:
            async with conn.cursor() as cur:
                await cur.executemany(
                    _INSERT_EDGE, [{"tenant": self._tenant, **edge} for edge in edges]
                )
