"""NetworkX index of the knowledge graph.

Builds a directed graph from a directory of notes: nodes are note ids (each carrying its parsed
`Note`), edges are `[[wikilink]]` relations. Retrieval is graph traversal (1-2 hops), not a vector
index. Parsing and assembly are cached per directory behind a stat fingerprint.
"""

import contextlib
import hashlib
import logging
import os
import subprocess
import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import networkx as nx

from chemclaw.core.config import settings
from chemclaw.core.logging import log_event
from chemclaw.core.metrics_bridge import record_metric
from chemclaw.kg.note import Note, NoteError, Relation, read_note, resolves_outside_graph

log = logging.getLogger(__name__)

# One `git log -1` on a local checkout. A bound rather than a setting: this is not a knob anybody
# would tune, and a hung `git` on the scheduled reindex path must not hold the pass open.
_GIT_REVISION_TIMEOUT_SECONDS = 10

# A directory's stat fingerprint: (path, mtime_ns, size) per note file. Stat-only, busts on any add,
# edit or delete; still O(notes), so `graph_cache_ttl_seconds` bounds how often it runs. Public
# because every corpus-derived cache (here and `kg.conflicts`) keys on it.
NotesFingerprint = frozenset[tuple[str, int, int]]

# Parsed-notes cache, keyed by directory and guarded by a lock because `load_notes` runs in worker
# threads. One entry holds the whole parsed corpus for the process's life, so memory tracks corpus
# size. Disabling the cache does not reduce peak memory: each concurrent reader then parses its own
# full copy.
_CACHE_LOCK = threading.Lock()
_NOTES_CACHE: dict[str, tuple[NotesFingerprint, list[Note]]] = {}

# Per-file parse results, keyed by directory then path, so a corpus that changed by one note is
# re-parsed by one note.
#
# `_NOTES_CACHE` answers "has anything changed"; this answers "what". Each entry is `(mtime_ns,
# size, outcome)`, the same stat fields `_dir_fingerprint` compares; the outcome is the parsed
# `Note`, `None` for a non-note file, or the `NoteError` message, so a reused entry re-emits the
# same warning and counts as a fresh parse would. Not cleared by `invalidate_cache` by default,
# since each entry is keyed on its own file's stat; entries for vanished files are dropped by the
# next scan. A caller comparing against the content hash of `note_file_fingerprints` must pass
# `reparse=True`, because a same-size, same-mtime edit is invisible to the stat.
_PARSED_FILES: dict[str, dict[str, tuple[int, int, Note | str | None]]] = {}

# Assembled-graph cache, same key and fingerprint as `_NOTES_CACHE`, so a warm query skips re-adding
# every node and edge.
_GRAPH_CACHE: dict[str, tuple[NotesFingerprint, nx.DiGraph]] = {}

# When each directory was last stat-scanned, so `graph_cache_ttl_seconds` can skip the scan on a
# warm query. Monotonic, so a clock adjustment cannot make a scan look fresh.
_LAST_SCAN: dict[str, float] = {}

# One re-entrant lock per directory, held across filling the caches above, so concurrent cold
# readers share one parse instead of duplicating it under GIL contention. Re-entrant because
# `build_graph` holds it across `cached_notes`. Never removed, including by `invalidate_cache`:
# replacing a lock another thread is waiting on would reintroduce the duplication.
_COMPUTE_LOCKS: dict[str, threading.RLock] = {}

# Newest note mtime per tree with the monotonic time of the scan that found it:
# `path -> (scanned_at, newest_mtime or None)`; `None` is a cached "no notes". Separate from
# `_LAST_SCAN` so the gauge's freshness does not depend on someone querying the graph.
_NEWEST_MTIME: dict[str, tuple[float, float | None]] = {}


@contextlib.contextmanager
def _corpus_lock(key: str) -> Iterator[None]:
    """Hold the computation lock for one notes directory, unless caching is off.

    With caching off there is nothing to share, so callers are not serialized.
    """
    if not settings.graph_cache_enabled:
        yield
        return
    with _CACHE_LOCK:
        lock = _COMPUTE_LOCKS.setdefault(key, threading.RLock())
    with lock:
        yield


