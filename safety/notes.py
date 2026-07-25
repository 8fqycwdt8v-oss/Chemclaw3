"""The hazard gate on agent-proposed procedure notes (D-080).

The screen is only useful if its flags reach the human who signs off. This module is where that
happens: an **agent-authored note that carries a procedure** must document any hazard flag at or
above the configured severity in a `## Hazards` section, and `kg.validate` (already in CI and run
on the PR that proposes the note) refuses the note otherwise. So the warning lands on the reviewer
before the merge, not in a log nobody reads.

Scoped deliberately narrowly, because a gate that fires on the wrong notes gets switched off:

- **agent-authored only** — a human writing up their own procedure has made their own hazard
  judgment; the PR-gate exists to review the *agent's* proposals (D-005);
- **procedure notes only** — a note with a `## Procedure` section is telling someone how to run
  something. A reaction record or a distilled rule that merely mentions a structure is not;
- **at or above `safety_gate_severity`** — the gate must fire rarely enough that a firing means
  something.
"""

import re

from chemclaw.config import settings
from kg.note import Note
from safety.screen import SafetyRulesError, at_least, screen_reaction

# Inline code spans, which is how a note writes a SMILES (`CCO.CC(=O)O>>CCOC(C)=O`).
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_PROCEDURE_HEADING = re.compile(r"(?mi)^##\s+procedure\b")
_HAZARDS_HEADING = re.compile(r"(?mi)^##\s+hazards\b")


def structures_in(note: Note) -> list[str]:
    """Every distinct component SMILES a note names, in first-seen order.

    Reads the `compound_smiles` field plus each inline code span, splitting a reaction SMILES
    (`A.B>>C`) into its components so each species is screened on its own and the pair rules see
    them all. A code span that is not a SMILES — a config key, a file path, a number with units —
    simply yields nothing, so no separate "is this a structure?" heuristic is needed: RDKit is the
    arbiter. Unparseable spans are silently ignored here; a note whose *structures* are broken is
    already `eln.validate`'s and the reviewer's problem, not the hazard gate's.
    """
    candidates = [note.compound_smiles] if note.compound_smiles else []
    for span in _CODE_SPAN.findall(note.body):
        candidates.extend(part for half in span.split(">>") for part in half.split("."))
    seen = {smiles: None for raw in candidates if (smiles := raw.strip()) and _is_structure(smiles)}
    return list(seen)


def _is_structure(candidate: str) -> bool:
    """Whether RDKit reads `candidate` as a molecule (the only test for "is this a SMILES")."""
    from rdkit import Chem

    return Chem.MolFromSmiles(candidate) is not None


def hazard_problems(note: Note) -> list[str]:
    """Return the gate's complaints about `note` (empty when it passes or does not apply).

    A missing or malformed rule table is reported as a problem rather than raised: `kg-validate`
    is a gate that prints problems and exits non-zero, and a traceback there reads as a broken
    tool rather than a blocked merge. Either way the PR does not pass.
    """
    if not settings.safety_gate_enabled or note.created_by != "agent":
        return []
    if not _PROCEDURE_HEADING.search(note.body) or _HAZARDS_HEADING.search(note.body):
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
