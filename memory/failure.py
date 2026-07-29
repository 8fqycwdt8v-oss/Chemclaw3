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
could only be prose, which meant `kg.conflicts` could not find it and a later query would serve
the refuted note with no indication anything was wrong. The relation is what makes the feedback
loop close.
"""

from datetime import date

from chemclaw.ids import stable_hash
from kg.note import Note, Relation


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
            `kg.conflicts` group the note with the evidence it disagrees with.
        as_of: When it was observed; today by default. Becomes `valid_from`, so a correction is
            not retroactively current for a period it says nothing about.
        confidence: How sure the reporter is. A single failed run is not a refutation of a
            general rule, and this is where that distinction is recorded rather than implied.

    Returns:
        An `agent`-authored note carrying a `contradicts` relation to `refutes`. It is *proposed*,
        never written: the caller passes it to `kg.pr_gate.propose_note` like any other note, and a
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
