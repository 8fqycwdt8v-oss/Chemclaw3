"""Notes that disagree with each other, surfaced rather than silently both returned (KM-8).

Retrieval used to hand back every matching note with no indication that two of them said
incompatible things. For a system whose output a chemist acts on, "here are two answers, one of
them wrong, good luck" is a worse failure than returning neither — it looks like corroboration.

**What this detects, precisely, and what it deliberately does not.** There is no property extractor
here, and there should not be: parsing "the yield was 82%" out of free prose and comparing it
across notes is a natural-language problem this layer would get subtly wrong, and a false conflict
is as damaging as a missed one. Two signals are used instead, both of which the data actually
supports:

- **Declared.** A `contradicts` or `supersedes` relation between two notes (STO-8). The author —
  human or agent — said so. Nothing is inferred.
- **Suspected.** Two notes of the same type, about the same compound, whose validity windows
  overlap and whose stated confidences disagree materially. This does not claim the notes conflict;
  it claims they are the kind of pair that a reader should look at, which is why it is reported at
  a lower severity and named `suspected`.

A conflict is a **flag on the evidence**, never a filter. Dropping one side would be this layer
deciding which of two curated notes is right, and it has no basis for that.
"""

import threading
from collections import defaultdict
from datetime import date
from pathlib import Path

from pydantic import BaseModel

from chemclaw.core.config import settings
from chemclaw.kg.graph import NotesFingerprint, cached_notes
from chemclaw.kg.note import Note

# Relations that assert an incompatibility outright. `superseded-by` is not here: it points from
# the *retired* note forward, and a retired note is already excluded from current-evidence sweeps
# by `Note.is_current`, so flagging it would report a disagreement the reader cannot act on.
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


def _declared(notes: list[Note], known: set[str]) -> list[Conflict]:
    """Conflicts an author stated through a `contradicts`/`supersedes` relation."""
    return [
        Conflict(
            note_id=note.id,
            other_id=relation.to,
            kind="declared",
            detail=f"{note.id} {relation.rel} {relation.to}",
        )
        for note in notes
        for relation in note.outgoing_relations()
        if relation.rel in _CONFLICTING_RELATIONS and relation.to in known
    ]


def _overlaps(left: Note, right: Note) -> bool:
    """Whether two notes' validity windows intersect at all.

    Two notes about one compound that were never simultaneously valid are a *history*, not a
    disagreement — the later one replaced the earlier one, which is the system working.
    """
    if left.valid_to is not None and right.valid_from is not None:
        if left.valid_to < right.valid_from:
            return False
    if right.valid_to is not None and left.valid_from is not None:
        if right.valid_to < left.valid_from:
            return False
    return True


def _suspected(notes: list[Note]) -> list[Conflict]:
    """Same-compound, same-type, concurrently-valid notes whose confidences disagree.

    Grouped by `(type, compound_smiles)` because that is the coarsest pairing that is still about
    one thing: two `reaction` notes on one compound may well describe different experiments, but a
    materially different confidence between them is worth a reader's eye. Notes with no stated
    confidence are skipped — an absent confidence is not a low one.
    """
    grouped: dict[tuple[str, str], list[Note]] = defaultdict(list)
    for note in notes:
        if note.compound_smiles and note.confidence is not None:
            grouped[(note.type, note.compound_smiles)].append(note)

    threshold = settings.conflict_confidence_gap
    found: list[Conflict] = []
    for group in grouped.values():
        ordered = sorted(group, key=lambda note: note.id)
        for index, left in enumerate(ordered):
            for right in ordered[index + 1 :]:
                assert left.confidence is not None and right.confidence is not None
                gap = abs(left.confidence - right.confidence)
                if gap < threshold or not _overlaps(left, right):
                    continue
                found.append(
                    Conflict(
                        note_id=left.id,
                        other_id=right.id,
                        kind="suspected",
                        detail=(
                            f"both describe {left.compound_smiles} as {left.type} notes valid at "
                            f"the same time, with confidence {left.confidence} vs "
                            f"{right.confidence}"
                        ),
                    )
                )
    return found


def find_conflicts(notes: list[Note], as_of: date | None = None) -> list[Conflict]:
    """Every conflict among `notes`, declared ones first, deduplicated by pair.

    `as_of` restricts the scan to notes current on that date, which is what a retrieval-time caller
    wants: a superseded note is already out of the evidence sweep, so reporting it as conflicting
    with its own replacement would be noise. Omit it to scan the whole corpus, which is what a
    curation pass over the graph wants.
    """
    scanned = [note for note in notes if as_of is None or note.is_current(as_of)]
    known = {note.id for note in scanned}
    found = _declared(scanned, known) + _suspected(scanned)

    seen: set[tuple[str, str]] = set()
    unique = []
    for conflict in found:
        if conflict.pair() in seen:
            continue
        seen.add(conflict.pair())
        unique.append(conflict)
    return unique


