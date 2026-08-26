"""The six precedent questions, asked of a seeded corpus — against both index backends.

Each test names the question it answers, because that is the acceptance criterion: this subsystem
exists because none of the six could be answered at all, and a passing assertion on a facet is not
the same as a chemist's question being answerable.

Both backends run the same body, for the reason `tests/test_label_index.py` gives: the in-memory
one is the reference and the SQL one is where a predicate quietly means something else. The
Postgres half skips when no database is reachable — a green local run without `make up` has
executed half of this file.
"""

import asyncio
from collections.abc import Awaitable, Callable

from chemclaw.core.config import settings
from chemclaw.science.fingerprints.molfp.fingerprint import molecule_definition
from chemclaw.science.fingerprints.store import InMemoryFingerprintStore, PostgresFingerprintStore
from chemclaw.science.labels.molecules import CORPUS_MOLECULES_TABLE, CorpusMolecules
from chemclaw.science.labels.records import ReactionLabel, SpeciesLabel
from chemclaw.science.labels.search import (
    agent_frequency,
    conditions_for_similar_products,
    reactions_with_product_substructure,
    substrate_precedents,
    workup_precedents,
)
from chemclaw.science.labels.store import InMemoryLabelIndex, LabelIndex, PostgresLabelIndex
from chemclaw.science.labels.vocabulary import SpeciesRole
from tests.pg import migrated_db_or_skip

_VERSION = "rxnlabel@1:std5:roles1"

# Three Buchwald-Hartwig aminations of the same aryl bromide with different ligands, and one Suzuki
# that must never show up in a Buchwald answer.
_ANILINE = "c1ccc(NC2CCCCC2)cc1"
_BIPHENYL = "c1ccc(-c2ccccc2)cc1"
_XPHOS = "CC(C)c1cc(C(C)C)c(-c2ccccc2P(C2CCCCC2)C2CCCCC2)c(C(C)C)c1"
_TBU3P = "CC(C)(C)P(C(C)(C)C)C(C)(C)C"


def _buchwald(
    tag: str, reaction_id: str, ligand: str, *, yield_percent: float | None, workup: str | None
) -> ReactionLabel:
    """One labelled Buchwald-Hartwig amination, ligand varied."""
    return ReactionLabel(
        source=f"{tag}-corpus",
        reaction_id=reaction_id,
        record_smiles=f"Brc1ccccc1.NC1CCCCC1>{ligand}.CC(C)(C)[O-].CC#N>{_ANILINE}",
        citation=f"US{reaction_id}B2",
        yield_percent=yield_percent,
        temperature_c=100.0,
        workup_text=workup,
        named_reaction="Buchwald-Hartwig amination",
        rxno_id="RXNO:0000192",
        method="smirks",
        labeller_version=_VERSION,
        species=[
            SpeciesLabel(
                ordinal=0,
                smiles="Brc1ccccc1",
                role="reactant",
                derived_role=SpeciesRole.STARTING_MATERIAL,
            ),
            SpeciesLabel(
                ordinal=1,
                smiles="NC1CCCCC1",
                role="reactant",
                derived_role=SpeciesRole.STARTING_MATERIAL,
            ),
            SpeciesLabel(ordinal=2, smiles=ligand, role="reagent", derived_role=SpeciesRole.LIGAND),
            SpeciesLabel(
                ordinal=3, smiles="CC(C)(C)[O-]", role="reagent", derived_role=SpeciesRole.BASE
            ),
            SpeciesLabel(
                ordinal=4, smiles="CC#N", role="solvent", derived_role=SpeciesRole.SOLVENT
            ),
            SpeciesLabel(
                ordinal=5,
                smiles=_ANILINE,
                role="product",
                derived_role=SpeciesRole.PRODUCT,
                functional_groups=["secondary amine", "arene"],
            ),
        ],
    )


