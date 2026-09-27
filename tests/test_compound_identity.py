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
    _is_organic,
    canonical_smiles,
    compound_id,
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


def test_an_amine_salt_drawn_as_an_ion_pair_is_its_free_base() -> None:
    """The half of the rule above that its own spellings could not reach.

    **Both assertions in `test_a_salt_and_its_free_base_are_one_compound` dodge the cationic case,
    and it broke without either of them noticing.** `CCN.Cl` is the *neutral* spelling, and
    `CC(=O)[O-].[Na+]` is an **anion**, which is the side `_neutralization_is_protonation` was
    written about. The other side is a protonated amine, drawn as an ion pair, which is how a
    supplier catalogue and an ELN both write a hydrochloride.

    **Why `CCN.Cl` cannot see it, instrumented rather than reasoned about**: this docstring used to
    say `Uncharger` "is a no-op and nothing asks whether the neutralisation added or removed a
    proton", and the second half is false — the guard is called unconditionally, on every path that
    reaches it. Driven: `Cleanup` leaves `CCN.Cl` neutral, `organic == 1` so `FragmentParent` *does*
    run and hands the guard `before = CCN`, `Uncharger` is indeed a no-op, and the guard is called
    once and returns True because the hydrogen count is 7 either side. The conclusion — that this
    spelling exercises neither arm of the charge test — is right; the mechanism is that the guard
    answers trivially, not that it is skipped. A reader who believed the old sentence would look for
    a branch that does not exist.

    That guard asked the hydrogen count of a cation, where neutralisation *removes* a proton, so it
    refused — and every one of these split into two `compound_id`s and two `compound_note`s, took a
    cache miss on work D-011 promises never to repeat, and ranked against itself as merely similar.
    The module docstring names this exact case as one that must work ("that claim holds for an amine
    hydrochloride").

    The list is drug salts rather than one probe because the failure was uniform across every amine
    salt in the shipped corpus and a single case reads as a special one. The nicotine-bitartrate
    test also missed it: that salt has *two* organic fragments, so there is no `FragmentParent`, the
    proton moves N->O, the net H count is unchanged, and the guard passes.

    **"Uniform across the class" was the wider claim and it was false; `std10` is what made it
    true.** `standardize` only reaches `Uncharger` when some fragment is `_is_organic`, and that
    test used to require a carbon bonded to hydrogen or to another carbon — which guanidinium's
    carbon, with three nitrogen neighbours and nothing else, does not have. Measured then: guanidine
    hydrochloride had `organic == 0`, returned before both the strip and the neutralisation, and did
    not collapse; metformin and acetamidine were covered only because their substituents put a C–C
    bond elsewhere in the fragment, which is an accident of substitution rather than the class being
    covered. `D-2026-09-22-a-version-bump-costs-the-same-whenever-it-is-taken` widened
    `_is_organic` — a carbon with three or more heavy neighbours, two of them nitrogen — and
    bumped the version, so the bare guanidinium below is the class boundary asserted rather than
    named as a limit.
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
    """The other direction, so the fix above cannot be a widening.

    `_neutralization_is_protonation` exists because sodium triacetoxyborohydride's charge sits on
    boron, which has no room for a fourth substituent — the only route to neutral is to *remove* the
    hydride, and letting that happen made a reducing agent share one `compound_id` with
    triacetoxyborane, a Lewis acid that reduces nothing. Exempting cations must not exempt that:
    it is an **anion**, so it still reaches the hydrogen-count test.
    """
    assert "[BH-]" in standard_smiles("CC(=O)O[BH-](OC(C)=O)OC(C)=O.[Na+]"), (
        "sodium triacetoxyborohydride was neutralised to triacetoxyborane, which reduces nothing"
    )
    assert standard_smiles("[BH4-].[Na+]") == "[BH4-].[Na+]"
    # And the anionic conjugate bases the guard is *meant* to collapse still collapse.
    assert standard_smiles("CC(=O)[O-].[Na+]") == "CC(=O)O"
    assert standard_smiles("CC(C)(C)[O-].[K+]") == "CC(C)(C)O"


def test_a_hydride_salt_of_an_organic_cation_keeps_its_hydride_too() -> None:
    """The shape the cation exemption re-broke: an anion and a cation in one string.

    Every fixture above is a salt of a *bare metal* cation, so it has one organic fragment,
    `FragmentParent` runs, and `before` reaches the guard at charge −1 — which is why the whole set
    passed an exemption keyed on `Chem.GetFormalCharge`, a number over the *whole* string.
    `standardize` keeps every fragment once two of them are organic, so a hydride paired with an
    organic cation arrives here whole, and one net number then answers a question about each
    species in it. Measured against the exemption as it shipped:

    | written as | net | standardized to |
    | --- | --- | --- |
    | `[BH4-]` + 2 × Et3NH+ | +1 | `B` — borane, plus two triethylamines |
    | `BH(OAc)3-` + 2 × Et3NH+ | +1 | triacetoxyborane, which reduces nothing |
    | `BH(OAc)3-` + 1 × Et3NH+ | 0 | kept as written |

    So the reducing agent shared a `compound_id` with a Lewis acid again, through the arm added to
    stop amine salts fragmenting — and nothing in the tree drove it, because the net-0 row is the
    only one of the three any fixture reached. Both charge states are asserted, and the net-zero
    row is a pin rather than a live catch: against the shipped one-conjunct arm, `>= 0` for `> 0`
    also turned that row into triacetoxyborane and passed all 343 tests of the chem subset, and
    the fix closes it **structurally** rather than by this assertion — once the arm also requires
    that no fragment be anionic, `>= 0` is behaviourally null, measured across 42 salts, hydrides,
    zwitterions and ion-pair spellings with zero differing outputs. It is null for a reason worth
    writing down: a string that is net-zero with no anionic fragment is either wholly neutral or a
    zwitterion, and a zwitterion moves its proton from the cationic side to the anionic one, so
    the count does not fall. The row stays because it is the shape that was wrong and nothing else
    in the file holds it.

    **Two cations, not one, and `[BH4-]` needs both of them**: `[BH4-]` is inorganic by
    `_is_organic`, so `[BH4-].Et3NH+` has *one* organic fragment, `FragmentParent` runs, and the
    hydride is discarded as a counterion before this guard is ever called — measured, that string
    standardizes to plain triethylamine. That is the counterion strip D-2026-07-31 decided rather
    than anything about the neutralization, so the fixture uses the two-cation spelling, which is
    the smallest one that reaches the branch under test.
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
    """The reason the gate tests bonds and not "contains a carbon".

    Carbonate contains carbon, so "contains a carbon" would make `[O-]C([O-])=O` the organic parent
    of K2CO3 and throw the potassium away — the exact collapse above. Cyanide is the same case, and
    NaCN and KCN are two reagents. Both still pass at `std10`, where the test is C–H, C–C, or a
    three-coordinate carbon holding two nitrogens: carbonate's carbon has no nitrogen and cyanide's
    has one neighbour. `_THE_ORGANIC_LINE` is the whole boundary, driven.
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


def test_a_solvate_is_not_its_solvent() -> None:
    """The defect `D-2026-08-27-a-solvate-is-not-its-solvent` closes, stated as it was reported.

    An ethylamine/THF solvate has no counterion to discard, so the old gate let `FragmentParent`
    break the tie by molecular weight and the solvate became *THF*: one `compound_id`, one note and
    one fingerprint row shared with the neat solvent. Both fragments are asserted to survive,
    because "the ids differ" alone would also pass if the pipeline had started deleting the
    solvent instead of the solute.
    """
    solvate = standard_smiles("CCN.C1CCOC1")
    assert compound_id("CCN.C1CCOC1") != compound_id("C1CCOC1")
    assert compound_id("CCN.C1CCOC1") != compound_id("CCN")
    assert set(solvate.split(".")) == {standard_smiles("C1CCOC1"), standard_smiles("CCN")}


def test_the_tie_break_no_longer_depends_on_which_solvent_it_is() -> None:
    """Why "keep the larger fragment" is not merely wrong here but *unstable*.

    Under the old rule the surviving compound was a property of the pair rather than of the
    compound: the same solute standardized to itself beside a small solvent and to the *solvent*
    beside a bulky one, so swapping THF for toluene silently changed which substance the record was
    about. Both solvates must now name the same solute.

    **The solute is pyridine (79.1) and the masses are the whole point.** It sits between THF
    (72.1) and toluene (92.1), so under the old rule the first solvate keeps pyridine and the
    second keeps *toluene* — the instability this test is named for. A heavier solute cannot show
    it: cresol (108.1) outweighs both solvents, so `FragmentParent` keeps cresol either way and
    this test passed with the rule reverted, which is how it was written first.
    """
    in_thf = standard_smiles("c1ccncc1.C1CCOC1")
    in_toluene = standard_smiles("c1ccncc1.Cc1ccccc1")
    solute = standard_smiles("c1ccncc1")
    assert solute in in_thf.split("."), f"the solvent won: {in_thf}"
    assert solute in in_toluene.split("."), f"the solvent won: {in_toluene}"


def test_the_solvate_rule_changed_no_shipped_reagent() -> None:
    """An absence test: exactly three shipped reagents lose a fragment, and they are the salts.

    The solvate rule is a *narrowing* of D-2026-08-01's counterion rule and must not re-litigate
    it, so the claim to pin is which entries of the shipped table the pipeline shrinks at all.
    Measured over `core/reagents.py`'s 68 distinct structures, before and after: three, all of them
    the salts that rule deliberately collapses (LDA onto diisopropylamide, HATU and TBTU onto their
    uronium cations). A fourth name appearing here means a solvate or a co-crystal is being deleted
    again; one of these three disappearing means the counterion rule has been broken from the other
    side, which is the direction nobody watches.

    **What this does not do, because the docstring used to imply it did.** Mutation-tested: revert
    the solvate rule entirely and this test stays green, because none of the 68 shipped structures
    is a solvate — that is precisely the measurement it exists to record. It is a guard on the
    *reagent table*, not on the rule. The rule itself is guarded by
    `test_a_solvate_is_not_its_solvent`, and by the tie-break test, whose solute was chosen so
    that the old rule loses it.
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
    """Keeping a species whole must not make its identity depend on how it was spelled.

    `Uncharger` used to run only on the path that also stripped, which was harmless while a
    kept-whole species was always a metal salt nobody writes two ways. A solvate rule puts organic
    ion pairs on that path, and nicotine bitartrate is written both as an ion pair and as a neutral
    co-crystal — so leaving the two coupled would have split one substance into two notes, trading
    this defect for the one D-2026-07-31 exists to prevent.
    """
    assert compound_id("C[NH+]1CCC[C@H]1c1cccnc1.[O-]C(=O)[C@H](O)[C@@H](O)C(=O)O") == compound_id(
        "CN1CCC[C@H]1c1cccnc1.OC(=O)[C@H](O)[C@@H](O)C(=O)O"
    )


