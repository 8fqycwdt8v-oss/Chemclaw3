"""Behavioral tests for the mcp-rxnfp reaction capability (plan step 3.4).

Proves DRFP is deterministic, invalid reactions fail clearly, and Tanimoto ranking over
reactions returns most-similar-first — without a database. The reaction path reuses the
generic fingerprint store, so ranking correctness is already covered by test_molfp; here
we prove the DRFP-specific fingerprinting and that it plugs into the shared store.
"""

import asyncio

import pytest
from drfp import DrfpEncoder

from chemclaw.core.chem import STANDARDIZATION_VERSION, standard_smiles
from chemclaw.core.config import settings
from chemclaw.ingest.eln.ord import Component, OrdReaction, Role
from chemclaw.kg.note import note_id_for_reaction
from chemclaw.science.fingerprints.rxnfp.fingerprint import (
    drfp_bitstring,
    reaction_definition,
)
from chemclaw.science.fingerprints.rxnfp.search import find_similar_reactions, record_for_reaction
from chemclaw.science.fingerprints.store import (
    FingerprintError,
    FingerprintRecord,
    InMemoryFingerprintStore,
    tanimoto,
)

# Three esterifications (similar) and one unrelated halogenation.
_ESTER_ETHYL = "CCO.CC(=O)O>>CCOC(C)=O"
_ESTER_PROPYL = "CCCO.CC(=O)O>>CCCOC(C)=O"
_ESTER_BUTYL = "CCCCO.CC(=O)O>>CCCCOC(C)=O"
_HALOGENATION = "c1ccccc1.BrBr>>Brc1ccccc1"


def test_drfp_is_deterministic_and_config_sized() -> None:
    """The same reaction yields the same fingerprint, sized to the configured width."""
    a = drfp_bitstring(_ESTER_ETHYL)
    assert a == drfp_bitstring(_ESTER_ETHYL)
    assert len(a) == settings.drfp_bits
    assert "1" in a  # a real reaction sets at least one bit


def test_invalid_reaction_raises() -> None:
    """A non-reaction (no `>>`) is a clear FingerprintError, not a DRFP-internal crash."""
    with pytest.raises(FingerprintError, match="unparseable reaction"):
        drfp_bitstring("CCO")


def test_empty_fingerprint_raises() -> None:
    """A degenerate reaction with no features is rejected, not stored as all-zeros."""
    with pytest.raises(FingerprintError, match="empty fingerprint"):
        drfp_bitstring(">>>")


def test_find_similar_reactions_ranks_by_tanimoto() -> None:
    """A reaction query returns the most similar reactions first, filtering the unrelated."""

    async def _run() -> None:
        store = InMemoryFingerprintStore()
        for rid, rxn in [
            ("ethyl", _ESTER_ETHYL),
            ("propyl", _ESTER_PROPYL),
            ("butyl", _ESTER_BUTYL),
            ("halogenation", _HALOGENATION),
        ]:
            await store.add(record_for_reaction(rid, rxn))

        hits = (await find_similar_reactions(store, _ESTER_ETHYL, threshold=0.1)).hits
        assert hits[0].id == "ethyl"  # exact match ranks first
        assert hits[0].similarity == pytest.approx(1.0)
        assert "halogenation" not in {h.id for h in hits}  # unrelated reaction excluded
        assert all(hits[i].similarity >= hits[i + 1].similarity for i in range(len(hits) - 1))
        assert hits[0].label == _ESTER_ETHYL  # the label carries the reaction SMILES

    asyncio.run(_run())


# --- The agent slot has to change the bits, not just the notation --------------------------------
#
# The measurements below are the point of this block. A previous change moved solvent and catalyst
# into the reaction SMILES' agent slot and four places recorded that the solvent no longer
# dominated similarity — but `DrfpEncoder.internal_encode` opens with `sides[0] += "." + sides[1]`,
# folding the agent slot straight back onto the reactants, so the three-part and two-part forms
# encode to byte-identical bits. Every one of those claims was false, and nothing here measured the
# thing they claimed. So these tests assert *numbers*, taken from the pinned drfp: a fingerprint
# change that produces the same bits is the exact failure being repaired.


