"""One canonical identity per molecule (gaps KNW-7, KNW-4).

A structure-derived identity lets a structural hit cite a compound note, and makes `DMF`,
`N,N-dimethylformamide` and `CN(C)C=O` one species so a campaign does not split on spelling.
"""

import asyncio
import csv

import pytest
from rdkit import Chem

from chemclaw.core.chem import (
    STANDARDIZATION_VERSION,
    InvalidSmilesError,
    _is_organic,
    canonical_smiles,
    compound_id,
    compound_id_of_standard,
    standard_smiles,
)
from chemclaw.core.reagents import _TABLE, display_name, resolve_compound_name, synonyms_of
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
from tests.siblings import SIBLING_SKIP, sibling_root


def test_the_same_molecule_gets_one_id_however_it_is_written() -> None:
    """Structure-derived, not name-derived — the property that makes a citation stable."""
    assert compound_id("CN(C)C=O") == compound_id("O=CN(C)C")
    assert compound_id("CCO") != compound_id("CCC")


def test_the_derivation_is_pinned_to_a_literal() -> None:
    """The id is a published citation, so its derivation is pinned to a literal.

    A round-trip check would pass if the scheme changed and every merged
    `knowledge/compound/<id>.md` became unreachable.
    """
    assert compound_id("CCO") == "compound-f29e20f49d41"
    assert compound_id("OCC") == "compound-f29e20f49d41"  # same molecule, other spelling


@pytest.mark.parametrize(
    "written",
    ["CCO", "CN(C)C=O", "CC[NH3+].[Br-]", "[Na+].[O-]C(=O)c1ccccc1", "Oc1ccccn1", "F/C=C/F"],
)
def test_an_already_standard_structure_hashes_to_its_compound_id_without_standardizing(
    written: str,
) -> None:
    """`compound_id_of_standard` equals `compound_id` on an already-standard structure.

    Stored labels are hashed directly, so if standardizing a standard SMILES moved it, hits would
    stop resolving.
    """
    standard = standard_smiles(written)
    assert standard_smiles(standard) == standard
    assert compound_id_of_standard(standard) == compound_id(written)


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
    """The synonym lookup standardizes its argument, so any spelling of the compound answers.

    DMSO and TBTU are cases where canonical and standardized keys differ; DMF is not.
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
# A structural hit carries the compound note id, so it is a citation rather than a substring scan.


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
    """A salt and its free base are one compound.

    `compound_id` keys the calculation cache and both fingerprint indices, so a separate salt id
    means repeated work and a molecule ranked as merely similar to itself.
    """
    assert compound_id("CCN.Cl") == compound_id("CCN")
    assert compound_id("CC(=O)[O-].[Na+]") == compound_id("CC(=O)O")


def test_an_amine_salt_drawn_as_an_ion_pair_is_its_free_base() -> None:
    """An amine salt drawn as an ion pair is its free base.

    The cationic case: a protonated amine with its counterion, as catalogues and ELNs write a
    hydrochloride. `_neutralization_is_protonation` must not refuse a cation, where neutralisation
    removes a proton. Several drug salts, since the rule is about the class. The bare guanidinium
    row is the boundary of `_is_organic` (a carbon with three or more heavy neighbours, two of them
    nitrogen).
    """
    for name, base, salt in (
        ("ethylamine", "CCN", "CC[NH3+].[Br-]"),
        ("pyridine", "c1ccncc1", "c1cc[nH+]cc1.[Cl-]"),
        ("lidocaine", "CCN(CC)CC(=O)Nc1c(C)cccc1C", "CC[NH+](CC)CC(=O)Nc1c(C)cccc1C.[Cl-]"),
        ("propranolol", "CC(C)NCC(O)COc1cccc2ccccc12", "CC(C)[NH2+]CC(O)COc1cccc2ccccc12.[Cl-]"),
        ("metformin", "CN(C)C(=N)N=C(N)N", "CN(C)C(=[NH2+])N=C(N)N.[Cl-]"),
        # The case the narrower sentence used to except: no C–H and no C–C anywhere in the cation.
        ("guanidine", "NC(N)=N", "NC(=[NH2+])N.[Cl-]"),
        ("acetamidine", "CC(N)=N", "CC(=[NH2+])N.[Cl-]"),
    ):
        assert compound_id(salt) == compound_id(base), (
            f"{name} as an ion pair is a different compound from its free base: "
            f"{standard_smiles(salt)!r} against {standard_smiles(base)!r}"
        )


def test_the_anion_the_neutralisation_guard_exists_for_is_still_kept_as_written() -> None:
    """The anion the neutralisation guard exists for is still kept as written.

    Neutralising sodium triacetoxyborohydride removes the hydride and yields triacetoxyborane, which
    reduces nothing. It is an anion, so the cation exemption must not reach it.
    """
    assert "[BH-]" in standard_smiles("CC(=O)O[BH-](OC(C)=O)OC(C)=O.[Na+]"), (
        "sodium triacetoxyborohydride was neutralised to triacetoxyborane, which reduces nothing"
    )
    assert standard_smiles("[BH4-].[Na+]") == "[BH4-].[Na+]"
    # And the anionic conjugate bases the guard is *meant* to collapse still collapse.
    assert standard_smiles("CC(=O)[O-].[Na+]") == "CC(=O)O"
    assert standard_smiles("CC(C)(C)[O-].[K+]") == "CC(C)(C)O"


def test_a_hydride_salt_of_an_organic_cation_keeps_its_hydride_too() -> None:
    """A hydride salt of an organic cation keeps its hydride too.

    With two organic fragments `standardize` keeps every fragment, so a net-charge test over the
    whole string would wrongly exempt a hydride paired with ammonium cations. The exemption also
    requires that no fragment be anionic. Both charge states are asserted; the net-zero row pins the
    shape. Two cations are needed with `[BH4-]`, because with one organic fragment the inorganic
    hydride is stripped as a counterion before this guard runs.
    """
    triacetoxy = "CC(=O)O[BH-](OC(C)=O)OC(C)=O"
    et3nh = "CC[NH+](CC)CC"
    for label, salt, hydride in (
        ("borohydride, net +1", f"[BH4-].{et3nh}.{et3nh}", "[BH4-]"),
        ("triacetoxyborohydride, net +1", f"{triacetoxy}.{et3nh}.{et3nh}", "[BH-]"),
        ("triacetoxyborohydride, net 0", f"{triacetoxy}.{et3nh}", "[BH-]"),
    ):
        assert hydride in standard_smiles(salt), (
            f"{label}: the hydride was neutralised away, leaving a Lewis acid that reduces "
            f"nothing — {standard_smiles(salt)!r}"
        )
    # And the cation of that same pair still collapses on its own, so this is not a widening back
    # onto the defect the exemption was written for.
    assert compound_id(f"{et3nh}.[Cl-]") == compound_id("CCN(CC)CC")


def test_two_tautomers_are_one_compound() -> None:
    """Two spellings of one substance, which a chemist would never file separately."""
    assert compound_id("CC(O)=NC") == compound_id("CC(=O)NC")


def test_two_different_molecules_stay_different() -> None:
    """The guard on the above: a pipeline that collapsed everything would pass those tests too."""
    assert compound_id("CCO") != compound_id("CCN")
    assert compound_id("c1ccccc1") != compound_id("c1ccncc1")


def test_a_calculation_input_keeps_the_charge_it_was_given() -> None:
    """A calculation input keeps the charge it was given.

    `canonical_smiles` ("same structure") stays spelling-only so submitted acetate is computed as
    acetate; `standard_smiles` ("same compound") neutralises.
    """
    assert canonical_smiles("CC(=O)[O-]") != canonical_smiles("CC(=O)O")
    assert standard_smiles("CC(=O)[O-]") == standard_smiles("CC(=O)O")


def _shipped(name: str) -> str:
    """The SMILES `chemclaw.core.reagents` actually ships for a reagent.

    The tests assert over the shipped table, which is what reaches `standardize` on ELN ingest.
    """
    resolved = resolve_compound_name(name)
    assert resolved is not None, f"{name} is no longer a shipped reagent"
    return resolved.smiles


def test_each_shipped_inorganic_reagent_is_its_own_compound() -> None:
    """Each shipped inorganic reagent is its own compound.

    With no organic parent, `FragmentParent` must not delete the anion (NaOH and KOH are not water,
    the carbonates are not carbonic acid).
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
    """A carbon-bearing anion is still inorganic.

    The gate tests bonds, not "contains a carbon": C–H, C–C, or a three-coordinate carbon holding
    two nitrogens. Carbonate and cyanide stay inorganic, so NaCN and KCN stay two reagents.
    `_THE_ORGANIC_LINE` is the boundary.
    """
    assert compound_id(_shipped("K2CO3")) != compound_id(_shipped("Cs2CO3"))
    assert standard_smiles("[Na+].[C-]#N") != standard_smiles("[K+].[C-]#N")