def invalidate_cache(notes_dir: Path | None = None, *, reparse: bool = False) -> None:
    """Drop cached notes/age so the next read re-scans immediately (the explicit bust hook).

    Every local writer of notes calls this so its own write is visible at once rather than after the
    TTL. Clearing every directory by default is deliberate: over-clearing costs one scan,
    under-clearing serves a just-written note as absent.

    `_GRAPH_CACHE` is kept: `build_graph` returns it only on exact fingerprint equality and
    otherwise uses it as the base for `_patch_graph`, so it cannot be served stale. The per-file
    parse cache is kept too unless `reparse` is set.

    Args:
        notes_dir: The corpus to drop, or `None` for every one this process has read.
        reparse: Also drop the per-file parse cache, so the next read comes off disk. Needed only by
            a caller that pairs the result against a content hash, such as the re-index job.
    """
    with _CACHE_LOCK:
        if notes_dir is None:
            _NOTES_CACHE.clear()
            _LAST_SCAN.clear()
            _NEWEST_MTIME.clear()
            if reparse:
                _PARSED_FILES.clear()
            return
        key = str(notes_dir)
        _NOTES_CACHE.pop(key, None)
        _LAST_SCAN.pop(key, None)
        _NEWEST_MTIME.pop(key, None)
        if reparse:
            _PARSED_FILES.pop(key, None)


def scan_notes_dir(notes_dir: Path) -> Iterator[tuple[Path, os.stat_result]]:
    """Every note file under `notes_dir` with its stat, in path order.

    The one definition of "what is a note file here". A file that vanishes between listing and stat
    (e.g. a `git pull` under a live query) is skipped, which reads as "changed" to a caller diffing
    fingerprints.
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


#: The fingerprint of a note file that exists but would not open. Never equal to a `sha256:` digest
#: and constant, so the note stays in `reindex_notes`'s `keep` set (a transient I/O fault does not
#: retire its index row) without re-embedding every pass.
UNREADABLE = "unreadable"


def note_file_fingerprints(notes_dir: Path) -> dict[str, str]:
    """A per-note change signal: `note id -> "sha256:<hex>"` over the file's own bytes.

    Keyed by note id (the file stem) so `retrieval.vector_index.reindex_notes` re-embeds only the
    notes whose entry differs from the last index run. A content hash rather than `mtime_ns:size`,
    because the index is shared between pods while each pod's checkout sets its own mtimes; the
    algorithm prefix makes the format visible in stored rows.

    Two files claiming one id resolve first in path order, as `_parse_notes` and `kg.validate` do. A
    file that cannot be read keeps an entry (`UNREADABLE`) so the reindex does not retire it; once
    readable, it re-embeds only if its bytes differ from what was indexed.
    """
    fingerprints: dict[str, str] = {}
    for path, _stat in scan_notes_dir(notes_dir):
        if path.stem in fingerprints:
            continue
        try:
            fingerprints[path.stem] = f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
        except OSError:
            fingerprints[path.stem] = UNREADABLE
    return fingerprints


def _git_stdout(notes_dir: Path, *args: str) -> str | None:
    """One `git` read on the corpus checkout, or `None` for any of the ordinary reasons it fails."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(notes_dir), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_REVISION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def corpus_revision(notes_dir: Path) -> int | None:
    """How many commits this corpus's checkout has behind it, or `None` if unknowable.

    Lets pods with differently aged clones of one corpus compare which is newer, so `reindex_notes`
    does not retire a note just because this pod's sidecar has not synced yet. A commit count (`git
    rev-list --count HEAD`) is monotone under ancestry and needs no clock; a timestamp has second
    resolution and a commit id is not orderable. Two divergent branches with equal counts cannot be
    ordered, which a single-remote deployment never produces.

    `None` (not a work tree, no `git`, no commits) means "no constraint": callers behave as if this
    did not exist.
    """
    out = _git_stdout(notes_dir, "rev-list", "--count", "HEAD")
    if out is None:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


