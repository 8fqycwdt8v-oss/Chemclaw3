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
from rdkit import Chem

from chemclaw.core.chem import (
    STANDARDIZATION_VERSION,
    InvalidSmilesError,
    canonical_smiles,
    compound_id,
    standard_smiles,
)
from chemclaw.core.reagents import display_name, known_names, resolve_compound_name, synonyms_of
from chemclaw.ingest.eln.compound import compound_note
from chemclaw.kg.note import KNOWN_NOTE_TYPES
from chemclaw.kg.render import render_note
from chemclaw.memory.progression import canonical_condition
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
    assert "dmf" in synonyms_of("CN(C)C=O")
    assert "dmf" not in synonyms_of("CCO")


def test_synonyms_answer_for_any_spelling_of_the_compound() -> None:
    """The lookup standardizes its argument, so a caller need not hold the indexed key already.

    DMF is the case where the canonical and standardized keys agree, so it cannot show this. DMSO
    (whose sulfoxide normalizes to a charge-separated form) and TBTU (written as its
    tetrafluoroborate) are the cases where they do not.
    """
    assert "dmso" in synonyms_of("CS(C)=O")
    assert "tbtu" in synonyms_of("CN(C)C(=[N+](C)C)On1nnc2ccccc21.F[B-](F)(F)F")


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
    hits = asyncio.run(find_substructure_matches(store, "c1ccccc1")).hits
    assert [h.smiles for h in hits] == ["CC(=O)Oc1ccccc1C(=O)O"]
    assert hits[0].compound_note_id == compound_note("CC(=O)Oc1ccccc1C(=O)O").id


def test_a_similarity_hit_cites_the_same_note_the_ingest_would_have_written() -> None:
    """The id on the hit is the *note's* id, not a second identity scheme beside it."""
    store = _indexed("CCO")
    hits = asyncio.run(find_similar_molecules(store, "CCO", threshold=0.1)).hits
    assert hits[0].compound_note_id == compound_note("CCO").id


def test_two_spellings_of_one_molecule_cite_one_note() -> None:
    """The point of a structure-derived id: the citation does not fork on how it was written."""
    store = _indexed("CN(C)C=O")
    hits = asyncio.run(find_similar_molecules(store, "O=CN(C)C", threshold=0.1)).hits
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
        search = await find_similar_molecules(store, "CCO", threshold=0.0)
        return [h.compound_note_id for h in search.hits]

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


def _shipped(name: str) -> str:
    """The SMILES `chemclaw.core.reagents` actually ships for a reagent.

    The inorganic tests below assert over the shipped table rather than over SMILES invented in
    this file, because the table is what reaches `standardize` on the live ELN ingest path — a
    test on a hand-written `[Na+].[OH-]` would keep passing if the table changed underneath it.
    """
    resolved = resolve_compound_name(name)
    assert resolved is not None, f"{name} is no longer a shipped reagent"
    return resolved.smiles


def test_each_shipped_inorganic_reagent_is_its_own_compound() -> None:
    """`FragmentParent` has no organic parent to keep here, so it used to delete the anion.

    NaOH and KOH both standardized to water; K2CO3, Cs2CO3, Na2CO3 and NaHCO3 all standardized to
    carbonic acid. Since `compound_id` keys the note, the fingerprint index and the species
    grouping, a NaOH→KOH base screen reported that nothing had changed and three compound bodies
    were written under one id.
    """
    inorganic = ("NaOH", "KOH", "K2CO3", "Cs2CO3", "Na2CO3", "NaHCO3", "NaH", "NaBH4", "NaN3")
    ids = {name: compound_id(_shipped(name)) for name in inorganic}
    assert len(set(ids.values())) == len(ids), ids
    assert compound_id("O") not in set(ids.values())


def test_standardizing_an_inorganic_reagent_loses_no_atoms() -> None:
    """The whole formula *is* the identity when there is no organic parent to be the compound."""
    for name in ("NaH", "NaBH4", "NaN3", "KOH", "K3PO4"):
        shipped = _shipped(name)
        standardized = standard_smiles(shipped)
        assert Chem.MolFromSmiles(standardized).GetNumAtoms() == (
            Chem.MolFromSmiles(shipped).GetNumAtoms()
        ), f"{name}: {shipped} -> {standardized}"