def test_a_bare_inorganic_anion_is_not_neutralized_into_another_reagent() -> None:
    """The guard on the neutralization, and it is not hypothetical — this was built and reverted.

    `rxnfp._standardize_species` standardizes a reaction one `.`-separated token at a time, so
    `standardize` meets `[OH-]` and `[BH4-]` *alone*, without the counterion that explains their
    charge. Letting `Uncharger` run on them returns water and **borane** — D-2026-08-01's NaOH and
    NaBH4 defect reappearing one ion at a time, on the live fingerprint path rather than in the
    reagent table where that ADR's own tests watch for it. Measured: it moved a pinned DRFP
    similarity in `tests/test_rxnfp.py` from 0.7937 to 0.7969, which is how it was caught.
    """
    assert standard_smiles("[OH-]") == "[OH-]"
    assert standard_smiles("[BH4-]") == "[BH4-]"
    assert standard_smiles("[O-]C([O-])=O") != standard_smiles("OC(O)=O")


def test_an_atom_map_is_not_part_of_a_compound_s_identity() -> None:
    """A map number is a reaction's bookkeeping written into a molecule's string.

    `[CH3:1][C:2](=[O:3])[OH:4]` is acetic acid. RXNMapper stamps those numbers onto every species
    of every corpus reaction, so the same substance arrived from the literature tier and the ELN
    tier as two `compound_id`s, two notes and two fingerprint rows carrying identical bits — the
    identity fragmentation D-2026-07-31 exists to prevent, for the one spelling that pipeline did
    not normalise. The numbering itself is the labeller's choice, so the third case is what a
    re-label of one corpus does to every row in it.
    """
    acetic = compound_id("CC(=O)O")
    assert compound_id("[CH3:1][C:2](=[O:3])[OH:4]") == acetic
    assert compound_id("[CH3:7][C:8](=[O:9])[OH:10]") == acetic
    assert standard_smiles("[CH3:1][C:2](=[O:3])[OH:4]") == standard_smiles("CC(=O)O")