#: `notes_dir -> (HEAD commit, note id -> date its file was first committed)`, per process, so a
#: later call scans only commits since the remembered one.
_ARRIVALS: dict[Path, tuple[str, dict[str, date]]] = {}
_ARRIVALS_LOCK = threading.Lock()


def _added_since(notes_dir: Path, since: str | None) -> dict[str, date] | None:
    """Note id -> date of the commit that added its file, over `since..HEAD` (or all of history).

    `--no-renames`: a note moved between type directories reads as arriving on the day it moved,
    which errs toward telling a digest subscriber once more. `--first-parent -m`: a note merged
    through a `--no-ff` merge is dated by the merge commit on this branch, not its side-branch
    commit, so it is not dated before a subscriber's watermark.
    """
    revisions = [f"{since}..HEAD"] if since else []
    out = _git_stdout(
        notes_dir,
        "log",
        "--first-parent",
        "-m",
        "--no-renames",
        "--diff-filter=A",
        "--name-only",
        "--relative",
        "--format=%x00%ct",
        *revisions,
        "--",
        ".",
    )
    if out is None:
        return None
    added: dict[str, date] = {}
    day: date | None = None
    # `log` is newest first and the first date seen for a path is kept, so a note deleted and
    # re-added arrives on its re-add — it is new again to anyone who was told it was gone.
    for line in out.splitlines():
        if line.startswith("\x00"):
            # `%ct` (UTC timestamp) rather than `%cs` (committer's local date), so the day compares
            # correctly against the UTC watermark.
            day = datetime.fromtimestamp(int(line[1:].strip()), UTC).date()
        elif line.endswith(".md") and day is not None:
            added.setdefault(Path(line).stem, day)
    return added


def note_arrivals(notes_dir: Path) -> dict[str, date]:
    """When each note arrived in this corpus: the date of the commit that added its file.

    Separate from `valid_from` (when a fact became true, often unknown): a digest asks what is new
    to a subscriber, and arrival answers that. Read from the notes repository's history, which every
    pod's clone shares, unlike file mtimes. Empty when the corpus is not a git work tree or `git`
    cannot answer, meaning "no constraint".
    """
    head = _git_stdout(notes_dir, "rev-parse", "HEAD")
    if head is None:
        return {}
    head = head.strip()
    with _ARRIVALS_LOCK:
        cached = _ARRIVALS.get(notes_dir)
    if cached is not None and cached[0] == head:
        return cached[1]
    since = None
    if (
        cached is not None
        and _git_stdout(notes_dir, "merge-base", "--is-ancestor", cached[0], head) is not None
    ):
        since = cached[0]
    added = _added_since(notes_dir, since)
    if added is None:
        # `rev-parse` answered, so this is a work tree and the scan itself failed (timeout, unsafe
        # directory). Logged because the empty result looks like "nothing new".
        log.warning(
            "kg.note_arrivals_unreadable: could not read when notes arrived in %s; undated notes "
            "are judged as they were before arrivals existed until this succeeds",
            notes_dir,
        )
        return cached[1] if cached is not None else {}
    arrivals = {**cached[1], **added} if since is not None and cached is not None else added
    with _ARRIVALS_LOCK:
        _ARRIVALS[notes_dir] = (head, arrivals)
    return arrivals


def note_in(graph: "nx.DiGraph[str]", note_id: str) -> Note | None:
    """The note `note_id` names in `graph`, or `None` when the graph does not define one.

    Not the same as `note_id in graph`: `_assemble_graph` mints a bare node for every
    cited-but-undefined link target, so membership is true for ids that resolve to nothing.
    """
    return graph.nodes[note_id].get("note") if note_id in graph else None


def dangling_links(notes: list[Note]) -> list[tuple[str, str]]:
    """Every `(source id, target id)` link in `notes` pointing at an id no note in `notes` defines.

    Sorted, so callers (`kg.validate`, `kg.analytics`) report the same order. A target in an
    external id namespace (`kg.note.resolves_outside_graph`, e.g. `reaction-<id>` rows in
    `reaction_records`) is not dangling; this cannot tell a real record id from a mistyped one.
    """
    defined = {note.id for note in notes}
    return sorted(
        (note.id, target)
        for note in notes
        for target in note.outgoing_links()
        if target not in defined and not resolves_outside_graph(target)
    )