def test_a_carbon_bearing_anion_is_still_inorganic() -> None:
    """The reason the gate tests C–H/C–C and not "contains a carbon".

    Carbonate contains carbon, so "contains a carbon" would make `[O-]C([O-])=O` the organic parent
    of K2CO3 and throw the potassium away — the exact collapse above. Cyanide is the same case, and
    NaCN and KCN are two reagents.
    """
    assert compound_id(_shipped("K2CO3")) != compound_id(_shipped("Cs2CO3"))
    assert standard_smiles("[Na+].[C-]#N") != standard_smiles("[K+].[C-]#N")


def test_a_metal_complex_is_not_its_ligand() -> None:
    """The same deletion as the base screen, reached through `Cleanup`'s metal disconnection.

    `Cleanup` splits Pd(OAc)2 into `[Pd+2]` beside two acetates, and `FragmentParent` then kept the
    acetate and threw the palladium away — so Pd(OAc)2 *was* acetic acid and Pd(dppf)Cl2 *was* the
    bare ligand. Suzuki chemistry is all over this corpus (`reizman_suzuki` is a shipped benchmark),
    so a screen over Pd sources reported that nothing had changed.
    """
    assert compound_id(_shipped("Pd(OAc)2")) != compound_id(_shipped("AcOH"))
    assert compound_id(_shipped("Pd(OAc)2")) != compound_id(_shipped("Pd(dppf)Cl2"))
    # And the metal itself is the distinction, not merely the presence of one.
    assert standard_smiles("CC(=O)O[Cu]OC(C)=O") != standard_smiles(_shipped("Pd(OAc)2"))


def test_n_butyllithium_is_not_butane() -> None:
    """The most dangerous instance of this defect, pinned by name.

    n-BuLi is pyrophoric and butane is a fuel gas. One `compound_id` means one cached calculation,
    one fingerprint row and one hazard screen for both — so this is a safety property, not only a
    bookkeeping one. The commercial form matters as much as the neat one: n-BuLi is supplied and
    logged as a solution in hexanes, and the solvent is the *larger* fragment, so the strip kept
    the hexane and threw the reagent away.
    """
    assert compound_id("CCCC[Li]") != compound_id("CCCC")
    assert compound_id("CCCC[Li].CCCCCC") != compound_id("CCCCCC")
    assert compound_id("CCCC[Li].CCCCCC") != compound_id("CCCC")


def test_a_metal_carbon_bond_is_the_reagent() -> None:
    """The same statement for the rest of the organometallic family, as supplied and neat.

    A Grignard in THF and an aryllithium in dibutyl ether hit the solvent case; an organozinc and
    a cuprate hit the `MetalDisconnector` case.
    """
    for reagent, stripped_to in (
        ("C[Mg]Br", "C"),  # MeMgBr, not methane
        ("CC(C)[Mg]Cl.C1CCOC1", "C1CCOC1"),  # iPrMgCl in THF, not THF
        ("C[Mg]Br.CCOCC", "CCOCC"),  # MeMgBr in ether, not ether
        ("[Li]c1ccccc1.CCCCOCCCC", "CCCCOCCCC"),  # PhLi in Bu2O, not Bu2O
        ("CC[Zn]CC", "CC"),  # Et2Zn, not ethane
        ("C[Cu]C.[Li+]", "C"),  # a Gilman cuprate, not methane
    ):
        assert compound_id(reagent) != compound_id(stripped_to), reagent


def test_trimethylaluminium_survives_its_own_metal_disconnection() -> None:
    """Why the M–C bond is read from the *input* molecule and not the cleaned one.

    `Cleanup`'s `MetalDisconnector` breaks Al–C — `C[Al](C)C` arrives at the strip as `[Al+3]`
    beside three methyl anions — while leaving Li–C and Mg–C intact. Aluminium is not d- or
    f-block, so nothing else catches it: read from the cleaned molecule, the evidence is gone and
    AlMe3 standardizes to methane, and AlMe3 in toluene to toluene. It is the same hazard as n-BuLi
    (pyrophoric, supplied as a solution), and the only case in this family where the two stages
    disagree — which is what makes the choice of stage a decision rather than a formality.
    """
    assert compound_id("C[Al](C)C") != compound_id("C")
    assert compound_id("C[Al](C)C.Cc1ccccc1") != compound_id("Cc1ccccc1")


