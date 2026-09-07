"""Retire memory notes whose cluster changed shape (BACKLOG follow-up to D-070/D-072).

Campaign and playbook ids are anchored on a cluster's *smallest* member id (`chemclaw.memory.ids`),
which keeps a note's id — and therefore the file it occupies in `knowledge/` — stable while the
cluster **grows**: periodic re-synthesis updates it in place. Two other transitions were not
covered:

- **merge** — two clusters become one, whose anchor is one of the two old anchors, leaving the
  *loser's* note in the graph as current knowledge describing a subset that no longer exists;
- **shrink** — the cluster loses its smallest member, so a new id is minted and the pre-shrink
  note stays current beside it.

In both cases retrieval can serve a stale note as fact with nothing linking it to the note that
replaced it. That is precisely what the bi-temporal fields exist to prevent, so this
module closes the window the *only* way the note schema already supports: the superseded note gets
`valid_to` set (excluded from current-evidence sweeps by `Note.is_current`, never deleted — it stays
in Git and remains reachable by id) plus a body line naming its replacement.

Only notes this synthesis itself minted are candidates. "Same type, overlapping members" is not
enough — see `_is_synthesis_minted` for the promoted-observation playbook it wrongly retired.

The retired note points forward with a real `superseded-by` edge to its successor (D-2026-08-27
wave). It used to be plain text only, because retirement and replacement went to *separate* PR-gate
branches and the link dangled if a reviewer merged the retirement first.

**That reason is gone and the guarantee is not, which is why this paragraph is rewritten rather
than deleted.** `D-2026-09-05-the-gate-is-deleted-not-dormant` removed the gate and every module
behind it, so there is no `propose_note` and no branch for `kg-validate` to check. What replaces
the branch as the unit is `kg/record.py`'s **write order** — dependencies, then the subject, then
the retirements — chosen because a reader can now see a half-written unit, and stated there as the
invariant that replaces "one PR is one reviewable change". The retirement is written last, after
the successor it cites, so the edge cannot resolve to nothing at any instant a reader could
observe. The cost is stated in `_build_write` rather than hidden: between those two writes both
notes are current and retrieval can serve both. A second successor (a split) is still named in
prose only.
"""

from datetime import date

from chemclaw.kg.note import Note, Relation
from chemclaw.memory.ids import is_cluster_anchored

_SUPERSEDED_MARKER = "Superseded by"


def supersede_updates(new_notes: list[Note], existing: list[Note], as_of: date) -> list[Note]:
    """Return retired copies of `existing` notes that `new_notes` replaced (empty when none).

    A note is superseded when it carries no end date yet, shares a type with this run's output,
    is one **this synthesis itself minted** (`_is_synthesis_minted`), keeps an id this run no
    longer mints, and cites at least one member that a new note now covers. The id check is what
    keeps ordinary growth untouched: a grown cluster re-mints the *same* id, so that note is
    updated in place by the normal publish, not retired here. Testing `valid_to` rather than
    `is_current` makes the job idempotent (a re-run cannot re-close, and re-append to, a note it
    already closed) and still covers a note whose validity starts in the future.

    The type match stays *beside* the lineage test rather than being replaced by it: the two rule
    out different things. Lineage rules out a note this job never wrote; the type match rules out
    an `optimization-campaign` note being retired by the *campaign* run that happens to share a
    reaction with it, which is a different job's cluster and a different id sequence.

    Args:
        new_notes: The notes this synthesis run just built (all of one or more memory types).
        existing: Already-merged notes to check against — typically the whole knowledge corpus;
            notes outside the synthesis lineage are ignored, so passing everything is safe.
        as_of: The run's date, used as the retired note's `valid_to`.

    Returns:
        One updated `Note` per superseded note, written by the same path as the new notes
        themselves (`kg/record.py`). Deterministic order (by note id) so a re-run yields the same
        set.
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

    The lineage test, and the reason it replaced a bare type match. "Same type, overlapping
    members" retires notes this job could never have written, and one such note now exists by
    design: since D-161 the observations tier promotes an observation into `playbook-<obs-hash>`,
    an id anchored on the observation's *scope* rather than on the cluster's smallest member, so
    `distill_playbooks` can never re-mint it. It was therefore always "an id this run no longer
    mints" and was proposed for retirement on every run, carrying the body line "this cluster's
    membership changed (merge or shrink)" — which is untrue of it, and writing one drops a
    human-approved playbook out of every current-evidence sweep via `Note.is_current`. There is no
    review step left to catch that, which makes the lineage test the control rather than a
    convenience. The same match caught human-authored notes
    of a memory type; what stops that reaching a person's file is
    `kg/git_writer._refuse_to_clobber_a_person`, which raises rather than overwrite a note a human
    wrote — a real guard with a real caller, not the gate's vanished pre-flight check.

    Reconstructing the id is the whole check, and it lives in `chemclaw.memory.ids` beside the
    `stable_id` it inverts — `memory.playbook` asks the same question to state a note's provenance,
    and the two must never be able to answer it differently. This wrapper exists to name what the
    answer *means* here, which is not the same thing as what it means there.
    """
    return is_cluster_anchored(note.id, note.outgoing_links())


def _successors_of(note: Note, members: dict[str, set[str]]) -> list[str]:
    """Ids of the new notes that took over any of `note`'s cited members (sorted, may be empty).

    Overlap — not equality — is the test: a merge hands every member to one successor, a split
    hands them to several, and both mean this note is no longer the current account of them.
    """
    cited = set(note.outgoing_links())
    return sorted(new_id for new_id, new_members in members.items() if cited & new_members)


def retire_note(note: Note, successors: list[str], as_of: date) -> Note:
    """Copy `note` with `valid_to` closed, a `superseded-by` edge, and prose naming the rest.

    The first successor gets the typed edge — it is the note whose submission carries this
    retirement, so the link resolves in the same PR — and every successor is named in the body.
    `valid_to` is never set before `valid_from` (the schema rejects that window, F10-G2): a note
    whose validity has not begun is closed at its own start date instead of today.

    Public because the observations tier retires a promoted playbook the same way when a superset
    finding replaces it (`durable.observation_jobs`), and two spellings of "how a note is retired"
    is how the two would come to disagree.
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