def _suzuki(tag: str) -> ReactionLabel:
    """A Suzuki that shares the aryl bromide — the control for every "Buchwald only" assertion."""
    return ReactionLabel(
        source=f"{tag}-corpus",
        reaction_id=f"{tag}-suzuki",
        record_smiles=f"Brc1ccccc1.OB(O)c1ccccc1>CCOCC>{_BIPHENYL}",
        citation="US7000000B2",
        yield_percent=91.0,
        named_reaction="Bromo Suzuki coupling",
        rxno_id="RXNO:0000140",
        method="smirks",
        labeller_version=_VERSION,
        species=[
            SpeciesLabel(
                ordinal=0,
                smiles="Brc1ccccc1",
                role="reactant",
                derived_role=SpeciesRole.STARTING_MATERIAL,
            ),
            SpeciesLabel(
                ordinal=1,
                smiles="OB(O)c1ccccc1",
                role="reactant",
                derived_role=SpeciesRole.STARTING_MATERIAL,
            ),
            SpeciesLabel(
                ordinal=2, smiles="CCOCC", role="solvent", derived_role=SpeciesRole.SOLVENT
            ),
            SpeciesLabel(
                ordinal=3,
                smiles=_BIPHENYL,
                role="product",
                derived_role=SpeciesRole.PRODUCT,
                functional_groups=["arene"],
            ),
        ],
    )


async def _seed(index: LabelIndex, tag: str) -> None:
    """Three Buchwalds, one Suzuki, and one unlabelled row so coverage has something to report."""
    rows = [
        _buchwald(
            tag,
            f"{tag}-b1",
            _XPHOS,
            yield_percent=88.0,
            workup="Diluted with water, extracted with EtOAc, dried over MgSO4.",
        ),
        _buchwald(tag, f"{tag}-b2", _XPHOS, yield_percent=72.0, workup=None),
        _buchwald(tag, f"{tag}-b3", _TBU3P, yield_percent=54.0, workup="Quenched with sat. NH4Cl."),
        _suzuki(tag),
    ]
    for row in rows:
        await index.record(row)
        await index.store_labels(row, _VERSION)
    # Unlabelled, so every coverage sentence in this file has to say PARTIAL rather than COMPLETE.
    await index.record(
        _buchwald(tag, f"{tag}-pending", _XPHOS, yield_percent=None, workup=None).model_copy(
            update={"labeller_version": None}
        )
    )


def _both_backends(body: Callable[[LabelIndex, str], Awaitable[None]]) -> None:
    """Run `body` against the in-memory backend and then Postgres, on disjoint source names."""

    async def _run() -> None:
        memory = InMemoryLabelIndex()
        await _seed(memory, "mem")
        await body(memory, "mem")

        await migrated_db_or_skip()
        durable = PostgresLabelIndex()
        await _seed(durable, "pg")
        await body(durable, "pg")

    asyncio.run(_run())


def test_q1_has_this_substrate_been_used_as_starting_material() -> None:
    """Question 1, and the role filter is the whole of it.

    The aryl bromide is a starting material in all four seeded reactions and a ligand in none, so
    asking for it as a ligand must return nothing — that is what makes the role a filter rather
    than decoration.
    """

    async def _body(index: LabelIndex, tag: str) -> None:
        found = await substrate_precedents(
            index, _VERSION, "Brc1ccccc1", role=SpeciesRole.STARTING_MATERIAL, limit=20
        )
        mine = [h for h in found.hits if h.source == f"{tag}-corpus"]
        assert len(mine) == 4
        assert all(h.citation.startswith("US") for h in mine)

        as_ligand = await substrate_precedents(
            index, _VERSION, "Brc1ccccc1", role=SpeciesRole.LIGAND, limit=20
        )
        assert [h for h in as_ligand.hits if h.source == f"{tag}-corpus"] == []

    _both_backends(_body)


