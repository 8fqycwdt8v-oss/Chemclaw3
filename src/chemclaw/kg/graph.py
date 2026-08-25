"""NetworkX index of the knowledge graph (plan step 2.3).

Builds a directed graph from a directory of notes: nodes are note ids (each
carrying its parsed `Note`), edges are `[[wikilink]]` relations. Retrieval is
graph traversal (D-004), so this indexer is the substrate the query skill walks
(1–2 hops), not a vector index.
"""

import contextlib
import logging
import os
import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import networkx as nx

from chemclaw.core.config import settings
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.kg.note import Note, NoteError, Relation, read_note, resolves_outside_graph

log = logging.getLogger(__name__)

# A directory's stat fingerprint: (path, mtime_ns, size) per note file. Cheap *per file* (stat only,
# no read/parse) and busts on any add, edit, or delete — so the cache below skips the expensive
# parse when nothing changed (KM-14). It is still O(notes) in total, which is why
# `graph_cache_ttl_seconds` bounds how often it runs (DA-5).
#
# Public, like `cached_notes` that hands it out: it is the key type of every cache derived from the
# corpus, here and in `chemclaw.kg.conflicts`, and a derived cache cannot annotate its own key
# without naming it.
NotesFingerprint = frozenset[tuple[str, int, int]]

# Parsed-notes cache, keyed by directory. Guarded by a lock because retrieval offloads `load_notes`
# to worker threads (`asyncio.to_thread`). One entry per directory; production reads one
# `knowledge_dir`, so this does not grow unbounded.
_CACHE_LOCK = threading.Lock()
_NOTES_CACHE: dict[str, tuple[NotesFingerprint, list[Note]]] = {}

# Assembled-graph cache, same key and same fingerprint as `_NOTES_CACHE`. The notes cache spares the
# parse, but every `find_notes`/`expand_note` call still re-added every node and edge — measured at
# ~86 ms per call for 10k notes, and the agent's documented flow (`find_notes` then `expand_note`)
# pays it twice per turn. Caching the assembled graph makes a warm interactive query O(1) work plus
# the stat scan, instead of O(N) node/edge insertion.
_GRAPH_CACHE: dict[str, tuple[NotesFingerprint, nx.DiGraph]] = {}

# When each directory was last stat-scanned (`time.monotonic`), so `graph_cache_ttl_seconds` can
# skip the scan itself on a warm query — the scan is O(notes) and is paid even on a cache hit, so
# it is the floor on interactive latency (DA-5). Monotonic, not wall-clock: a clock adjustment
# must not make a scan look arbitrarily old (harmless) or arbitrarily fresh (a stale read).
_LAST_SCAN: dict[str, float] = {}

# One re-entrant lock per directory, held across the *filling* of the caches above rather than
# merely around the dict accesses — the distinction `chemclaw.kg.conflicts` already draws for the
# conflict index and this module did not.
#
# Measured, on a 2,000-note corpus: one thread parsing a cold tree costs 198 ms, four concurrent
# threads cost 2,521 ms and eight cost 6,219 ms. Eight callers did not pay 8× the work, they paid
# 31×, because eight parses of the same tree contend on the GIL as well as duplicating each other.
# That shape is not exotic here: a `gather_evidence` sweep runs its sources under `asyncio.gather`
# with `load_notes` offloaded to a thread each, and every cold start and every `invalidate_cache`
# (the PR-gate's parked-checkout repair, the note reindex) puts them all on a miss together.
#
# Re-entrant because `build_graph` holds it across `cached_notes`, so the parse and the assembly
# behind one fingerprint happen once between all callers rather than once each.
#
# Never removed, including by `invalidate_cache`: dropping a lock another thread is standing in
# would hand the next caller a different lock object and quietly restore the duplication this
# exists to prevent. A `threading.RLock` per notes directory is a few dozen bytes and production
# reads one directory.
_COMPUTE_LOCKS: dict[str, threading.RLock] = {}


@contextlib.contextmanager
def _corpus_lock(key: str) -> Iterator[None]:
    """Hold the computation lock for one notes directory, unless caching is off.

    With `graph_cache_enabled` false there is nothing to fill and nothing to share, so every
    caller is asking for its own parse and serializing them would answer a question nobody asked.
    """
    if not settings.graph_cache_enabled:
        yield
        return
    with _CACHE_LOCK:
        lock = _COMPUTE_LOCKS.setdefault(key, threading.RLock())
    with lock:
        yield


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


