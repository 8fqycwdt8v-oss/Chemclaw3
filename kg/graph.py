"""NetworkX index of the knowledge graph (plan step 2.3).

Builds a directed graph from a directory of notes: nodes are note ids (each
carrying its parsed `Note`), edges are `[[wikilink]]` relations. Retrieval is
graph traversal (D-004), so this indexer is the substrate the query skill walks
(1–2 hops), not a vector index.
"""

import threading
from pathlib import Path

import networkx as nx

from chemclaw.config import settings
from kg.note import Note, NoteError, read_note

# A directory's stat fingerprint: (path, mtime_ns, size) per note file. Cheap to compute (stat only,
# no read/parse) and busts on any add, edit, or delete — so the cache below never serves a stale
# note while still skipping the expensive parse when nothing changed (KM-14).
_Fingerprint = frozenset[tuple[str, int, int]]

# Parsed-notes cache, keyed by directory. Guarded by a lock because retrieval offloads `load_notes`
# to worker threads (`asyncio.to_thread`). One entry per directory; production reads one
# `knowledge_dir`, so this does not grow unbounded.
_CACHE_LOCK = threading.Lock()
_NOTES_CACHE: dict[str, tuple[_Fingerprint, list[Note]]] = {}


def _dir_fingerprint(notes_dir: Path) -> _Fingerprint:
    """Stat every note file under `notes_dir`; return the (path, mtime_ns, size) fingerprint."""
    entries: set[tuple[str, int, int]] = set()
    for path in notes_dir.rglob("*.md"):
        try:
            stat = path.stat()
        except OSError:
            # A note removed between listing and stat (e.g. a `git pull` rewriting the tree
            # under a live query): treat it as absent. It simply drops out of the fingerprint,
            # which correctly busts the cache on the next stable read — never a crashed query.
            continue
        entries.add((str(path), stat.st_mtime_ns, stat.st_size))
    return frozenset(entries)


def _parse_notes(notes_dir: Path) -> list[Note]:
    """Parse every note under `notes_dir` (recursively), skipping non-note and invalid files."""
    notes = []
    for path in sorted(notes_dir.rglob("*.md")):
        try:
            note = read_note(path)
        except NoteError:
            continue
        if note is not None:
            notes.append(note)
    return notes


def load_notes(notes_dir: Path) -> list[Note]:
    """Parse every note under `notes_dir` (recursively), skipping non-note and invalid files.

    A malformed note (bad YAML or a schema violation) is skipped, not raised: graph building
    and evidence retrieval must not be blocked by one bad file. Reporting those failures is
    `kg.validate`'s job (it reads notes with its own error-collecting loop), so the two do not
    conflict — the indexer stays resilient, the validator stays strict.

    The result is cached per directory behind a stat fingerprint (KM-14), so interactive retrieval
    does not re-parse the whole tree on every query; any change to a note busts the cache, so a read
    is never stale. A shallow copy is returned so a caller cannot mutate the cached list, and `Note`
    is frozen, so the shared note instances cannot be mutated either.
    """
    if not settings.graph_cache_enabled:
        return _parse_notes(notes_dir)
    key = str(notes_dir)
    fingerprint = _dir_fingerprint(notes_dir)
    with _CACHE_LOCK:
        cached = _NOTES_CACHE.get(key)
        if cached is not None and cached[0] == fingerprint:
            return list(cached[1])
    notes = _parse_notes(notes_dir)
    with _CACHE_LOCK:
        _NOTES_CACHE[key] = (fingerprint, notes)
    return list(notes)


def build_graph(notes_dir: Path) -> nx.DiGraph:
    """Build the directed note graph from `notes_dir`.

    Every note becomes a node keyed by its id with the `Note` on the `note`
    attribute. Each `[[wikilink]]` becomes an edge id → target. A link to an
    unknown id still creates the edge (a dangling node with no `note` attribute),
    so `kg.validate` can report it rather than the graph silently dropping it.
    """
    graph: nx.DiGraph = nx.DiGraph()
    notes = load_notes(notes_dir)
    for note in notes:
        graph.add_node(note.id, note=note)
    for note in notes:
        for target in note.outgoing_links():
            graph.add_edge(note.id, target)
    return graph


def neighborhood(graph: nx.DiGraph, note_id: str, hops: int = 1) -> set[str]:
    """Return note ids within `hops` of `note_id`, following links both ways.

    Chemical relations are meaningful in both directions (a precursor and a
    product reference each other), so traversal is undirected over the directed
    graph — the 1–2 hop expansion the query skill uses (D-004).
    """
    if note_id not in graph:
        raise KeyError(f"unknown note id: {note_id!r}")
    undirected = graph.to_undirected(as_view=True)
    lengths = nx.single_source_shortest_path_length(undirected, note_id, cutoff=hops)
    return set(lengths) - {note_id}
