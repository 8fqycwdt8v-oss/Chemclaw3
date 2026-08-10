"""The hazard gate on agent-proposed procedure notes (D-080).

The screen is only useful if its flags reach the human who signs off. This module is where that
happens: an **agent-authored note that carries a procedure** must document any hazard flag at or
above the configured severity in a `## Hazards` section, and `chemclaw.kg.validate` (already in CI
and run
on the PR that proposes the note) refuses the note otherwise. So the warning lands on the reviewer
before the merge, not in a log nobody reads.

Scoped deliberately narrowly, because a gate that fires on the wrong notes gets switched off:

- **agent-authored only** — a human writing up their own procedure has made their own hazard
  judgment; the PR-gate exists to review the *agent's* proposals (D-005);
- **notes that tell someone what to run** — a `## Procedure` section, *or* a note whose very type
  is a proposal of conditions (`experiment-proposal`, `bo-candidate`). The heading alone was the
  whole test, and it missed exactly the class that matters most: a `bo-candidate` names the
  conditions a surrogate model wants a human to physically run, and it has no `## Procedure`
  heading because it is a table of parameters. So the one note type that proposes an experiment
  nobody has performed was the one type never screened. A reaction record or a distilled rule that
  merely mentions a structure is still out of scope;
- **at or above `safety_gate_severity`** — the gate must fire rarely enough that a firing means
  something.
"""

import re

from chemclaw.core.config import settings
from chemclaw.kg.note import Note
from chemclaw.science.safety.screen import (
    SafetyRulesError,
    at_least,
    parse_molecule,
    screen_reaction,
)

# Inline code spans, which is how a note writes a SMILES (`CCO.CC(=O)O>>CCOC(C)=O`).
_CODE_SPAN = re.compile(r"`([^`\n]+)`")

# Everything a SMILES cannot contain, which is therefore prose wherever it appears inside a code
# span: whitespace, control characters, and anything outside printable ASCII. It is the same
# statement `core.chem.require_molecule` makes when it refuses those inputs, read the other way
# round — there it decides "this string is not a molecule", here it decides where one ends. Used to
# cut a span into candidate tokens *before* anything is asked about them (see `structures_in`).
_NOT_IN_A_SMILES = re.compile(r"[^\x21-\x7e]+")
_PROCEDURE_HEADING = re.compile(r"(?mi)^##\s+procedure\b")
_HAZARDS_HEADING = re.compile(r"(?mi)^##\s+hazards\b")

# Note types that *are* a proposal of conditions, heading or no heading. Both are minted by
# machines and describe work nobody has run yet, which is precisely when a structural alert is
# worth a reviewer's attention: there is no chemist who has already stood at the bench and formed
# their own judgment about the mixture.
_PROPOSES_CONDITIONS: frozenset[str] = frozenset({"experiment-proposal", "bo-candidate"})


def proposes_a_procedure(note: Note) -> bool:
    """Whether `note` tells someone what to run, by its section structure or by its type."""
    return note.type in _PROPOSES_CONDITIONS or bool(_PROCEDURE_HEADING.search(note.body))


