"""Notes that disagree with each other, surfaced rather than silently both returned.

Two disagreeing notes returned without comment look like corroboration. No property is extracted
from prose (a false conflict is as damaging as a missed one); two signals are used:

- Declared: a `contradicts` or `supersedes` relation between two notes. Nothing is inferred.
- Suspected: two notes of the same type about the same compound, with overlapping validity windows
  and materially different confidences. Reported at lower severity as worth a look.

A conflict is a flag on the evidence, never a filter: this layer has no basis to pick a side.
"""

import heapq
import logging
import threading
from collections import defaultdict
from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

from chemclaw.core.config import settings
from chemclaw.kg.graph import NotesFingerprint, cached_notes
from chemclaw.kg.note import Note

log = logging.getLogger(__name__)

# Relations that assert an incompatibility outright. Not `superseded-by`: it points from the retired
# note, which current-evidence sweeps already exclude.
_CONFLICTING_RELATIONS = frozenset({"contradicts", "supersedes"})


class Conflict(BaseModel):
    """Two notes that disagree, and on what basis the disagreement is claimed.

    `kind` is load-bearing rather than decorative: a `declared` conflict is a fact recorded by an
    author, while a `suspected` one is a heuristic's suggestion. A reader — and a report — should
    weigh them differently, so the model refuses to flatten them into one "conflict".
    """

    note_id: str
    other_id: str
    kind: str
    detail: str
    # How strongly this pair disagrees, so a note's flags can be ranked and the tail dropped.
    # Declared conflicts are pinned at 1.0; suspected ones carry their confidence gap.
    severity: float = 1.0

    def pair(self) -> tuple[str, str]:
        """The unordered pair, for deduplicating a conflict found from both ends."""
        return (
            (self.note_id, self.other_id)
            if self.note_id < self.other_id
            else (
                self.other_id,
                self.note_id,
            )
        )


class NoteConflicts(BaseModel):
    """What one note disagrees with: the strongest few ids, and how many there were in total.

    The count is not decoration. `ids` is capped (`conflict_max_per_note`) because a note on a
    heavily-worked substrate can be flagged against a hundred others, and a hundred ids on an
    evidence chunk is noise a chemist cannot act on. But this repository's rule is that a silent
    truncation reads as completeness — so the number that was cut is carried beside the list rather
    than dropped with it, and every surface that renders the ids says "3 of 141" when that is the
    truth. A reader who sees three ids and no count would reasonably conclude there were three.
    """

    ids: list[str]
    total: int

    @property
    def truncated(self) -> int:
        """How many disagreements are not named in `ids` — zero when the list is complete."""
        return max(0, self.total - len(self.ids))


def _strongest(note_id: str, conflicts: list[Conflict]) -> NoteConflicts:
    """One note's disagreements, worst first and capped, with the full count kept.

    Ordered by kind (declared before suspected), then severity, then id. Kind comes first because a
    suspected gap can also reach 1.0, and a stated contradiction must never be displaced by a
    heuristic.
    """
    ranked = sorted(
        {
            (conflict.other_id if conflict.note_id == note_id else conflict.note_id): conflict
            for conflict in conflicts
        }.items(),
        key=lambda item: (item[1].kind != "declared", -item[1].severity, item[0]),
    )
    cap = settings.conflict_max_per_note
    return NoteConflicts(ids=[other_id for other_id, _ in ranked[:cap]], total=len(ranked))


def _declared(notes: list[Note], known: set[str]) -> list[Conflict]:
    """Conflicts an author stated through a `contradicts`/`supersedes` relation.

    A self-edge is an authoring mistake and is excluded.
    """
    return [
        Conflict(
            note_id=note.id,
            other_id=relation.to,
            kind="declared",
            detail=f"{note.id} {relation.rel} {relation.to}",
        )
        for note in notes
        for relation in note.outgoing_relations()
        if relation.rel in _CONFLICTING_RELATIONS
        and relation.to in known
        and relation.to != note.id
    ]


def _suspected_conflict(
    note: Note, confidence: float, other: Note, other_confidence: float
) -> Conflict:
    """One suspected pair, its severity the confidence gap — the one place the prose is written."""
    return Conflict(
        note_id=note.id,
        other_id=other.id,
        kind="suspected",
        detail=(
            f"both describe {note.compound_smiles} as {note.type} notes valid at "
            f"the same time, with confidence {confidence} vs {other_confidence}"
        ),
        severity=abs(confidence - other_confidence),
    )


