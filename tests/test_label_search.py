"""The six precedent questions, asked of a seeded corpus, against both index backends.

Each test names the chemist's question it answers. Both backends run the same body, as in
`tests/test_label_index.py`; the Postgres half skips without a database.
"""

import asyncio
import re
import time
from collections.abc import Awaitable, Callable

import pytest
from rdkit import Chem

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.science.fingerprints.molfp.fingerprint import molecule_definition
from chemclaw.science.fingerprints.rxnfp.search import record_for_reaction
from chemclaw.science.fingerprints.store import (
    FingerprintError,
    InMemoryFingerprintStore,
    PostgresFingerprintStore,
)
from chemclaw.science.labels.molecules import (
    CORPUS_MOLECULES_TABLE,
    CorpusMolecules,
    VerifyDeadlineExceeded,
    _verify_within,
)
from chemclaw.science.labels.pattern import compile_query
from chemclaw.science.labels.reactions import transformation_of
from chemclaw.science.labels.records import ReactionLabel, SpeciesLabel
from chemclaw.science.labels.search import (
    agent_frequency,
    conditions_for_similar_products,
    conditions_for_similar_reactions,
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
    """Question 1: has this substrate been used as a starting material?

    The aryl bromide is never a ligand, so asking for it as one must return nothing, which makes the
    role a filter.
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
    """An empty answer says which kind of empty it is.

    "No precedent" and "nothing matching has been labelled yet" are opposite claims.
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


async def test_q2_conditions_that_worked_for_similar_products() -> None:
    """Question 2: neighbours in fingerprint space first, then their reactions.

    Run against the in-memory fingerprint store only — the two-pass shape is what is under test,
    and pgvector's ranking is already covered by `tests/test_molfp_postgres.py`.
    """
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


async def test_q4_reactions_whose_product_matches_a_smarts() -> None:
    """Question 4: reactions whose product matches a SMARTS.

    Postgres-only: the screen is a GIN containment index, and a Python twin would be a second
    definition of soundness.
    """
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


def test_the_partial_verdict_can_never_print_a_share_of_100_or_0() -> None:
    """The PARTIAL verdict can never print a share of 100% or 0%.

    Rounding would print 4,999 of 5,000 as "(100%)". The share is floored to a tenth and clamped;
    asserted as a property over the edges.
    """
    from chemclaw.science.labels.records import CorpusCoverage

    for labelled, total in ((4999, 5000), (99999, 100000), (1, 100000), (1, 5000), (1, 3)):
        verdict = CorpusCoverage(labelled=labelled, total=total).verdict
        assert verdict.startswith("PARTIAL")
        match = re.search(r"\((\d+\.\d)%\)", verdict)
        assert match is not None, verdict
        printed = match.group(1)
        assert printed not in {"100.0", "0.0"}, (
            f"{labelled} of {total} printed ({printed}%) in the branch that says it is not complete"
        )
        assert 0.0 < float(printed) < 100.0


def test_a_fully_labelled_scope_still_says_complete() -> None:
    """The other side of the clamp: 100% belongs to the COMPLETE branch and only to it."""
    from chemclaw.science.labels.records import CorpusCoverage

    assert CorpusCoverage(labelled=5000, total=5000).verdict.startswith("COMPLETE")


async def test_a_corpus_sitting_exactly_on_the_cap_is_not_reported_as_truncated() -> None:
    """A corpus sitting exactly on the cap is not reported as truncated.

    Truncation is observed by reading one row past the cap, as `molfp.search` does; `len == cap`
    cannot tell "more" from "exactly that many". Two selenides, so only these rows match and a count
    can be asserted in the shared database.
    """
    await migrated_db_or_skip()
    molecules = CorpusMolecules()
    corpus = ["C[Se]C", "CC[Se]C"]
    await molecules.add_many(corpus)
    try:
        exactly, truncated = await molecules.containing("[#34]", 2)
        assert sorted(exactly) == sorted(corpus)
        assert truncated is False, "a complete answer was reported as a sample"

        capped, truncated = await molecules.containing("[#34]", 1)
        assert len(capped) == 1
        assert truncated is True, "a screen that really did cut a row said it had not"
    finally:
        await _drop_corpus_molecules(corpus)


async def test_the_corpus_molecule_table_is_the_fingerprint_store_pointed_elsewhere() -> None:
    """No new similarity code: `PostgresFingerprintStore` is already table-parameterised."""
    await migrated_db_or_skip()
    await CorpusMolecules().add_many([_ANILINE])
    store = PostgresFingerprintStore(
        CORPUS_MOLECULES_TABLE, settings.ecfp_bits, molecule_definition()
    )
    assert not await store.is_empty()


# Cheap to index and expensive to verify: long chains have no tautomers to enumerate, and a
# never-matching wildcard SMARTS must walk each one, so the verify step is measurable at 300 rows.
_CHAIN_CORPUS = ["C" * i + "O" + "C" * (240 - i) for i in range(1, 301)]
# 48 wildcards ending in a phosphorus no chain carries: every candidate is verified in full and
# none matches, which is the shape of the query a chemist's "does anything here look like…" takes
# when the answer is no.
_UNMATCHABLE = "~".join(["[*]"] * 48) + "~[#15]"


async def _drop_corpus_molecules(structures: list[str]) -> None:
    """Delete the structures a test seeded, so the next test's similarity search never sees them.

    `tests/pg.py` isolates the run, not the test, and leftover chains would be near neighbours of
    the hexadecane the "no neighbours" test relies on.
    """
    async with db.connection(settings.postgres_dsn) as conn:
        await conn.execute("DELETE FROM corpus_molecules WHERE id = ANY(%s)", (structures,))
        await conn.commit()


async def _worst_tick_gap(
    work: Awaitable[object], *, interval: float = 0.005
) -> tuple[float, float]:
    """Run `work` beside a ticker; return how long `work` took and the ticker's worst gap.

    The worst gap is how long every other session on the pod would freeze.
    """
    gaps: list[float] = []
    stop = asyncio.Event()

    async def _tick() -> None:
        last = asyncio.get_running_loop().time()
        while not stop.is_set():
            await asyncio.sleep(interval)
            now = asyncio.get_running_loop().time()
            gaps.append(now - last)
            last = now

    ticker = asyncio.create_task(_tick())
    await asyncio.sleep(interval * 10)  # let the ticker settle before the work starts
    started = asyncio.get_running_loop().time()
    await work
    elapsed = asyncio.get_running_loop().time() - started
    stop.set()
    await ticker
    return elapsed, max(gaps)


async def test_the_substructure_verify_does_not_freeze_the_event_loop() -> None:
    """The substructure verify does not freeze the event loop.

    RDKit matching of the screen's survivors runs off the loop, as in
    `molfp.find_substructure_matches`, so SSE streams, `/healthz` and token validation keep running.
    Asserted as a measured gap, since a thread the loop awaits synchronously would still freeze it.
    """
    await migrated_db_or_skip()
    molecules = CorpusMolecules()
    await molecules.add_many(_CHAIN_CORPUS)
    try:
        elapsed, worst = await _worst_tick_gap(molecules.containing(_UNMATCHABLE, 5000))
    finally:
        await _drop_corpus_molecules(_CHAIN_CORPUS)
    # The verify really did run inside the measured window: without this the gap assertion
    # would also pass on a screen that returned nothing.
    assert elapsed > 0.25, f"the verify finished in {elapsed:.3f}s — it was not the work"
    assert worst < 0.1, f"the loop was blocked for {worst * 1000:.0f} ms during the verify"


async def test_a_substructure_verify_that_runs_too_long_is_cut_off_rather_than_awaited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verify carries the wall-clock bound the sibling scan does; without it there was none.

    A short adversarial SMARTS can match for minutes whatever the record cap is, so bounding the
    inputs is not bounding the work. The caller is released with an error naming the setting.
    """
    monkeypatch.setattr(settings, "substructure_match_timeout_seconds", 0.001)

    await migrated_db_or_skip()
    molecules = CorpusMolecules()
    corpus = _CHAIN_CORPUS[:30]
    await molecules.add_many(corpus)
    try:
        with pytest.raises(FingerprintError, match="CHEMCLAW_SUBSTRUCTURE_MATCH_TIMEOUT"):
            await molecules.containing(_UNMATCHABLE, 5000)
    finally:
        await _drop_corpus_molecules(corpus)


def test_a_verify_past_its_deadline_stops_instead_of_matching_the_rest_of_the_candidates() -> None:
    """A verify past its deadline stops instead of matching the rest of the candidates.

    `asyncio.wait_for` cannot stop a thread, so `_verify_within` checks the deadline between
    candidates. Asserted as a candidate count (`VerifyDeadlineExceeded.examined`): a handful of the
    300 when bounded, all 300 when not, independent of machine speed.
    """
    query = compile_query(_UNMATCHABLE)
    molecule = Chem.MolFromSmiles(_CHAIN_CORPUS[0])
    started = time.perf_counter()
    molecule.HasSubstructMatch(query)
    per_candidate = time.perf_counter() - started

    with pytest.raises(VerifyDeadlineExceeded) as stopped:
        _verify_within(_CHAIN_CORPUS, query, time.monotonic() + per_candidate * 5)

    assert _verify_within(_CHAIN_CORPUS, query, time.monotonic() + 3600) == [], (
        "unmatchable, so the unbounded verify really did examine every candidate"
    )
    assert stopped.value.total == len(_CHAIN_CORPUS), (
        f"the refusal counts against {stopped.value.total} candidates where the corpus has "
        f"{len(_CHAIN_CORPUS)}, so it is not describing this verify"
    )
    assert stopped.value.reached < len(_CHAIN_CORPUS) // 4, (
        f"the verify reached {stopped.value.reached} of {len(_CHAIN_CORPUS)} candidates past its "
        "deadline, which is the bound failing to reach the worker thread"
    )


def test_an_oversized_substructure_query_is_refused_before_anything_is_scanned() -> None:
    """An oversized substructure query is refused before anything is scanned.

    `substructure_query_max_length` applies on the path the `reactions_making_substructure` tool
    takes. Asserted through `containing`; no database is needed, which itself shows nothing was
    screened.
    """
    oversized = "~".join(["[*]"] * settings.substructure_query_max_length)
    with pytest.raises(FingerprintError, match="CHEMCLAW_SUBSTRUCTURE_QUERY_MAX_LENGTH"):
        asyncio.run(CorpusMolecules().containing(oversized, 5000))


async def _reaction_fingerprints(tag: str) -> InMemoryFingerprintStore:
    """The corpus reaction index for one seeded tag, keyed exactly as the drain keys it.

    The source rides on the record exactly as both ingest paths set it, so a `Match` carries the
    `(source, id)` pair `Facet.reaction_keys` narrows on and nothing composes or splits a string.
    """
    store = InMemoryFingerprintStore()
    for row in (
        _buchwald(tag, f"{tag}-b1", _XPHOS, yield_percent=88.0, workup=None),
        _buchwald(tag, f"{tag}-b2", _XPHOS, yield_percent=72.0, workup=None),
        _buchwald(tag, f"{tag}-b3", _TBU3P, yield_percent=54.0, workup=None),
        _suzuki(tag),
    ):
        await store.add(
            record_for_reaction(row.reaction_id, transformation_of(row.record_smiles)).model_copy(
                update={"source": row.source}
            )
        )
    return store


def test_q7_has_this_transformation_been_run_and_under_what_conditions() -> None:
    """Question 7: has this transformation been run, and under what conditions?

    The Buchwald and Suzuki share the aryl bromide and make different products, so product
    similarity separates them for the wrong reason; querying in transformation space returns the
    three Buchwalds and never the Suzuki. Both backends, since `Facet.reaction_keys` is an `unnest`
    zip in SQL and a tuple membership test in Python.
    """

    async def body(index: LabelIndex, tag: str) -> None:
        found = await conditions_for_similar_reactions(
            index,
            await _reaction_fingerprints(tag),
            _VERSION,
            f"Brc1ccccc1.NC1CCCCC1>>{_ANILINE}",
            threshold=0.5,
        )
        assert {hit.reaction_id for hit in found.hits} == {f"{tag}-b1", f"{tag}-b2", f"{tag}-b3"}
        assert all(hit.citation for hit in found.hits)
        # The coverage denominator is the neighbour set, not the whole corpus: `reaction_keys` is
        # answerable by an unlabelled row, so it belongs to `_in_scope`. Without that the
        # denominator would be every seeded row and the sentence would understate the labelling.
        assert found.coverage.total == 3

    _both_backends(body)


def test_no_neighbours_is_not_an_empty_answer_over_the_whole_corpus() -> None:
    """No neighbours is an empty answer, not the whole corpus.

    An empty `Facet()` selects the entire index, so the result must be empty hits with a coverage
    sentence.
    """

    async def body(index: LabelIndex, tag: str) -> None:
        found = await conditions_for_similar_reactions(
            index,
            InMemoryFingerprintStore(),
            _VERSION,
            f"Brc1ccccc1.NC1CCCCC1>>{_ANILINE}",
        )
        assert found.hits == []
        assert found.coverage is not None

    _both_backends(body)