def structures_in(note: Note) -> list[str]:
    """Every distinct component SMILES a note names, in first-seen order.

    Reads the `compound_smiles` field plus each inline code span. A span is cut on everything a
    SMILES cannot contain (`_NOT_IN_A_SMILES`) **first**, and every token is then split on `>>` and
    `.` so a reaction SMILES (`A.B>>C`) is screened as its species and the pair rules see them all.
    A token that is not a SMILES — a config key, a file path, a number, a unit — simply yields
    nothing, so no separate "is this a structure?" heuristic is needed: RDKit is the arbiter
    (`_is_structure`). Unparseable tokens are silently ignored here; a note whose *structures* are
    broken is already `chemclaw.ingest.eln.validate`'s and the reviewer's problem, not the gate's.

    **Cutting the span before asking anything is what keeps a hazard from disappearing.** A note
    body is prose, and a machine-written span routinely carries an annotation beside the structure:
    `` `CN=[N+]=[N-] (2 equiv)` ``, `` `CCO at 80 °C` ``. A SMILES contains no whitespace and
    nothing outside printable ASCII — precisely what `core.chem.require_molecule` refuses — so
    those characters inside a span are prose by construction, which makes them the one separator
    that can be split on without a guess about spelling. Asking the strict predicate about a whole
    *span* instead turned it into a filter, and a span it rejected was dropped rather than screened
    narrowly: measured on this build, a body holding `` `CN=[N+]=[N-] (2 equiv)` `` yielded no
    structures and therefore no hazard problem at all, while `screen_structure("CN=[N+]=[N-]")` on
    the same azide returns a high-severity `organic-azide` flag. An agent-written
    `experiment-proposal` is exactly that input class, so the gate had stopped screening the notes
    it exists for (`D-2026-08-09-a-valid-prefix-is-not-a-molecule`, decision 5).

    The tokens are a **superset** of what a bare `Chem.MolFromSmiles` over the whole span used to
    yield: that parser stops at the first whitespace and skips a non-ASCII run at either end of
    what is left, so whatever it read is one of these tokens. No note can therefore lose a flag to
    the strict screen, and several gain one — `` `CCO CN=[N+]=[N-]` `` screens both species where
    the lenient parse screened only the ethanol, and `` `CN=[N+]=[N-]—the azide` `` keeps its azide
    where the lenient parse failed outright.

    **The cut errs towards screening, deliberately.** `` `80 °C` `` yields the token `C`, which is
    methane, so the gate screens a molecule the note never named. That is the price of the sentence
    above: any rule that recovers the azide from `` `CN=[N+]=[N-]°` `` recovers methane from
    `` `°C` ``, because the two strings have the same shape. It is not the "clean screen of a
    molecule nobody asked about" this package refuses elsewhere — that refusal is about a *tool
    result* a chemist reads, while this list is an input to a gate whose only output is "this note
    needs a `## Hazards` section". An extra molecule can add a flag, never remove one, and every
    rule in the table is a multi-atom motif that a lone atom cannot match.
    """
    candidates = [note.compound_smiles] if note.compound_smiles else []
    candidates.extend(_CODE_SPAN.findall(note.body))
    seen = {
        part: None
        for raw in candidates
        for token in _NOT_IN_A_SMILES.split(raw)
        for half in token.split(">>")
        for part in half.split(".")
        if part and _is_structure(part)
    }
    return list(seen)


def _is_structure(candidate: str) -> bool:
    """Whether the screen would accept `candidate` as a molecule — the "is this a SMILES" test.

    Asks `parse_molecule`, which is the acceptance test `screen_reaction` itself applies, rather
    than a bare `Chem.MolFromSmiles`. The two must be one predicate, or this gate hands the screen
    input the screen refuses: RDKit reads a valid prefix, so a span like `` `CCO at 80 °C` `` used
    to pass as a structure here and would then reach a screen that refuses it, turning a note this
    gate is documented to pass into a `kg-validate` failure on every note containing one.

    **It is asked about a token, never about a whole code span**, and that is what makes strictness
    safe here rather than dangerous. `structures_in` has already cut the span on every character a
    SMILES cannot hold, so the two inputs `require_molecule` refuses for narrowing — embedded
    whitespace and non-ASCII — cannot reach it from here, and what is left is the honest question
    "is this token a molecule". On a span, a strict predicate is a *filter*:
    `` `CN=[N+]=[N-] (2 equiv)` `` is not a molecule, so the span vanishes and its azide is never
    screened. On a token it is a *classifier*: the azide is kept and `(2` and `equiv)` are dropped.
    Narrow screening is the failure this predicate prevents and no screening is the failure
    `structures_in`'s split prevents; only the two together prevent both.

    Which is also why the answer is a boolean rather than the exception: a broken structure in a
    note body is `chemclaw.ingest.eln.validate`'s problem and the reviewer's, not the hazard gate's.
    """
    try:
        parse_molecule(candidate)
    except SafetyRulesError:
        return False
    return True


def hazard_problems(note: Note) -> list[str]:
    """Return the gate's complaints about `note` (empty when it passes or does not apply).

    A missing or malformed rule table is reported as a problem rather than raised: `kg-validate`
    is a gate that prints problems and exits non-zero, and a traceback there reads as a broken
    tool rather than a blocked merge. Either way the PR does not pass.
    """
    if not settings.safety_gate_enabled or note.created_by != "agent":
        return []
    if not proposes_a_procedure(note) or _HAZARDS_HEADING.search(note.body):
        return []
    structures = structures_in(note)
    if not structures:
        return []
    try:
        result = screen_reaction(structures)
    except SafetyRulesError as exc:
        return [f"note {note.id!r}: hazard screening failed: {exc}"]
    if not at_least(result.max_severity, settings.safety_gate_severity):
        return []
    flagged = ", ".join(sorted({flag.rule_id for flag in result.flags}))
    return [
        f"note {note.id!r} proposes a procedure with {result.max_severity}-severity hazard "
        f"flags ({flagged}) but has no '## Hazards' section — document the flags and their "
        "controls so the reviewer sees them before merging"
    ]