def test_a_group_1_or_2_counterion_is_still_a_spectator() -> None:
    """The constraint the metal rule had to satisfy: sodium benzoate *is* benzoic acid.

    Group 1/2 balances a charge and is never what the compound is, which is why the block —
    not "contains a metal" — is what the gate tests.
    """
    assert compound_id("[Na+].[O-]C(=O)c1ccccc1") == compound_id("OC(=O)c1ccccc1")
    assert compound_id("CC(=O)[O-].[Na+]") == compound_id("CC(=O)O")
    assert standard_smiles(_shipped("LDA")) == standard_smiles("CC(C)NC(C)C")  # the Li is dropped
    # LiHMDS is the case that separates the two rules: same lithium as n-BuLi, but Li–N rather
    # than Li–C, so it is an ionic salt and collapses while n-BuLi does not.
    assert compound_id("C[Si](C)(C)[N-][Si](C)(C)C.[Li+]") == compound_id("C[Si](C)(C)N[Si](C)(C)C")


def test_an_organic_salt_still_loses_its_counterion() -> None:
    """The guard on the gate: over-correcting it would undo D-2026-07-31 itself.

    TBTU is shipped as its tetrafluoroborate; the compound is the uronium cation, and the free
    base / hydrochloride pair below is the same statement for a molecule a chemist submits.
    """
    assert "." not in standard_smiles(_shipped("TBTU"))  # one fragment left: the BF4 is gone
    assert compound_id("CCN.Cl") == compound_id("CCN")
    assert compound_id("[Na+].[O-]C(=O)c1ccccc1") == compound_id("OC(=O)c1ccccc1")


def test_standardization_is_recorded_in_the_fingerprint_definition() -> None:
    """Rows indexed under an older notion of sameness must fall out, not be ranked against new ones.

    The same failure-safe behaviour a changed ECFP radius already gets: the store filters on the
    definition, so a stale row is invisible to search until a re-index rebuilds it.
    """
    assert STANDARDIZATION_VERSION in molecule_definition()
    assert STANDARDIZATION_VERSION in reaction_definition()


# --- one id, one body -------------------------------------------------------------------------
#
# The id answers "same compound?" and the body used to answer "same structure?", so two spellings
# shared an id and disagreed about everything under it.


def test_one_compound_id_means_one_note_body() -> None:
    """Every field of the note comes from the key the id is hashed from, or the note forks.

    `compound_dependencies` re-proposes the compound note a merged note links, and relies on that
    re-proposal rendering byte-identically. With the body keyed on the spelling instead, a QM note
    carrying a canonicalized-only SMILES re-proposed the *same id* with a rewritten structure
    field — a diff on every ingest, and the last spelling ingested won.
    """
    free_base = compound_note("CCN")
    hydrochloride = compound_note("CCN.Cl")
    assert free_base.id == hydrochloride.id
    assert free_base.compound_smiles == hydrochloride.compound_smiles
    assert free_base.body == hydrochloride.body
    assert render_note(free_base) == render_note(hydrochloride)  # the "no diff" contract, literally


def test_the_note_records_the_structure_its_id_was_derived_from() -> None:
    """A note whose body contradicts its id cannot be cited: the citation resolves to the id."""
    note = compound_note("CC(=O)[O-].[Na+]")
    assert note.compound_smiles == standard_smiles("CC(=O)[O-].[Na+]")
    assert note.id == compound_id(note.compound_smiles)
    assert note.compound_smiles in note.body


def test_a_base_screen_reads_as_two_different_bases() -> None:
    """Both defects in one statement, on the path that reported the base screen changed nothing."""
    naoh, koh = compound_note(_shipped("NaOH")), compound_note(_shipped("KOH"))
    assert naoh.id != koh.id
    assert "sodium hydroxide" in naoh.body
    assert "potassium hydroxide" in koh.body


def test_every_shipped_reagent_note_carries_its_name() -> None:
    """A named reagent must never render as an anonymous structure.

    The note is keyed on the standardized SMILES while `reagents` is keyed on the canonical one,
    and for seven shipped reagents those differ (DMSO, SOCl2, TBTU, HATU, LDA and both palladium
    entries) — so the name lookup missed and a chemist opening the DMSO note saw no name at all.
    Asserted over the whole shipped table, because the gap was invisible on DMF, where the two
    keys happen to agree.
    """
    anonymous = [n for n in known_names() if "- name: " not in compound_note(_shipped(n)).body]
    assert anonymous == []


