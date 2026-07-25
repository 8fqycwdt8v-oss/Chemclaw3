"""NetworkX index of the knowledge graph (plan step 2.3).

Builds a directed graph from a directory of notes: nodes are note ids (each
carrying its parsed `Note`), edges are `[[wikilink]]` relations. Retrieval is
graph traversal (D-004), so this indexer is the substrate the query skill walks
(1–2 hops), not a vector index.
"""

import threading
import time
from pathlib import Path

import networkx as nx

from chemclaw.config import settings
from kg.note import Note, NoteError, read_note

# A directory's stat fingerprint: (path, mtime_ns, size) per note file. Cheap *per file* (stat only,
# no read/parse) and busts on any add, edit, or delete — so the cache below skips the expensive
# parse when nothing changed (KM-14). It is still O(notes) in total, which is why
# `graph_cache_ttl_seconds` bounds how often it runs (DA-5).
_Fingerprint = frozenset[tuple[str, int, int]]

# Parsed-notes cache, keyed by directory. Guarded by a lock because retrieval offloads `load_notes`
# to worker threads (`asyncio.to_thread`). One entry per directory; production reads one
# `knowledge_dir`, so this does not grow unbounded.
_CACHE_LOCK = threading.Lock()
_NOTES_CACHE: dict[str, tuple[_Fingerprint, list[Note]]] = {}

# Assembled-graph cache, same key and same fingerprint as `_NOTES_CACHE`. The notes cache spares the
# parse, but every `find_notes`/`expand_note` call still re-added every node and edge — measured at
# ~86 ms per call for 10k notes, and the agent's documented flow (`find_notes` then `expand_note`)
# pays it twice per turn. Caching the assembled graph makes a warm interactive query O(1) work plus
# the stat scan, instead of O(N) node/edge insertion.
_GRAPH_CACHE: dict[str, tuple[_Fingerprint, nx.DiGraph]] = {}

# When each directory was last stat-scanned (`time.monotonic`), so `graph_cache_ttl_seconds` can
# skip the scan itself on a warm query — the scan is O(notes) and is paid even on a cache hit, so
# it is the floor on interactive latency (DA-5). Monotonic, not wall-clock: a clock adjustment
# must not make a scan look arbitrarily old (harmless) or arbitrarily fresh (a stale read).
_LAST_SCAN: dict[str, float] = {}


def invalidate_cache(notes_dir: Path | None = None) -> None:
    """Drop cached notes/graph so the next read re-scans immediately (the explicit bust hook).

    The TTL window trades a little freshness for latency, but a change this process *makes* should
    never wait it out — so every local writer of notes (today: the PR-gate submitter) calls this
    and the authoring loop stays instant. Clearing every directory by default is deliberate: note
    writes are rare next to queries, so the cost of over-clearing is one extra scan, while the cost
    of under-clearing is serving a note the caller just wrote as absent.
    """
    with _CACHE_LOCK:
        if notes_dir is None:
            _NOTES_CACHE.clear()
            _GRAPH_CACHE.clear()
            _LAST_SCAN.clear()
            return
        key = str(notes_dir)
        _NOTES_CACHE.pop(key, None)
        _GRAPH_CACHE.pop(key, None)
        _LAST_SCAN.pop(key, None)


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


def _cached_notes(notes_dir: Path) -> tuple[_Fingerprint | None, list[Note]]:
    """The parsed notes plus the fingerprint they were parsed at (`None` when caching is off).

    Handing the fingerprint back is what lets `build_graph` reuse it to key its own cache: the
    stat scan is the dominant cost of a warm read (~76 ms for 10k notes), so computing it once
    per call rather than once per cache layer matters.
    """
    if not settings.graph_cache_enabled:
        return None, _parse_notes(notes_dir)
    key = str(notes_dir)
    ttl = settings.graph_cache_ttl_seconds
    now = time.monotonic()
    if ttl > 0:
        with _CACHE_LOCK:
            cached = _NOTES_CACHE.get(key)
            scanned_at = _LAST_SCAN.get(key)
            if cached is not None and scanned_at is not None and now - scanned_at < ttl:
                # Inside the window: trust the last scan and skip it. The cached fingerprint is
                # returned unchanged so `build_graph` still keys its own cache consistently.
                return cached[0], cached[1]
    fingerprint = _dir_fingerprint(notes_dir)
    with _CACHE_LOCK:
        _LAST_SCAN[key] = now
        cached = _NOTES_CACHE.get(key)
        if cached is not None and cached[0] == fingerprint:
            return fingerprint, cached[1]
    notes = _parse_notes(notes_dir)
    with _CACHE_LOCK:
        _NOTES_CACHE[key] = (fingerprint, notes)
    return fingerprint, notes


def load_notes(notes_dir: Path) -> list[Note]:
    """Parse every note under `notes_dir` (recursively), skipping non-note and invalid files.

    A malformed note (bad YAML or a schema violation) is skipped, not raised: graph building
    and evidence retrieval must not be blocked by one bad file. Reporting those failures is
    `kg.validate`'s job (it reads notes with its own error-collecting loop), so the two do not
    conflict — the indexer stays resilient, the validator stays strict.

    The result is cached per directory behind a stat fingerprint (KM-14), so interactive retrieval
    does not re-parse the whole tree on every query; any change to a note busts the cache. Within
    `graph_cache_ttl_seconds` the scan itself is skipped too (DA-5), so an externally-made change
    can lag by up to that window — local writers call `invalidate_cache` to bypass it, and `0`
    restores always-scan. A shallow copy is returned so a caller cannot mutate the cached list, and
    `Note` is frozen, so the shared note instances cannot be mutated either.
    """
    return list(_cached_notes(notes_dir)[1])


def _assemble_graph(notes: list[Note]) -> nx.DiGraph:
    """Assemble the directed note graph from already-parsed notes."""
    graph: nx.DiGraph = nx.DiGraph()
    for note in notes:
        graph.add_node(note.id, note=note)
    for note in notes:
        for target in note.outgoing_links():
            graph.add_edge(note.id, target)
    return graph


def build_graph(notes_dir: Path) -> nx.DiGraph:
    """Build the directed note graph from `notes_dir`.

    Every note becomes a node keyed by its id with the `Note` on the `note`
    attribute. Each `[[wikilink]]` becomes an edge id → target. A link to an
    unknown id still creates the edge (a dangling node with no `note` attribute),
    so `kg.validate` can report it rather than the graph silently dropping it.

    Cached behind the same stat fingerprint as the parsed notes, so a warm interactive query
    skips reassembly entirely. The cached graph is **frozen** (`nx.freeze`) rather than copied:
    copying a large graph would give back most of the saving, and freezing makes the shared
    instance safe for the same reason `Note` is frozen — no reader can corrupt it for the next
    query. Readers (`find_notes`, `expand_note`, `neighborhood`) only traverse; a caller that
    genuinely needs a mutable graph should take `graph.copy()`.
    """
    fingerprint, notes = _cached_notes(notes_dir)
    if fingerprint is None:
        return _assemble_graph(notes)
    key = str(notes_dir)
    with _CACHE_LOCK:
        cached = _GRAPH_CACHE.get(key)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
    graph = nx.freeze(_assemble_graph(notes))
    with _CACHE_LOCK:
        _GRAPH_CACHE[key] = (fingerprint, graph)
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
