"""The note store: what the readers of the knowledge graph ask, and the git-backed answer.

`NoteStore` is the read interface to notes. Readers (`agent`, `retrieval`, `durable`, `memory`,
`evals`) call `note_store()` rather than `kg.graph`, so the backing store is a setting
(`kg_store`), not a property of the call site. `GitNoteStore` answers from the Markdown tree by
delegation only. Writes are not here: `kg.record.record_note` is the one write path.

Reads are synchronous because their callers already run them in a worker thread. `CorpusState` is
the part a cache or an index needs, namely whether the corpus moved and what each note's content
digest is; a note is never read through it.
"""

from collections.abc import Collection, Hashable, Sequence
from datetime import date
from pathlib import Path
from typing import NamedTuple, Protocol, assert_never

import networkx as nx

from chemclaw.core.config import settings
from chemclaw.kg import graph as corpus
from chemclaw.kg.note import Note, NoteError, Relation, read_note
from chemclaw.kg.search import term_coverage


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


class CorpusState(Protocol):
    """What a cache or an index asks of the corpus without reading a note's content."""

    def snapshot(self) -> tuple[Hashable | None, list[Note]]:
        """Every note with a token that changes whenever any of them does.

        The token is `None` when the store is not caching, which tells a caller there is nothing to
        key its own cache on. Token and notes come from one read, so they cannot disagree.
        """
        ...

    def signature(self) -> tuple[int, int]:
        """`(note count, newest change marker)`: cheap, and changes on any add, edit or delete."""
        ...

    def arrivals(self) -> dict[str, date]:
        """Note id to the date it reached the corpus; empty when the store cannot say."""
        ...

    def fingerprints(self) -> dict[str, str]:
        """Note id to `sha256:<hex>` of its content, for every note the store holds.

        A note that exists but cannot be read keeps an entry, so an index does not retire it. What
        is hashed is the store's own: file bytes for the git store, the canonical render for a
        database one, so values are comparable within a store and never across two.
        """
        ...

    def unparseable(self, note_ids: Collection[str]) -> list[str]:
        """Which of `note_ids` hold a note that fails the schema, as opposed to no note at all."""
        ...

    def revision(self) -> int | None:
        """A number that never goes down as the corpus grows, or `None` when it has none to give."""
        ...

    def refresh(self, *, reparse: bool = False) -> None:
        """Forget what was read so the next read sees the store as it is now.

        `reparse` also drops the parsed copies, for a caller pairing content digests with bodies.
        """
        ...


class NoteStore(CorpusState, Protocol):
    """The knowledge graph's notes: read by id, by relation, by neighbourhood and by search.

    An id the store does not define is `None` from `get` and `current_successor`, and a `KeyError`
    from `related` and `neighborhood`, which cannot answer for it. Dangling link targets are nodes
    in `neighborhood()` and `edges()`, and never notes.
    """

    def get(self, note_id: str) -> Note | None:
        """The note `note_id` names, or `None`."""
        ...

    def get_many(self, note_ids: Collection[str]) -> dict[str, Note | None]:
        """`get` for each id from one read of the corpus, so the answers agree with each other.

        Reads the corpus even when `note_ids` is empty, so an unreadable corpus fails here too.
        """
        ...

    def notes(self) -> list[Note]:
        """Every readable note; a note that fails the schema is skipped, not raised."""
        ...

    def related(self, note_id: str, rel: str, as_of: date | None = None) -> list[str]:
        """The sorted ids `note_id` points at through `rel`; `as_of` applies each edge's window."""
        ...

    def neighborhood(self, note_id: str, hops: int = 1) -> set[str]:
        """Node ids within `hops` of `note_id`, following links both ways, `note_id` excluded."""
        ...

    def edges(self, note_id: str) -> list[Edge]:
        """Every link into or out of `note_id` with the relations on it; empty for an unlinked id.

        Not an error for an id nothing mentions: a compound id derived from a structure may be
        cited by notes and defined by none.
        """
        ...

    def current_successor(self, note_id: str, as_of: date) -> Note | None:
        """The first note current on `as_of` that a chain of supersede links leads to."""
        ...

    def search(self, terms: Sequence[str], as_of: date) -> SearchResult:
        """The notes current on `as_of` that contain at least one of `terms`, as substrings."""
        ...

    def graph(self) -> nx.DiGraph:
        """The whole note graph, for analytics that need a whole-graph algorithm and nothing else.

        A request reads through the methods above. Shared and frozen; copy it to change it.
        """
        ...


