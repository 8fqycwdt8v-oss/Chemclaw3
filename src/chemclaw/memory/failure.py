"""Recording that evidence was wrong, in band, and readable the moment it lands.

A `failure-mode` note refutes another note by being written beside it, never by editing or deleting
it, and carries a `contradicts` edge so `kg.conflicts` and retrieval flag the refuted note.
`close_refuted_note` is the optional second half that closes the refuted note's validity, for when
"it held until then" is true. `failures_against` is the query side.
"""

from collections.abc import Collection, Iterable
from datetime import date

from chemclaw.core.errors import ChemclawError
from chemclaw.core.ids import stable_hash
from chemclaw.kg.note import Note, Relation


def failure_note(
    refutes: str,
    what_happened: str,
    *,
    reported_by: str,
    compound_smiles: str | None = None,
    as_of: date | None = None,
    confidence: float | None = None,
) -> Note:
    """Build the `failure-mode` note recording that `refutes` did not hold in practice.

    The id derives from the refuted note and the observation text, so re-reporting the same failure
    is idempotent while a different observation is its own note.

    Args:
        refutes: The id of the note this contradicts, e.g. a playbook that misfired.
        what_happened: What was actually observed; required and never synthesized.
        reported_by: Who observed it, for the provenance line.
        compound_smiles: The molecule involved, if any; lets `kg.conflicts` group the note with the
            evidence it disagrees with.
        as_of: When it was observed; today by default. Becomes `valid_from`.
        confidence: How sure the reporter is; one failed run does not refute a general rule.

    Returns:
        An `agent`-authored note carrying a `contradicts` relation to `refutes`, built but not
        written; pass it to `chemclaw.kg.record.record_note`.
    """
    observed = as_of or date.today()
    digest = stable_hash({"refutes": refutes, "what_happened": what_happened}, chars=10)
    body = (
        f"Reported by {reported_by} on {observed.isoformat()}: "
        f"[[contradicts:{refutes}]] did not hold.\n\n"
        f"{what_happened.strip()}\n"
    )
    return Note(
        id=f"failure-{digest}",
        type="failure-mode",
        compound_smiles=compound_smiles,
        created_by="agent",
        source=f"feedback:{reported_by}",
        confidence=confidence,
        valid_from=observed,
        tags=["failure-mode"],
        # Also in frontmatter so the edge `kg.conflicts` reads does not depend on the body; the pair
        # is deduplicated into one edge.
        relations=[Relation(rel="contradicts", to=refutes, confidence=confidence)],
        body=body,
    )


def close_refuted_note(note: Note, failure_id: str, held_until: date) -> Note:
    """Copy `note` with its validity closed on `held_until` and a line naming the refutation.

    Opt-in, because `valid_to` asserts the claim was true up to that date. Use it for "this held
    until March and then the process changed": the note leaves current-evidence retrieval while
    remaining in Git. For "this was never true" leave the note open: the schema cannot express it,
    and the `contradicts` edge keeps the claim visible and marked as disputed.

    Args:
        note: The note being retired; its `valid_to` must still be open, so a re-close cannot extend
            validity or append this line twice.
        failure_id: The id of the `failure-mode` note, cited as a `[[wikilink]]`. Both land in one
            write with the subject first, so the link never dangles.
        held_until: The last date on which the claim did hold, as the chemist states it.

    Returns:
        An amended copy, to be passed as `record_note`'s `superseded` so the refutation and the
        retirement land together.

    Raises:
        ChemclawError: When `held_until` predates the note's own `valid_from`; reported with both
            dates rather than clamped, since the date came from a person.
    """
    if note.valid_from is not None and held_until < note.valid_from:
        raise ChemclawError(
            f"cannot retire {note.id} on {held_until.isoformat()}: it only became valid on "
            f"{note.valid_from.isoformat()}, so that window ends before it starts"
        )
    body = (
        f"{note.body.rstrip()}\n\n"
        f"Refuted by [[{failure_id}]]: this held until {held_until.isoformat()} and no longer "
        "does. Kept for the record; excluded from current-evidence retrieval.\n"
    )
    return note.model_copy(update={"valid_to": held_until, "body": body})


def failures_against(
    notes: Iterable[Note],
    *,
    cited: Collection[str] = (),
    structures: Collection[str] = (),
) -> list[Note]:
    """Every recorded failure that bears on a set of citations or a set of molecules.

    Lets a design check against documented failures, not only against what the chemist listed as
    forbidden. Two joins:

    - `cited`: note ids the design rests on, matched exactly against each failure's `contradicts`
      target.
    - `structures`: SMILES the design uses, matched against a failure's `compound_smiles`. Weaker (a
      molecule in two routes is not the same claim), so callers treat it as a warning. Both sides
      are canonicalized with `canonical_smiles` (not `standard_smiles`: a salt is a different
      substance to charge), so recall does not depend on how the SMILES was spelled.

    Pure, over notes the caller loaded. Returns notes rather than a reduced model because
    `protocols` may not import `memory`; `agent/` reduces them into the check's input.

    Args:
        notes: The corpus, or any subset of it. Non-`failure-mode` notes are ignored.
        cited: Note ids the design cites.
        structures: SMILES the design uses, in any spelling; canonicalized here.

    Returns:
        The failure notes that bear on either, deduplicated by id, in corpus order.
    """
    # Deferred for the reason `kg/conflicts.py` defers the same import: it pulls RDKit, and this
    # module is imported by callers that only ever write a failure note.
    from chemclaw.core.chem import canonical_smiles

    wanted_ids = {ref for ref in cited if ref}
    wanted_structures = {canonical_smiles(smiles) for smiles in structures if smiles}
    found: dict[str, Note] = {}
    for note in notes:
        if note.type != "failure-mode":
            continue
        refuted = [rel.to for rel in note.outgoing_relations() if rel.rel == "contradicts"]
        by_citation = any(target in wanted_ids for target in refuted)
        smiles = note.compound_smiles or ""
        by_structure = bool(smiles) and canonical_smiles(smiles) in wanted_structures
        if by_citation or by_structure:
            found.setdefault(note.id, note)
    return list(found.values())


def observation_of(note: Note) -> str:
    """The observation out of a failure note's body, without the provenance line above it.

    Falls back to the first line for a hand-written note in another shape, so a failure is never
    dropped for its formatting.
    """
    lines = [line.strip() for line in note.body.splitlines() if line.strip()]
    if not lines:
        return ""
    observation = next((line for line in lines[1:] if not line.startswith("[[")), "")
    return observation or lines[0]
