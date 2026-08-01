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

A compound note is the one identity both needed. The id is derived from the **standardized** SMILES
(`chemclaw.core.chem.compound_id`, which lives in `core` so a connector can cite a note without
importing the graph), so the same molecule always maps to the same note from any source — which is
what makes a citation stable and a vocabulary possible.

The note's *body* is written from that same standardized SMILES, not from a merely canonicalized
one. Two spellings that share an id must render byte-identically, or re-proposing an already-merged
note produces a diff and the last spelling ingested wins: `compound_note("CCN")` and
`compound_note("CCN.Cl")` are one note by id, so they have to be one note by content too.

The note deliberately records only what is *known with certainty*: the standardized structure, the
recognised name when `chemclaw.core.reagents` knows one, and the synonyms that resolve to it. No
predicted properties — those live in the calculation cache and would go stale here.
"""

from chemclaw.core.chem import compound_id, require_standard_smiles
from chemclaw.core.reagents import display_name, synonyms_of
from chemclaw.kg.note import Note


def compound_note(smiles: str) -> Note:
    """Build the `compound` note for a molecule (idempotent: same compound, same note).

    Authored as `agent`, so it passes the PR-gate like every other machine-written note (D-005).

    Every field is derived from the **standardized** SMILES, the same key `compound_id` hashes and
    the same one `ingest.eln.ingest` indexes on. Deriving the id from one notion of sameness and
    the body from another gave two spellings of one compound a shared id and different bodies, so
    re-proposing an already-merged note rewrote its structure line and the last spelling ingested
    won — the opposite of the "renders byte-identically, produces no diff" contract
    `compound_dependencies` relies on.
    """
    standard = require_standard_smiles(smiles)
    name = display_name(standard)
    aliases = synonyms_of(standard)
    body = f"Compound `{standard}`.\n\n"
    if name:
        body += f"- name: {name}\n"
    if aliases:
        # Spelled out rather than only listed as tags, because the lexical index reads bodies:
        # this is what lets a trivial-name query match a structure-keyed corpus (KNW-4).
        body += f"- also written: {', '.join(aliases)}\n"
    return Note(
        id=compound_id(standard),
        type="compound",
        compound_smiles=standard,
        created_by="agent",
        tags=["compound"],
        body=body,
    )


def compound_dependencies(note: Note) -> list[Note]:
    """The compound notes `note` links to that a submission must carry with it (STO-7).

    The rule that unblocked crosslinking, stated once and applied at the gate rather than in every
    note-minting connector: **a note that links a compound note gets that compound note.** Because
    `compound_id` is derived from the canonical structure, the target is fully determined by the
    SMILES the note already carries — so the note can honestly write `[[compound-<hash>]]` and the
    PR-gate makes the link resolve, instead of the note avoiding the link because the target might
    not exist yet (`connectors/qm/knowledge.py` documented exactly that avoidance).

    Returns an empty list for a note with no `compound_smiles` or one that does not link its
    compound. Re-proposing a compound note that is already merged is a no-op: it renders
    byte-identically, so the submission produces no diff for it.
    """
    if not note.compound_smiles:
        return []
    try:
        wanted = compound_id(note.compound_smiles)
    except ValueError:
        # An unparseable SMILES is the note's own problem to report; it is not this function's
        # place to fail a submission over a field it only reads opportunistically.
        return []
    if wanted not in note.outgoing_links():
        return []
    return [compound_note(note.compound_smiles)]