def test_a_metal_complex_is_not_its_ligand() -> None:
    """A metal complex is not its ligand.

    `Cleanup` disconnects the metal; the strip must not then keep the acetate or the phosphine and
    discard the palladium.
    """
    assert compound_id(_shipped("Pd(OAc)2")) != compound_id(_shipped("AcOH"))
    assert compound_id(_shipped("Pd(OAc)2")) != compound_id(_shipped("Pd(dppf)Cl2"))
    # And the metal itself is the distinction, not merely the presence of one.
    assert standard_smiles("CC(=O)O[Cu]OC(C)=O") != standard_smiles(_shipped("Pd(OAc)2"))


def test_n_butyllithium_is_not_butane() -> None:
    """n-Butyllithium is not butane, neat or as a solution in hexanes.

    One id would mean one hazard screen for a pyrophoric reagent and a fuel gas. In solution the
    solvent is the larger fragment, so the strip must not keep the hexane.
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
    """Trimethylaluminium survives its own metal disconnection.

    `MetalDisconnector` breaks Al–C (but not Li–C or Mg–C), so the M–C bond is read from the input
    molecule rather than the cleaned one; otherwise AlMe3 standardizes to methane.
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


def test_a_solvate_is_not_its_solvent() -> None:
    """A solvate is not its solvent (`D-2026-08-27-a-solvate-is-not-its-solvent`).

    Both fragments are asserted to survive, since "the ids differ" would also pass if the solvent
    were deleted instead.
    """
    solvate = standard_smiles("CCN.C1CCOC1")
    assert compound_id("CCN.C1CCOC1") != compound_id("C1CCOC1")
    assert compound_id("CCN.C1CCOC1") != compound_id("CCN")
    assert set(solvate.split(".")) == {standard_smiles("C1CCOC1"), standard_smiles("CCN")}


def test_the_tie_break_no_longer_depends_on_which_solvent_it_is() -> None:
    """The surviving compound does not depend on which solvent it is with.

    Pyridine (79.1) lies between THF (72.1) and toluene (92.1), so "keep the larger fragment" would
    keep pyridine in one solvate and toluene in the other; a heavier solute could not show this.
    """
    in_thf = standard_smiles("c1ccncc1.C1CCOC1")
    in_toluene = standard_smiles("c1ccncc1.Cc1ccccc1")
    solute = standard_smiles("c1ccncc1")
    assert solute in in_thf.split("."), f"the solvent won: {in_thf}"
    assert solute in in_toluene.split("."), f"the solvent won: {in_toluene}"


def test_the_solvate_rule_changed_no_shipped_reagent() -> None:
    """Exactly three shipped reagents lose a fragment, and they are the salts.

    LDA, HATU and TBTU collapse by the counterion rule. A fourth name means a solvate or co-crystal
    is being deleted; one missing means the counterion rule broke. This guards the reagent table,
    not the solvate rule, which `test_a_solvate_is_not_its_solvent` and the tie-break test cover.
    """
    shipped = {r.smiles: r.name for r in map(resolve_compound_name, sorted(_TABLE)) if r}
    assert len(shipped) == 68, "the shipped table changed; re-measure before editing the set below"
    shrunk = {
        name
        for smiles, name in shipped.items()
        if Chem.MolFromSmiles(standard_smiles(smiles)).GetNumAtoms()
        < Chem.MolFromSmiles(smiles).GetNumAtoms()
    }
    assert shrunk == {"lithium diisopropylamide", "HATU", "TBTU"}