def test_a_map_number_does_not_survive_into_a_note_body_or_a_fingerprint() -> None:
    """The two places the un-normalised string used to land, asserted separately.

    `compound_id` agreeing is not enough on its own: the body renders the standardized structure
    and the ECFP4 row is built from it, so a map number left in either would put a reaction's
    bookkeeping into the compound record and into the bits a search ranks on.
    """
    assert ":" not in standard_smiles("[CH3:1][C:2](=[O:3])[OH:4]")
    assert ecfp_bitstring("[CH3:1][C:2](=[O:3])[OH:4]") == ecfp_bitstring("CC(=O)O")


def test_a_calculation_key_is_still_the_structure_as_it_was_submitted() -> None:
    """The guard on the above: clearing maps belongs to "same compound", not to "same structure".

    `canonical_smiles` keys the calculation cache and the QM dedup id, where the rule is that the
    key is what the caller asked for. Nothing submits a mapped species for calculation, so this
    pins the *scope* of the change rather than a behaviour anyone relies on.
    """
    assert canonical_smiles("[CH3:1][C:2](=[O:3])[OH:4]") != canonical_smiles("CC(=O)O")


def _hydrogens(mol: Chem.Mol) -> int:
    """Every hydrogen in a molecule, whether implicit on a heavy atom or an atom of its own."""
    return sum(a.GetTotalNumHs() + (1 if a.GetAtomicNum() == 1 else 0) for a in mol.GetAtoms())