def conflicts_by_note(conflicts: list[Conflict]) -> dict[str, list[Conflict]]:
    """Index conflicts by *each* participating note id, so either end finds the pair.

    A retriever that surfaced only one of two disagreeing notes must still be able to say so; a
    conflict recorded under one id only would be invisible from the other side, which is exactly
    the half a reader is most likely to be holding.
    """
    index: dict[str, list[Conflict]] = defaultdict(list)
    for conflict in conflicts:
        index[conflict.note_id].append(conflict)
        index[conflict.other_id].append(conflict)
    return dict(index)


# The derived conflict map, one entry per directory, validated against the notes' stat fingerprint
# *and* the date it was computed for — `find_conflicts(as_of=…)` scans only the notes current on
# that day, so yesterday's map is a different answer, not a stale one. One entry per directory,
# overwritten on a miss, so it cannot grow.
#
# The lock is held across the *computation*, not merely around the dict access, which is the one
# place this differs from `chemclaw.kg.graph`'s caches. Retrieval reaches this from three worker
# threads at once (the sources of one sweep run under `asyncio.gather`), so a lock that only
# guarded the lookup would let all three miss together and compute the same answer three times in
# parallel — measured at 4,238 ms for the first sweep of a 2,000-note corpus against 1,525 ms for
# one computation. A second caller waiting is strictly better than a second caller duplicating: it
# waits exactly as long as the work it would otherwise have redone. Nothing here awaits, and
# `chemclaw.kg.graph`'s lock is only ever taken *inside* this one, so there is no cycle to deadlock
# on.
_INDEX_LOCK = threading.Lock()
_INDEX_CACHE: dict[str, tuple[NotesFingerprint, date, dict[str, list[str]]]] = {}


def conflict_index(notes_dir: Path, as_of: date) -> dict[str, list[str]]:
    """Map each current note id to the ids it disagrees with — cached behind the notes fingerprint.

    The shape retrieval wants: bare ids, so a chunk can carry `conflicts_with` without dragging the
    `Conflict` models (and their prose `detail`) into the model's context. Computed over the *whole*
    current corpus rather than over the notes a query matched, because a chunk must be flagged even
    when the note it conflicts with was not itself retrieved — which is precisely the case where a
    reader would otherwise see one side and assume it settled.

    **Why it is cached, measured rather than assumed.** This was recomputed from scratch inside
    every `SourceRetriever.retrieve` call: once per `gather_evidence` sweep under the default
    single-source config, three times with `vector` and `lexical` also enabled, and once per section
    of a development report. On a 2,000-note corpus shaped like a real programme (many runs on a few
    substrates) one computation measured **1,525 ms** — so a three-source sweep spent 4.6 s, of
    which 3.0 s was the same answer computed twice more, and the next sweep over an unchanged corpus
    paid all of it again. Every other artifact derived from the corpus (the parsed notes, the
    assembled graph) was already cached behind the same fingerprint; this one was the exception, not
    a deliberate omission.

    It ran on the event loop, too. `load_notes` was offloaded to a thread and the scan that follows
    it was not, so seconds of CPU sat between every other concurrent turn on that worker and its
    next token. Callers now offload the whole function; nothing here awaits, so a caller may.

    The cached map is handed back shared rather than copied, for the reason `build_graph` freezes
    its graph instead of copying it: a per-call copy of a whole-corpus artifact gives most of the
    saving back. Treat it as read-only. The one path that could leak it is a chunk's
    `conflicts_with`, and pydantic validation builds that list afresh.

    Returns an empty map when conflict detection is off or the directory is absent — the caller
    treats "no conflicts" and "not looking for conflicts" identically, because a flag that is not
    computed is one no reader should be shown.
    """
    if not settings.conflict_detection_enabled or not notes_dir.exists():
        return {}
    key = str(notes_dir)
    with _INDEX_LOCK:
        fingerprint, notes = cached_notes(notes_dir)
        cached = _INDEX_CACHE.get(key)
        if cached is not None and cached[0] == fingerprint and cached[1] == as_of:
            return cached[2]
        index = {
            note_id: sorted(
                {
                    conflict.other_id if conflict.note_id == note_id else conflict.note_id
                    for conflict in conflicts
                }
            )
            for note_id, conflicts in conflicts_by_note(find_conflicts(notes, as_of=as_of)).items()
        }
        # Not stored when caching is off (`graph_cache_enabled=false` yields no fingerprint): there
        # would be no key to invalidate it against, so every read would serve the first corpus this
        # process ever saw.
        if fingerprint is not None:
            _INDEX_CACHE[key] = (fingerprint, as_of, index)
    return index
