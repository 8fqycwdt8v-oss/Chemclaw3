"""The note store: what the readers of the knowledge graph ask, and the git-backed answer.

`NoteStore` is the read interface to notes. Readers (`agent`, `retrieval`, `durable`, `memory`,
`evals`) call `note_store()` rather than `kg.graph`, so the backing store is a setting
(`kg_store`), not a property of the call site. `GitNoteStore` answers from the Markdown tree by
delegation and, through the injected `NoteWriter`, its writes. `kg.record.record_note` is the one
write path; `NoteUnit` is the value `WritableNoteStore.put` takes, in the order `record` writes
files. A reader asks for `NoteStore`, so a read-only caller cannot write by type.

Reads are coroutines, so a database store can use the process's one async pool. `GitNoteStore` does
its file and graph work in a worker thread and never blocks the event loop. `CorpusState` is the
part a cache or an index needs, namely whether the corpus moved and what each note's content digest
is; a note is never read through it.
"""

import asyncio
from collections.abc import Collection, Hashable, Sequence
from dataclasses import dataclass
from datetime import date
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple, Protocol, assert_never

import networkx as nx

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.kg import graph as corpus
from chemclaw.kg.note import Note, NoteError, Relation, read_note
from chemclaw.kg.search import term_coverage

if TYPE_CHECKING:
    from chemclaw.kg.postgres_store import PostgresNoteStore
    from chemclaw.kg.record import NoteWriter


class SearchResult(NamedTuple):
    """What a term search over the current notes found.

    `searched` is how many notes were current on the date asked, whether or not they matched, so a
    caller can tell an empty corpus from a miss. `matches` is `(coverage, note)` for every note that
    matches at least one term, in note id order; `coverage` is how many of the terms matched.
    """

    searched: int
    matches: list[tuple[int, Note]]


class Edge(NamedTuple):
    """The typed relations a note asserts about another, as one link of the graph.

    All of them, whatever their windows: whether an edge holds on a date is the caller's question
    (`Relation.is_current`).
    """

    src: str
    dst: str
    relations: tuple[Relation, ...]


class NoteWriteError(ChemclawError):
    """A write the store refuses because of what it would do to the notes, not because it broke."""


class HumanNoteProtected(NoteWriteError):
    """An agent-authored note would replace a note a human wrote.

    Record a new note that contradicts or supersedes it instead.
    """


class NoteRevisionConflict(NoteWriteError):
    """The note changed since the revision the writer read: re-read it and decide again.

    `expected` is the revision the writer named (0 for "must not exist yet") and `actual` the one
    the store holds (0 when the note is absent).
    """

    def __init__(self, note_id: str, expected: int, actual: int) -> None:
        """Name the note and the two revisions."""
        super().__init__(
            f"note {note_id!r} is at revision {actual}, not the {expected} this write was based "
            "on; it was changed by someone else. Re-read it and apply the change again"
        )
        self.note_id = note_id
        self.expected = expected
        self.actual = actual


#: Which part of a unit a note is: written before the subject so its links resolve, the subject,
#: or a note closed because the subject replaces it.
Role = Literal["dependency", "subject", "retirement"]


@dataclass(frozen=True)
class NoteUnit:
    """One note and everything that must land with it, in the order it lands.

    The order is dependencies, then the subject, then retirements, each citing the one before
    (`kg.record`). A note id appears once, at its first role. `expected_revision` makes the
    subject's write conditional on the revision the writer read: `None` writes whatever is there,
    `0` requires the note to be new, `n` requires revision `n`. `arrived` dates the subject's first
    appearance when it is imported rather than recorded now; a store that keeps no arrival date of
    its own (the tree dates a note by its commit) ignores it.
    """

    subject: Note
    dependencies: tuple[Note, ...] = ()
    retirements: tuple[Note, ...] = ()
    expected_revision: int | None = None
    arrived: date | None = None

    def ordered(self) -> list[tuple[Role, Note]]:
        """The notes in write order, each id once, with the role it is written in."""
        seen = {self.subject.id}
        members: list[tuple[Role, Note]] = []
        for dependency in self.dependencies:
            if dependency.id not in seen:
                seen.add(dependency.id)
                members.append(("dependency", dependency))
        members.append(("subject", self.subject))
        for retired in self.retirements:
            if retired.id not in seen:
                seen.add(retired.id)
                members.append(("retirement", retired))
        return members