def test_a_hydride_reagent_is_not_neutralized_into_a_non_reducing_ester() -> None:
    """`Uncharger` neutralizes an anion by *adding* a proton — except when it cannot.

    Sodium triacetoxyborohydride's charge sits on boron, and boron has no room for another
    substituent, so the only way to neutralize it is to take the hydride away. Measured, the
    hydrogen count fell 10 → 9 and `CC(=O)O[BH-](OC(C)=O)OC(C)=O.[Na+]` standardized to
    `CC(=O)OB(OC(C)=O)OC(C)=O` — triacetoxyborane, which reduces nothing. The three existing
    guards all pass it: one organic fragment, a group-1 counterion, no metal–carbon bond. So a
    reductive amination and a Lewis-acid step shared a `compound_id`, a fingerprint row and a note,
    and that is D-2026-08-01's "the discarded fragment is the reactive centre" wearing the
    neutralization step instead of the strip.
    """
    borohydride = standard_smiles("CC(=O)O[BH-](OC(C)=O)OC(C)=O.[Na+]")
    assert borohydride != standard_smiles("CC(=O)OB(OC(C)=O)OC(C)=O")
    assert "[BH-]" in borohydride, borohydride
    assert compound_id("CC(=O)O[BH-](OC(C)=O)OC(C)=O.[Na+]") == compound_id(
        "CC(=O)O[BH-](OC(C)=O)OC(C)=O.[K+]"
    )