def _parsed_files(notes_dir: Path) -> list[tuple[Path, os.stat_result]]:
    """The tree's note files, with the per-file parse cache trimmed to exactly them.

    Materialized because the trim needs the whole set: a stale entry for a deleted path could
    otherwise serve a recreated file with matching stat.
    """
    found = list(scan_notes_dir(notes_dir))
    if not settings.graph_cache_enabled:
        return found
    live = {str(path) for path, _ in found}
    with _CACHE_LOCK:
        cached = _PARSED_FILES.setdefault(str(notes_dir), {})
        for gone in [key for key in cached if key not in live]:
            del cached[gone]
    return found


def _note_for(notes_dir: Path, path: Path, stat: os.stat_result) -> tuple[Note | str | None, int]:
    """One file's parse outcome, reused when its `(mtime_ns, size)` has not moved.

    The outcome is the parsed `Note`, `None` for a non-note file, or the `NoteError` message, so a
    cached failure reproduces the same warning and metric as a fresh parse.

    Args:
        notes_dir: The corpus this file belongs to, the cache's first key.
        path: The note file.
        stat: That file's stat from the same scan the fingerprint was taken from.

    Returns:
        The parse outcome and 1 if it came from the cache, else 0.
    """
    signature = (stat.st_mtime_ns, stat.st_size)
    key = str(path)
    if settings.graph_cache_enabled:
        with _CACHE_LOCK:
            entry = _PARSED_FILES.get(str(notes_dir), {}).get(key)
        if entry is not None and (entry[0], entry[1]) == signature:
            return entry[2], 1
    outcome: Note | str | None
    try:
        outcome = read_note(path)
    except NoteError as exc:
        outcome = str(exc)
    if settings.graph_cache_enabled:
        with _CACHE_LOCK:
            _PARSED_FILES.setdefault(str(notes_dir), {})[key] = (*signature, outcome)
    return outcome, 0


def _parse_notes(notes_dir: Path) -> list[Note]:
    """Parse every note under `notes_dir` (recursively), skipping non-note and invalid files.

    A file the schema rejects is skipped so one bad note cannot block every query, but it is logged
    at WARNING and counted, since the served tree can differ from the validated repository. A second
    file claiming an id already taken is skipped and reported the same way; first in path order
    wins, matching `kg.validate` and `note_file_fingerprints`.
    """
    started = time.perf_counter()
    notes: dict[str, tuple[Path, Note]] = {}
    unparseable = 0
    duplicate = 0
    reused = 0
    for path, stat in _parsed_files(notes_dir):
        note, reused_this = _note_for(notes_dir, path, stat)
        reused += reused_this
        if isinstance(note, str):
            log.warning("skipping unparseable note %s: %s", path, note)
            record_metric(lambda m: m.increment("chemclaw_notes_unparseable_total"))
            unparseable += 1
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
            duplicate += 1
            continue
        notes[note.id] = (path, note)
    skipped = unparseable + duplicate
    # One summary per pass gives the per-file warnings a denominator, so a typo can be told from a
    # partial sync that dropped much of the corpus.
    log_event(
        log,
        "kg.indexed",
        "parsed %d note(s) from %s in %.3fs (%d unparseable, %d duplicate id, %d reused)",
        len(notes),
        notes_dir,
        time.perf_counter() - started,
        unparseable,
        duplicate,
        reused,
        level=logging.INFO if skipped else logging.DEBUG,
        notes=len(notes),
        unparseable=unparseable,
        duplicate_id=duplicate,
        reused=reused,
        duration_s=round(time.perf_counter() - started, 3),
    )
    return [note for _, note in notes.values()]


