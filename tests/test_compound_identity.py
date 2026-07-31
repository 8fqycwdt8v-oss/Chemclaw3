"""One canonical identity per molecule (gaps KNW-7, KNW-4).

Molecules were indexed by SMILES but were not graph citizens, which cost two different things:

- A structural hit could cite nothing (the `FingerprintReactionRetriever` citation caveat exists
  for exactly this), so the agent bridged via `find_notes(smiles)` — a literal substring match, the
  fragile path KM-4 flags.
- Condition species were free strings, so `DMF`, `N,N-dimethylformamide` and `CN(C)C=O` were three
  unrelated tokens and one optimization campaign could split in two on spelling alone.

Both wanted the same thing: a structure-derived identity, which is what these pin.
"""

import asyncio

import pytest

from chemclaw.core.chem import (
    STANDARDIZATION_VERSION,
    InvalidSmilesError,
    canonical_smiles,
    compound_id,
    standard_smiles,
)
from chemclaw.ingest.eln.compound import compound_note, synonyms_for
from chemclaw.kg.note import KNOWN_NOTE_TYPES
from chemclaw.memory.optimization import canonical_condition
from chemclaw.science.fingerprints.molfp.fingerprint import ecfp_bitstring, molecule_definition
from chemclaw.science.fingerprints.molfp.search import (
    find_similar_molecules,
    find_substructure_matches,
    record_for,
)
from chemclaw.science.fingerprints.rxnfp.fingerprint import reaction_definition
from chemclaw.science.fingerprints.store import FingerprintRecord, InMemoryFingerprintStore


def test_the_same_molecule_gets_one_id_however_it_is_written() -> None:
    """Structure-derived, not name-derived — the property that makes a citation stable."""
    assert compound_id("CN(C)C=O") == compound_id("O=CN(C)C")
    assert compound_id("CCO") != compound_id("CCC")


def test_the_derivation_is_pinned_to_a_literal() -> None:
    """The id is a *published* citation, so its derivation may not drift silently.

    A literal, not a round trip: `compound_id(x) == compound_id(x)` would still pass if the
    scheme changed, and every already-merged `knowledge/compound/<id>.md` would then be
    unreachable from a fresh hit. This value predates the move of the derivation from
    `ingest.eln.compound` into `core.chem` (D-154) and must survive it.
    """
    assert compound_id("CCO") == "compound-f29e20f49d41"
    assert compound_id("OCC") == "compound-f29e20f49d41"  # same molecule, other spelling


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


# --- The citation the identity was for (D-154) ------------------------------------------------
#
# The module docstring above says a structural hit "could cite nothing", which is why the agent
# was told to bridge via `find_notes(smiles)`. The identity closed half of that; these close the
# other half — the hit now carries the note id, so the bridge is a citation, not a substring scan.


def _indexed(*smiles: str) -> InMemoryFingerprintStore:
    """A molecule index keyed the way ingestion keys it: by the structure itself."""
    store = InMemoryFingerprintStore(definition=molecule_definition())

    async def _fill() -> None:
        for one in smiles:
            await store.add(record_for(one, one))

    asyncio.run(_fill())
    return store


def test_a_substructure_hit_cites_the_compound_note_for_what_it_matched() -> None:
    """The functional-group question lands on the graph instead of on a substring search."""
    store = _indexed("CC(=O)Oc1ccccc1C(=O)O", "CCO")
    hits = asyncio.run(find_substructure_matches(store, "c1ccccc1"))
    assert [h.smiles for h in hits] == ["CC(=O)Oc1ccccc1C(=O)O"]
    assert hits[0].compound_note_id == compound_note("CC(=O)Oc1ccccc1C(=O)O").id


def test_a_similarity_hit_cites_the_same_note_the_ingest_would_have_written() -> None:
    """The id on the hit is the *note's* id, not a second identity scheme beside it."""
    store = _indexed("CCO")
    hits = asyncio.run(find_similar_molecules(store, "CCO", threshold=0.1))
    assert hits[0].compound_note_id == compound_note("CCO").id


def test_two_spellings_of_one_molecule_cite_one_note() -> None:
    """The point of a structure-derived id: the citation does not fork on how it was written."""
    store = _indexed("CN(C)C=O")
    hits = asyncio.run(find_similar_molecules(store, "O=CN(C)C", threshold=0.1))
    assert hits[0].compound_note_id == compound_id("CN(C)C=O")


def test_an_unciteable_row_yields_no_citation_rather_than_sinking_the_search() -> None:
    """Ingestion canonicalizes leniently, so a junk label can reach the index.

    Raising here would let one bad row hide every real hit — the rule the substructure scan
    already follows when it skips a record that no longer parses.
    """
    store = InMemoryFingerprintStore(definition=molecule_definition())

    async def _run() -> list[str | None]:
        await store.add(record_for("CCO", "CCO"))
        await store.add(
            FingerprintRecord(
                id="junk",
                label="not-a-molecule",
                bits=ecfp_bitstring("CCO"),
                definition=molecule_definition(),
            )
        )
        hits = await find_similar_molecules(store, "CCO", threshold=0.0)
        return [h.compound_note_id for h in hits]

    cited = asyncio.run(_run())
    assert compound_id("CCO") in cited
    assert None in cited


# --- standardization: the "same compound" question (D-2026-07-31-two-spellings) ---------------


def test_a_salt_and_its_free_base_are_one_compound() -> None:
    """The classic production failure: identity fragmenting on the counterion.

    `compound_id` keys the calculation cache and both fingerprint indices, so a hydrochloride
    minting its own id means cache misses on work D-011 promises never to repeat, and a similarity
    search that ranks a molecule against itself as merely similar.
    """
    assert compound_id("CCN.Cl") == compound_id("CCN")
    assert compound_id("CC(=O)[O-].[Na+]") == compound_id("CC(=O)O")


def test_two_tautomers_are_one_compound() -> None:
    """Two spellings of one substance, which a chemist would never file separately."""
    assert compound_id("CC(O)=NC") == compound_id("CC(=O)NC")


def test_two_different_molecules_stay_different() -> None:
    """The guard on the above: a pipeline that collapsed everything would pass those tests too."""
    assert compound_id("CCO") != compound_id("CCN")
    assert compound_id("c1ccccc1") != compound_id("c1ccncc1")


def test_a_calculation_input_keeps_the_charge_it_was_given() -> None:
    """The other direction, and the one that makes this two functions rather than one.

    A chemist submitting acetate means acetate. Standardizing the *calculation* key would neutralize
    it and silently compute the conjugate acid — so `canonical_smiles` answers "same structure" and
    stays spelling-only, while `standard_smiles` answers "same compound".
    """
    assert canonical_smiles("CC(=O)[O-]") != canonical_smiles("CC(=O)O")
    assert standard_smiles("CC(=O)[O-]") == standard_smiles("CC(=O)O")


def test_standardization_is_recorded_in_the_fingerprint_definition() -> None:
    """Rows indexed under an older notion of sameness must fall out, not be ranked against new ones.

    The same failure-safe behaviour a changed ECFP radius already gets: the store filters on the
    definition, so a stale row is invisible to search until a re-index rebuilds it.
    """
    assert STANDARDIZATION_VERSION in molecule_definition()
    assert STANDARDIZATION_VERSION in reaction_definition()