def test_neutralization_never_costs_the_species_a_hydrogen() -> None:
    """The general form, since the reagent above is one instance of it rather than the rule.

    "The counterion meets its conjugate acid" is a claim that neutralization *protonates*. Where it
    instead deprotonates, the output is a different substance with a different formula, and no
    element list or pKa table is needed to see it — the hydrogen count says so.

    Every fixture is a salt of a bare metal cation, and that is what makes the count readable end
    to end rather than a statement about one internal step: the discarded counterion carries no
    hydrogen of its own, so any hydrogen the pipeline loses came out of the compound. Written first
    with an amine hydrochloride in the list, it failed on `CCN.Cl` — 8 → 7, because the strip
    discards HCl whole, which is the pipeline working. The invariant is about the neutralization,
    and this is the fixture set over which the whole pipeline reports it faithfully.
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
    """The half of D-2026-09-09 that is an *accepted* consequence rather than a fixed defect.

    KOtBu and tert-butanol reach one `compound_id`, and so do NaOMe/MeOH, NaOEt/EtOH, LiHMDS/HMDS
    and LDA/diisopropylamine. That is the same rule as sodium acetate and sodium benzoate, which
    D-2026-07-31 decided and this suite has pinned since: the counterion is not part of the
    identity. Separating the alkoxides would take a pKa-shaped predicate — "an anion whose
    conjugate acid is weak enough that the salt is the reagent" — and a bespoke normalization is a
    bespoke notion of sameness, which is the thing `core/chem.py` opens by refusing.

    It is asserted rather than left implicit because it has a real cost this file should name: a
    base screen over NaOMe / NaOEt / KOtBu reads as three collapses onto three *solvents*, and the
    counterion that a chemist is varying is exactly what is discarded. Whoever revisits that reads
    this test first.
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
    # apiece. The cyanamides are here because counting nitrogens *without* the degree put them on
    # the organic side, where the two spellings split: `[Ca+2].[N-]=C=[N-]` neutralised to the
    # carbodiimide tautomer and `[Na+].[NH-]C#N` to the nitrile one, which the canonicalizer does
    # not merge — so calcium cyanamide shared an id with free HN=C=NH.
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
)