def _suzuki(solvent: str) -> OrdReaction:
    """One Suzuki coupling, varying only the solvent — the pair the claim was always about."""
    return OrdReaction(
        reaction_id=f"rxn-{solvent}",
        inputs=[
            Component(smiles="Brc1ccc(C)cc1", role=Role.REACTANT),
            Component(smiles="OB(O)c1ccccc1", role=Role.REACTANT),
            Component(smiles="[K+].[K+].[O-]C([O-])=O", role=Role.REAGENT),
            Component(smiles="CC(=O)O[Pd]OC(C)=O", role=Role.CATALYST),
            Component(smiles=solvent, role=Role.SOLVENT),
        ],
        outcomes=[Component(smiles="Cc1ccc(-c2ccccc2)cc1", role=Role.PRODUCT)],
        provenance="eln:chemist-a",
    )


_THF, _METHF = _suzuki("C1CCOC1"), _suzuki("CC1CCCO1")


def test_the_agent_slot_alone_changes_no_bits_which_is_why_species_are_excluded() -> None:
    """The defect, pinned so no future change can re-claim the notation as a fix.

    DRFP folds `>agents>` onto the reactants before it shingles anything, so writing a solvent in
    the middle slot and writing it on the left are the same input. This is a property of the pinned
    encoder rather than of our code, which is exactly why it is worth asserting: it is the fact
    that makes `transformation_smiles` necessary, and a reader looking at the three-part record
    form has no way to guess it.
    """
    folded_back = (
        "Brc1ccc(C)cc1.OB(O)c1ccccc1.[K+].[K+].[O-]C([O-])=O.CC(=O)O[Pd]OC(C)=O.C1CCOC1"
        ">>Cc1ccc(-c2ccccc2)cc1"
    )
    assert drfp_bitstring(_THF.reaction_smiles()) == drfp_bitstring(folded_back)


def test_excluding_the_agents_actually_moves_the_bits() -> None:
    """The fix has to be visible in the fingerprint, not only in the string.

    Held against the *standardized* three-part form rather than against `reaction_smiles`, so the
    exclusion is the only difference under test. Compared with the raw record form this assertion
    survives removing the exclusion entirely — standardization alone moves the bits, and the test
    would then pass while measuring the wrong half of the change. That is the same shape of
    mistake as the defect itself: a claim about the encoding checked against something that
    happens to differ for another reason.
    """
    excluded = _THF.transformation_smiles()
    reactants, products = excluded.split(">>")
    agents = ".".join(
        standard_smiles(c.smiles) for c in _THF.inputs if c.role in {Role.SOLVENT, Role.CATALYST}
    )
    assert drfp_bitstring(f"{reactants}>{agents}>{products}") != drfp_bitstring(excluded)


def test_two_solvents_of_one_coupling_are_the_same_transformation() -> None:
    """The behaviour the whole change is for, as a number.

    Indexed as recorded, the THF and 2-MeTHF runs of one coupling scored 0.82 against each other —
    the solvent, present only on the left, survives DRFP's symmetric difference whole and spends a
    large constant block of bits on the variable being optimized. Indexed as transformations they
    are identical, which is the honest answer: they are the same chemistry run two ways, and the
    solvent is recorded beside the note rather than inside the structure.

    `as_recorded`'s pin moved from 0.8194 to 0.7937 under REV-1 (`drfp_bitstring` now standardizes
    every `.`-separated species, agent slot included, so the two calls under test are symmetric
    with `test_the_agent_slot_alone_changes_no_bits...`'s hand-folded fixture — see
    `_standardize_species`). The shift is `Cleanup`'s metal disconnection reaching this fixture's
    agents for the first time: `CC(=O)O[Pd]OC(C)=O` becomes two acetate anions plus bare Pd2+,
    identically on both sides, so the qualitative claim (`as_indexed > as_recorded`, near-1.0 once
    agents are excluded) is untouched.
    """
    as_recorded = tanimoto(
        drfp_bitstring(_THF.reaction_smiles()), drfp_bitstring(_METHF.reaction_smiles())
    )
    as_indexed = tanimoto(
        drfp_bitstring(_THF.transformation_smiles()),
        drfp_bitstring(_METHF.transformation_smiles()),
    )
    assert as_recorded == pytest.approx(0.7937, abs=1e-3)
    assert as_indexed == pytest.approx(1.0)
    assert as_indexed > as_recorded


