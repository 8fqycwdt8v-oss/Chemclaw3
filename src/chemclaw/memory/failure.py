"""Recording that evidence was wrong, in band and through the gate (KM-12).

The system could record what it learned and never that it learned something was *false*. Every
memory path — campaigns, playbooks, interactions — writes positive knowledge, and the `failure-mode`
note type sat in `KNOWN_NOTE_TYPES` with a comment calling it "a negative result worth not
repeating" and nothing anywhere minting one. A chemist who saw a bad recommendation had no way to
say so that the graph would remember.

Two design points, both about not taking a shortcut:

**It writes through the PR-gate like everything else.** A correction is machine-written text
asserting that curated knowledge is wrong, which is *more* in need of human sign-off than the note
it refutes, not less. There is deliberately no direct-mutation path, and no way to edit or delete
the refuted note — it stays exactly as it was, and the disagreement is a new note plus an edge.

**The edge is `contradicts`, so retrieval can see it.** Before typed edges (STO-8) a correction
could only be prose, which meant `chemclaw.kg.conflicts` could not find it and a later query would
serve
the refuted note with no indication anything was wrong. The relation is what makes the feedback
loop close.

`close_refuted_note` is the *optional* second half, and it is optional because `valid_to` means
something narrower than "this note is wrong" — see its docstring for the measurement.
"""

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

    Args:
        refutes: The id of the note this contradicts — a playbook that misfired, a computed
            result that experiment disagreed with.
        what_happened: What was actually observed. The value of a negative result is entirely in
            this text, so it is required and never synthesized.
        reported_by: Who observed it, for the provenance line the audit trail needs.
        compound_smiles: The molecule involved, when there is one — this is what lets
            `chemclaw.kg.conflicts` group the note with the evidence it disagrees with.
        as_of: When it was observed; today by default. Becomes `valid_from`, so a correction is
            not retroactively current for a period it says nothing about.
        confidence: How sure the reporter is. A single failed run is not a refutation of a
            general rule, and this is where that distinction is recorded rather than implied.

    Returns:
        An `agent`-authored note carrying a `contradicts` relation to `refutes`. It is *proposed*,
        never written: the caller passes it to `chemclaw.kg.record.record_note` like any other
        note, and a
        human decides whether the graph accepts the correction.

    The id is derived from the refuted note and the observation text, so re-reporting the identical
    failure is idempotent while a genuinely different observation about the same note is its own
    note — two people hitting two different problems with one playbook should produce two records.
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
        # Also stated in frontmatter, because this is the edge `kg.conflicts` reads and it should
        # not depend on the body surviving an edit. `outgoing_relations` dedupes the pair, so the
        # two forms produce one edge.
        relations=[Relation(rel="contradicts", to=refutes, confidence=confidence)],
        body=body,
    )


def close_refuted_note(note: Note, failure_id: str, held_until: date) -> Note:
    """Copy `note` with its validity closed on `held_until` and a line naming the refutation.

    The amendment that turns a failure report into a *correction*: without it the refuted claim
    keeps reading as current fact and every future query serves it (flagged, but served).
    `valid_to` is the only retirement the schema has — the note is never edited away or deleted,
    it stays in Git, stays reachable by explicit id, and only leaves current-evidence sweeps via
    `Note.is_current` (KM-7).

    **Why this is a separate, opt-in step rather than something `failure_note` always does.**
    `valid_to` is a *valid-time* bound: it asserts the claim was true up to that date. Measured on
    a `playbook` note refuted by a `failure_note`:

    - left open — `find_conflicts` reports the disagreement both in a corpus scan *and* at
      retrieval time, so `retrieval.retrievers._conflict_index` flags every chunk of the refuted
      note with the failure note's id;
    - closed with `valid_to` — the note drops out of retrieval entirely (correct: nothing serves
      it any more), the retrieval-time conflict scan therefore reports nothing, and
      `is_current(<a date inside the window>)` still answers **True**.

    That last line is the whole reason for the choice. "This held until March and then the process
    changed" is exactly what the field says, and closing it is right. "This was never true" has
    **no representation in this schema** — closing such a note would record a period during which
    the system claims the wrong answer was valid, which is a fresh false statement in the one place
    (a time-scoped query) the bi-temporal fields exist to answer honestly. For that case the caller
    leaves the note open and lets the `contradicts` edge do the work: the claim stays visible and
    arrives permanently marked as disputed, which is the truthful record.

    Args:
        note: The already-merged note being retired. Its own `valid_to` must still be open — a
            re-close would either extend a closed note's validity or append this line twice, the
            idempotence trap `memory.supersede` guards the same way.
        failure_id: The id of the `failure-mode` note reporting this, cited as a `[[wikilink]]`.
            Safe to link because both files ride in **one** PR-gate submission, so the target
            exists in the same commit that adds the citation and `kg-validate` sees a resolvable
            link (unlike `memory.supersede`, whose replacement is a separate proposal).
        held_until: The last date on which the claim did hold — the chemist's, not today's.

    Returns:
        An amended copy, ready to ride alongside the failure note in one submission so a human
        reviews the refutation and the retirement as the single decision they actually are.

    Raises:
        ChemclawError: When `held_until` predates the note's own `valid_from`, which is a window
            the schema rejects outright. Reported here, with both dates, rather than clamped: the
            date came from a person, and silently moving it would file a retirement they did not
            ask for.
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