#: Where the organic/inorganic line falls, one row per species, `True` meaning organic.
#:
#: **The drive that set `std10` lives here rather than in a commit message.** The first version of
#: this change said "driven over twenty species" in four places and left nothing that reproduces
#: it; a review then drove 125 and found the moved class was 29 wide rather than six, and found
#: two species the prose had not considered.
#: `D-2026-08-01-the-count-lives-in-the-test-not-in-the-prose` is the standing rule and this is it
#: applied: the drive is the table, so the next person who
#: touches `_is_organic` learns what moves by running it.
#:
#: The rows below the divider are the ones that decided the *shape* of the predicate. Counting
#: nitrogens alone put the cyanamide family on the organic side; reading the carbon's degree as
#: well is what keeps it out, and each of those rows is a measurement that would flip if the
#: degree clause were dropped.
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
    """The narrowing, driven: without the degree, four rows above flip and two ids merge.

    `D-2026-09-22-a-version-bump-costs-the-same-whenever-it-is-taken` records why this matters.
    The nitrogen-only spelling made `[Ca+2].[N-]=C=[N-]` reach `Uncharger`, which produced the
    **carbodiimide** tautomer, while `[Na+].[NH-]C#N` produced the nitrile one — two spellings of
    one substance under two ids, with calcium cyanamide sharing free carbodiimide's.
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
    """RDKit's chooser and `_is_organic` answer "which fragment is the compound?" differently.

    **The behaviour table above could not see this, and that is the finding.** Every one of its
    salt rows exercises the keep-one-organic-fragment branch, and on every one of them
    `rdMolStandardize.FragmentParent` happens to agree — so the pipeline could be changed here
    without a single row moving. The disagreement only shows on a species where the chooser's
    metric wins: it counts atoms *including hydrogens* and defaults to `preferOrganic=False`, so
    `[NH4+]` (five atoms) beats formate (four), and `Uncharger` then turned ammonium formate into
    **ammonia**.

    Driven against RDKit rather than asserted, so this reds if upstream changes its mind — which
    would be the day to re-read the call site, not to delete this.
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
    """Urea hydrogen peroxide is a bench oxidant, and it used to be urea.

    `standardize` discarded every other fragment on the strength of the count alone, without
    asking what a discarded fragment *is* — so a neutral co-former went the same way as a
    bromide. Two ways a spectator earns discarding now, and between them they need no list of
    this repository's own: it carries a charge, or it is a solvent RDKit's curated list knows.

    **The charge half is what keeps the coupling reagents working**, and driving the list alone is
    how that was found: `FragmentRemover` is a *pharmaceutical salt* list, and measured it carries
    hexafluorophosphate but **not** tetrafluoroborate — so a version that asked only the list left
    TBTU and TSTU carrying their anions while HATU and PyBOP were fine. Charge reads all of them
    without naming any. (A first version of this docstring said the list knew neither, which was
    the half that had not been driven.)

    **And the question is asked of each spectator, not of the set.** `all(...)` over them coupled
    them: one unrecognised neutral preserved every other fragment too, so `CC[NH3+].[Cl-].OO` kept
    its chloride. Whether a bromide is a counterion cannot depend on what else is in the string.
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


def test_the_standardization_version_is_pinned_to_the_behaviour_it_names() -> None:
    """A bump is the only thing that retires a stale row, and nothing held the number to the rules.

    **Measured: `std8` -> `std7` passed all 344 tests of the chem subset**, and every derived string
    moved with it, so the rename was invisible. That is the whole failure mode of a version: the
    point of a bump is that rows indexed under an older notion of sameness fall *out* of similarity
    search rather than being ranked against corrected ones, so a behaviour change that forgets the
    bump silently serves a mixture, and a version change that means nothing retires a corpus for
    free. `test_standardization_is_recorded_in_the_fingerprint_definition` above asserts the
    constant is *in* each definition, which any consistent value satisfies, and
    `test_stereo_identity.py` asserts only that it is not two specific older strings.

    So the constant is pinned to a literal **beside** a table of what this build actually does.
    Changing the pipeline without the version reds the table; changing the version without the
    pipeline reds the literal; doing both together is the deliberate act, and it is two edits in
    one file that a reviewer sees as one diff. The third derived string, the labeller stamp
    `f"{remote}:{STANDARDIZATION_VERSION}:{VOCABULARY_VERSION}"`, is pinned where it is asserted,
    in `tests/test_label_enrichment.py`.

    **The table is what a bump has to be weighed against, and the cost is in `durable/retention.py`:
    a bump is a permanent doubling of `molecule_fingerprints` and `reaction_fingerprints`, because
    the runtime role holds no `DELETE` and superseded rows are never reclaimed.** The recovery is
    the runbook's — delete the corpus's `corpus_cursors` row and re-run the ELN sync. Read that
    before adding a row here with a new number.
    """
    assert STANDARDIZATION_VERSION == "std11", (
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
    anonymous = [n for n in sorted(_TABLE) if "- name: " not in compound_note(_shipped(n)).body]
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