class PutOutcome(NamedTuple):
    """What a write did: the revision every note now holds, and which ones it just wrote.

    `written` lists the ids that received a new revision, in write order. A note written with
    identical content, a dependency that was already there and a retirement left alone are not in
    it, so an idempotent repeat has an empty `written`.
    """

    subject: str
    revisions: dict[str, int]
    written: tuple[str, ...]

    @property
    def notes(self) -> int:
        """1 when the subject received a revision, else 0: the count `record` books as recorded."""
        return int(self.subject in self.written)


class CorpusState(Protocol):
    """What a cache or an index asks of the corpus without reading a note's content."""

    async def snapshot(self) -> tuple[Hashable | None, list[Note]]:
        """Every note with a token that changes whenever any of them does.

        The token is `None` when the store is not caching, which tells a caller there is nothing to
        key its own cache on. Token and notes come from one read, so they cannot disagree.
        """
        ...

    async def signature(self) -> tuple[int, int]:
        """`(note count, newest change marker)`: cheap, and changes on any add, edit or delete."""
        ...

    async def arrivals(self) -> dict[str, date]:
        """Note id to the date it reached the corpus; empty when the store cannot say."""
        ...

    async def fingerprints(self) -> dict[str, str]:
        """Note id to `sha256:<hex>` of its content, for every note the store holds.

        A note that exists but cannot be read keeps an entry, so an index does not retire it. What
        is hashed is the store's own: file bytes for the git store, the canonical render for a
        database one, so values are comparable within a store and never across two.
        """
        ...

    async def unparseable(self, note_ids: Collection[str]) -> list[str]:
        """Which of `note_ids` hold a note that fails the schema, as opposed to no note at all."""
        ...

    async def revision(self) -> int | None:
        """A number that never goes down as the corpus grows, or `None` when it has none to give."""
        ...

    async def refresh(self, *, reparse: bool = False) -> None:
        """Forget what was read so the next read sees the store as it is now.

        `reparse` also drops the parsed copies, for a caller pairing content digests with bodies.
        """
        ...


class NoteStore(CorpusState, Protocol):
    """The knowledge graph's notes: read by id, by relation, by neighbourhood and by search.

    An id the store does not define is `None` from `get` and `current_successor`, and a `KeyError`
    from `related`, which cannot answer for it. Dangling link targets are nodes in `neighborhood()`
    and `edges()`, and never notes: `neighborhood` of an id no note defines and no note cites is a
    `KeyError`, and of one that is cited is the notes that cite it.
    """

    async def get(self, note_id: str) -> Note | None:
        """The note `note_id` names, or `None`."""
        ...

    async def get_many(self, note_ids: Collection[str]) -> dict[str, Note | None]:
        """`get` for each id from one read of the corpus, so the answers agree with each other.

        Reads the corpus even when `note_ids` is empty, so an unreadable corpus fails here too.
        """
        ...

    async def notes(self) -> list[Note]:
        """Every readable note; a note that fails the schema is skipped, not raised."""
        ...

    async def related(self, note_id: str, rel: str, as_of: date | None = None) -> list[str]:
        """The sorted ids `note_id` points at through `rel`; `as_of` applies each edge's window."""
        ...

    async def neighborhood(self, note_id: str, hops: int = 1) -> set[str]:
        """Node ids within `hops` of `note_id`, following links both ways, `note_id` excluded.

        Empty for `hops` below 1. The store applies no ceiling of its own; callers bound `hops`
        (`graph_max_hops`).
        """
        ...

    async def edges(self, note_id: str) -> list[Edge]:
        """Every link into or out of `note_id` with the relations on it; empty for an unlinked id.

        Not an error for an id nothing mentions: a compound id derived from a structure may be
        cited by notes and defined by none.
        """
        ...

    async def current_successor(self, note_id: str, as_of: date) -> Note | None:
        """The first note current on `as_of` that a chain of supersede links leads to."""
        ...

    async def current_successors(
        self, note_ids: Collection[str], as_of: date
    ) -> dict[str, Note | None]:
        """`current_successor` for each id from one read of the corpus, so the answers agree."""
        ...

    async def search(self, terms: Sequence[str], as_of: date) -> SearchResult:
        """The notes current on `as_of` that contain at least one of `terms`, as substrings."""
        ...

    async def graph(self) -> nx.DiGraph:
        """The whole note graph, for analytics that need a whole-graph algorithm and nothing else.

        A request reads through the methods above. Shared and frozen; copy it to change it.
        """
        ...