def _widest_disagreements(
    candidates: list[tuple[Note, float]],
    note: Note,
    confidence: float,
    threshold: float,
    cap: int,
) -> list[Conflict]:
    """The `cap` notes in `candidates` that disagree most with `note`, widest gap first.

    `candidates` is sorted by confidence, so the widest disagreements sit at the two ends. Walking
    inward and taking the wider side visits candidates in descending disagreement, so the walk stops
    once the wider side falls under `threshold`: at most `cap` steps per note rather than quadratic.

    Caller contract: every candidate overlaps the note's validity window. An overlap check here
    would consume steps without ending the walk and defeat the early stop. The note itself is
    skipped.
    """
    low, high = 0, len(candidates) - 1
    taken: list[Conflict] = []
    while low <= high and len(taken) < cap:
        below, below_confidence = candidates[low]
        above, above_confidence = candidates[high]
        # The signed gaps to the two ends; at least one is non-negative while `low <= high` and,
        # whichever is larger, no candidate still between them can beat it.
        if confidence - below_confidence >= above_confidence - confidence:
            other, gap, other_confidence = below, confidence - below_confidence, below_confidence
            low += 1
        else:
            other, gap, other_confidence = above, above_confidence - confidence, above_confidence
            high -= 1
        if other is note:
            continue  # the walk reached the note itself, which is not a disagreement
        if gap < threshold:
            break
        taken.append(_suspected_conflict(note, confidence, other, other_confidence))
    return taken


def _conditional_disagreements(
    windowed: list[tuple[Note, float]], threshold: float, cap: int
) -> list[Conflict]:
    """Suspected pairs among windowed notes whose overlap is genuinely conditional.

    A start-ordered interval sweep, so only truly overlapping pairs are examined. Pairs the walks
    already cover (two endless or two startless notes) are skipped, which also keeps a corpus of
    `valid_from`-only notes from making the sweep quadratic. Per-note output is capped at the `cap`
    widest gaps via a heap.
    """
    events = sorted(windowed, key=lambda pair: (pair[0].valid_from or date.min, pair[0].id))
    bounded: list[tuple[Note, float]] = []  # active notes with a closed end (valid_to set)
    endless: list[tuple[Note, float]] = []  # active notes with valid_from set, valid_to absent
    # Per note id: a min-heap of (gap, tiebreak, conflict) holding its `cap` widest pairs.
    best: dict[str, list[tuple[float, int, Conflict]]] = defaultdict(list)
    tiebreak = 0

    def _keep(note_id: str, gap: float, conflict: Conflict) -> None:
        nonlocal tiebreak
        tiebreak += 1
        heap = best[note_id]
        if len(heap) < cap:
            heapq.heappush(heap, (gap, tiebreak, conflict))
        elif gap > heap[0][0]:
            heapq.heapreplace(heap, (gap, tiebreak, conflict))

    for note, confidence in events:
        start = note.valid_from or date.min
        bounded = [(n, c) for n, c in bounded if n.valid_to is not None and n.valid_to >= start]
        candidates = bounded if note.valid_to is None else bounded + endless
        for other, other_confidence in candidates:
            # Skip the guaranteed classes the walks own: both endless, or both startless.
            if note.valid_to is None and other.valid_to is None:
                continue
            if note.valid_from is None and other.valid_from is None:
                continue
            gap = abs(confidence - other_confidence)
            if gap < threshold:
                continue
            conflict = _suspected_conflict(note, confidence, other, other_confidence)
            _keep(note.id, gap, conflict)
            _keep(other.id, gap, conflict)
        if note.valid_to is None:
            if note.valid_from is not None:
                endless.append((note, confidence))
        else:
            bounded.append((note, confidence))

    return [conflict for heap in best.values() for _, _, conflict in heap]


@lru_cache(maxsize=4096)
def _grouping_smiles(smiles: str) -> str:
    """The canonical form a conflict group keys on, falling back to the raw string.

    Canonical so two spellings of one molecule pair up. Unparseable input keeps its raw spelling, so
    the note is still scanned.
    """
    from chemclaw.core.chem import InvalidSmilesError, canonical_smiles

    try:
        return canonical_smiles(smiles)
    except InvalidSmilesError:
        return smiles


def _suspected(notes: list[Note], cap: int) -> list[Conflict]:
    """Same-compound, same-type, concurrently-valid notes whose confidences disagree.

    Grouped by `(type, canonical compound_smiles)`. Notes without a stated confidence are skipped:
    absent is not low. Each note contributes at most `cap` pairs per candidate class, found by
    window class so every comparison either must overlap (early-stopping end-walks) or is known to
    (the sweep):

    - every note against the open notes (no window);
    - endless notes (`valid_to` absent) against each other;
    - startless notes (`valid_from` absent) against each other;
    - everything else by the interval sweep.
    """
    # Carry the confidence with the note so the not-None narrowing is structural rather than an
    # `assert` (stripped under `python -O`).
    grouped: dict[tuple[str, str], list[tuple[Note, float]]] = defaultdict(list)
    for note in notes:
        if note.compound_smiles and note.confidence is not None:
            grouped[(note.type, _grouping_smiles(note.compound_smiles))].append(
                (note, note.confidence)
            )

    threshold = settings.conflict_confidence_gap
    found: list[Conflict] = []
    for group in grouped.values():
        # Sorted by confidence so the widest disagreements sit at the ends; id breaks ties.
        ordered = sorted(group, key=lambda pair: (pair[1], pair[0].id))
        open_notes = [
            pair for pair in ordered if pair[0].valid_from is None and pair[0].valid_to is None
        ]
        endless = [
            pair for pair in ordered if pair[0].valid_to is None and pair[0].valid_from is not None
        ]
        startless = [
            pair for pair in ordered if pair[0].valid_from is None and pair[0].valid_to is not None
        ]
        windowed = [
            pair
            for pair in ordered
            if pair[0].valid_from is not None or pair[0].valid_to is not None
        ]
        for note, confidence in ordered:
            found.extend(_widest_disagreements(open_notes, note, confidence, threshold, cap))
        for note, confidence in endless:
            found.extend(_widest_disagreements(endless, note, confidence, threshold, cap))
        for note, confidence in startless:
            found.extend(_widest_disagreements(startless, note, confidence, threshold, cap))
        found.extend(_conditional_disagreements(windowed, threshold, cap))
    return found