class GitNoteStore:
    """`NoteStore` over a directory of Markdown notes.

    Every method delegates to `kg.graph`, behind its stat-fingerprint caches. It holds no state
    beyond the directory, so it is cheap to make per call and two of them over one directory share
    the module's caches.
    """

    def __init__(self, notes_dir: Path) -> None:
        """Serve the notes under `notes_dir`."""
        self._dir = notes_dir

    def get(self, note_id: str) -> Note | None:
        """The note `note_id` names; a cited-but-undefined id is `None`, not a bare node."""
        return corpus.note_in(corpus.build_graph(self._dir), note_id)

    def get_many(self, note_ids: Collection[str]) -> dict[str, Note | None]:
        """One graph read, then a lookup per id."""
        graph = corpus.build_graph(self._dir)
        return {note_id: corpus.note_in(graph, note_id) for note_id in note_ids}

    def notes(self) -> list[Note]:
        """Every parseable note under the directory, cached behind its stat fingerprint."""
        return corpus.load_notes(self._dir)

    def related(self, note_id: str, rel: str, as_of: date | None = None) -> list[str]:
        """See `kg.graph.related`."""
        return corpus.related(corpus.build_graph(self._dir), note_id, rel, as_of)

    def neighborhood(self, note_id: str, hops: int = 1) -> set[str]:
        """See `kg.graph.neighborhood`."""
        return corpus.neighborhood(corpus.build_graph(self._dir), note_id, hops=hops)

    def edges(self, note_id: str) -> list[Edge]:
        """The graph's out-edges of `note_id` followed by its in-edges; a self-link once."""
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

    def current_successor(self, note_id: str, as_of: date) -> Note | None:
        """See `kg.graph.current_successor`."""
        return corpus.current_successor(corpus.build_graph(self._dir), note_id, as_of)

    def search(self, terms: Sequence[str], as_of: date) -> SearchResult:
        """Substring-match `terms` over the notes current on `as_of`, in note id order.

        Discovery serves current evidence only: a not-yet-valid or expired note is not surfaced as
        current fact, and stays reachable by explicit id.
        """
        matches: list[tuple[int, Note]] = []
        searched = 0
        for note in sorted(self.notes(), key=lambda candidate: candidate.id):
            if not note.is_current(as_of):
                continue
            searched += 1
            coverage = term_coverage(note, terms)
            if coverage:
                matches.append((coverage, note))
        return SearchResult(searched=searched, matches=matches)

    def graph(self) -> nx.DiGraph:
        """The assembled graph, cached and frozen by `kg.graph.build_graph`."""
        return corpus.build_graph(self._dir)

    def snapshot(self) -> tuple[Hashable | None, list[Note]]:
        """The notes and the stat fingerprint they were parsed at."""
        return corpus.cached_notes(self._dir)

    def signature(self) -> tuple[int, int]:
        """`(note files, newest mtime in ns)`, from a stat of each file and no parse."""
        count = 0
        newest = 0
        for _, stat in corpus.scan_notes_dir(self._dir):
            count += 1
            newest = max(newest, stat.st_mtime_ns)
        return count, newest

    def arrivals(self) -> dict[str, date]:
        """The date each note's file was first committed; see `kg.graph.note_arrivals`."""
        return corpus.note_arrivals(self._dir)

    def fingerprints(self) -> dict[str, str]:
        """Per-file content digests; see `kg.graph.note_file_fingerprints`."""
        return corpus.note_file_fingerprints(self._dir)

    def unparseable(self, note_ids: Collection[str]) -> list[str]:
        """The files named by `note_ids` that hold a note failing the schema.

        Re-reads each with `read_note`, the one definition of both, so a frontmatter-less
        `README.md` raises no alarm.
        """
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

    def revision(self) -> int | None:
        """The checkout's commit count; see `kg.graph.corpus_revision`."""
        return corpus.corpus_revision(self._dir)

    def refresh(self, *, reparse: bool = False) -> None:
        """Drop the cached scan so the next read looks at the directory again."""
        corpus.invalidate_cache(self._dir, reparse=reparse)


def note_store(notes_dir: Path | None = None) -> NoteStore:
    """The store `settings.kg_store` selects.

    `notes_dir` points the git store at another tree, for a fixture or a second checkout. A store
    that does not keep notes in a directory ignores it.
    """
    match settings.kg_store:
        case "git":
            return GitNoteStore(notes_dir if notes_dir is not None else settings.knowledge_path)
        case _:
            assert_never(settings.kg_store)