def test_an_organic_salt_keeps_one_identity_however_its_proton_was_drawn() -> None:
    """An organic salt keeps one identity however its proton was drawn.

    `Uncharger` runs on the keep-whole path too, so nicotine bitartrate as an ion pair and as a
    neutral co-crystal is one compound.
    """
    assert compound_id("C[NH+]1CCC[C@H]1c1cccnc1.[O-]C(=O)[C@H](O)[C@@H](O)C(=O)O") == compound_id(
        "CN1CCC[C@H]1c1cccnc1.OC(=O)[C@H](O)[C@@H](O)C(=O)O"
    )


def test_a_bare_inorganic_anion_is_not_neutralized_into_another_reagent() -> None:
    """A bare inorganic anion is not neutralized into another reagent.

    `rxnfp._standardize_species` standardizes one `.`-token at a time, so `[OH-]` and `[BH4-]`
    arrive alone and must not become water and borane.
    """
    assert standard_smiles("[OH-]") == "[OH-]"
    assert standard_smiles("[BH4-]") == "[BH4-]"
    assert standard_smiles("[O-]C([O-])=O") != standard_smiles("OC(O)=O")


def test_an_atom_map_is_not_part_of_a_compound_s_identity() -> None:
    """An atom map is not part of a compound's identity.

    RXNMapper stamps map numbers on every species, so mapped and unmapped spellings, and differently
    numbered ones, must give one `compound_id`.
    """
    acetic = compound_id("CC(=O)O")
    assert compound_id("[CH3:1][C:2](=[O:3])[OH:4]") == acetic
    assert compound_id("[CH3:7][C:8](=[O:9])[OH:10]") == acetic
    assert standard_smiles("[CH3:1][C:2](=[O:3])[OH:4]") == standard_smiles("CC(=O)O")


def test_a_map_number_does_not_survive_into_a_note_body_or_a_fingerprint() -> None:
    """A map number does not survive into a note body or a fingerprint.

    Both are built from the standardized structure, asserted separately from the id.
    """
    assert ":" not in standard_smiles("[CH3:1][C:2](=[O:3])[OH:4]")
    assert ecfp_bitstring("[CH3:1][C:2](=[O:3])[OH:4]") == ecfp_bitstring("CC(=O)O")


def test_a_calculation_key_is_still_the_structure_as_it_was_submitted() -> None:
    """A calculation key is still the structure as it was submitted.

    Clearing maps belongs to "same compound", not to `canonical_smiles`; this pins the scope.
    """
    assert canonical_smiles("[CH3:1][C:2](=[O:3])[OH:4]") != canonical_smiles("CC(=O)O")


def _hydrogens(mol: Chem.Mol) -> int:
    """Every hydrogen in a molecule, whether implicit on a heavy atom or an atom of its own."""
    return sum(a.GetTotalNumHs() + (1 if a.GetAtomicNum() == 1 else 0) for a in mol.GetAtoms())


def test_a_hydride_reagent_is_not_neutralized_into_a_non_reducing_ester() -> None:
    """A hydride reagent is not neutralized into a non-reducing ester.

    Neutralising sodium triacetoxyborohydride can only remove the hydride (H 10 → 9), giving
    triacetoxyborane; the other guards all pass it, so the hydrogen-count guard is what stops it.
    """
    borohydride = standard_smiles("CC(=O)O[BH-](OC(C)=O)OC(C)=O.[Na+]")
    assert borohydride != standard_smiles("CC(=O)OB(OC(C)=O)OC(C)=O")
    assert "[BH-]" in borohydride, borohydride
    assert compound_id("CC(=O)O[BH-](OC(C)=O)OC(C)=O.[Na+]") == compound_id(
        "CC(=O)O[BH-](OC(C)=O)OC(C)=O.[K+]"
    )


def test_neutralization_never_costs_the_species_a_hydrogen() -> None:
    """Neutralization never costs the species a hydrogen.

    Neutralisation must protonate, never deprotonate. Fixtures are salts of bare metal cations,
    whose counterion carries no hydrogen, so any hydrogen lost came from the compound. (An amine
    hydrochloride loses HCl whole in the strip, which is correct, so it is not in this set.)
    """
    for salt in (
        "CC(=O)O[BH-](OC(C)=O)OC(C)=O.[Na+]",  # NaBH(OAc)3, the reagent above
        "CC(=O)[O-].[Na+]",  # sodium acetate: protonation, must still collapse
        "CC(C)(C)[O-].[K+]",  # KOtBu: protonation, see the test below
        "C[S-].[Na+]",  # sodium thiomethoxide
        "[O-]C(=O)c1ccccc1.[Na+]",  # sodium benzoate, D-2026-07-31's own example
    ):
        before = Chem.MolFromSmiles(salt)
        after = Chem.MolFromSmiles(standard_smiles(salt))
        assert _hydrogens(after) >= _hydrogens(before), salt


def test_an_alkali_salt_of_an_organic_acid_still_collapses_including_the_alkoxides() -> None:
    """An alkali salt of an organic acid still collapses, including the alkoxides.

    KOtBu and tert-butanol share an id, like sodium acetate and acetic acid: the counterion is not
    part of the identity. The accepted cost: a base screen over alkoxides collapses onto their
    alcohols. Separating them would need a pKa-shaped notion of sameness, which `core/chem.py`
    refuses.
    """
    assert compound_id("CC(C)(C)[O-].[K+]") == compound_id("CC(C)(C)O")
    assert compound_id("CC(C)(C)[O-].[Na+]") == compound_id("CC(C)(C)[O-].[K+]")
    assert compound_id("[Na+].[O-]C") == compound_id("CO")
    assert compound_id("C[Si](C)(C)[N-][Si](C)(C)C.[Li+]") == compound_id("C[Si](C)(C)N[Si](C)(C)C")
    # And the guard on it: the wholly inorganic bases D-2026-08-01 rescued are still held apart,
    # so this is a decision about organic conjugate acids and not a pipeline that collapses
    # everything.
    assert compound_id(_shipped("NaOH")) != compound_id(_shipped("KOH"))