def cached_notes(notes_dir: Path) -> tuple[NotesFingerprint | None, list[Note]]:
    """The parsed notes plus the fingerprint they were parsed at (`None` when caching is off).

    The fingerprint is returned so derived caches (`build_graph`, `kg.conflicts`) key on the same
    token without paying the stat scan again or disagreeing about whether the corpus changed. The
    scan and parse run under the directory's `_corpus_lock`, so concurrent cold callers wait for one
    parse instead of each doing their own.
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
            cached = _NOTES_CACHE.get(key)
            if cached is not None and cached[0] == fingerprint:
                _LAST_SCAN[key] = now
                # A shallow copy so a caller that sorts in place cannot corrupt the shared cache;
                # `Note` is frozen.
                return fingerprint, list(cached[1])
        notes = _parse_notes(notes_dir)
        with _CACHE_LOCK:
            # Stamp the scan only once its notes are in the cache; stamping earlier lets a
            # concurrent TTL fast path pair a fresh timestamp with the old corpus.
            _NOTES_CACHE[key] = (fingerprint, notes)
            _LAST_SCAN[key] = now
        return fingerprint, list(notes)


def _within_ttl(key: str, ttl: float) -> tuple[NotesFingerprint, list[Note]] | None:
    """The cached entry for `key` while its last scan is still inside `ttl`, else None.

    Inside the window the scan is skipped. A function because `cached_notes` asks it twice, before
    and after waiting on the lock.
    """
    if ttl <= 0:
        return None
    with _CACHE_LOCK:
        cached = _NOTES_CACHE.get(key)
        scanned_at = _LAST_SCAN.get(key)
        if cached is not None and scanned_at is not None and time.monotonic() - scanned_at < ttl:
            # Copied for the reason `cached_notes` copies: the cached list must never be handed
            # out live to a caller that might mutate it.
            return cached[0], list(cached[1])
    return None


def load_notes(notes_dir: Path) -> list[Note]:
    """Parse every note under `notes_dir` (recursively), skipping non-note and invalid files.

    A malformed note is skipped, not raised, so one bad file cannot block retrieval; strict
    reporting is `kg.validate`'s job. Cached behind a stat fingerprint; within
    `graph_cache_ttl_seconds` an external change may lag (local writers call `invalidate_cache`, and
    `0` always scans). Returns a fresh shallow copy of frozen notes.
    """
    return cached_notes(notes_dir)[1]


#: Share of the corpus that may change before a rebuild is preferred to an incremental patch. A
#: patch costs a graph copy plus per-note work, and breaks even with a rebuild near 40%; 0.25
#: leaves margin because declining a patch is never wrong, only slower. A single note write changes
#: 1/N.
_MAX_PATCHED_FRACTION = 0.25


def _attach_edges(graph: nx.DiGraph, note: Note) -> None:
    """Write `note`'s outgoing edges into `graph`, each carrying the relations it asserts.

    Shared by the full rebuild and the incremental patch so both produce the same graph. Relations
    to one target share one edge (`nx.DiGraph`). `add_edge` mints a bare node for an undefined
    target, which `dangling_links` and `neighborhood` rely on.
    """
    by_target: dict[str, list[Relation]] = defaultdict(list)
    for relation in note.outgoing_relations():
        by_target[relation.to].append(relation)
    for target, edges in by_target.items():
        graph.add_edge(note.id, target, relations=tuple(edges))


def _drop_if_uncited(graph: nx.DiGraph, node_id: str) -> None:
    """Remove `node_id` if it is a bare node nothing links to or from any more.

    A rebuild would not produce such a node. A node carrying a `note` is never dropped here.
    """
    if "note" not in graph.nodes[node_id] and graph.degree(node_id) == 0:
        graph.remove_node(node_id)


def _detach_note(graph: nx.DiGraph, note_id: str) -> None:
    """Undo everything the note `note_id` defines contributed to `graph`, keeping its citations.

    Not `graph.remove_node`, which also drops in-edges that belong to other notes. Only this note's
    out-edges and its `note` attribute are removed, leaving what a rebuild without this note would
    produce.
    """
    for target in [target for _, target in graph.out_edges(note_id)]:
        graph.remove_edge(note_id, target)
        _drop_if_uncited(graph, target)
    graph.nodes[note_id].pop("note", None)
    _drop_if_uncited(graph, note_id)


def _patch_graph(previous: nx.DiGraph, notes: list[Note]) -> nx.DiGraph | None:
    """`previous` brought up to `notes` by touching only what changed, or None to rebuild instead.

    Changed notes are found by object identity: `_PARSED_FILES` returns the same frozen `Note` for
    an unchanged file, so `is` is O(1) and errs only toward extra work. The previous note set is
    read off the graph's node attributes. The patch is applied to a copy, because the cached graph
    is shared and frozen and may be mid-traversal in another reader.

    Returns:
        The patched graph, or None when the change exceeds `_MAX_PATCHED_FRACTION` of the corpus.
    """
    current = {note.id: note for note in notes}
    before = {node: data["note"] for node, data in previous.nodes(data=True) if "note" in data}
    changed = [note_id for note_id, note in current.items() if before.get(note_id) is not note]
    gone = [note_id for note_id in before if note_id not in current]
    if len(changed) + len(gone) > len(current) * _MAX_PATCHED_FRACTION:
        return None
    graph = previous.copy()
    # All detaches before any attach, so the result does not depend on iteration order.
    for note_id in gone:
        _detach_note(graph, note_id)
    for note_id in changed:
        if note_id in before:
            _detach_note(graph, note_id)
    for note_id in changed:
        graph.add_node(note_id, note=current[note_id])
        _attach_edges(graph, current[note_id])
    log_event(
        log,
        "kg.graph_patched",
        "patched note graph: %d changed, %d removed, of %d note(s)",
        len(changed),
        len(gone),
        len(current),
        level=logging.DEBUG,
        changed=len(changed),
        removed=len(gone),
        notes=len(current),
    )
    return graph


def _assemble_graph(notes: list[Note]) -> nx.DiGraph:
    """Assemble the directed note graph from already-parsed notes, edges carrying their relations.

    Every edge's `relations` attribute is the tuple of `Relation`s asserted between those two notes.
    A `DiGraph` with a tuple per edge rather than a `MultiDiGraph`, so `graph[a][b]` keeps its
    meaning for existing readers. One node per id is guaranteed by `_parse_notes`.
    """
    graph: nx.DiGraph = nx.DiGraph()
    for note in notes:
        graph.add_node(note.id, note=note)
    for note in notes:
        _attach_edges(graph, note)
    return graph


def build_graph(notes_dir: Path) -> nx.DiGraph:
    """Build the directed note graph from `notes_dir`.

    Every note becomes a node keyed by its id with the `Note` on the `note` attribute; each
    `[[wikilink]]` becomes an edge. A link to an unknown id still creates a bare node, so
    `kg.validate` can report it.

    Cached behind the notes fingerprint and frozen (`nx.freeze`) so the shared instance cannot be
    mutated; a caller needing a mutable graph takes `graph.copy()`. A stale cached graph is patched
    (`_patch_graph`) unless too much changed. The re-entrant corpus lock spans parse and assembly,
    so concurrent cold callers share one of each.
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
        patched = _patch_graph(cached[1], notes) if cached is not None else None
        graph = nx.freeze(patched if patched is not None else _assemble_graph(notes))
        with _CACHE_LOCK:
            _GRAPH_CACHE[key] = (fingerprint, graph)
        return graph