def test_q3_which_ligands_were_used_for_buchwald_couplings() -> None:
    """Question 3 — and the answer is only possible because `ligand` is a derived role.

    The recorded vocabulary has five values and none of them is "ligand"; all three phosphines here
    were charged as `reagent`. Every count in this table comes from the derived column.
    """

    async def _body(index: LabelIndex, tag: str) -> None:
        report = await agent_frequency(
            index,
            _VERSION,
            named_reaction="Buchwald-Hartwig amination",
            roles=frozenset({SpeciesRole.LIGAND}),
            limit=50,
        )
        ligands = {a.smiles: a for a in report.agents if a.role is SpeciesRole.LIGAND}
        assert set(ligands) == {_XPHOS, _TBU3P}
        assert ligands[_XPHOS].count == 2
        assert ligands[_TBU3P].count == 1
        # The Suzuki's diethyl ether is a solvent in a different reaction and must not appear.
        assert "CCOCC" not in ligands
        # Median yield over the recorded values only, so it is the number a chemist can check.
        assert ligands[_XPHOS].median_yield_percent == 80.0
        assert "Popularity is not suitability" in report.verdict
        # The unlabelled row is in scope and uncounted, and the sentence has to say so.
        assert "PARTIAL" in report.coverage.verdict

    _both_backends(_body)


def test_q5_workhorse_conditions_for_a_product_bearing_a_functional_group() -> None:
    """Question 5: the same roll-up, narrowed by what the *product* carries.

    "secondary amine" is on the Buchwald products and not on the Suzuki's biphenyl, so the group
    filter must drop the Suzuki without the caller naming it.
    """

    async def _body(index: LabelIndex, tag: str) -> None:
        report = await agent_frequency(
            index, _VERSION, product_functional_group="secondary amine", limit=50
        )
        roles = {a.role for a in report.agents}
        # No role filter, so "conditions" means every role — which is what the question asks for.
        assert {SpeciesRole.LIGAND, SpeciesRole.BASE, SpeciesRole.SOLVENT} <= roles
        assert all(a.smiles != "CCOCC" for a in report.agents)

        arene = await agent_frequency(index, _VERSION, product_functional_group="arene", limit=50)
        assert any(a.smiles == "CCOCC" for a in arene.agents)

    _both_backends(_body)


def test_q6_how_to_work_up_a_reaction_with_this_reagent() -> None:
    """Question 6 — and a reaction that recorded no workup is not a workup precedent."""

    async def _body(index: LabelIndex, tag: str) -> None:
        found = await workup_precedents(index, _VERSION, _XPHOS, limit=20)
        mine = [h for h in found.hits if h.source == f"{tag}-corpus"]
        assert len(mine) == 1
        assert mine[0].workup_text is not None and "EtOAc" in mine[0].workup_text

    _both_backends(_body)


def test_a_precedent_carries_the_recipe_the_fingerprint_drops() -> None:
    """Why the record phase keeps the agents: a precedent must say what was in the flask."""

    async def _body(index: LabelIndex, tag: str) -> None:
        found = await substrate_precedents(index, _VERSION, _XPHOS, limit=20)
        mine = next(h for h in found.hits if h.source == f"{tag}-corpus")
        assert mine.agents["ligand"] == [_XPHOS]
        assert mine.agents["solvent"] == ["CC#N"]
        assert mine.agents["base"] == ["CC(C)(C)[O-]"]
        # Substrates and products are not "agents" — they are what the reaction is about.
        assert "starting-material" not in mine.agents

    _both_backends(_body)


def test_an_empty_answer_says_which_kind_of_empty_it_is() -> None:
    """The distinction the whole coverage layer exists for.

    "We have no precedent for this" and "nothing matching has been labelled yet" are opposite
    claims, and a bare empty list is both. A live run once made exactly this mistake on the
    fingerprint index and told a chemist the second as though it were the first.
    """

    async def _body(index: LabelIndex, tag: str) -> None:
        found = await substrate_precedents(index, _VERSION, "CCCCCCCCCCCCCCCC", limit=20)
        assert found.hits == []
        assert found.verdict.startswith("NO PRECEDENT FOUND IN THE LABELLED CORPUS")
        assert "PARTIAL" in found.verdict

    _both_backends(_body)