def scan_notes_dir(notes_dir: Path) -> Iterator[tuple[Path, os.stat_result]]:
    """Every note file under `notes_dir` with its stat, in path order.

    The one definition of "what is a note file here" and the one place the race is tolerated. It
    was written out four times — twice in this module, once in `chemclaw.kg.validate`, and once
    more in `chemclaw.evals.retrieval`, whose own comment conceded it was "matching
    `chemclaw.kg.graph`'s fingerprint tolerance". Four copies of a glob is four chances for one of
    them to disagree about the extension, the recursion or the ordering.

    A file that vanishes between listing and stat (a `git pull` rewriting the tree under a live
    query) is skipped rather than raised: it simply drops out, which reads correctly as "changed"
    to a caller diffing fingerprints and never crashes a query.
    """
    for path in sorted(notes_dir.rglob("*.md")):
        try:
            stat = path.stat()
        except OSError:
            continue
        yield path, stat


def _dir_fingerprint(notes_dir: Path) -> NotesFingerprint:
    """Stat every note file under `notes_dir`; return the (path, mtime_ns, size) fingerprint."""
    return frozenset(
        (str(path), stat.st_mtime_ns, stat.st_size) for path, stat in scan_notes_dir(notes_dir)
    )


def note_file_fingerprints(notes_dir: Path) -> dict[str, str]:
    """A cheap per-note change signal: `note id -> "mtime_ns:size"`, stat-only (no read/parse).

    Same stat-only scan `_dir_fingerprint` does for the whole-tree cache (KM-14), but keyed per note
    id (the file's stem — `note.type/note.id.md` is the one filename shape a note is written under,
    `chemclaw.kg.submission.NoteFile`) rather than folded into one aggregate. A single fingerprint
    can
    only answer "did anything change"; this answers "which ones", which is what an incremental
    rebuild needs — `chemclaw.retrieval.vector_index.reindex_notes` re-embeds a note only when its
    entry here differs from what was stored at the last index run, instead of the whole corpus on
    every scheduled pass (D-2026-08-02-embed-only-what-changed).

    Two files claiming one id resolve **first in path order**, the same way `_parse_notes` and
    `chemclaw.kg.validate` resolve one. It used to be a dict comprehension, where the *last* file
    won — so the served corpus held one note and the reindex diffed the other, and `reindex_notes`
    could embed one file's text under the other's id. Two scans of one tree disagreeing about which
    file is a note is worse than either answer.
    """
    fingerprints: dict[str, str] = {}
    for path, stat in scan_notes_dir(notes_dir):
        fingerprints.setdefault(path.stem, f"{stat.st_mtime_ns}:{stat.st_size}")
    return fingerprints


def dangling_links(notes: list[Note]) -> list[tuple[str, str]]:
    """Every `(source id, target id)` link in `notes` pointing at an id no note in `notes` defines.

    Sorted, so two callers reporting it produce the same order. There were two implementations of
    this — `chemclaw.kg.validate`, which fails a merge on it, and `chemclaw.kg.analytics`, which
    reports it as a gap in the graph a deployment is serving. They are different *uses* of one
    question, and the question is asked here once.

    Deliberately over a note list rather than over the assembled graph: a dangling target is a node
    with no `note` attribute there, which is the same fact expressed in a form that only one of the
    two callers has.

    A target in an **external id namespace** is not dangling (`kg.note.resolves_outside_graph`): it
    names a row in a store rather than a note in this tree. Since D-2026-08-25 an ELN transcription
    is data in `reaction_records`, while `memory.campaign` and `memory.optimization` still cite each
    run as `[[reaction-<id>]]` — so without this every campaign and optimization note would be
    reported broken for links that resolve. What is genuinely lost is stated at the constant: this
    function can no longer tell a real record from a typo'd one, and the live lane checks that.
    """
    defined = {note.id for note in notes}
    return sorted(
        (note.id, target)
        for note in notes
        for target in note.outgoing_links()
        if target not in defined and not resolves_outside_graph(target)
    )