def test_standardization_is_recorded_in_the_fingerprint_definition() -> None:
    """Rows indexed under an older notion of sameness must fall out, not be ranked against new ones.

    The same failure-safe behaviour a changed ECFP radius already gets: the store filters on the
    definition, so a stale row is invisible to search until a re-index rebuilds it.
    """
    assert STANDARDIZATION_VERSION in molecule_definition()
    assert STANDARDIZATION_VERSION in reaction_definition()


#: The three ferrocenyl Pd G3 precatalysts of `Chemclaw3_mock`'s ORD seed, verbatim — the species
#: `standardize` was not a fixed point on — beside what the dppf one standardizes to.
_DTBPF_PD_G3 = (
    "CS(O[Pd]1([P](C(C)(C)C)(C(C)(C)C)C2=CC=CC2[Fe]C3C(P(C(C)(C)C)C(C)(C)C)=CC=C3)"
    "C4=CC=CC=C4C5=C([NH2]1)C=CC=C5)(=O)=O"
)
_DPPF_PD_G3 = (
    "CS(O[Pd]1([P](C2=CC=CC=C2)(C3=CC=CC=C3)C4=CC=CC4[Fe]C5C(P(C6=CC=CC=C6)C7=CC=CC=C7)=CC=C5)"
    "C8=CC=CC=C8C9=C([NH2]1)C=CC=C9)(=O)=O"
)
_JOSIPHOS_PD_G3 = (
    "CC(P(C(C)(C)C)C(C)(C)C)C1=C(C([Fe]C2C=CC=C2)C=C1)[P]([Pd]3(OS(C)(=O)=O)"
    "C4=CC=CC=C4C5=C([NH2]3)C=CC=C5)(C6CCCCC6)C7CCCCC7"
)
_DPPF_PD_G3_STANDARD = (
    "CS(=O)(=O)[O-].Nc1ccccc1-c1cccc[c]1[Pd+].[Fe+2]"
    ".c1ccc(P(c2ccccc2)c2ccc[cH-]2)cc1.c1ccc(P(c2ccccc2)c2ccc[cH-]2)cc1"
)


#: What `standardize` does at `STANDARDIZATION_VERSION`, measured, one row per decision the
#: pipeline takes. Every value here is an output of this build rather than a hand-written
#: expectation, so a row that changes is a change in the notion of sameness and nothing else.
_STANDARDIZATION_AT_THIS_VERSION = (
    # the counterion strip and the cationic/anionic halves of the neutralisation
    ("CC[NH3+].[Br-]", "CCN"),
    ("CCN.Cl", "CCN"),
    ("c1cc[nH+]cc1.[Cl-]", "c1ccncc1"),
    ("CC(=O)[O-].[Na+]", "CC(=O)O"),
    # the hydride the neutralisation guard exists for, in all three of its charge states
    ("CC(=O)O[BH-](OC(C)=O)OC(C)=O.[Na+]", "CC(=O)O[BH-](OC(C)=O)OC(C)=O"),
    ("[BH4-].[Na+]", "[BH4-].[Na+]"),
    ("[BH4-].CC[NH+](CC)CC.CC[NH+](CC)CC", "CC[NH+](CC)CC.CC[NH+](CC)CC.[BH4-]"),
    (
        "CC(=O)O[BH-](OC(C)=O)OC(C)=O.CC[NH+](CC)CC",
        "CC(=O)O[BH-](OC(C)=O)OC(C)=O.CC[NH+](CC)CC",
    ),
    # atom maps, the solvate kept whole, the metal-carbon bond, the wholly inorganic reagent
    ("[CH3:1][C:2](=[O:3])[OH:4]", "CC(=O)O"),
    ("CCN.C1CCOC1", "C1CCOC1.CCN"),
    ("CC[Mg]Br", "C[CH2][Mg][Br]"),
    ("[OH-].[Na+]", "[Na+].[OH-]"),
    # and the boundary of `_is_organic`, which moved at std10: a carbon with two nitrogen
    # neighbours is organic, so a bare guanidinium salt now reaches the neutralisation branch
    ("NC(=[NH2+])N.[Cl-]", "N=C(N)N"),
    ("NC(N)=O.Cl", "NC(N)=O"),
    ("Nc1nc(N)nc(N)n1.Cl", "Nc1nc(N)nc(N)n1"),
    # a two-coordinate carbon is the other side of that line, and these must stay two reagents
    # apiece. The cyanamides stay inorganic; otherwise the two spellings neutralise to different
    # tautomers and calcium cyanamide shares an id with free HN=C=NH.
    ("[Na+].[C-]#N", "[C-]#N.[Na+]"),
    ("[K+].[C-]#N", "[C-]#N.[K+]"),
    ("[K+].[S-]C#N", "N#C[S-].[K+]"),
    ("[Na+].[N-]=C=O", "[N-]=C=O.[Na+]"),
    ("[K+].[K+].[O-]C([O-])=O", "O=C([O-])[O-].[K+].[K+]"),
    ("[Na+].[NH-]C#N", "N#C[NH-].[Na+]"),
    ("[Ca+2].[N-]=C=[N-]", "[Ca+2].[N-]=C=[N-]"),
    # Which fragment is kept when exactly one is organic — the module's own answer, not RDKit's.
    # Every row above exercises this branch on a species where the two agree, which is why none of
    # them moved when they were made to disagree; this one is the disagreement.
    ("[NH4+].[O-]C=O", "O=CO"),
    ("[NH4+].CC(=O)[O-]", "CC(=O)O"),
    ("[Na+].[O-]C=O", "O=CO"),
    # A neutral co-former is not a counterion: urea hydrogen peroxide is a bench oxidant and stays
    # one, while a charged spectator and a known solvent still go. See
    # `test_a_neutral_co_former_is_not_a_counterion`.
    ("NC(N)=O.OO", "NC(N)=O.OO"),
    ("CCN.OO", "CCN.OO"),
    ("CCN.O", "CCN"),
    ("CN(C)C(=[N+](C)C)On1nnc2ccccc21.F[B-](F)(F)F", "CN(C)C(On1nnc2ccccc21)=[N+](C)C"),
    # `std12`: a salt of an acid the catalogue omits, both spellings, one per acid in
    # `_IONISABLE_NEUTRAL_ACIDS`. Under `std11` every neutral spelling here kept its acid.
    ("CCN.OCl(=O)(=O)=O", "CCN"),
    ("CC[NH3+].[O-]Cl(=O)(=O)=O", "CCN"),
    ("CCN.F[B-](F)(F)[FH+]", "CCN"),
    ("CC[NH3+].F[B-](F)(F)F", "CCN"),
    ("CCN.NS(=O)(=O)O", "CCN"),
    ("CC[NH3+].NS(=O)(=O)[O-]", "CCN"),
    ("CCN.SC#N", "CCN"),
    ("CCN.N=C=S", "CCN"),
    ("CC[NH3+].[S-]C#N", "CCN"),
    ("CCN.OC(=O)O", "CCN"),
    ("CC[NH3+].OC(=O)[O-]", "CCN"),
    ("CCN.O[PH2]=O", "CCN"),
    ("CC[NH3+].[O-][PH2]=O", "CCN"),
    ("CCN.OB(O)O", "CCN"),
    ("CC[NH3+].[O-]B(O)O", "CCN"),
    # and the neutrals that cannot ionise stay, which is the line the table draws
    ("CCN.FB(F)F", "CCN.FB(F)F"),
    # and beside a partner with no basic site the acid is a second component, not a counterion
    ("OB(O)c1ccccc1.OB(O)O", "OB(O)O.OB(O)c1ccccc1"),
    ("O=C(O)c1ccccc1.OC(=O)O", "O=C(O)O.O=C(O)c1ccccc1"),
    ("O=[N+]([O-])c1ccccc1.OCl(=O)(=O)=O", "O=[N+]([O-])c1ccccc1.[O-][Cl+3]([O-])([O-])O"),
    ("CCN.O=C=O", "CCN.O=C=O"),
    # `std12` re-perceives a cyclopentadienyl ring after a step moves its charge (`_cleaned`,
    # `_uncharged`), so ferrocene and a bare Cp⁻ standardize to valid, stable forms. The precatalyst
    # rows are `Chemclaw3_mock`'s ORD seed, verbatim.
    ("C1(C=CC=C1)[Fe]C1C=CC=C1", "[Fe+2].c1cc[cH-]c1.c1cc[cH-]c1"),
    ("[CH-]1C=CC=C1", "C1=CCC=C1"),
    ("CC(C)(C)P(C(C)(C)C)[C-]1C=CC=C1", "CC(C)(C)P(C1C=CC=C1)C(C)(C)C"),
    (_DPPF_PD_G3, _DPPF_PD_G3_STANDARD),
)