def test_an_unlabelled_reaction_is_never_presented_as_a_precedent() -> None:
    """It has no roles, no name and no groups, so it can satisfy no facet — and it says so."""

    async def _body(index: LabelIndex, tag: str) -> None:
        found = await substrate_precedents(index, _VERSION, "Brc1ccccc1", limit=50)
        assert f"{tag}-pending" not in {h.reaction_id for h in found.hits}
        assert found.coverage.total > found.coverage.labelled

    _both_backends(_body)


def test_q2_conditions_that_worked_for_similar_products() -> None:
    """Question 2: neighbours in fingerprint space first, then their reactions.

    Run against the in-memory fingerprint store only — the two-pass shape is what is under test,
    and pgvector's ranking is already covered by `tests/test_molfp_postgres.py`.
    """

    async def _run() -> None:
        index = InMemoryLabelIndex()
        await _seed(index, "sim")
        molecules = InMemoryFingerprintStore(molecule_definition())
        from chemclaw.science.fingerprints.molfp.search import record_for

        for smiles in (_ANILINE, _BIPHENYL):
            await molecules.add(record_for(smiles, smiles))

        found = await conditions_for_similar_products(
            index, molecules, _VERSION, _ANILINE, threshold=0.99, limit=20
        )
        assert {h.reaction_id for h in found.hits} == {"sim-b1", "sim-b2", "sim-b3"}
        assert all(h.named_reaction == "Buchwald-Hartwig amination" for h in found.hits)

        # A product nothing resembles is a genuine "no neighbours", not an open facet that would
        # have selected the whole corpus.
        none = await conditions_for_similar_products(
            index, molecules, _VERSION, "CCCCCCCCCCCCCCCC", threshold=0.99, limit=20
        )
        assert none.hits == []

    asyncio.run(_run())


def test_q4_reactions_whose_product_matches_a_smarts() -> None:
    """Question 4, over the pattern screen: find the structures, then find their reactions.

    Postgres-only, because the screen is a GIN containment index and there is no in-memory twin —
    a Python reimplementation would be a second definition of soundness, which is the one property
    this search rests on.
    """

    async def _run() -> None:
        await migrated_db_or_skip()
        index = PostgresLabelIndex()
        await _seed(index, "smarts")
        molecules = CorpusMolecules()
        await molecules.add_many([_ANILINE, _BIPHENYL])

        found = await reactions_with_product_substructure(
            index, molecules, _VERSION, "c1ccccc1[NX3;H1]", limit=20
        )
        mine = {h.reaction_id for h in found.hits if h.source == "smarts-corpus"}
        assert mine == {"smarts-b1", "smarts-b2", "smarts-b3"}

        # The same query narrowed by name is still one facet, not a second search.
        suzukis = await reactions_with_product_substructure(
            index, molecules, _VERSION, "c1ccccc1-c1ccccc1", limit=20
        )
        assert {h.named_reaction for h in suzukis.hits if h.source == "smarts-corpus"} == {
            "Bromo Suzuki coupling"
        }

    asyncio.run(_run())


def test_the_corpus_molecule_table_is_the_fingerprint_store_pointed_elsewhere() -> None:
    """No new similarity code: `PostgresFingerprintStore` is already table-parameterised."""

    async def _run() -> None:
        await migrated_db_or_skip()
        await CorpusMolecules().add_many([_ANILINE])
        store = PostgresFingerprintStore(
            CORPUS_MOLECULES_TABLE, settings.ecfp_bits, molecule_definition()
        )
        assert not await store.is_empty()

    asyncio.run(_run())