def test_a_reagent_note_lists_the_spellings_of_its_own_compound_only() -> None:
    """The vocabulary must be built on the standardized key without fabricating a membership.

    Folding the synonym list onto the standardized key is only safe because no two shipped
    reagents share one: while Pd(OAc)2 standardized to acetic acid, this would have written
    `pd(oac)2` onto the acetic-acid note — a name for a compound that is not that compound.
    """
    assert compound_note(_shipped("DMSO")).body.count("also written: dimethylsulfoxide, dmso") == 1
    assert "pd(oac)2" not in compound_note(_shipped("AcOH")).body
    by_compound: dict[str, set[str | None]] = {}
    for name in known_names():
        shipped = _shipped(name)
        by_compound.setdefault(compound_id(shipped), set()).add(display_name(shipped))
    assert [names for names in by_compound.values() if len(names) > 1] == []


@pytest.mark.parametrize(
    ("spelling", "expected"),
    [
        # Hydrazine, every way a catalogue sells it. The free base is a liquid nobody stores; the
        # salts are what is weighed out, and their SMILES is the reason the hazard rule needed
        # widening at all.
        ("hydrazine", "hydrazine"),
        ("N2H4", "hydrazine"),
        ("hydrazine hydrate", "hydrazine hydrate"),
        ("hydrazine hydrochloride", "hydrazine hydrochloride"),
        ("hydrazine sulfate", "hydrazine sulfate"),
        ("UDMH", "1,1-dimethylhydrazine"),
        ("phenylhydrazine", "phenylhydrazine"),
        # The solid peroxide, beside the liquid the table already had.
        ("Na2O2", "sodium peroxide"),
        ("sodium peroxide", "sodium peroxide"),
    ],
)
def test_a_reagent_the_hazard_rules_were_widened_for_can_be_named(
    spelling: str, expected: str
) -> None:
    """The table held no hydrazine at all, so a rule written for it could not be checked by name.

    `Chemclaw3-mcp`'s `hydrazine` rule and the hydrazine arm of `oxidizer-with-reductant` were each
    widened twice — once for the `NX4+` of a salt, once to drop an H requirement for UDMH — and both
    widenings could only ever be pinned by SMILES. That is half a path. A chemist writes "hydrazine
    sulfate" in an ELN or asks about it in a turn, and whether the screen sees a protonated or a
    neutral spelling is the *source's* choice; the reagent table is what turns the name into either.
    With no entry, the name resolved to nothing and the screen was never reached.

    Asserted here; the screening half is asserted in
    `Chemclaw3-mcp:servers/safety/tests/test_pairs.py`
    (`test_the_hydrazine_arm_fires_on_every_form_a_catalogue_sells`), because the two repositories
    own the two halves and neither can state the whole claim alone.

    Sodium peroxide is the same shape one motif over: the table had hydrogen peroxide and not the
    solid, and the solid is the molecule whose one-coordinate-anion SMILES has now defeated three
    separate screening patterns.
    """
    resolved = resolve_compound_name(spelling)
    assert resolved is not None, f"{spelling!r} resolves to nothing"
    assert resolved.name == expected


def test_oversized_smiles_is_refused_not_crashed() -> None:
    """A molecule past the atom/length cap raises instead of segfaulting the process.

    RDKit's canonical-SMILES writer and the tautomer canonicalizer are unbounded-recursive and
    SIGSEGV on a large linear molecule (measured between ~16k and ~20k atoms) — an uncatchable
    crash that takes the whole worker and every concurrent session with it, reachable by a ~20 KB
    SMILES that clears the 1 MB body cap and as an ELN poison pill. `require_molecule` is the one
    gate every SMILES caller shares, so the bound lives there; the lenient helpers passthrough.
    """
    from chemclaw.core.chem import canonical_smiles, require_molecule, standard_smiles

    huge = "C" * 20000
    with pytest.raises(InvalidSmilesError):
        require_molecule(huge)
    # lenient helpers must not crash — they return the input unchanged, exactly like an unparseable
    # string, rather than handing an oversized molecule to the writer.
    assert canonical_smiles(huge) == huge
    assert standard_smiles(huge) == huge
    # a real reagent well under the cap still parses
    assert require_molecule("CC(=O)Oc1ccccc1C(=O)O").GetNumAtoms() == 13
