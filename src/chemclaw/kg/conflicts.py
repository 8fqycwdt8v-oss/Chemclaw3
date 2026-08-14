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
    # How strongly this pair disagrees, on one scale so a note's flags can be *ranked* rather than
    # merely listed. A declared conflict is pinned at the top because an author said so and no
    # heuristic outranks that; a suspected one carries its confidence gap, which `_suspected`
    # already computes to decide whether to report the pair at all. Ranking is what lets the index
    # keep the disagreements a reader can act on and drop the tail — see `conflict_index`.
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

    Declared conflicts rank above every suspected one (an author stated them), then severity, then
    the id — dropping a stated contradiction to make room for a heuristic's guess would invert the
    whole point of `Conflict.kind`, and the id tiebreak keeps two equal pairs deterministic rather
    than dict-ordered.

    **`kind` is the first key because `severity` alone could not carry that guarantee.** It read
    `(-severity, other_id)`, and the two scales meet: `Conflict.severity` defaults to `1.0` and
    `_declared` never overrides it, while `_suspected` passes the confidence *gap*, which reaches
    `1.0` exactly when a 0.0-confidence note faces a 1.0-confidence one. At that tie the ranking
    fell through to the note id — so whether a stated contradiction survived the cap or a
    heuristic's guess displaced it was decided alphabetically. Ordering on `kind` first states the
    invariant the docstring always claimed, and leaves `severity` a meaningful scale within each
    kind rather than a sentinel doing two jobs.
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


def _widest_disagreements(
    ordered: list[tuple[Note, float]], index: int, threshold: float, cap: int
) -> list[Conflict]:
    """The `cap` notes in `ordered` that disagree most with `ordered[index]`, widest gap first.

    `ordered` is sorted by confidence, so the partners that disagree most with any member sit at
    the two ends. Walking inward from both ends and always taking the wider side visits candidates
    in descending order of disagreement — which means the walk can **stop** as soon as the wider
    side falls under `threshold`, since nothing further in can exceed it.

    That is what turns the pairwise scan from quadratic into `cap` steps per note in the ordinary
    case. It matters at the shape a real programme has: an optimization campaign is many runs on
    one substrate, and a synthetic 2,000-note corpus over 7 substrates enumerated **141,156** pairs
    in 637 ms and put ~141 ids on every evidence chunk reaching the model. Both numbers are the
    same fact — a scan that flags a note against every other note on its substrate tells a reader
    nothing they can act on, and costs the most exactly where the system is most used.

    A note is never its own partner (the walk steps over itself without ending), and the threshold
    check is what ends the walk rather than the note count, so a group whose members all agree
    costs one comparison per note.
    """
    note, confidence = ordered[index]
    low, high = 0, len(ordered) - 1
    taken: list[Conflict] = []
    while low <= high and len(taken) < cap:
        below, below_confidence = ordered[low]
        above, above_confidence = ordered[high]
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
        if not _overlaps(note, other):
            continue
        taken.append(
            Conflict(
                note_id=note.id,
                other_id=other.id,
                kind="suspected",
                # The partner's confidence is carried out of the branch that chose it, not
                # re-derived from the gap. The re-derivation asked `other is below`, and when the
                # two pointers meet `below` *is* `above` — so the test answered True for both
                # branches and the `above` case rendered `2*confidence - gap`: a number belonging
                # to no note, in prose a chemist reads at the PR gate.
                detail=(
                    f"both describe {note.compound_smiles} as {note.type} notes valid at "
                    f"the same time, with confidence {confidence} vs {other_confidence}"
                ),
                severity=gap,
            )
        )
    return taken


def _suspected(notes: list[Note], cap: int) -> list[Conflict]:
    """Same-compound, same-type, concurrently-valid notes whose confidences disagree.

    Grouped by `(type, compound_smiles)` because that is the coarsest pairing that is still about
    one thing: two `reaction` notes on one compound may well describe different experiments, but a
    materially different confidence between them is worth a reader's eye. Notes with no stated
    confidence are skipped — an absent confidence is not a low one.

    Each note contributes at most `cap` pairs, its widest disagreements first. This is a change of
    *claim*, not only of volume: the exhaustive list said "here is every note on this substrate
    whose confidence differs from yours", which is a fact about the corpus rather than a signal
    about the note. KM-8's stated target was a signal, and the strongest few disagreements are one.
    """
    # Carry the confidence alongside the note rather than re-reading `note.confidence` inside the
    # loop: the filter above already established it is not None, and expressing that structurally
    # is what removes the `assert` that used to narrow the type here. An `assert` is stripped under
    # `python -O`, so a narrowing that only holds because of one is a narrowing that can stop
    # holding in production and nowhere else.
    grouped: dict[tuple[str, str], list[tuple[Note, float]]] = defaultdict(list)
    for note in notes:
        if note.compound_smiles and note.confidence is not None:
            grouped[(note.type, note.compound_smiles)].append((note, note.confidence))

    threshold = settings.conflict_confidence_gap
    found: list[Conflict] = []
    for group in grouped.values():
        # Sorted by confidence, which is what puts a note's widest disagreements at the ends and
        # lets `_widest_disagreements` stop early; the id is the tiebreak so the scan stays
        # deterministic over notes that state the same confidence.
        ordered = sorted(group, key=lambda pair: (pair[1], pair[0].id))
        for index in range(len(ordered)):
            found.extend(_widest_disagreements(ordered, index, threshold, cap))
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
_INDEX_CACHE: dict[str, tuple[NotesFingerprint, date, dict[str, NoteConflicts]]] = {}


def conflict_index(notes_dir: Path, as_of: date) -> dict[str, NoteConflicts]:
    """Map each current note id to what it disagrees with — cached behind the notes fingerprint.

    The shape retrieval wants: bare ids and a count, so a chunk can carry `conflicts_with` without
    dragging the `Conflict` models (and their prose `detail`) into the model's context. The ids are
    the strongest few and the count is all of them — `NoteConflicts` says why both travel.
    Computed over the *whole*
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
            note_id: _strongest(note_id, conflicts)
            for note_id, conflicts in conflicts_by_note(find_conflicts(notes, as_of=as_of)).items()
        }
        # Not stored when caching is off (`graph_cache_enabled=false` yields no fingerprint): there
        # would be no key to invalidate it against, so every read would serve the first corpus this
        # process ever saw.
        if fingerprint is not None:
            _INDEX_CACHE[key] = (fingerprint, as_of, index)
    return index
