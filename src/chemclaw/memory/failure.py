"""Recording that evidence was wrong, in band, and readable the moment it lands (KM-12).

The system could record what it learned and never that it learned something was *false*. Every
memory path — campaigns, playbooks, interactions — writes positive knowledge, and the `failure-mode`
note type sat in `KNOWN_NOTE_TYPES` with a comment calling it "a negative result worth not
repeating" and nothing anywhere minting one. A chemist who saw a bad recommendation had no way to
say so that the graph would remember.

Two design points, both about not taking a shortcut:

**It refutes by writing a new note, never by touching the old one.** There is no direct-mutation
path and no way to edit or delete the refuted note: it stays exactly as it was, and the
disagreement is a new note plus an edge. That was the design under the PR-gate and it is what
survived the gate's removal — the sentence here used to say a human signed the correction off
first, and `D-2026-09-05-the-gate-follows-behaviour-not-knowledge` deleted the reviewer while this
paragraph went on describing one. Correction is the control now, which makes the edge below the
mechanism rather than a courtesy to a reviewer.

**The edge is `contradicts`, so retrieval can see it.** Before typed edges (STO-8) a correction
could only be prose, which meant `chemclaw.kg.conflicts` could not find it and a later query would
serve
the refuted note with no indication anything was wrong. The relation is what makes the feedback
loop close.

`close_refuted_note` is the *optional* second half, and it is optional because `valid_to` means
something narrower than "this note is wrong" — see its docstring for the measurement.
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
        An `agent`-authored note carrying a `contradicts` relation to `refutes`. Built, not
        written: the caller passes it to `chemclaw.kg.record.record_note` like any other note,
        which commits it straight into the graph — so it is readable, and flags the note it
        refutes, as soon as that returns.

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
        note: The note already in the graph that is being retired. Its own `valid_to` must still
            be open — a re-close would either extend a closed note's validity or append this line
            twice, the idempotence trap `memory.supersede` guards the same way.
        failure_id: The id of the `failure-mode` note reporting this, cited as a `[[wikilink]]`.
            Safe to link because both files ride in **one** write, in the order
            `kg.record._build_write` fixes — the subject first, the retirement after it — so the
            target exists before the citation does and no reader sees a dangling link.
        held_until: The last date on which the claim did hold — the chemist's, not today's.

    Returns:
        An amended copy, to be passed as `record_note`'s `superseded` so the refutation and the
        retirement land as the single act they are.

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


def failures_against(
    notes: Iterable[Note],
    *,
    cited: Collection[str] = (),
    structures: Collection[str] = (),
) -> list[Note]:
    """Every recorded failure that bears on a set of citations or a set of molecules.

    **`memory/failure.py` was a builder with no query side, and that is most of why the failure
    memory did not work.** A `failure-mode` note could be written, indexed and retrieved by anyone
    who went looking — and nothing went looking. The 2026-09-13 audit put it plainly: a design can
    cite a playbook and repeat a documented failure sitting in the same graph, because
    `protocols/checks.py::forbidden_absent` tests only what the chemist typed into
    `request.forbidden`, never what the corpus records as having already failed.

    Two joins, and the first is the exact one:

    - **`cited`** — note ids the design rests on. A failure's `contradicts` edge names the note it
      refutes, so this is an equality rather than a resemblance. It is the case the audit named and
      it needs no fuzzy matching to be right.
    - **`structures`** — SMILES the design uses. A failure recorded against a molecule bears on a
      design that charges it even when the design cites nothing. Weaker, because a molecule
      appearing in two routes is not the same claim twice, which is why what comes back is offered
      as a warning rather than a blocker.

      **Canonicalized on both sides, because this was a raw-string comparison under an Args block
      that said "Canonical SMILES".** Nothing canonicalizes a note's `compound_smiles` on the way
      in — a chemist writes what their ELN exported — so a failure noted against `OCC`, `C(O)C` or
      `[CH3][CH2][OH]` was invisible to a design charging `CCO`, and `no_documented_failure` came
      back clean. Driven on those four spellings of ethanol: one of four matched. The sibling this
      docstring names as "the same shape", `kg/conflicts.py`, keys its groups on
      `core.chem.canonical_smiles` for exactly this reason, so recall no longer depends on whose
      keyboard the SMILES came off. `canonical_smiles` rather than `standard_smiles`: a salt or a
      charge state is genuinely a different substance to charge into a flask, and this is a warning
      about a specific one.

    Both are read off notes the caller already loaded, so this makes no I/O and holds no opinion
    about where the corpus lives — the same shape `kg/conflicts.py` has for the same reason.

    Args:
        notes: The corpus, or any subset of it. Non-`failure-mode` notes are ignored.
        cited: Note ids the design cites.
        structures: SMILES the design uses, in any spelling — canonicalized here.

    **Notes rather than a reduced model, because `protocols` may not import `memory`.**
    `tests/test_layering.py` allows `protocols -> core` and `protocols -> science` and nothing else,
    which is right: a deterministic check must not depend on a corpus being loadable. So this
    answers in the knowledge graph's own vocabulary and the caller that has both — `agent/`, which
    may import either — reduces what it finds into the check's input.

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

    `failure_note` writes "Reported by … on …: [[contradicts:…]] did not hold." and then the
    observation, so the first non-empty line after that is what a chemist needs to read. Falls back
    to the whole first line for a note somebody wrote by hand in another shape — this is a message,
    not a parser, and a failure worth surfacing must not be dropped for being formatted unusually.
    """
    lines = [line.strip() for line in note.body.splitlines() if line.strip()]
    if not lines:
        return ""
    observation = next((line for line in lines[1:] if not line.startswith("[[")), "")
    return observation or lines[0]