def _parse_notes(notes_dir: Path) -> list[Note]:
    """Parse every note under `notes_dir` (recursively), skipping non-note and invalid files.

    A file the schema rejects is skipped so one bad note cannot block every query — but it is
    *said*, at WARNING, and counted. It used to be dropped in silence on the argument that
    `kg-validate` reports it, which is true of the repository and not of the tree a pod is
    serving: a note corrupted by a partial sync leaves a deployment retrieving less than it
    should with nothing anywhere saying so.

    **A second file claiming an id already taken is skipped on exactly the same terms**, and for
    exactly the same reason. It used to be neither reported nor decided: the notes were both
    returned, `_assemble_graph` then called `add_node` twice on one id, and whichever file sorted
    *last* silently replaced the other — so one of two curated notes was unreachable by every
    query, with the winner depending on a directory name. `kg-validate` fails a duplicate id, which
    again is a property of the repository rather than of the tree a pod is serving; an rsync that
    lands a renamed note before removing the old one produces this state in a healthy deployment.

    First in path order wins, matching `chemclaw.kg.validate`'s `id_to_path` and
    `note_file_fingerprints`, so every reader of one tree names the same file.
    """
    notes: dict[str, tuple[Path, Note]] = {}
    for path, _ in scan_notes_dir(notes_dir):
        try:
            note = read_note(path)
        except NoteError as exc:
            log.warning("skipping unparseable note %s: %s", path, exc)
            record_metric(lambda m: m.increment("chemclaw_notes_unparseable_total"))
            continue
        if note is None:
            continue
        claimed = notes.get(note.id)
        if claimed is not None:
            log.warning(
                "skipping %s: note id %r is already defined by %s — one of the two is "
                "unreachable until the duplicate is resolved",
                path,
                note.id,
                claimed[0],
            )
            record_metric(lambda m: m.increment("chemclaw_notes_duplicate_id_total"))
            continue
        notes[note.id] = (path, note)
    return [note for _, note in notes.values()]


def cached_notes(notes_dir: Path) -> tuple[NotesFingerprint | None, list[Note]]:
    """The parsed notes plus the fingerprint they were parsed at (`None` when caching is off).

    Handing the fingerprint back is what lets `build_graph` reuse it to key its own cache: the
    stat scan is the dominant cost of a warm read (~76 ms for 10k notes), so computing it once
    per call rather than once per cache layer matters.

    **Public because it is the seam every derived-from-notes cache keys on.** The fingerprint is
    the answer to "may I reuse what I computed last time", and any artifact derived from the whole
    corpus — the assembled graph here, the conflict index in `chemclaw.kg.conflicts` — needs
    exactly that token and nothing else. A second derivation that computed its own fingerprint
    would pay the stat scan twice and, worse, could disagree with this one about whether the corpus
    had changed.

    **Concurrent misses wait rather than duplicate.** The scan and the parse happen under this
    directory's `_corpus_lock`, so eight threads arriving on a cold cache together produce one
    parse and seven waiters instead of eight parses fighting over the GIL — measured at 6,219 ms
    against the 198 ms of the single parse they were all repeating. A waiter that reaches the lock
    finds the answer it queued for and never rescans, because the winner has just stamped
    `_LAST_SCAN`.
    """
    if not settings.graph_cache_enabled:
        return None, _parse_notes(notes_dir)
    key = str(notes_dir)
    ttl = settings.graph_cache_ttl_seconds
    warm = _within_ttl(key, ttl)
    if warm is not None:
        return warm
    with _corpus_lock(key):
        # Re-checked after the wait, and this is the whole of the fix — the check above is only the
        # fast path for callers that never had to queue.
        warm = _within_ttl(key, ttl)
        if warm is not None:
            return warm
        now = time.monotonic()
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


def _within_ttl(key: str, ttl: float) -> tuple[NotesFingerprint, list[Note]] | None:
    """The cached entry for `key` while its last scan is still inside `ttl`, else None.

    Inside the window the last scan is trusted and this one is skipped: the scan is O(notes) and is
    paid even on a cache hit, so it is the floor on interactive latency (DA-5). The cached
    fingerprint comes back unchanged, so `build_graph` still keys its own cache consistently.

    A function rather than an inlined branch because `cached_notes` asks the question twice — once
    to stay off the lock, once after waiting on it — and the two must not drift.
    """
    if ttl <= 0:
        return None
    with _CACHE_LOCK:
        cached = _NOTES_CACHE.get(key)
        scanned_at = _LAST_SCAN.get(key)
        if cached is not None and scanned_at is not None and time.monotonic() - scanned_at < ttl:
            return cached
    return None


def load_notes(notes_dir: Path) -> list[Note]:
    """Parse every note under `notes_dir` (recursively), skipping non-note and invalid files.

    A malformed note (bad YAML or a schema violation) is skipped, not raised: graph building
    and evidence retrieval must not be blocked by one bad file. Reporting those failures is
    `chemclaw.kg.validate`'s job (it reads notes with its own error-collecting loop), so the two do
    not
    conflict — the indexer stays resilient, the validator stays strict.

    The result is cached per directory behind a stat fingerprint (KM-14), so interactive retrieval
    does not re-parse the whole tree on every query; any change to a note busts the cache. Within
    `graph_cache_ttl_seconds` the scan itself is skipped too (DA-5), so an externally-made change
    can lag by up to that window — local writers call `invalidate_cache` to bypass it, and `0`
    restores always-scan. A shallow copy is returned so a caller cannot mutate the cached list, and
    `Note` is frozen, so the shared note instances cannot be mutated either.
    """
    return list(cached_notes(notes_dir)[1])