def find_conflicts(notes: list[Note], as_of: date | None = None) -> list[Conflict]:
    """Every conflict among `notes`, declared ones first, deduplicated by pair.

    `as_of` restricts the scan to notes current on that date (retrieval); omit it to scan the whole
    corpus (curation).
    """
    scanned = [note for note in notes if as_of is None or note.is_current(as_of)]
    known = {note.id for note in scanned}
    found = _declared(scanned, known) + _suspected(scanned, settings.conflict_max_per_note)

    seen: set[tuple[str, str]] = set()
    unique = []
    for conflict in found:
        if conflict.pair() in seen:
            continue
        seen.add(conflict.pair())
        unique.append(conflict)
    return unique


def conflicts_by_note(conflicts: list[Conflict]) -> dict[str, list[Conflict]]:
    """Index conflicts by each participating note id, so either end finds the pair.

    A retriever that surfaced only one side must still be able to flag it.
    """
    index: dict[str, list[Conflict]] = defaultdict(list)
    for conflict in conflicts:
        index[conflict.note_id].append(conflict)
        index[conflict.other_id].append(conflict)
    return dict(index)


# The derived conflict map, one entry per directory, valid for one notes fingerprint and one `as_of`
# (`None` is the whole-corpus scan, a different answer). Overwritten on a miss, so it cannot grow.
#
# The per-directory lock is held across the computation, not just the dict access, so concurrent
# retrieval threads wait for one computation instead of duplicating it. The corpus snapshot is taken
# before this lock, so lock order is always graph-then-index. Locks are never removed, as in
# `graph._COMPUTE_LOCKS`.
_LOCKS_GUARD = threading.Lock()
_INDEX_LOCKS: dict[str, threading.Lock] = {}
_INDEX_CACHE: dict[str, tuple[NotesFingerprint, date | None, dict[str, NoteConflicts]]] = {}

# Warn once per process that with `graph_cache_enabled=false` every retrieval pays the full scan.
_WARNED_UNCACHED = False


def conflict_index(notes_dir: Path, as_of: date | None) -> dict[str, NoteConflicts]:
    """Map each note id to what it disagrees with, cached behind the notes fingerprint and `as_of`.

    `as_of` follows `find_conflicts`: a date scans only notes current on it, `None` the whole
    corpus. A caller that serves retired notes must pass `None`, or those notes are never scanned
    and read as conflict-free.

    Returns bare ids (the strongest few) and a count, so chunks can carry `conflicts_with` without
    the full `Conflict` models. Computed over the whole current corpus, so a chunk is flagged even
    when its counterpart was not retrieved. Cached because every retrieval source and report section
    would otherwise recompute it. Synchronous: callers offload it to a thread. The map is shared,
    not copied; treat it as read-only.

    Returns an empty map when conflict detection is off or the directory is absent.
    """
    if not settings.conflict_detection_enabled or not notes_dir.exists():
        return {}
    key = str(notes_dir)
    # Snapshot before taking this module's lock; `cached_notes` has its own lock, and nesting them
    # would couple both critical sections for a whole cold parse.
    fingerprint, notes = cached_notes(notes_dir)
    if fingerprint is None:
        global _WARNED_UNCACHED
        if not _WARNED_UNCACHED:
            _WARNED_UNCACHED = True
            log.warning(
                "graph_cache_enabled=false: the conflict index cannot be cached, so every "
                "retrieval pays the full conflict scan for %s",
                notes_dir,
            )
        # Not stored: there would be no key to invalidate it against, so every read would serve
        # the first corpus this process ever saw.
        return {
            note_id: _strongest(note_id, conflicts)
            for note_id, conflicts in conflicts_by_note(find_conflicts(notes, as_of=as_of)).items()
        }
    with _LOCKS_GUARD:
        lock = _INDEX_LOCKS.setdefault(key, threading.Lock())
    with lock:
        cached = _INDEX_CACHE.get(key)
        if cached is not None and cached[0] == fingerprint and cached[1] == as_of:
            return cached[2]
        index = {
            note_id: _strongest(note_id, conflicts)
            for note_id, conflicts in conflicts_by_note(find_conflicts(notes, as_of=as_of)).items()
        }
        _INDEX_CACHE[key] = (fingerprint, as_of, index)
    return index