def test_a_reagent_still_belongs_to_the_transformation() -> None:
    """The other half of the rule: a base is consumed stoichiometrically and stays on the left.

    Without this, "exclude what is not the transformation" quietly becomes "exclude everything
    that is not a named reactant", which would erase the base and ligand screens that process
    development actually runs.
    """
    without_base = _THF.model_copy(
        update={"inputs": [c for c in _THF.inputs if c.role is not Role.REAGENT]}
    )
    assert drfp_bitstring(_THF.transformation_smiles()) != drfp_bitstring(
        without_base.transformation_smiles()
    )


def test_the_indexed_string_is_standardized() -> None:
    """`STANDARDIZATION_VERSION` is in the definition, so the rows have to actually be standardized.

    They were not: `reaction_smiles` built the string from raw `c.smiles`, so the standardization
    half of the definition bump was bits-neutral for every reaction row while the token claimed
    otherwise. Asserted through a spelling RDKit re-canonicalizes, so it fails if the call is
    dropped rather than merely if the pipeline changes.
    """
    assert _THF.transformation_smiles().startswith("Cc1ccc(Br)cc1")  # from `Brc1ccc(C)cc1`
    assert STANDARDIZATION_VERSION in reaction_definition()


def test_the_definition_retires_rows_built_under_the_old_encoding() -> None:
    """Old rows must fall out of similarity search rather than be ranked against new ones.

    The store refuses to rank across definitions, so the token is the whole retirement mechanism —
    and a token that did not move would leave rows encoding a solvent-dominated fingerprint being
    compared against solvent-neutral ones and reporting the difference as chemistry.
    """
    definition = reaction_definition()
    assert "agents-excluded" in definition
    assert record_for_reaction("r", _THF.transformation_smiles()).definition == definition


# --- REV-1: a query is standardized the same way the index is ------------------------------------


def test_a_charged_species_query_matches_the_row_indexed_from_its_neutral_form() -> None:
    """A query spelling one reagent as its charged form still finds the standardized row.

    The index is built from `transformation_smiles()`, which runs every species through
    `standard_smiles` before fingerprinting (acetic acid, not the acetate anion). Before REV-1's
    fix a query spelled any other way scored against a form it could never equal — measured, not
    argued: acetate and acetic acid are different molecules to a DRFP that has not standardized
    them, so a match here can only come from the query being standardized the same way.
    """

    async def _run() -> None:
        store = InMemoryFingerprintStore()
        indexed = OrdReaction(
            reaction_id="ester",
            inputs=[
                Component(smiles="CCO", role=Role.REACTANT),
                Component(smiles="CC(=O)O", role=Role.REACTANT),
            ],
            outcomes=[Component(smiles="CCOC(C)=O", role=Role.PRODUCT)],
            provenance="eln:chemist-a",
        )
        await store.add(record_for_reaction("ester", indexed.transformation_smiles()))

        # The query spells the acid as its conjugate base (acetate), not the neutral form the
        # index was built from.
        charged_query = "CCO.CC(=O)[O-]>>CCOC(C)=O"
        hits = (await find_similar_reactions(store, charged_query, threshold=0.99)).hits
        assert hits and hits[0].id == "ester"
        assert hits[0].similarity == pytest.approx(1.0)

    asyncio.run(_run())