#: Where the organic/inorganic line falls, one row per species, `True` meaning organic.
#:
#: The table is the drive behind `_is_organic`, so a change to it shows what moves. The rows below
#: the divider decided the predicate's shape: the carbon's degree is what keeps the cyanamides out.
_THE_ORGANIC_LINE = (
    # --- organic at std10, inorganic at std9: the class the bump exists for -------------------
    ("urea", "NC(N)=O", True),
    ("thiourea", "NC(N)=S", True),
    ("selenourea", "NC(N)=[Se]", True),
    ("guanidine", "NC(N)=N", True),
    ("nitroguanidine", "NC(N)=N[N+]([O-])=O", True),
    ("melamine", "Nc1nc(N)nc(N)n1", True),
    ("biuret", "NC(=O)NC(N)=O", True),
    ("semicarbazide", "NNC(N)=O", True),
    ("dicyandiamide", "N#CNC(N)=N", True),
    ("cyanuric acid", "O=c1[nH]c(=O)[nH]c(=O)[nH]1", True),
    ("cyanuric chloride", "Clc1nc(Cl)nc(Cl)n1", True),
    ("trichloroisocyanuric acid", "O=c1n(Cl)c(=O)n(Cl)c(=O)n1Cl", True),
    ("5-aminotetrazole", "Nc1nnn[nH]1", True),
    # --- organic at both versions, by the C–H or C–C clause -----------------------------------
    ("acetamidine", "CC(N)=N", True),
    ("metformin", "CN(C)C(=N)N=C(N)N", True),
    ("tetramethylurea", "CN(C)C(=O)N(C)C", True),
    ("DMF", "CN(C)C=O", True),
    ("cyanogen", "N#CC#N", True),
    ("calcium carbide", "[C-]#[C-]", True),
    # --- inorganic at both, and every one of these is a reagent that must stay itself ---------
    ("cyanide", "[C-]#N", False),
    ("cyanate", "[N-]=C=O", False),
    ("isocyanic acid", "N=C=O", False),
    ("thiocyanate", "[S-]C#N", False),
    ("fulminate", "[C-]#[N+][O-]", False),
    ("cyanogen bromide", "BrC#N", False),
    ("carbonate", "[O-]C([O-])=O", False),
    ("bicarbonate", "OC([O-])=O", False),
    ("carbamic acid", "NC(O)=O", False),
    ("carbamate", "NC([O-])=O", False),
    ("carbon monoxide", "[C-]#[O+]", False),
    ("carbon dioxide", "O=C=O", False),
    ("carbon disulfide", "S=C=S", False),
    ("carbonyl sulfide", "O=C=S", False),
    ("phosgene", "ClC(Cl)=O", False),
    ("thiophosgene", "ClC(Cl)=S", False),
    ("tetrafluoromethane", "FC(F)(F)F", False),
    ("carbon tetrachloride", "ClC(Cl)(Cl)Cl", False),
    ("cyanoborohydride", "[BH3-]C#N", False),
    # --- the degree clause: two-coordinate carbon, two nitrogens ------------------------------
    # Nitrogen-counting alone made every one of these organic. See
    # `_STANDARDIZATION_AT_THIS_VERSION` for what that then did to the two cyanamide spellings.
    ("cyanamide", "NC#N", False),
    ("cyanamide dianion", "[N-]=C=[N-]", False),
    ("carbodiimide", "N=C=N", False),
    ("dicyanamide", "N#C[N-]C#N", False),
)


