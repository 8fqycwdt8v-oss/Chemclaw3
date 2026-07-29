"""Retire memory notes whose cluster changed shape (BACKLOG follow-up to D-070/D-072).

Campaign and playbook ids are anchored on a cluster's *smallest* member id (`chemclaw.memory.ids`),
which
keeps a note's id — and therefore its PR-gate branch and merged file — stable while the cluster
**grows**: periodic re-synthesis updates it in place. Two other transitions were not covered:

- **merge** — two clusters become one, whose anchor is one of the two old anchors, leaving the
  *loser's* note in the graph as current knowledge describing a subset that no longer exists;
- **shrink** — the cluster loses its smallest member, so a new id is minted and the pre-shrink
  note stays current beside it.

In both cases retrieval can serve a stale note as fact with nothing linking it to the note that
replaced it. Under GxP that is precisely what the bi-temporal fields exist to prevent, so this
module closes the window the *only* way the note schema already supports: the superseded note gets
`valid_to` set (excluded from current-evidence sweeps by `Note.is_current`, never deleted — it stays
in Git and remains reachable by id) plus a body line naming its replacement.

The replacement is named as **plain text, not a `[[wikilink]]`**: the replacement note is itself an
unmerged proposal in the same run, so a link would dangle and fail `kg-validate` if a reviewer
merged the supersede PR first — an ordering trap for a human, in exchange for an edge nothing
traverses (a non-current note is already out of retrieval).
"""

from datetime import date

from chemclaw.kg.note import Note

_SUPERSEDED_MARKER = "Superseded by"


def supersede_updates(new_notes: list[Note], existing: list[Note], as_of: date) -> list[Note]:
    """Return retired copies of `existing` notes that `new_notes` replaced (empty when none).

    A note is superseded when it carries no end date yet, shares a type with this run's output,
    keeps an id this run no longer mints, and cites at least one member that a new note now covers.
    The id check is what keeps ordinary growth untouched: a grown cluster re-mints the *same* id,
    so that note is updated in place by the normal publish, not retired here. Testing `valid_to`
    rather than `is_current` makes the job idempotent (a re-run cannot re-close, and re-append to,
    a note it already closed) and still covers a note whose validity starts in the future.

    Args:
        new_notes: The notes this synthesis run just built (all of one or more memory types).
        existing: Already-merged notes to check against — typically the whole knowledge corpus;
            notes of unrelated types are ignored, so passing everything is safe.
        as_of: The run's date, used as the retired note's `valid_to`.

    Returns:
        One updated `Note` per superseded note, ready to go through the same PR-gate as the
        new notes themselves. Deterministic order (by note id) so a re-run proposes the same set.
    """
    new_ids = {note.id for note in new_notes}
    types = {note.type for note in new_notes}
    members: dict[str, set[str]] = {note.id: set(note.outgoing_links()) for note in new_notes}
    retired = [
        _retire(note, successors, as_of)
        for note in sorted(existing, key=lambda n: n.id)
        if note.type in types
        and note.id not in new_ids
        and note.valid_to is None
        and (successors := _successors_of(note, members))
    ]
    return retired


def _successors_of(note: Note, members: dict[str, set[str]]) -> list[str]:
    """Ids of the new notes that took over any of `note`'s cited members (sorted, may be empty).

    Overlap — not equality — is the test: a merge hands every member to one successor, a split
    hands them to several, and both mean this note is no longer the current account of them.
    """
    cited = set(note.outgoing_links())
    return sorted(new_id for new_id, new_members in members.items() if cited & new_members)


def _retire(note: Note, successors: list[str], as_of: date) -> Note:
    """Copy `note` with `valid_to` closed and a line naming the notes that replaced it.

    `valid_to` is never set before `valid_from` (the schema rejects that window, F10-G2): a note
    whose validity has not begun is closed at its own start date instead of today.
    """
    valid_to = as_of
    if note.valid_from is not None and note.valid_from > as_of:
        valid_to = note.valid_from
    replaced = ", ".join(successors)
    body = (
        f"{note.body.rstrip()}\n\n"
        f"{_SUPERSEDED_MARKER} {replaced} on {as_of.isoformat()}: this cluster's membership "
        "changed (merge or shrink), so the note above is no longer the current account of its "
        "experiments. Kept for the record; excluded from current-evidence retrieval.\n"
    )
    return note.model_copy(update={"valid_to": valid_to, "body": body})