def test_a_tautomer_query_matches_the_row_indexed_from_its_canonical_tautomer() -> None:
    """A query spelling one reagent as another tautomer still finds the standardized row.

    Acetylacetone's enol and keto forms are different SMILES for the same substance;
    `standard_smiles` canonicalizes to one, and a query in the other tautomer must land on the
    same row.
    """

    async def _run() -> None:
        store = InMemoryFingerprintStore()
        indexed = OrdReaction(
            reaction_id="acac-amine",
            inputs=[
                Component(smiles="CC(=O)CC(C)=O", role=Role.REACTANT),  # keto
                Component(smiles="CCN", role=Role.REACTANT),
            ],
            outcomes=[Component(smiles="CCNC(C)=CC(C)=O", role=Role.PRODUCT)],
            provenance="eln:chemist-a",
        )
        await store.add(record_for_reaction("acac-amine", indexed.transformation_smiles()))

        enol_query = "CC(O)=CC(C)=O.CCN>>CCNC(C)=CC(C)=O"
        hits = (await find_similar_reactions(store, enol_query, threshold=0.99)).hits
        assert hits and hits[0].id == "acac-amine"
        assert hits[0].similarity == pytest.approx(1.0)

    asyncio.run(_run())


def test_an_already_standardized_query_is_bits_neutral() -> None:
    """REV-1's fix must be a no-op wherever standardization was already a no-op.

    `_ESTER_ETHYL` is already every species' `standard_smiles` form (plain organic, neutral,
    one tautomer), so folding it through the new per-species pass must reproduce exactly the
    bits `DrfpEncoder` computes with no preprocessing at all — proving the fix changes what a
    query means only where standardization does real work, per the backlog row's own claim.
    """
    direct = DrfpEncoder.encode(_ESTER_ETHYL, n_folded_length=settings.drfp_bits)[0]
    direct_bits = "".join("1" if value else "0" for value in direct)
    assert drfp_bitstring(_ESTER_ETHYL) == direct_bits


# --- An empty index must not answer "we have never run this" -------------------------------------


def test_an_empty_reaction_index_reports_that_the_search_was_not_run() -> None:
    """The live-run defect, on the exact tool that produced it (finding 6 of the grounded run).

    `similar_reactions` returning `{"result": []}` over a never-backfilled table was read as "we
    have no precedent for this transformation". The empty index must say so itself.
    """

    async def _run() -> None:
        search = await find_similar_reactions(InMemoryFingerprintStore(), _ESTER_ETHYL)
        assert search.hits == []
        assert search.index_empty is True
        payload = search.model_dump()  # what MCP ships to the model
        assert "SEARCH NOT RUN" in payload["verdict"]
        assert "NOT evidence" in payload["verdict"]

    asyncio.run(_run())


def test_a_populated_reaction_index_with_no_match_is_a_genuine_negative() -> None:
    """An indexed corpus that simply holds nothing similar reads as a real answer, not a gap."""

    async def _run() -> None:
        store = InMemoryFingerprintStore()
        await store.add(record_for_reaction("halogenation", _HALOGENATION))
        search = await find_similar_reactions(store, _ESTER_ETHYL, threshold=0.9)
        assert search.hits == []
        assert search.index_empty is False
        assert "genuine negative" in search.verdict
        assert "SEARCH NOT RUN" not in search.verdict

    asyncio.run(_run())


def test_a_reaction_hit_is_unaffected() -> None:
    """Regression guard: a real precedent still comes back, with the index not flagged empty."""

    async def _run() -> None:
        store = InMemoryFingerprintStore()
        await store.add(record_for_reaction("ethyl", _ESTER_ETHYL))
        search = await find_similar_reactions(store, _ESTER_ETHYL, threshold=0.1)
        assert [h.id for h in search.hits] == ["ethyl"]
        assert search.index_empty is False
        assert search.verdict.startswith("1 indexed reaction(s) matched")

    asyncio.run(_run())


# --- one entry id, two ELNs ----------------------------------------------------------------------


def _sited(reaction_id: str, source: str, reaction_smiles: str) -> FingerprintRecord:
    """One site's fingerprint of an entry id both sites happen to use."""
    return record_for_reaction(reaction_id, reaction_smiles).model_copy(update={"source": source})