def test_the_organic_line_is_where_the_version_says_it_is() -> None:
    """`_is_organic` over every species that decided it, so the drive is reproducible.

    A fragment-level test rather than a `standardize` one: this is the predicate, and what
    `standardize` does with its answer is the behaviour table below.
    """
    wrong = {}
    for name, smiles, expected in _THE_ORGANIC_LINE:
        molecule = Chem.MolFromSmiles(smiles)
        assert molecule is not None, f"{name} ({smiles}) does not parse"
        fragments = Chem.GetMolFrags(molecule, asMols=True)
        actual = any(_is_organic(fragment) for fragment in fragments)
        if actual is not expected:
            wrong[name] = f"{smiles} is organic={actual}, expected {expected}"
    assert not wrong, wrong


def test_the_degree_clause_is_what_keeps_the_cyanamides_out() -> None:
    """The degree clause is what keeps the cyanamides out.

    Without it, four rows above flip and `[Ca+2].[N-]=C=[N-]` and `[Na+].[NH-]C#N` neutralise to
    different tautomers under two ids.
    """

    def nitrogens_only(fragment: Chem.Mol) -> bool:
        """The rejected spelling: two nitrogens, whatever the carbon's coordination."""
        for atom in fragment.GetAtoms():
            if atom.GetAtomicNum() != 6:
                continue
            neighbours = [one.GetAtomicNum() for one in atom.GetNeighbors()]
            if atom.GetTotalNumHs(includeNeighbors=True) > 0 or 6 in neighbours:
                return True
            if neighbours.count(7) >= 2:
                return True
        return False

    from chemclaw.core.chem import _is_organic

    flipped = [
        name
        for name, smiles, _ in _THE_ORGANIC_LINE
        if any(
            nitrogens_only(fragment) != _is_organic(fragment)
            for fragment in Chem.GetMolFrags(Chem.MolFromSmiles(smiles), asMols=True)
        )
    ]
    assert flipped == ["cyanamide", "cyanamide dianion", "carbodiimide", "dicyanamide"], flipped
    assert compound_id("[Ca+2].[N-]=C=[N-]") != compound_id("N=C=N")
    assert compound_id("[Ca+2].[N-]=C=[N-]") != compound_id("[Na+].[NH-]C#N")


def test_the_parent_is_the_fragment_this_module_calls_organic() -> None:
    """The parent is the fragment this module calls organic, not RDKit's chooser.

    `rdMolStandardize.FragmentParent` counts atoms including hydrogens, so `[NH4+]` beats formate
    and ammonium formate would become ammonia. Driven against RDKit, so this reds if upstream
    changes.
    """
    from rdkit.Chem.MolStandardize import rdMolStandardize

    pair = Chem.MolFromSmiles("[NH4+].[O-]C=O")
    chooser = Chem.MolToSmiles(rdMolStandardize.LargestFragmentChooser().choose(pair))
    assert chooser == "[NH4+]", (
        f"RDKit now keeps {chooser!r} for ammonium formate. The call site no longer asks it, so "
        "nothing breaks — but the measurement this test records has changed; re-read it"
    )
    assert standard_smiles("[NH4+].[O-]C=O") == "O=CO", (
        "ammonium formate standardized to the inorganic half; the module's own `_is_organic` says "
        "which fragment is the compound and `standardize` must use that answer"
    )
    # The control: the module's answer and RDKit's agree on every salt the table already pins, so
    # the change above moves exactly one thing.
    for written in ("CC[NH3+].[Br-]", "[NH4+].CC(=O)[O-]", "NC(=[NH2+])N.[Cl-]", "[Na+].[O-]C=O"):
        molecule = Chem.MolFromSmiles(written)
        organic = [f for f in Chem.GetMolFrags(molecule, asMols=True) if _is_organic(f)]
        assert len(organic) == 1, written
        assert Chem.MolToSmiles(organic[0]) == Chem.MolToSmiles(
            rdMolStandardize.FragmentParent(molecule)
        ), written


def test_a_neutral_co_former_is_not_a_counterion() -> None:
    """A neutral co-former is not a counterion: urea hydrogen peroxide is not urea.

    A spectator is discarded only if it carries a charge or is on RDKit's curated solvent list. The
    charge half covers anions the list lacks (tetrafluoroborate). The question is asked per
    spectator, so one unrecognised neutral does not preserve the chloride beside it.
    """
    assert compound_id("NC(N)=O.OO") != compound_id("NC(N)=O"), "UHP is not urea"
    assert compound_id("CCN.OO") != compound_id("CCN"), "and the same for any organic-H2O2"
    # The three that must still collapse: a charged counterion, a known solvent, and the salt of
    # an organic acid whose counterion this module has always discarded.
    assert compound_id("CC[NH3+].[Br-]") == compound_id("CCN")
    assert compound_id("CCN.O") == compound_id("CCN")
    assert compound_id("CC(=O)[O-].[Na+]") == compound_id("CC(=O)O")
    # And the counterion RDKit's list does not know, which is why charge is read first.
    assert standard_smiles("CN(C)C(=[N+](C)C)On1nnc2ccccc21.F[B-](F)(F)F") == standard_smiles(
        "CN(C)C(=[N+](C)C)On1nnc2ccccc21"
    ), "TBTU kept its tetrafluoroborate; a salt list curated for pharma does not know it"
    # The coupling, driven: an unrecognised neutral beside a counterion keeps only itself.
    assert standard_smiles("CC[NH3+].[Cl-].OO") == "CCN.OO", (
        "the chloride survived because a peroxide was in the same string"
    )
    assert standard_smiles("NC(N)=O.OO.O") == "NC(N)=O.OO", (
        "and the water went while the H2O2 stayed"
    )


