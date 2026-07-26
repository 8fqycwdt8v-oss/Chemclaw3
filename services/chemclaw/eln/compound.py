"""Molecules as graph citizens (gaps KNW-7, KNW-4).

Molecules were indexed by SMILES in a fingerprint table but were **not notes**, with two
consequences the analysis traced back to the same root:

- A structural hit could not cite anything. `FingerprintReactionRetriever`'s citation-honesty caveat
  (CHECKMATE 5b, F3) exists precisely because a substructure or similarity hit returns a SMILES with
  no note behind it, so the agent had to bridge via `find_notes(smiles)` — a literal substring match
  over note bodies, the fragile path KM-4 flags.
- There was no canonical place to record what a species *is*. Solvents, bases and catalysts were
  free strings or raw SMILES with no controlled vocabulary, so `DMF`, `N,N-dimethylformamide` and
  `CN(C)C=O` were three unrelated tokens to every lexical path, and the optimization-campaign
  grouping compared conditions that were textually different and chemically identical (KNW-4).

A compound note is the one identity both needed. The id is derived from the canonical SMILES via the
shared `stable_hash`, so the same molecule always maps to the same note from any source — which is
what makes a citation stable and a vocabulary possible.

The note deliberately records only what is *known with certainty*: the canonical structure, the
recognised name when `chemclaw.reagents` knows one, and the synonyms that resolve to it. No
predicted properties — those live in the calculation cache and would go stale here.
"""

from chemclaw.chem import require_canonical_smiles
from chemclaw.ids import stable_hash
from chemclaw.reagents import display_name, known_names, resolve_compound_name
from kg.note import Note


def compound_id(smiles: str) -> str:
    """The stable note id for a molecule, derived from its canonical structure.

    Structure-derived rather than name-derived, so two sources that spell the same molecule
    differently still reach one note — the property that makes a citation from a fingerprint hit
    meaningful at all.
    """
    return f"compound-{stable_hash(require_canonical_smiles(smiles), chars=12)}"


def synonyms_for(smiles: str) -> list[str]:
    """Every recognised spelling that resolves to this structure (the KNW-4 vocabulary).

    Written into the note body so the *lexical* retrieval leg can match a trivial name against a
    structure-keyed corpus — the concrete fix for "DMF and CN(C)C=O are unrelated tokens".
    """
    canonical = require_canonical_smiles(smiles)
    matches = []
    for name in known_names():
        resolved = resolve_compound_name(name)
        if resolved is not None and resolved.smiles == canonical:
            matches.append(name)
    return sorted(matches)


def compound_note(smiles: str) -> Note:
    """Build the `compound` note for a molecule (idempotent: same structure, same note).

    Authored as `agent`, so it passes the PR-gate like every other machine-written note (D-005).
    """
    canonical = require_canonical_smiles(smiles)
    name = display_name(canonical)
    aliases = synonyms_for(canonical)
    body = f"Compound `{canonical}`.\n\n"
    if name:
        body += f"- name: {name}\n"
    if aliases:
        # Spelled out rather than only listed as tags, because the lexical index reads bodies.
        body += f"- also written: {', '.join(aliases)}\n"
    return Note(
        id=compound_id(canonical),
        type="compound",
        compound_smiles=canonical,
        created_by="agent",
        tags=["compound"],
        body=body,
    )