class WritableNoteStore(NoteStore, Protocol):
    """A `NoteStore` that can also write a `NoteUnit`: both stores `kg_store` selects.

    Writing is not on `NoteStore` because readers are the great majority of callers and must not be
    able to write; `kg.record.record_note` is the one caller that does.
    """

    async def put(self, unit: NoteUnit) -> PutOutcome:
        """Write `unit` (dependencies, then the subject, then retirements) and say what changed.

        Idempotent: a repeat of the same unit changes nothing and reports an empty `written`.
        """
        ...


class GitNoteStore:
    """`NoteStore` over a directory of Markdown notes.

    Each method runs its synchronous twin (`_get` for `get`, and so on) in a worker thread, because
    the twin may scan or parse the disk and takes `kg.graph`'s blocking locks. The twins delegate to
    `kg.graph`, behind its stat-fingerprint caches. The store holds no state beyond the directory,
    so it is cheap to make per call and two of them over one directory share the module's caches.
    """

    def __init__(self, notes_dir: Path, writer: "NoteWriter | None" = None) -> None:
        """Serve the notes under `notes_dir`; `put` writes through `writer` (default: git's)."""
        self._dir = notes_dir
        self._writer = writer

    async def put(self, unit: NoteUnit) -> PutOutcome:
        """Write `unit` as the file writer does: one commit, files in the unit's order.

        The files land under `settings.knowledge_dir` of the writer's checkout, which is the tree
        this store reads when it is the configured one. The tree keeps no revision numbers, so
        `revisions` is empty and `expected_revision` is refused rather than ignored.
        """
        from chemclaw.kg.git_writer import default_writer
        from chemclaw.kg.record import _build_write

        if unit.expected_revision is not None:
            raise NoteWriteError(
                "the git store keeps no revisions, so a write cannot be conditional on one"
            )
        write = _build_write(
            unit.subject, settings.knowledge_dir, list(unit.dependencies), list(unit.retirements)
        )
        outcome = await (self._writer or default_writer()).write(write)
        written = (unit.subject.id,) if outcome.notes else ()
        return PutOutcome(subject=unit.subject.id, revisions={}, written=written)

    async def get(self, note_id: str) -> Note | None:
        """The note `note_id` names; a cited-but-undefined id is `None`, not a bare node."""
        return await asyncio.to_thread(self._get, note_id)

    def _get(self, note_id: str) -> Note | None:
        return corpus.note_in(corpus.build_graph(self._dir), note_id)

    async def get_many(self, note_ids: Collection[str]) -> dict[str, Note | None]:
        """One graph read, then a lookup per id."""
        return await asyncio.to_thread(self._get_many, note_ids)

    def _get_many(self, note_ids: Collection[str]) -> dict[str, Note | None]:
        graph = corpus.build_graph(self._dir)
        return {note_id: corpus.note_in(graph, note_id) for note_id in note_ids}

    async def notes(self) -> list[Note]:
        """Every parseable note under the directory, cached behind its stat fingerprint."""
        return await asyncio.to_thread(self._notes)

    def _notes(self) -> list[Note]:
        return corpus.load_notes(self._dir)

    async def related(self, note_id: str, rel: str, as_of: date | None = None) -> list[str]:
        """See `kg.graph.related`."""
        return await asyncio.to_thread(self._related, note_id, rel, as_of)

    def _related(self, note_id: str, rel: str, as_of: date | None) -> list[str]:
        return corpus.related(corpus.build_graph(self._dir), note_id, rel, as_of)

    async def neighborhood(self, note_id: str, hops: int = 1) -> set[str]:
        """See `kg.graph.neighborhood`."""
        return await asyncio.to_thread(self._neighborhood, note_id, hops)

    def _neighborhood(self, note_id: str, hops: int) -> set[str]:
        return corpus.neighborhood(corpus.build_graph(self._dir), note_id, hops=hops)

    async def edges(self, note_id: str) -> list[Edge]:
        """The graph's out-edges of `note_id` followed by its in-edges; a self-link once."""
        return await asyncio.to_thread(self._edges, note_id)

    def _edges(self, note_id: str) -> list[Edge]:
        graph = corpus.build_graph(self._dir)
        if note_id not in graph:
            return []
        found: dict[tuple[str, str], Edge] = {}
        for source, target, data in [
            *graph.out_edges(note_id, data=True),
            *graph.in_edges(note_id, data=True),
        ]:
            found.setdefault((source, target), Edge(source, target, tuple(data["relations"])))
        return list(found.values())

    async def current_successor(self, note_id: str, as_of: date) -> Note | None:
        """See `kg.graph.current_successor`."""
        return await asyncio.to_thread(self._current_successor, note_id, as_of)

    def _current_successor(self, note_id: str, as_of: date) -> Note | None:
        return corpus.current_successor(corpus.build_graph(self._dir), note_id, as_of)

    async def current_successors(
        self, note_ids: Collection[str], as_of: date
    ) -> dict[str, Note | None]:
        """One graph read, then a successor walk per id."""
        return await asyncio.to_thread(self._current_successors, note_ids, as_of)

    def _current_successors(self, note_ids: Collection[str], as_of: date) -> dict[str, Note | None]:
        graph = corpus.build_graph(self._dir)
        return {note_id: corpus.current_successor(graph, note_id, as_of) for note_id in note_ids}

    async def search(self, terms: Sequence[str], as_of: date) -> SearchResult:
        """Substring-match `terms` over the notes current on `as_of`, in note id order.

        Discovery serves current evidence only: a not-yet-valid or expired note is not surfaced as
        current fact, and stays reachable by explicit id.
        """
        return await asyncio.to_thread(self._search, terms, as_of)

    def _search(self, terms: Sequence[str], as_of: date) -> SearchResult:
        matches: list[tuple[int, Note]] = []
        searched = 0
        for note in sorted(self._notes(), key=lambda candidate: candidate.id):
            if not note.is_current(as_of):
                continue
            searched += 1
            coverage = term_coverage(note, terms)
            if coverage:
                matches.append((coverage, note))
        return SearchResult(searched=searched, matches=matches)

    async def graph(self) -> nx.DiGraph:
        """The assembled graph, cached and frozen by `kg.graph.build_graph`."""
        return await asyncio.to_thread(corpus.build_graph, self._dir)

    async def snapshot(self) -> tuple[Hashable | None, list[Note]]:
        """The notes and the stat fingerprint they were parsed at."""
        return await asyncio.to_thread(corpus.cached_notes, self._dir)

    async def signature(self) -> tuple[int, int]:
        """`(note files, newest mtime in ns)`, from a stat of each file and no parse."""
        return await asyncio.to_thread(self._signature)

    def _signature(self) -> tuple[int, int]:
        count = 0
        newest = 0
        for _, stat in corpus.scan_notes_dir(self._dir):
            count += 1
            newest = max(newest, stat.st_mtime_ns)
        return count, newest

    async def arrivals(self) -> dict[str, date]:
        """The date each note's file was first committed; see `kg.graph.note_arrivals`."""
        return await asyncio.to_thread(corpus.note_arrivals, self._dir)

    async def fingerprints(self) -> dict[str, str]:
        """Per-file content digests; see `kg.graph.note_file_fingerprints`."""
        return await asyncio.to_thread(corpus.note_file_fingerprints, self._dir)

    async def unparseable(self, note_ids: Collection[str]) -> list[str]:
        """The files named by `note_ids` that hold a note failing the schema.

        Re-reads each with `read_note`, the one definition of both, so a frontmatter-less
        `README.md` raises no alarm.
        """
        return await asyncio.to_thread(self._unparseable, note_ids)

    def _unparseable(self, note_ids: Collection[str]) -> list[str]:
        wanted = set(note_ids)
        if not wanted:
            return []
        unparsed: list[str] = []
        seen: set[str] = set()
        for path, _ in corpus.scan_notes_dir(self._dir):
            if path.stem not in wanted or path.stem in seen:
                continue
            seen.add(path.stem)
            try:
                read_note(path)
            except NoteError:
                unparsed.append(path.stem)
        return unparsed

    async def revision(self) -> int | None:
        """The checkout's commit count; see `kg.graph.corpus_revision`."""
        return await asyncio.to_thread(corpus.corpus_revision, self._dir)

    async def refresh(self, *, reparse: bool = False) -> None:
        """Drop the cached scan so the next read looks at the directory again."""
        await asyncio.to_thread(partial(corpus.invalidate_cache, self._dir, reparse=reparse))


def postgres_note_store() -> "PostgresNoteStore":
    """The Postgres store of this deployment's tenant: where `dual` and `postgres` write."""
    from chemclaw.kg.postgres_store import PostgresNoteStore

    return PostgresNoteStore()


def note_store(notes_dir: Path | None = None) -> NoteStore:
    """The store `settings.kg_store` selects for reading.

    `dual` reads the tree, which stays the authority until the reads flip. `notes_dir` points the
    git store at another tree, for a fixture or a second checkout. A store that does not keep notes
    in a directory ignores it.
    """
    match settings.kg_store:
        case "git" | "dual":
            return GitNoteStore(notes_dir if notes_dir is not None else settings.knowledge_path)
        case "postgres":
            return postgres_note_store()
        case _:
            assert_never(settings.kg_store)
