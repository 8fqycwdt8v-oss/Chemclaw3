"""One canonical identity per molecule (gaps KNW-7, KNW-4).

Molecules were indexed by SMILES but were not graph citizens, which cost two different things:

- A structural hit could cite nothing (the `FingerprintReactionRetriever` citation caveat exists
  for exactly this), so the agent bridged via `find_notes(smiles)` — a literal substring match, the
  fragile path KM-4 flags.
- Condition species were free strings, so `DMF`, `N,N-dimethylformamide` and `CN(C)C=O` were three
  unrelated tokens and one optimization campaign could split in two on spelling alone.

Both wanted the same thing: a structure-derived identity, which is what these pin.
"""

import pytest

from chemclaw.core.chem import InvalidSmilesError
from chemclaw.ingest.eln.compound import compound_id, compound_note, synonyms_for
from chemclaw.kg.note import KNOWN_NOTE_TYPES
from chemclaw.memory.optimization import canonical_condition


def test_the_same_molecule_gets_one_id_however_it_is_written() -> None:
    """Structure-derived, not name-derived — the property that makes a citation stable."""
    assert compound_id("CN(C)C=O") == compound_id("O=CN(C)C")
    assert compound_id("CCO") != compound_id("CCC")


def test_an_unparseable_structure_is_rejected_rather_than_hashed() -> None:
    """Hashing a bad string would mint a stable id for a molecule that does not exist."""
    with pytest.raises(InvalidSmilesError):
        compound_id("not-a-molecule")


def test_the_note_is_agent_authored_so_it_passes_the_pr_gate() -> None:
    """A compound note is machine-written knowledge like any other (D-005)."""
    note = compound_note("CCO")
    assert note.created_by == "agent"
    assert note.type in KNOWN_NOTE_TYPES


def test_the_note_carries_the_synonyms_in_its_body() -> None:
    """Written into the body, not only tags, because the lexical retrieval leg reads bodies.

    This is the concrete fix for a trivial-name query missing a structure-keyed corpus.
    """
    body = compound_note("CN(C)C=O").body
    assert "N,N-dimethylformamide" in body
    assert "dmf" in body
    assert "CN(C)C=O" in body


def test_a_molecule_with_no_recognised_name_still_gets_a_note() -> None:
    """Most real compounds are not bench reagents; they must still be citable."""
    note = compound_note("CC(C)(C)c1ccc(cc1)C(=O)NC1CCNCC1")
    assert note.compound_smiles
    assert "also written" not in note.body


def test_synonyms_resolve_only_to_the_asked_structure() -> None:
    """A synonym list that leaked another molecule's spellings would be worse than none."""
    assert "dmf" in synonyms_for("CN(C)C=O")
    assert "dmf" not in synonyms_for("CCO")


def test_condition_spellings_fold_to_one_token() -> None:
    """An optimization campaign must not split in two because someone typed the full name."""
    folded = {canonical_condition(x) for x in ("DMF", "N,N-dimethylformamide", "CN(C)C=O")}
    assert len(folded) == 1


def test_an_unknown_species_folds_to_itself_rather_than_vanishing() -> None:
    """An unrecognised reagent is still a real condition; dropping it would merge distinct runs."""
    assert canonical_condition("  Mystery-Solvent ") == "mystery-solvent"
    assert canonical_condition("DMF") != canonical_condition("mystery-solvent")


def test_the_vocabulary_reuses_the_one_identity_table() -> None:
    """So the grouping vocabulary cannot drift from the hazard screen's or the calculators'."""
    from chemclaw.core.reagents import resolve_compound_name

    resolved = resolve_compound_name("DIPEA")
    assert resolved is not None
    assert canonical_condition("DIPEA") == resolved.smiles