def related(graph: nx.DiGraph, note_id: str, rel: str, as_of: date | None = None) -> list[str]:
    """The ids `note_id` points at through relation `rel`, ordered.

    Directed, since a reversed relation usually means something else. `as_of` applies each edge's
    validity window; omit it to see every asserted edge. An id that the graph does not define
    (checked via `note_in`) is an error, not an empty answer.
    """
    if note_in(graph, note_id) is None:
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


def _replaced_by(graph: nx.DiGraph, node_id: str) -> set[str]:
    """The ids a supersede link names as `node_id`'s replacement, from either end of the link.

    Both ends, because either half may be absent: a person's retired note never receives its
    `superseded-by` amendment, and a replacement may name an id no note defines.
    """
    forward = {
        target
        for _, target, data in graph.out_edges(node_id, data=True)
        if any(relation.rel == "superseded-by" for relation in data.get("relations", ()))
    }
    backward = {
        source
        for source, _, data in graph.in_edges(node_id, data=True)
        if any(relation.rel == "supersedes" for relation in data.get("relations", ()))
    }
    return forward | backward


def current_successor(graph: nx.DiGraph, node_id: str, as_of: date) -> Note | None:
    """The first current note a chain of supersede links leads to from `node_id`, or `None`.

    Follows links (an id may be superseded repeatedly) breadth-first in id order until a note
    current on `as_of`, so answers are deterministic; a cycle ends the walk. `node_id` itself is
    never the answer.
    """
    if node_id not in graph:
        return None
    seen = {node_id}
    frontier = [node_id]
    while frontier:
        following: list[str] = []
        for current in frontier:
            for candidate in sorted(_replaced_by(graph, current) - seen):
                seen.add(candidate)
                note = note_in(graph, candidate)
                if note is not None and note.is_current(as_of):
                    return note
                following.append(candidate)
        frontier = following
    return None