#: Every acid in `_IONISABLE_NEUTRAL_ACIDS`, as written neutral and as its anion.
_IONISABLE_ACIDS = {
    "perchloric acid": ("OCl(=O)(=O)=O", "[O-]Cl(=O)(=O)=O"),
    "tetrafluoroboric acid": ("F[B-](F)(F)[FH+]", "F[B-](F)(F)F"),
    "sulfamic acid": ("NS(=O)(=O)O", "NS(=O)(=O)[O-]"),
    "thiocyanic acid": ("SC#N", "[S-]C#N"),
    "isothiocyanic acid": ("N=C=S", "[S-]C#N"),
    "carbonic acid": ("OC(=O)O", "OC(=O)[O-]"),
    "hypophosphorous acid": ("O[PH2]=O", "[O-][PH2]=O"),
    "boric acid": ("OB(O)O", "[O-]B(O)O"),
}

#: Partners with a site that could take the proton, free and protonated.
_BASES = {
    "primary amine": ("CCN", "CC[NH3+]"),
    "tertiary amine": ("CCN(CC)CC", "CC[NH+](CC)CC"),
    "pyridine": ("c1ccncc1", "c1cc[nH+]cc1"),
    "imidazole": ("c1c[nH]cn1", "c1c[nH]c[nH+]1"),
    "amidine": ("CC(=N)N", "CC(=[NH2+])N"),
    "guanidine": ("NC(=N)N", "NC(=[NH2+])N"),
    # no basic site at all, only the charge: the gate's fourth arm, and the only row that reaches it
    "quaternary ammonium": ("C[N+](C)(C)C", "C[N+](C)(C)C"),
}

#: Partners with no such site: beside one of the acids they make a mixture, not a salt.
_NOT_BASES = {
    "boronic acid": "OB(O)c1ccccc1",
    "phenol": "Oc1ccccc1",
    "carboxylic acid": "OC(=O)c1ccccc1",
    "amide": "CC(N)=O",
    "carbamate": "CCOC(N)=O",
    "sulfonamide": "CS(N)(=O)=O",
    "aniline": "Nc1ccccc1",
    "nitroarene": "O=[N+]([O-])c1ccccc1",
    "pyrrole": "c1cc[nH]c1",
    "ester": "CCOC(C)=O",
}


