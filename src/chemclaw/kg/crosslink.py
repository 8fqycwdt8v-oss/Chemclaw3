"""Reading the graph from the calculation store's side (STO-7).

`Note.calc_refs` points a note at the calculations behind it. This module is the other direction —
given a calculation key, which notes rest on it? — and the reason it is nine lines of dict-building
rather than an index is that the parsed notes are *already* cached in memory by `chemclaw.kg.graph`
(behind a stat fingerprint, KM-14). A second store would be a derived index of a derived index.

Why the direction matters. Before this, the calculation store and the knowledge graph were
disjoint: "what we computed" and "what we know" could not reference each other, so a stale
calculation could not be traced to the conclusions drawn from it, and a conclusion could not be
traced to the run that produced it. That is a provenance gap in a GxP system, not only an
ergonomic one. The forward direction makes a note auditable; this direction makes a recomputation
actionable — when a method version changes and a cached result is invalidated, this is what says
which notes now rest on something the system would no longer reproduce.
"""

from collections import defaultdict
from pathlib import Path

from chemclaw.kg.graph import load_notes
from chemclaw.kg.note import Note


def calc_ref_index(notes: list[Note]) -> dict[str, list[Note]]:
    """Map every cited calculation key to the notes citing it.

    Built over `calc_refs` *and* the calculation half of `artifact_refs`, so a note that cites only
    a specific Hessian is still found by a query about the calculation that produced it — the
    artifact is part of that run, and a caller asking "what rests on this calculation" means to
    include it.
    """
    index: dict[str, list[Note]] = defaultdict(list)
    for note in notes:
        for key in cited_calculations(note):
            index[key].append(note)
    return dict(index)


def cited_calculations(note: Note) -> list[str]:
    """Every calculation key this note rests on, deduplicated in first-seen order.

    An artifact reference contributes the key of the calculation that produced it: `artifact_refs`
    is `<calc_key>#<name>`, and the part before the `#` is a citation of that run whether or not
    the note also listed it outright.
    """
    ordered: dict[str, None] = dict.fromkeys(note.calc_refs)
    for ref in note.artifact_refs:
        key, _, _ = ref.rpartition("#")
        ordered.setdefault(key, None)
    return list(ordered)


def notes_for_calculation(notes_dir: Path, calc_key: str) -> list[Note]:
    """Every note in `notes_dir` that rests on `calc_key`, ordered by id.

    Reads through `chemclaw.kg.graph.load_notes`, so it shares the parsed-note cache with every
    other reader
    and a warm call costs a stat scan rather than a parse of the tree.
    """
    matches = calc_ref_index(load_notes(notes_dir)).get(calc_key, [])
    return sorted(matches, key=lambda note: note.id)