def neighborhood(graph: nx.DiGraph, note_id: str, hops: int = 1) -> set[str]:
    """Return graph node ids within `hops` of `note_id`, following links both ways.

    Chemical relations are meaningful in both directions, so traversal is undirected. The result
    holds node ids, not necessarily note ids: dangling nodes such as external `reaction-<id>`
    citations are included, so a caller needing notes filters through `note_in`.
    """
    if note_id not in graph:
        raise KeyError(f"unknown note id: {note_id!r}")
    undirected = graph.to_undirected(as_view=True)
    lengths = nx.single_source_shortest_path_length(undirected, note_id, cutoff=hops)
    return set(lengths) - {note_id}


def _newest_note_mtime(notes_dir: Path) -> float | None:
    """The newest note's mtime under `notes_dir`, or None for a tree with no note in it.

    The scan is O(notes) and its caller is a gauge rendered on every scrape (synchronously, on the
    event loop), so it is cached for `knowledge_age_scan_ttl_seconds`. Only the mtime is cached; the
    age is recomputed against `time.time()` on every read, so a stalled corpus keeps ageing and the
    cache can only delay noticing a newer corpus. Not under `_corpus_lock`, so a scrape never waits
    on a parse. Concurrent scans write back ordered by `scanned_at`, so the latest-started scan
    wins.
    """
    key = str(notes_dir)
    ttl = settings.knowledge_age_scan_ttl_seconds
    if ttl > 0:
        with _CACHE_LOCK:
            cached = _NEWEST_MTIME.get(key)
            if cached is not None and time.monotonic() - cached[0] < ttl:
                return cached[1]
    # Stamped from before the scan, not after: the entry is only as fresh as the moment the scan
    # started looking, and dating it later would extend the window by the scan's own duration.
    scanned_at = time.monotonic()
    newest = max((stat.st_mtime for _, stat in scan_notes_dir(notes_dir)), default=None)
    with _CACHE_LOCK:
        # Skip if a scan that started at or after this one already wrote its result.
        cached = _NEWEST_MTIME.get(key)
        if cached is None or scanned_at >= cached[0]:
            _NEWEST_MTIME[key] = (scanned_at, newest)
    return newest


#: What `knowledge_sync_age_seconds` reports for a tree with no note. Negative so it cannot be read
#: as an age; 0 would look like a fresh corpus.
NO_NOTES = -1.0


def knowledge_sync_age_seconds() -> float:
    """Seconds since the newest note on this pod's knowledge tree was last written.

    Read from the volume itself, so a sync sidecar that silently stopped refreshing becomes visible
    as a metric. It measures the age of the newest content on this pod, not of the last sync run, so
    a quiet corpus also ages; the alert threshold is deployment-specific
    (`monitoring.alerts.knowledgeCorpusStaleSeconds`).
    """
    newest = _newest_note_mtime(settings.knowledge_path)
    if newest is None:
        return NO_NOTES
    # Clamped at zero: a note written by a sidecar whose clock is a second ahead of this container's
    # would otherwise publish a negative age, which is the value that means "no notes".
    return max(0.0, time.time() - newest)


# Bound at import: any process that reads the graph can report its age.
record_metric(
    lambda m: m.bind_gauge("chemclaw_knowledge_sync_age_seconds", knowledge_sync_age_seconds)
)