def _assemble_graph(notes: list[Note]) -> nx.DiGraph:
    """Assemble the directed note graph from already-parsed notes, edges carrying their relations.

    Every edge gets a `relations` attribute: the tuple of `Relation` objects asserted between those
    two notes (STO-8). Until this, `add_edge(note.id, target)` recorded no attributes at all, so
    nothing could ask for a compound's precursors or for the note that contradicts another — the
    links existed and the relations did not.

    **Kept on `nx.DiGraph` with a tuple per edge, rather than moved to `nx.MultiDiGraph`.** A
    multigraph models parallel edges properly, and would change the meaning of `graph[a][b]` for
    every existing reader — `neighborhood`, `chemclaw.kg.analytics`, the retrievers — to solve a
    case that
    does not arise: two notes standing in several relations at once is rare, and a tuple represents
    it exactly as well for every query anyone actually runs. The cost is that an edge is a set of
    relations rather than one, which is why the attribute is named in the plural.

    One node per id is `_parse_notes`'s guarantee, not this loop's: `add_node` would happily
    overwrite, and did, which is why the duplicate is now resolved and reported where the files are
    still in hand to name.
    """
    graph: nx.DiGraph = nx.DiGraph()
    for note in notes:
        graph.add_node(note.id, note=note)
    for note in notes:
        by_target: dict[str, list[Relation]] = defaultdict(list)
        for relation in note.outgoing_relations():
            by_target[relation.to].append(relation)
        for target, edges in by_target.items():
            graph.add_edge(note.id, target, relations=tuple(edges))
    return graph


def build_graph(notes_dir: Path) -> nx.DiGraph:
    """Build the directed note graph from `notes_dir`.

    Every note becomes a node keyed by its id with the `Note` on the `note`
    attribute. Each `[[wikilink]]` becomes an edge id → target. A link to an
    unknown id still creates the edge (a dangling node with no `note` attribute),
    so `chemclaw.kg.validate` can report it rather than the graph silently dropping it.

    Cached behind the same stat fingerprint as the parsed notes, so a warm interactive query
    skips reassembly entirely. The cached graph is **frozen** (`nx.freeze`) rather than copied:
    copying a large graph would give back most of the saving, and freezing makes the shared
    instance safe for the same reason `Note` is frozen — no reader can corrupt it for the next
    query. Readers (`expand_note`, `neighborhood`) only traverse; a caller that genuinely needs a
    mutable graph should take `graph.copy()`.

    The corpus lock is taken around the parse *and* the assembly, not around each separately, so
    concurrent cold callers share one of each. `_corpus_lock` is re-entrant for exactly this:
    `cached_notes` takes it again inside, and re-acquiring a lock this thread already holds is the
    difference between one assembly and one per caller.
    """
    key = str(notes_dir)
    with _corpus_lock(key):
        fingerprint, notes = cached_notes(notes_dir)
        if fingerprint is None:
            return _assemble_graph(notes)
        with _CACHE_LOCK:
            cached = _GRAPH_CACHE.get(key)
            if cached is not None and cached[0] == fingerprint:
                return cached[1]
        graph = nx.freeze(_assemble_graph(notes))
        with _CACHE_LOCK:
            _GRAPH_CACHE[key] = (fingerprint, graph)
        return graph


def related(graph: nx.DiGraph, note_id: str, rel: str, as_of: date | None = None) -> list[str]:
    """The ids `note_id` points at through relation `rel`, ordered, newest edges included.

    The query typed edges exist to make possible: "what are this compound's precursors", "what does
    this note contradict". Directed on purpose, unlike `neighborhood` — a relation has a direction
    and a *reversed* one usually means something different (`precursor-of` reversed is not
    `precursor-of`), so the caller asks about the direction it means.

    `as_of` applies the edge's own validity window (STO-9), so a relation that stopped holding is
    excluded from a current-evidence query while remaining in git and in the graph. Omit it to see
    every asserted edge regardless of when it held.
    """
    if note_id not in graph:
        raise KeyError(f"unknown note id: {note_id!r}")
    found = []
    for _, target, data in graph.out_edges(note_id, data=True):
        for relation in data.get("relations", ()):
            if relation.rel != rel:
                continue
            if as_of is not None and not relation.is_current(as_of):
                continue
            found.append(target)
            break
    return sorted(found)


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