@pytest.mark.parametrize("base", sorted(_BASES))
@pytest.mark.parametrize("acid", sorted(_IONISABLE_ACIDS))
def test_a_salt_written_neutral_or_ionic_is_one_compound(
    acid: str, base: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A salt written neutral or ionic is one compound.

    Beside a partner that can take the proton, the neutral spelling is the salt; the table of bases
    is what makes both spellings reach the free base.
    """
    from chemclaw.core import chem

    neutral_acid, anion = _IONISABLE_ACIDS[acid]
    free, protonated = _BASES[base]
    neutral, ionic = f"{free}.{neutral_acid}", f"{protonated}.{anion}"
    assert compound_id(neutral) == compound_id(ionic) == compound_id(free)
    chem._standardized.cache_clear()
    monkeypatch.setattr(chem, "_IONISABLE_NEUTRAL_SPECTATORS", frozenset())
    try:
        assert compound_id(neutral) != compound_id(ionic), f"{acid} agreed without the table"
    finally:
        chem._standardized.cache_clear()


@pytest.mark.parametrize("partner", sorted(_NOT_BASES))
@pytest.mark.parametrize("acid", sorted(_IONISABLE_ACIDS))
def test_an_acid_beside_a_partner_that_cannot_take_its_proton_is_a_mixture(
    acid: str, partner: str
) -> None:
    """An acid beside a partner that cannot take its proton is a mixture, not a salt.

    E.g. a boronic acid with boric acid, or a compound with a carbonate buffer, keeps its own id.
    """
    neutral_acid, _ = _IONISABLE_ACIDS[acid]
    parent = _NOT_BASES[partner]
    assert compound_id(f"{parent}.{neutral_acid}") != compound_id(parent)


@pytest.mark.parametrize("acid", sorted(_IONISABLE_ACIDS))
def test_an_acid_registered_alone_is_untouched(acid: str) -> None:
    """With no organic fragment there is nothing to be the salt of: the acid is its own compound."""
    from rdkit.Chem.MolStandardize import rdMolStandardize

    neutral_acid, _ = _IONISABLE_ACIDS[acid]
    cleaned = Chem.MolToSmiles(rdMolStandardize.Cleanup(Chem.MolFromSmiles(neutral_acid)))
    assert standard_smiles(neutral_acid) == cleaned


def test_the_table_is_exactly_the_acids_the_catalogue_omits() -> None:
    """Every acid on it is one RDKit's catalogue does not strip, so no row is redundant.

    The day upstream adds one, this reds and the row is deleted rather than kept as a second
    answer to a question the catalogue already answers — which is how a table and a list drift.
    """
    from rdkit.Chem.MolStandardize import rdMolStandardize

    from chemclaw.core import chem

    remover = rdMolStandardize.FragmentRemover()
    for acid, spellings in chem._IONISABLE_NEUTRAL_ACIDS.items():
        for spelling in spellings:
            pair = rdMolStandardize.Cleanup(Chem.MolFromSmiles(f"CCN.{spelling}"))
            left = Chem.GetMolFrags(remover.remove(pair), asMols=True)
            assert len(left) == 2, f"RDKit's catalogue now strips {acid} ({spelling})"
    assert len(chem._IONISABLE_NEUTRAL_ACIDS) == 7


def test_the_standardization_version_is_pinned_to_the_behaviour_it_names() -> None:
    """The standardization version is pinned to the behaviour it names.

    A bump retires rows indexed under an older notion of sameness, so the constant is pinned to a
    literal beside a table of what this build does: changing the pipeline without the version reds
    the table, the version without the pipeline reds the literal. The labeller stamp is pinned in
    `tests/test_label_enrichment.py`. A bump permanently doubles the fingerprint tables
    (`durable/retention.py`); recovery is `make rekey-compounds` (`tests/test_compound_rekey.py`).
    """
    assert STANDARDIZATION_VERSION == "std12", (
        "the standardization version changed. That is a decision with a cost — see this test's "
        "docstring — so update the literal and the table below together, and say in the commit "
        "message which rows moved"
    )
    assert molecule_definition().endswith(STANDARDIZATION_VERSION), molecule_definition()
    assert reaction_definition().endswith(STANDARDIZATION_VERSION), reaction_definition()
    for written, standardized in _STANDARDIZATION_AT_THIS_VERSION:
        assert standard_smiles(written) == standardized, (
            f"{written!r} standardizes to {standard_smiles(written)!r} at "
            f"{STANDARDIZATION_VERSION}, not {standardized!r}. If that is intended, the notion of "
            "sameness changed and every stored fingerprint row was derived under the old one, so "
            "the version has to move with it"
        )


# --- one id, one body -------------------------------------------------------------------------
#
# The note body answers "same compound?" like the id, so spellings sharing an id share a body.


def test_one_compound_id_means_one_note_body() -> None:
    """One compound id means one note body.

    `compound_dependencies` relies on a re-proposed compound note rendering byte-identically; a body
    keyed on spelling would rewrite the structure on every ingest.
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
    """Every shipped reagent note carries its name.

    The note is keyed on the standardized SMILES and `reagents` on the canonical one, which differ
    for several reagents (e.g. DMSO); asserted over the whole table.
    """
    anonymous = [n for n in sorted(_TABLE) if "- name: " not in compound_note(_shipped(n)).body]
    assert anonymous == []


def test_a_reagent_note_lists_the_spellings_of_its_own_compound_only() -> None:
    """A reagent note lists the spellings of its own compound only.

    Folding synonyms onto the standardized key is safe only because no two shipped reagents share
    one.
    """
    assert compound_note(_shipped("DMSO")).body.count("also written: dimethylsulfoxide, dmso") == 1
    assert "pd(oac)2" not in compound_note(_shipped("AcOH")).body
    by_compound: dict[str, set[str | None]] = {}
    for name in sorted(_TABLE):
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
    """Reagents the hazard rules were widened for can be named.

    Whether the safety screen sees a protonated or neutral spelling is the source's choice, and the
    reagent table turns a name into either; without an entry the name resolves to nothing. The
    screening half is in `Chemclaw3-mcp:servers/safety/tests/test_pairs.py`
    (`test_the_hydrazine_arm_fires_on_every_form_a_catalogue_sells`).
    """
    resolved = resolve_compound_name(spelling)
    assert resolved is not None, f"{spelling!r} resolves to nothing"
    assert resolved.name == expected


def test_oversized_smiles_is_refused_not_crashed() -> None:
    """A molecule past the atom/length cap raises instead of segfaulting the process.

    RDKit's canonical-SMILES writer and tautomer canonicalizer recurse without bound and SIGSEGV on
    a large linear molecule, killing the worker. `require_molecule` is the shared gate, so the bound
    lives there; the lenient helpers pass through.
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


# --- a fixed point ------------------------------------------------------------------------------
#
# `compound_note`, `compound_id` and `compound_id_of_standard` hash the standard form after two, one
# and zero passes, so they agree only if `standardize` is a fixed point on its own output.


def _not_a_fixed_point(written: str) -> str | None:
    """Why `written` breaks the fixed point, or None — every way the three ids can disagree."""
    standard = standard_smiles(written)
    if Chem.MolFromSmiles(standard) is None:
        return f"{written!r} standardizes to {standard!r}, which does not parse"
    again = standard_smiles(standard)
    if again != standard:
        return f"{written!r} -> {standard!r} -> {again!r}"
    if compound_note(written).id != compound_id(written):
        return f"{written!r}: compound_note id is not compound_id"
    return None


@pytest.mark.parametrize(
    "written",
    [
        _DTBPF_PD_G3,
        _DPPF_PD_G3,
        _JOSIPHOS_PD_G3,
        "C1(C=CC=C1)[Fe]C1C=CC=C1",
        "Cl[Zr](Cl)(C1C=CC=C1)C1C=CC=C1",
        "CC1=C(C)C(C)([Rh](Cl)Cl)C(C)=C1C",
        "[CH-]1C=CC=C1",
        "[cH-]1cccc1.[Na+]",
        "CC(C)(C)P(C(C)(C)C)[C-]1C=CC=C1",
        "c1ccc(P(c2ccccc2)[c-]2cccc2)cc1",
        *(written for written, _ in _STANDARDIZATION_AT_THIS_VERSION),
    ],
)
def test_standardize_is_a_fixed_point_on_its_own_output(written: str) -> None:
    """Standardizing a standard form returns it, so `compound_id(raw)` is `compound_note(raw).id`.

    The first rows are ferrocenyl precatalysts (Cp aromaticity, a `Reionize` flip between passes)
    and per-token Cp⁻ spellings from `rxnfp`; the rest cover every decision the version table pins.
    """
    assert _not_a_fixed_point(written) is None, _not_a_fixed_point(written)


def test_standardize_is_a_fixed_point_on_every_molecule_of_the_mock_seed() -> None:
    """The same property over every structure `Chemclaw3_mock` seeds, whole and per component.

    Per component because `rxnfp` standardizes one `.`-token at a time. Read from the sibling's
    CSVs, so new seed molecules are covered; without the checkout only breadth is lost.
    """
    checkout, reason = sibling_root("CHEMCLAW_MOCK_REPO", "Chemclaw3_mock")
    if checkout is None:
        pytest.skip(f"{SIBLING_SKIP} {reason}; the mock seed's molecules are NOT checked")
    tables = sorted((checkout / "app" / "eln" / "real_data").glob("*.csv"))
    assert tables, f"{checkout} holds no app/eln/real_data/*.csv to read the seed from"
    written: set[str] = set()
    for table in tables:
        with table.open(newline="") as handle:
            for row in csv.DictReader(handle):
                for column, cell in row.items():
                    if column and "smiles" in column and cell:
                        written.add(cell)
                        written.update(cell.split("."))
    parsed = sorted(s for s in written if Chem.MolFromSmiles(s) is not None)
    assert any("[Fe]" in s for s in parsed), "the seed no longer carries a ferrocene to test"
    failures = [why for s in parsed if (why := _not_a_fixed_point(s))]
    assert not failures, "\n".join(failures)
