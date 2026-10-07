"""Molecules as graph citizens: one `compound` note per standardized structure.

A compound note lets a structural hit cite something and gives a species one canonical identity, so
`DMF` and `CN(C)C=O` are the same thing. The id comes from the standardized SMILES
(`chemclaw.core.chem.compound_id`, in `core` so connectors can cite without importing the graph),
and the body is written from the same standardized SMILES, so two spellings sharing an id render
byte-identically.

The note records only what is certain: the structure, a recognised name from
`chemclaw.core.reagents`, and synonyms. No predicted properties, which belong to the calculation
cache.
"""

import logging
import re

from chemclaw.core.chem import compound_id, require_standard_smiles
from chemclaw.core.reagents import display_name, synonyms_of
from chemclaw.kg.note import Note

log = logging.getLogger(__name__)

#: `compound_id`'s shape: the prefix and a 12-hex-digit structure hash. A slug-named seed note
#: (`compound-thf`) never matches, and neither does anything a person would type as a name.
STRUCTURAL_COMPOUND_ID = re.compile(r"compound-[0-9a-f]{12}")


def compound_note(smiles: str) -> Note:
    """Build the `compound` note for a molecule (idempotent: same compound, same note).

    Authored as `agent`. Every field derives from the standardized SMILES — the key `compound_id`
    hashes and `ingest.eln.ingest` indexes — so re-proposing an existing note produces no diff.
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
    """The compound notes `note` links to that a submission must carry with it.

    A note that links a compound note gets that compound note: the target is determined by the
    note's SMILES, so the note can write `[[compound-<hash>]]` and `kg/record.py` writes the
    dependency first. Empty for a note with no `compound_smiles` or no compound link. Re-writing an
    existing compound note is a no-op.

    A note linking its compound under a pre-`STANDARDIZATION_VERSION`-bump id still gets the current
    compound note; the old link resolves through `memory.compound_rekey`'s `supersedes` link, and
    the mismatch is logged.
    """
    if not note.compound_smiles:
        return []
    try:
        wanted = compound_id(note.compound_smiles)
    except ValueError:
        # An unparseable SMILES is the note's own problem to report; it is not this function's
        # place to fail a submission over a field it only reads opportunistically.
        return []
    links = note.outgoing_links()
    if wanted not in links:
        stale = [link for link in links if STRUCTURAL_COMPOUND_ID.fullmatch(link)]
        if not stale:
            return []
        log.warning(
            "note %s links %s but its structure's current compound id is %s — recording that "
            "compound as its dependency; `make rekey-compounds` links the old id to it",
            note.id,
            ", ".join(stale),
            wanted,
        )
    return [compound_note(note.compound_smiles)]