def test_two_sources_sharing_an_entry_id_keep_two_fingerprints() -> None:
    """`EXP-1001` at two sites is two experiments, and the index key has to be able to say so.

    Keyed on the bare id, the second ingest overwrote the first and the first site's chemistry
    stopped being findable at all — worse than the transcription tier's version of this defect
    (D-2026-08-26), because there the losing row survived and only the citation was ambiguous.
    The in-memory backend is asserted here for the same reason it is asserted anywhere: it is the
    reference the Postgres backend is required to match, and `tests/test_rxnfp_postgres.py` runs
    the identical scenario in SQL.
    """

    async def _run() -> None:
        store = InMemoryFingerprintStore()
        await store.add(_sited("EXP-1001", "eln-a", _ESTER_ETHYL))
        await store.add(_sited("EXP-1001", "eln-b", _HALOGENATION))

        assert len(await store.all_records()) == 2, "one site's chemistry was overwritten"
        for smiles, source in ((_ESTER_ETHYL, "eln-a"), (_HALOGENATION, "eln-b")):
            hits = (await find_similar_reactions(store, smiles, threshold=0.99)).hits
            assert [(h.id, h.source) for h in hits] == [("EXP-1001", source)]

    asyncio.run(_run())


def test_a_hit_carries_the_source_its_citation_needs() -> None:
    """A search knows which site it matched; a bare `reaction-<id>` citation cannot say it.

    `note_id_for_reaction` is the one definition of that spelling, so the assertion is that the
    two hits spell two different citations — not that either equals a literal.
    """

    async def _run() -> None:
        store = InMemoryFingerprintStore()
        await store.add(_sited("EXP-1001", "eln-a", _ESTER_ETHYL))
        await store.add(_sited("EXP-1001", "eln-b", _HALOGENATION))

        cited = {
            note_id_for_reaction(hit.id, hit.source)
            for smiles in (_ESTER_ETHYL, _HALOGENATION)
            for hit in (await find_similar_reactions(store, smiles, threshold=0.99)).hits
        }
        assert len(cited) == 2, f"two runs, one citation: {cited}"
        assert all(citation.startswith("reaction-") for citation in cited)

    asyncio.run(_run())


def test_a_single_source_deployment_is_unchanged() -> None:
    """One enabled ELN has nothing to disambiguate, and pays nothing for the key change.

    One row per entry id, and the citation is the bare `reaction-<id>` every merged note already
    carries — the property that makes this migration safe to apply to a deployment that will never
    enable a second source.
    """

    async def _run() -> None:
        store = InMemoryFingerprintStore()
        await store.add(_sited("EXP-1001", "eln-a", _ESTER_ETHYL))
        await store.add(_sited("EXP-1001", "eln-a", _ESTER_PROPYL))  # the entry, amended

        records = await store.all_records()
        assert len(records) == 1
        assert records[0].label == _ESTER_PROPYL
        assert note_id_for_reaction("EXP-1001") == "reaction-EXP-1001"

    asyncio.run(_run())


def test_a_sourced_write_supersedes_the_row_migration_063_could_not_name() -> None:
    """A row stored before the key had a source half is replaced, never duplicated.

    `063` backfills every row a single-claimant `reaction_labels` row can name; what it leaves
    under the empty source would otherwise sit beside its own replacement with identical bits and
    one label, so a similarity search would report two precedents where a chemist has one run.
    """

    async def _run() -> None:
        store = InMemoryFingerprintStore()
        await store.add(record_for_reaction("EXP-1001", _ESTER_ETHYL))  # pre-063 row
        await store.add(_sited("EXP-1001", "eln-a", _ESTER_ETHYL))

        hits = (await find_similar_reactions(store, _ESTER_ETHYL, threshold=0.99)).hits
        assert [(h.id, h.source) for h in hits] == [("EXP-1001", "eln-a")]

    asyncio.run(_run())


def test_a_source_name_that_cannot_be_split_back_out_is_refused() -> None:
    """The citation separator is the one character a source name may not contain.

    A source called `eln.a` would spell `reaction-eln.a.EXP-1001`, which splits on its first
    separator into a source that is not the one it came from — an id that resolves, to the wrong
    run. Refused where the citation is built, so the failure names the source rather than
    surfacing as a missing record much later.
    """
    with pytest.raises(ValueError, match="separates"):
        note_id_for_reaction("EXP-1001", "eln.a")
