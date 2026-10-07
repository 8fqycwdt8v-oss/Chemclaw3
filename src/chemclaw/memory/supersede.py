"""Retire memory notes whose cluster changed shape.

Campaign and playbook ids are anchored on a cluster's smallest member id, so a growing cluster
is updated in place. Two transitions are not: a **merge** leaves the losing anchor's note
describing a subset that no longer exists, and a **shrink** mints a new id beside the old note.
Either way retrieval could serve a stale note as current fact.

So the superseded note gets `valid_to` set (excluded by `Note.is_current`, never deleted), a
`superseded-by` edge to its first successor, and a body line naming every successor. Only
notes this synthesis itself minted are candidates (`_is_synthesis_minted`). `kg/record.py`
writes the retirement after the successor it cites, so the edge always resolves; until then both
notes are briefly current.
"""

from datetime import date

from chemclaw.kg.note import Note, Relation
from chemclaw.memory.ids import is_cluster_anchored

_SUPERSEDED_MARKER = "Superseded by"


def supersede_updates(new_notes: list[Note], existing: list[Note], as_of: date) -> list[Note]:
    """Return retired copies of `existing` notes that `new_notes` replaced (empty when none).

    A note is superseded when it has no end date yet, shares a type with this run's output, was
    minted by this synthesis, keeps an id this run no longer mints, and cites a member a new note
    now covers. Ordinary growth re-mints the same id and is updated in place, not retired. Testing
    `valid_to` rather than `is_current` makes the job idempotent. The type match rules out a note
    from a different job's cluster that happens to share a reaction.

    Args:
        new_notes: The notes this synthesis run just built.
        existing: Already-merged notes to check against; notes outside the synthesis lineage are
            ignored, so passing the whole corpus is safe.
        as_of: The run's date, used as the retired note's `valid_to`.

    Returns:
        One updated `Note` per superseded note, sorted by note id.
    """
    new_ids = {note.id for note in new_notes}
    types = {note.type for note in new_notes}
    members: dict[str, set[str]] = {note.id: set(note.outgoing_links()) for note in new_notes}
    retired = [
        retire_note(note, successors, as_of)
        for note in sorted(existing, key=lambda n: n.id)
        if note.type in types
        and note.id not in new_ids
        and note.valid_to is None
        and _is_synthesis_minted(note)
        and (successors := _successors_of(note, members))
    ]
    return retired


def _is_synthesis_minted(note: Note) -> bool:
    """True when `note`'s id is exactly the one memory synthesis mints from the members it cites.

    A bare type match would retire notes this job could never have written, such as a playbook
    promoted from an observation (anchored on the observation's scope), and drop them out of every
    current-evidence sweep. Nothing downstream reviews the retirement, so this test is the control.
    The id reconstruction lives in `chemclaw.memory.ids` beside the `stable_id` it inverts.
    """
    return is_cluster_anchored(note.id, note.outgoing_links())


def _successors_of(note: Note, members: dict[str, set[str]]) -> list[str]:
    """Ids of the new notes that took over any of `note`'s cited members (sorted, may be empty).

    Overlap, not equality: a merge hands every member to one successor, a split to several.
    """
    cited = set(note.outgoing_links())
    return sorted(new_id for new_id, new_members in members.items() if cited & new_members)


#: Why a synthesis run retires a note — the default, because that job is the original caller.
_CLUSTER_CHANGED = (
    "this cluster's membership changed (merge or shrink), so the note above is no longer the "
    "current account of its experiments"
)


def retire_note(
    note: Note, successors: list[str], as_of: date, reason: str = _CLUSTER_CHANGED
) -> Note:
    """Copy `note` with `valid_to` closed, a `superseded-by` edge, and prose naming the rest.

    The first successor gets the typed edge; every successor is named in the body. `valid_to` is
    never set before `valid_from` (the schema rejects it), so a not-yet-valid note closes at its
    own start date. Public so every retirement path (observations, compound re-keying) shares one
    shape; `reason` is the sentence the retired body gives.
    """
    valid_to = as_of
    if note.valid_from is not None and note.valid_from > as_of:
        valid_to = note.valid_from
    replaced = ", ".join(successors)
    body = (
        f"{note.body.rstrip()}\n\n"
        f"{_SUPERSEDED_MARKER} {replaced} on {as_of.isoformat()}: {reason}. "
        "Kept for the record; excluded from current-evidence retrieval.\n"
    )
    relations = [
        *note.relations,
        Relation(rel="superseded-by", to=successors[0]),
    ]
    return note.model_copy(update={"valid_to": valid_to, "body": body, "relations": relations})


def carrier_of(retired: Note) -> str:
    """Which successor's submission carries this retirement — the typed edge's target."""
    for relation in retired.relations:
        if relation.rel == "superseded-by":
            return relation.to
    raise ValueError(f"{retired.id!r} is not a retirement this module produced")
